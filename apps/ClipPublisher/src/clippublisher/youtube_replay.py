from __future__ import annotations

import json
import logging
import os
import re
import random
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from clippublisher.config import AppConfig
from clippublisher.models import QueueItem
from clippublisher.queue import _read_manifest, _ready_manifest_path, build_queue_item
from clippublisher.runner import original_title, posted_alert_details, send_posted_alert
from clippublisher.token_manager import read_access_token, resolve_publish_access_token
from clippublisher.youtube_client import (
    YouTubePolicyError,
    YouTubeScreeningError,
    is_configured as youtube_is_configured,
    publish_to_youtube,
    screen_youtube_content,
)
from common.alerts import send_failure_alert
from common.env import is_test_mode
from common.creators import active_creator_name, read_creator
from common.platforms import INSTAGRAM, YOUTUBE
from common.reporting import publish_usage_details

logger = logging.getLogger(__name__)

# v20.0 (used by the publish path) sunsets 2026-09-24 and lacks the unified "views" media metric (v22.0+).
GRAPH_API_VERSION = "v24.0"
INSIGHTS_URL = "https://graph.facebook.com/{version}/{media_id}/insights"
PLAN_FILE = "youtube_plan.json"
HISTORY_FILE = "youtube_history.json"
PLAN_VERSION = 1
SCAN_DIRS = ("Archive", "processing")
# Meta error codes that are worth retrying later (throttling / temporary service issues).
TRANSIENT_GRAPH_CODES = {1, 2, 4, 17, 32, 341, 613, 80001, 80002}

PLANNED = "pending"
ATTEMPTED = "attempted"
PUBLISHED = "published"
FAILED = "failed"


class TransientInsightsError(RuntimeError):
    """Views are unknown for now; prepare must be retried instead of guessing."""


class UnavailableInsightsError(RuntimeError):
    """Meta definitively has no views for this media; the clip is excluded from ranking."""


@dataclass(frozen=True)
class Candidate:
    clip_id: str
    media_id: str
    run_dir: Path
    published_at: datetime


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def instagram_media_id(result: Any) -> str:
    """IG media id from a publish_state result: legacy single-IG dict or nested {"instagram": {...}}."""
    if not isinstance(result, dict):
        return ""
    nested = result.get("instagram")
    if isinstance(nested, dict):
        return str(nested.get("id") or "").strip()
    if "youtube" in result:
        return ""
    return str(result.get("id") or "").strip()


def youtube_video_id(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    nested = result.get("youtube")
    return str(nested.get("id") or "").strip() if isinstance(nested, dict) else ""


class YouTubeReplay:
    """Replays yesterday's best-viewed Instagram clips to YouTube in today's slots."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        version = config.instagram_insights_api_version
        if not re.fullmatch(r"v\d+\.0", version) or int(version[1:-2]) < 23:
            raise ValueError("INSTAGRAM_INSIGHTS_API_VERSION must be v23.0 or newer (for example v24.0).")
        self.insights_api_version = version
        if not 0 <= config.youtube_start_hour < config.youtube_end_hour <= 24:
            raise ValueError("YouTube replay requires 0 <= start hour < end hour <= 24.")
        self.tz = ZoneInfo(config.timezone_name)
        self.plan_path = config.queue_dir / PLAN_FILE
        self.history_path = config.queue_dir / HISTORY_FILE

    # ----- persistence -------------------------------------------------

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            payload = _read_json(self.history_path)
        except (OSError, ValueError) as error:
            raise RuntimeError(f"YouTube replay history {self.history_path} is unreadable: {error}") from error
        if payload is None:
            return []
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError(f"YouTube replay history {self.history_path} has an invalid structure.")
        return [entry for entry in entries if isinstance(entry, dict)]

    def _save_history(self, entries: list[dict[str, Any]]) -> None:
        _atomic_write_json(self.history_path, {"version": PLAN_VERSION, "entries": entries})

    def _upsert_history(self, record: dict[str, Any]) -> None:
        entries = self._load_history()
        key = (record.get("date"), record.get("clip_id"), record.get("slot"))
        entries = [entry for entry in entries if (entry.get("date"), entry.get("clip_id"), entry.get("slot")) != key]
        entries.append(record)
        self._save_history(entries)

    def _load_plan(self) -> Any:
        try:
            return _read_json(self.plan_path)
        except (OSError, ValueError) as error:
            logger.error("YouTube replay plan %s is unreadable: %s", self.plan_path, error)
            return {"_corrupt": True}

    def _save_plan(self, plan: dict[str, Any]) -> None:
        _atomic_write_json(self.plan_path, plan)

    def _matches_account(self, plan: dict[str, Any]) -> bool:
        return (
            plan.get("profile") == self.config.replay_profile
            and plan.get("instagram_user_id") == self.config.instagram_user_id
        )

    @staticmethod
    def _is_valid_plan(plan: Any, today: date) -> bool:
        if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION:
            return False
        if plan.get("date") != today.isoformat() or plan.get("status") != "complete":
            return False
        entries = plan.get("entries")
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict) or not all(entry.get(key) for key in ("slot", "clip_id", "run_dir", "status")):
                return False
            try:
                _require_aware(datetime.fromisoformat(str(entry["slot"])), "slot")
            except ValueError:
                return False
        return True

    # ----- candidate discovery -------------------------------------------------

    def _parse_timestamp(self, raw: Any, run_dir: Path) -> datetime | None:
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            logger.error("YouTube replay: unparseable publish timestamp %r in %s; skipping record.", raw, run_dir)
            return None
        if value.tzinfo is None:
            logger.warning("YouTube replay: naive publish timestamp %r in %s; assuming %s.", raw, run_dir, self.config.timezone_name)
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz)

    def _publish_states(self) -> list[tuple[Path, dict[str, Any]]]:
        records: list[tuple[Path, dict[str, Any]]] = []
        for folder in SCAN_DIRS:
            base = self.config.queue_dir / folder
            if not base.is_dir():
                continue
            for run_dir in sorted((path for path in base.iterdir() if path.is_dir()), key=lambda path: path.name):
                state_path = run_dir / "publisher" / "publish_state.json"
                try:
                    state = _read_json(state_path)
                except (OSError, ValueError) as error:
                    logger.error("YouTube replay: unreadable publish state %s: %s", state_path, error)
                    continue
                if isinstance(state, dict):
                    records.append((run_dir, state))
        return records

    @staticmethod
    def _clip_id(run_dir: Path, state: dict[str, Any], item: QueueItem | None) -> str:
        if item is not None:
            return item.clip_id
        manifest_path = _ready_manifest_path(run_dir)
        manifest = _read_manifest(manifest_path) if manifest_path is not None else {}
        return str(manifest.get("clip_id") or state.get("clip_id") or run_dir.name)

    def _alert_policy_rejection(self, item: QueueItem, error: YouTubePolicyError, action: str) -> None:
        creator = read_creator(item.run_dir).get("display_name", "")
        active_creator_name.set(creator)
        summary = " ".join(str(error).split())
        if len(summary) > 500:
            summary = summary[:497] + "..."
        try:
            send_failure_alert(
                app_name="ClipPublisher",
                error=YouTubePolicyError(summary),
                context="YouTube clip rejected by AI policy screening (not uploaded)",
                clip_id=item.clip_id,
                details=f"Creator: {creator or 'unknown (legacy item)'}\nAuthor: @{item.author_handle}\n{action}\n",
                title=original_title(item.manifest_path, item.caption),
                subject=f"Rejected - {item.clip_id}",
                platform="YouTube",
            )
        except Exception:
            logger.exception("YouTube rejection alert could not be sent for %s.", item.clip_id)

    def _collect(
        self, yesterday: date, history: list[dict[str, Any]], *, notify_rejections: bool = False,
    ) -> tuple[list[tuple[Candidate, QueueItem]], list[dict[str, Any]]]:
        excluded: list[dict[str, Any]] = []
        on_youtube = {str(entry.get("clip_id")) for entry in history if entry.get("clip_id")}
        found: list[tuple[Candidate, QueueItem | None, dict[str, Any]]] = []
        for run_dir, state in self._publish_states():
            if state.get("phase") != "published" or state.get("status") not in {"success", "partial"}:
                continue
            if self.config.creators:
                creator = read_creator(run_dir).get("name") or self.config.creators[0].name
                if not any(c.name == creator and c.youtube_posts_per_day > 0 for c in self.config.creators):
                    continue
            item = build_queue_item(run_dir, use_ai_summary=True)
            clip_id = self._clip_id(run_dir, state, item)
            if youtube_video_id(state.get("result")):
                on_youtube.add(clip_id)
            media_id = instagram_media_id(state.get("result"))
            if not media_id:
                continue
            per_platform = state.get("published_at") if isinstance(state.get("published_at"), dict) else {}
            published_at = self._parse_timestamp(per_platform.get("instagram") or state.get("updated_at"), run_dir)
            if published_at is None or published_at.date() != yesterday:
                continue
            found.append((Candidate(clip_id, media_id, run_dir, published_at), item, state))

        ready: list[tuple[Candidate, QueueItem]] = []
        for candidate, item, _ in found:
            reason = ""
            if candidate.clip_id in on_youtube:
                reason = "already_on_youtube"
                logger.info("YouTube replay: %s (%s) is already on YouTube; excluded.", candidate.clip_id, candidate.run_dir.name)
            elif item is None:
                reason = "missing_local_media"
                logger.error("YouTube replay: %s (%s) has no local source manifest/editor video; excluded.", candidate.clip_id, candidate.run_dir)
            else:
                try:
                    screen_youtube_content(self.config, item)
                except YouTubePolicyError as error:
                    reason = f"youtube_ai_policy: {error}"
                    logger.warning(
                        "YouTube replay: %s (%s) excluded by AI policy review; next-ranked clip will be considered. %s",
                        candidate.clip_id,
                        candidate.run_dir.name,
                        error,
                    )
                    if notify_rejections:
                        self._alert_policy_rejection(
                            item, error, "Excluded from selection; next-highest-view eligible clip will be considered.",
                        )
            if reason:
                excluded.append(self._excluded(candidate, reason))
            else:
                if not item.youtube_description:
                    logger.warning("YouTube replay: %s has no stored YouTube metadata; its title would fall back to the Instagram text. Run ClipAI --backfill-youtube.", candidate.clip_id)
                ready.append((candidate, item))
        logger.info(
            "YouTube replay: found %s successful Instagram post(s) from %s in %s; %s eligible for an Insights views request.",
            len(found), yesterday, self.config.queue_dir, len(ready),
        )
        return ready, excluded

    def _excluded(self, candidate: Candidate, reason: str) -> dict[str, Any]:
        return {
            "clip_id": candidate.clip_id,
            "media_id": candidate.media_id,
            "run_dir": self._relative(candidate.run_dir),
            "reason": reason,
        }

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.queue_dir).as_posix()
        except ValueError:
            return str(path)

    # ----- insights -------------------------------------------------

    def fetch_views(self, media_id: str, access_token: str) -> int:
        url = INSIGHTS_URL.format(version=self.insights_api_version, media_id=media_id)
        try:
            response = requests.get(url, params={"metric": "views", "access_token": access_token}, timeout=(10, 60))
        except requests.RequestException as error:
            raise TransientInsightsError(f"network error fetching views for media {media_id}: {error}") from error
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
            code = error.get("code")
            message = f"HTTP {response.status_code} for media {media_id}: code={code} {error.get('message') or response.text[:300]}"
            if response.status_code >= 500 or response.status_code == 429 or code in TRANSIENT_GRAPH_CODES or error.get("is_transient"):
                raise TransientInsightsError(message)
            if code in {10, 190, 200, 294} or response.status_code in {401, 403}:
                raise TransientInsightsError(f"access token rejected ({message}); fix the token, prepare will retry")
            raise UnavailableInsightsError(message)
        if not isinstance(payload, dict):
            raise TransientInsightsError(f"non-JSON insights response for media {media_id}: {response.text[:300]}")
        for metric in payload.get("data") or []:
            if not isinstance(metric, dict) or metric.get("name") != "views":
                continue
            values = metric.get("values")
            value = values[0].get("value") if isinstance(values, list) and values and isinstance(values[0], dict) else None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                break
            return value
        raise TransientInsightsError(f"insights response for media {media_id} has no 'views' value: {payload}")

    # ----- public API -------------------------------------------------

    def prepare(self, now: datetime, slots: list[datetime]) -> bool:
        """Build today's ranked plan once. True = complete snapshot persisted (maybe empty); False = retry later."""
        _require_aware(now, "now")
        for slot in slots:
            _require_aware(slot, "slot")
        local_now = now.astimezone(self.tz)
        today = local_now.date()
        yesterday = today - timedelta(days=1)

        existing = self._load_plan()
        if self._is_valid_plan(existing, today):
            if not self._matches_account(existing):
                logger.error("YouTube replay plan belongs to another or unidentified account; preparation blocked.")
                return False
            logger.info("YouTube replay plan for %s is already complete; not re-ranking.", today)
            return True
        if existing is not None and isinstance(existing, dict) and (existing.get("_corrupt") or existing.get("date") == today.isoformat()):
            backup = self.plan_path.with_name(f"{PLAN_FILE}.invalid-{local_now.strftime('%Y%m%dT%H%M%S')}")
            try:
                os.replace(self.plan_path, backup)
            except OSError:
                pass
            logger.error("YouTube replay plan for %s was invalid; moved to %s and re-ranking (history still excludes uploaded clips).", today, backup)

        try:
            history = self._load_history()
        except RuntimeError as error:
            logger.error("YouTube replay prepare blocked: %s", error)
            return False

        plan = self._build_plan(
            local_now, slots, history, lambda: resolve_publish_access_token(self.config), notify_rejections=True,
        )
        if plan is None:
            return False
        self._save_plan(plan)
        logger.info("YouTube replay plan for %s saved: %s of %s ranked clip(s) scheduled from %s.", today, len(plan["entries"]), len(plan["entries"]) + len(plan["unscheduled"]), yesterday)
        return True

    def simulate(self, now: datetime, slots: list[datetime]) -> dict[str, Any] | None:
        """Rank exactly like prepare, but read-only: no plan/history/token writes and no token refresh."""
        _require_aware(now, "now")
        for slot in slots:
            _require_aware(slot, "slot")
        return self._build_plan(now.astimezone(self.tz), slots, self._load_history(), lambda: read_access_token(self.config))

    def _build_plan(
        self, local_now: datetime, slots: list[datetime], history: list[dict[str, Any]], token_fn,
        *, notify_rejections: bool = False,
    ) -> dict[str, Any] | None:
        today = local_now.date()
        yesterday = today - timedelta(days=1)
        ordered_slots = sorted(set(slots))[:max(0, self.config.youtube_posts_per_day)]
        try:
            ready, excluded = self._collect(yesterday, history, notify_rejections=notify_rejections)
        except YouTubeScreeningError as error:
            logger.error("YouTube replay AI screening unavailable; plan not finalized, will retry. %s", error)
            return None
        ranked: list[dict[str, Any]] = []
        if ready and ordered_slots:
            try:
                access_token = token_fn()
            except Exception as error:
                logger.error("YouTube replay prepare: could not resolve the Instagram access token (%s); will retry.", error)
                return None
            best: dict[str, dict[str, Any]] = {}
            for candidate, _ in ready:
                try:
                    views = self.fetch_views(candidate.media_id, access_token)
                except TransientInsightsError as error:
                    logger.error("YouTube replay prepare: views unknown for %s (%s); plan not finalized, will retry. %s", candidate.clip_id, candidate.media_id, error)
                    return None
                except UnavailableInsightsError as error:
                    logger.error("YouTube replay: views unavailable for %s (%s); plan not finalized. %s", candidate.clip_id, candidate.media_id, error)
                    return None
                logger.info("Instagram views: clip_id=%s media_id=%s views=%s", candidate.clip_id, candidate.media_id, views)
                row = {
                    "clip_id": candidate.clip_id,
                    "media_id": candidate.media_id,
                    "run_dir": self._relative(candidate.run_dir),
                    "views": views,
                    "instagram_published_at": candidate.published_at.isoformat(),
                }
                if self.config.creators:
                    creator = read_creator(candidate.run_dir)
                    row["creator"] = creator.get("name") or self.config.creators[0].name
                    row["creator_name"] = creator.get("display_name") or self.config.creators[0].display_name
                current = best.get(candidate.clip_id)
                if current is None or self._rank_key(row) < self._rank_key(current):
                    best[candidate.clip_id] = row
            ranked = sorted(best.values(), key=self._rank_key)

        selected = ranked[:len(ordered_slots)]
        if self.config.creators:
            selected = []
            for creator in self.config.creators:
                selected.extend(
                    [row for row in ranked if row["creator"] == creator.name][:creator.youtube_posts_per_day]
                )
            random.SystemRandom().shuffle(selected)
            selected = selected[:len(ordered_slots)]
        selected_ids = {row["clip_id"] for row in selected}
        reserves = [row for row in ranked if row["clip_id"] not in selected_ids]
        ranks = {row["clip_id"]: index + 1 for index, row in enumerate(ranked)}
        entries = [
            {
                **row,
                "rank": ranks[row["clip_id"]],
                "slot": slot.astimezone(self.tz).isoformat(),
                "scheduled_for": slot.astimezone(self.tz).isoformat(),
                "status": PLANNED,
            }
            for index, (row, slot) in enumerate(zip(selected, ordered_slots))
        ]
        plan = {
            "version": PLAN_VERSION,
            "date": today.isoformat(),
            "profile": self.config.replay_profile,
            "instagram_user_id": self.config.instagram_user_id,
            "source_date": yesterday.isoformat(),
            "status": "complete",
            "prepared_at": local_now.isoformat(),
            "graph_api_version": self.insights_api_version,
            "slots": [slot.astimezone(self.tz).isoformat() for slot in ordered_slots],
            "entries": entries,
            "unscheduled": [row["clip_id"] for row in reserves],
            "reserves": [
                {**row, "rank": ranks[row["clip_id"]]}
                for index, row in enumerate(reserves)
            ],
            "excluded": excluded,
        }
        return plan

    @staticmethod
    def _rank_key(row: dict[str, Any]) -> tuple:
        return (-int(row["views"]), str(row["instagram_published_at"]), str(row["clip_id"]), str(row["media_id"]))

    def process_due(self, now: datetime) -> bool:
        """Upload at most one due planned clip to YouTube. True only when an upload succeeded."""
        _require_aware(now, "now")
        if (
            self.config.youtube_selection_mode != "instagram_top"
            or self.config.youtube_posts_per_day <= 0
            or is_test_mode()
        ):
            return False
        local_now = now.astimezone(self.tz)
        today = local_now.date()
        plan = self._load_plan()
        if not self._is_valid_plan(plan, today):
            logger.debug("YouTube replay: no complete plan for %s; nothing to upload.", today)
            return False
        if not self._matches_account(plan):
            logger.error("YouTube replay plan belongs to another or unidentified account; upload blocked.")
            return False
        if local_now.hour >= self.config.youtube_end_hour:
            changed = False
            for entry in plan["entries"]:
                if entry.get("status") in {PLANNED, "planned"}:
                    entry["status"] = FAILED
                    entry["error"] = "missed_window: YouTube posting window has ended; not uploaded"
                    logger.warning("YouTube replay skipped %s: posting window ended.", entry["clip_id"])
                    changed = True
            if changed:
                self._save_plan(plan)
            return False
        if local_now.hour < self.config.youtube_start_hour:
            return False
        if sum(bool(entry.get("attempted_at")) for entry in plan["entries"]) >= self.config.youtube_posts_per_day:
            logger.info("YouTube replay daily attempt quota reached; no further uploads.")
            return False
        due = [
            entry for entry in plan["entries"]
            if entry.get("status") in {PLANNED, "planned"} and datetime.fromisoformat(str(entry["slot"])) <= now
        ]
        if not due:
            return False
        entry = min(due, key=lambda value: (datetime.fromisoformat(str(value["slot"])), int(value.get("rank") or 0)))

        try:
            history = self._load_history()
        except RuntimeError as error:
            logger.error("YouTube replay upload blocked: %s", error)
            return False
        if sum(
            bool(record.get("attempted_at")) and record.get("date") == today.isoformat()
            for record in history
        ) >= self.config.youtube_posts_per_day:
            logger.info("YouTube replay daily history attempt quota reached; no further uploads.")
            return False
        if any(record.get("clip_id") == entry["clip_id"] for record in history):
            entry["status"] = FAILED
            entry["error"] = "duplicate: clip already in YouTube replay history"
            self._save_plan(plan)
            logger.error("YouTube replay: %s is already in history; not uploading again.", entry["clip_id"])
            return False

        approval = None
        while True:
            run_dir = self.config.queue_dir / entry["run_dir"]
            active_creator_name.set(str(entry.get("creator_name") or ""))
            item = build_queue_item(run_dir, use_ai_summary=True) if run_dir.is_dir() else None
            valid_media = (
                item is not None
                and item.clip_id == entry["clip_id"]
                and any(
                    run_dir.resolve().is_relative_to((self.config.queue_dir / folder).resolve())
                    for folder in SCAN_DIRS
                )
            )
            if not valid_media:
                break  # Existing upload failure handling records missing or mismatched media.
            try:
                approval = screen_youtube_content(self.config, item)
            except YouTubeScreeningError as error:
                logger.error("YouTube replay AI screening unavailable; slot remains pending. %s", error)
                return False
            except YouTubePolicyError as error:
                reason = f"youtube_ai_policy: {error}"
                plan.setdefault("excluded", []).append({
                    "clip_id": entry["clip_id"],
                    "media_id": entry.get("media_id"),
                    "run_dir": entry["run_dir"],
                    "reason": reason,
                })
                logger.warning("YouTube replay excluded %s before upload: %s", entry["clip_id"], error)
                unavailable = {record.get("clip_id") for record in history}
                unavailable.update(row["clip_id"] for row in plan["entries"])
                reserves = plan.get("reserves", [])
                replacement = None
                for index, candidate in enumerate(reserves):
                    if candidate["clip_id"] not in unavailable and candidate.get("creator") == entry.get("creator"):
                        replacement = reserves.pop(index)
                        break
                if replacement is None:
                    entry["status"] = FAILED
                    entry["error"] = reason
                    self._save_plan(plan)
                    self._alert_policy_rejection(
                        item, error, "No eligible reserve clips remain; this slot will not be uploaded.",
                    )
                    return False
                entry.update(replacement)
                plan["unscheduled"] = [row["clip_id"] for row in reserves]
                self._save_plan(plan)
                self._alert_policy_rejection(
                    item, error,
                    f"Trying next-highest-view reserve: {replacement['clip_id']} ({replacement['views']} Instagram views).",
                )
                continue
            break

        record = {
            "date": plan["date"],
            "slot": entry["slot"],
            "scheduled_for": entry.get("scheduled_for") or entry["slot"],
            "clip_id": entry["clip_id"],
            "media_id": entry.get("media_id"),
            "run_dir": entry["run_dir"],
            "views": entry.get("views"),
        }

        entry["status"] = ATTEMPTED
        entry["attempted_at"] = local_now.isoformat()
        self._save_plan(plan)
        self._upsert_history({**record, "status": ATTEMPTED, "attempted_at": entry["attempted_at"]})

        try:
            if item is None:
                raise RuntimeError(f"archived clip media is missing in {run_dir}")
            resolved_run = run_dir.resolve()
            if not any(
                resolved_run.is_relative_to((self.config.queue_dir / folder).resolve())
                for folder in SCAN_DIRS
            ) or item.clip_id != entry["clip_id"]:
                raise RuntimeError("Replay media path or clip identity does not match the archived plan.")
            if not youtube_is_configured(self.config):
                raise RuntimeError("YouTube is not configured (credentials or token file missing)")
            result = publish_to_youtube(self.config, item, approval=approval)
            if not isinstance(result, dict) or not result.get("id"):
                raise RuntimeError(f"YouTube upload returned no video id: {result}")
        except Exception as error:
            entry["status"] = FAILED
            entry["error"] = str(error)
            self._save_plan(plan)
            self._upsert_history({**record, "status": FAILED, "attempted_at": entry["attempted_at"], "error": str(error)})
            logger.error("YouTube replay upload failed for %s; it will not be retried: %s", entry["clip_id"], error)
            try:
                send_failure_alert(
                    app_name="ClipPublisher",
                    error=error,
                    context=f"YouTube replay upload failure for {entry['run_dir']} (not retried)",
                    clip_id=entry["clip_id"],
                    details=f"Instagram views yesterday: {entry.get('views')}\nSlot: {entry['slot']}\n"
                    + f"Creator: {entry.get('creator_name') or 'unknown (legacy item)'}\n"
                    + publish_usage_details(self.config.queue_dir, replay=True),
                    subject=f"Fail [YT replay #{entry.get('rank')}] - {entry['clip_id']}",
                    platform="YouTube",
                    title=original_title(item.manifest_path, item.caption) if item else "",
                )
            except Exception:
                logger.exception("YouTube replay failure alert could not be sent for %s.", entry["clip_id"])
            return False

        published_at = datetime.now(self.tz).isoformat()
        self._upsert_history({**record, "status": PUBLISHED, "attempted_at": entry["attempted_at"], "published_at": published_at, "youtube": result})
        entry["status"] = PUBLISHED
        entry["published_at"] = published_at
        entry["youtube"] = result
        self._save_plan(plan)
        logger.info("YouTube replay uploaded %s (rank %s, %s IG views): %s", entry["clip_id"], entry.get("rank"), entry.get("views"), result.get("url") or result.get("id"))
        try:
            send_posted_alert(
                clip_id=item.clip_id,
                author_handle=item.author_handle,
                platform=YOUTUBE,
                publish_result=result,
                details=posted_alert_details(self.config, item, replay=True)
                + f"\nReplay of yesterday's Instagram rank #{entry.get('rank')} ({entry.get('views')} views)\n",
                subject=f"Success [YT replay #{entry.get('rank')}] - {item.clip_id}",
            )
        except Exception:
            logger.exception("YouTube replay success alert could not be sent for %s.", entry["clip_id"])
        return True
