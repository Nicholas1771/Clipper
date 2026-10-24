from __future__ import annotations

from common.creators import config_for_run, read_creator

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clippublisher.config import AppConfig
from clippublisher.models import QueueItem
from common.alerts import render_alert_html, send_email_alert, send_failure_alert
from clippublisher.instagram_client import publish_to_instagram
from clippublisher.queue import claim_item, discover_queue_items, is_item_ready, update_scheduler_publish_state
from clippublisher.youtube_client import is_configured as youtube_is_configured, publish_to_youtube
from common.env import is_test_mode, parse_bool
from common.platforms import INSTAGRAM, PLATFORM_LABELS, YOUTUBE, read_slot_platforms
from clippublisher.token_manager import resolve_publish_access_token
from common.reporting import publish_usage_details, read_run_reporting, record_post


logger = logging.getLogger(__name__)

YOUTUBE_SELECTION_MODE_ENV = "YOUTUBE_SELECTION_MODE"
INSTAGRAM_TOP = "instagram_top"


def youtube_selection_mode() -> str:
    return os.getenv(YOUTUBE_SELECTION_MODE_ENV, "shared").strip().lower() or "shared"


def youtube_replay_enabled() -> bool:
    """Scheduled replay uploads run only in instagram_top mode, outside test mode."""
    return youtube_selection_mode() == INSTAGRAM_TOP and not is_test_mode()


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def next_run_text(queue_dir: Path, tz_name: str, *, replay: bool = False) -> str:
    state = load_json(queue_dir / ("youtube_plan.json" if replay else "scheduler_state.json"))
    now = datetime.now(ZoneInfo(tz_name))
    upcoming = []
    slots = (state.get("slots") if isinstance(state, dict) else None) or []
    if replay:
        slots = [
            entry.get("scheduled_for") or entry.get("slot")
            for entry in (state.get("entries") if isinstance(state, dict) else None) or []
            if isinstance(entry, dict) and entry.get("status") in {"pending", "planned"}
        ]
    for raw in slots:
        try:
            slot = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if slot > now:
            upcoming.append(slot)
    if not upcoming:
        return "none left today (a new schedule is built tomorrow)"
    return min(upcoming).astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def original_title(manifest_path: Path, fallback: str = "") -> str:
    manifest = load_json(manifest_path)
    if isinstance(manifest, dict):
        text = str(manifest.get("text") or "").strip()
        if text:
            return text
    return fallback


def alert_details(config: AppConfig, item: Any) -> str:
    return (
        f"Creator: {read_creator(item.run_dir).get('display_name') or getattr(config, 'streamer_hashtag', '') or 'unknown (legacy item)'}\n"
        f"Author: @{item.author_handle}\n"
        f"Next scheduled run: {next_run_text(config.queue_dir, config.timezone_name)}\n"
        f"{publish_usage_details(config.queue_dir, item.run_dir)}"
    )


def posted_alert_details(config: AppConfig, item: QueueItem, *, replay: bool = False) -> str:
    manifest = load_json(item.manifest_path)
    manifest = manifest if isinstance(manifest, dict) else {}
    engagement = manifest.get("engagement") or {}
    views = engagement.get("plays") if isinstance(engagement, dict) else None
    views_text = f"{views:,}" if isinstance(views, int) and not isinstance(views, bool) and views >= 0 else "unknown"
    age_text = "unknown"
    created_at = manifest.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise ValueError("source timestamp has no timezone")
            hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if hours < 0:
                raise ValueError("source timestamp is in the future")
            age_text = f"{hours:.1f} hours"
        except ValueError as error:
            logger.warning("Could not calculate TikTok age for %s: %s", item.clip_id, error)
    if views_text == "unknown" or age_text == "unknown":
        logger.warning("Source views or age are unavailable for success alert %s.", item.clip_id)

    branding = load_json(item.run_dir / "editor" / "branding.json")
    branding_text = "unknown (item predates branding tracking)"
    if isinstance(branding, dict) and branding.get("mode") == "Original":
        branding_text = "Original"
    elif isinstance(branding, dict) and branding.get("mode") == "Manual":
        placement = branding.get("placement")
        fraction = branding.get("fraction")
        if (branding.get("method") == "protected_regions"
                and isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and 0 <= fraction <= 1):
            branding_text = f"Manual [protected regions, y={fraction:.4f}]"
        elif isinstance(placement, int) and not isinstance(placement, bool) and placement > 0:
            branding_text = f"Manual [{placement}]"
        else:
            branding_text = "Manual [unavailable: unnumbered fallback]"
    else:
        logger.warning("Editor branding metadata is unavailable for success alert %s.", item.clip_id)
    author = str(manifest.get("author_handle") or item.author_handle).lstrip("@")
    return (
        f"Next scheduled run: {next_run_text(config.queue_dir, config.timezone_name, replay=replay)}\n\n"
        f"Source\n"
        f"Creator: {read_creator(item.run_dir).get('display_name') or getattr(config, 'streamer_hashtag', '') or 'unknown (legacy item)'}\n"
        f"Clip ID: {item.clip_id}\n"
        f"Author: @{author}\n"
        f"Link: {manifest.get('url') or 'unknown'}\n"
        f"Views: {views_text}\n"
        f"Age: {age_text}\n"
        f"Caption: {str(manifest.get('text') or '').strip() or 'unavailable'}\n\n"
        f"Apify\n"
        f"{publish_usage_details(config.queue_dir, item.run_dir, replay=replay, compact=True)}\n"
        f"Editor\n"
        f"Branding: {branding_text}\n"
    )


def posted_alert_html(body: str) -> str:
    return render_alert_html(body)


def alert_subject(config: AppConfig, clip_id: str, outcome: str) -> str:
    state = load_json(config.queue_dir / "scheduler_state.json")
    position = ""
    if isinstance(state, dict):
        slot_index = state.get("slot_index")
        total = state.get("total_slots") or len(state.get("slots") or [])
        if isinstance(slot_index, int) and slot_index >= 0 and total:
            position = f" [{slot_index + 1}/{total}]"
    return f"{outcome}{position} - {clip_id}"


def send_posted_alert(*, clip_id: str, author_handle: str, platform: str = INSTAGRAM, publish_result: dict[str, Any] | None = None, details: str = "", title: str = "", subject: str | None = None) -> None:
    result = publish_result if isinstance(publish_result, dict) else {}
    subject = subject or f"Success - {clip_id}"
    if platform == YOUTUBE:
        line = f"YouTube video: {result.get('url') or result.get('id') or 'unknown'}"
    else:
        line = f"Instagram publish id: {result.get('id') or 'unknown'}"
    body = (
        f"{line}\n"
        f"{details or f'Clip ID: {clip_id}' + chr(10) + f'Author: @{author_handle}' + chr(10)}"
    )
    if title:
        body += f"\nCaption: {title}\n"
    send_email_alert(
        app_name="ClipPublisher", subject=subject, body=body,
        html_body=posted_alert_html(body), platform=PLATFORM_LABELS.get(platform, platform),
    )


class QueuePublisherRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._base_config = config
        self._youtube_replay: Any = None

    def _shared_slot_platforms(self) -> list[str]:
        platforms = read_slot_platforms(self.config.queue_dir)
        if youtube_selection_mode() == INSTAGRAM_TOP and YOUTUBE in platforms:
            # YouTube is fed only by the separate replay plan in this mode, never by the shared queue.
            logger.info("YOUTUBE_SELECTION_MODE=instagram_top: shared queue item will not be uploaded to YouTube.")
            platforms = [platform for platform in platforms if platform != YOUTUBE]
        return platforms

    def process_youtube_replay(self) -> bool:
        """Upload at most one due clip from the YouTube replay plan. Never raises."""
        if not youtube_replay_enabled():
            return False
        try:
            if self._youtube_replay is None:
                from clippublisher.youtube_replay import YouTubeReplay

                self._youtube_replay = YouTubeReplay(self.config)
            return bool(self._youtube_replay.process_due(datetime.now(ZoneInfo(self.config.timezone_name))))
        except Exception:
            logger.exception("YouTube replay processing failed; the shared Instagram queue continues unchanged.")
            return False

    def _sourcer_seen_ids_path(self) -> Path:
        state_raw = os.getenv("STATE_FILE", "data/state.json").strip()
        if os.name != "nt" and state_raw.startswith("C:"):
            state_raw = "data/state.json"
        state_file = Path(state_raw).expanduser()
        if not state_file.is_absolute():
            state_file = Path(__file__).resolve().parents[4] / state_file
        return state_file

    def _record_seen_id(self, clip_id: str) -> None:
        if not clip_id:
            return
        state_file = self._sourcer_seen_ids_path()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = load_json(state_file) if state_file.exists() else {"seen_ids": []}
        if not isinstance(payload, dict):
            payload = {"seen_ids": []}
        seen_ids = {str(value) for value in payload.get("seen_ids", [])}
        seen_ids.add(clip_id)
        payload["seen_ids"] = sorted(seen_ids)
        payload["updated_at"] = datetime.now(ZoneInfo(self.config.timezone_name)).isoformat()
        save_json(state_file, payload)
        logger.info("Recorded successful published clip ID %s in seen history at %s.", clip_id, state_file)
        self._sync_seen_to_production()

    def _sync_seen_to_production(self) -> None:
        """Workstation only (SYNC_SEEN_TO_PRODUCTION=true): append local seen IDs to production. Never raises."""
        if not parse_bool(os.getenv("SYNC_SEEN_TO_PRODUCTION"), strict=False):
            return
        profile = os.getenv("CLIPPER_PROFILE", "").strip().lower()
        script = Path(__file__).resolve().parents[4] / "sync_seen_ids.py"
        manual = f"python sync_seen_ids.py --{profile}"
        try:
            completed = subprocess.run([sys.executable, str(script), f"--{profile}"], capture_output=True, text=True, timeout=90)
        except Exception as exc:
            logger.warning("Could not sync seen IDs to production (%s). Run `%s` manually.", type(exc).__name__, manual)
            return
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode == 0:
            logger.info("Synced seen IDs to production: %s", output)
        else:
            logger.warning("Seen-ID sync to production failed: %s Run `%s` manually.", output, manual)

    def _archive_completed_item(self, run_dir: Path) -> Path:
        archive_dir = self.config.queue_dir / "Archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / run_dir.name
        suffix = 1
        while destination.exists():
            destination = archive_dir / f"{run_dir.name}_{suffix}"
            suffix += 1
        try:
            run_dir.rename(destination)
        except (PermissionError, OSError):
            shutil.move(str(run_dir), str(destination))
        logger.info("Archived published clip %s to %s.", run_dir.name, destination)
        return destination

    def _quarantine_failed_item(self, claimed: Any, error: Exception) -> None:
        """A publish is attempted exactly once: park the failed item so it is never retried."""
        run_dir = claimed.run_dir
        try:
            publisher_dir = run_dir / "publisher"
            publisher_dir.mkdir(parents=True, exist_ok=True)
            (publisher_dir / "publish_state.json").write_text(
                json.dumps({
                    "phase": "failed",
                    "status": "error",
                    "error": str(error),
                    "updated_at": datetime.now(ZoneInfo(self.config.timezone_name)).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
            archive_dir = self.config.queue_dir / "Archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            destination = archive_dir / f"{run_dir.name}_failed"
            suffix = 1
            while destination.exists():
                destination = archive_dir / f"{run_dir.name}_failed_{suffix}"
                suffix += 1
            shutil.move(str(run_dir), str(destination))
            logger.error("Publish failed once for %s; moved to %s and will not be retried.", run_dir.name, destination)
        except Exception:
            logger.exception("Could not quarantine failed publish item %s.", run_dir.name)

    def _has_pending_publish_stage(self) -> bool:
        if not self.config.queue_dir.exists():
            return False
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state_path = child / "queue_state.json"
            if not state_path.exists():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict) and state.get("phase") in {"edited", "published"}:
                return True
        return False

    def _publish_claimed(self, claimed: Any, platforms: list[str]) -> None:
        try:
            self._publish_claimed_item(claimed, platforms)
        except Exception:
            update_scheduler_publish_state(self.config, "publish_failed")
            raise
        update_scheduler_publish_state(self.config, "published")

    def _publish_claimed_item(self, claimed: Any, platforms: list[str]) -> None:
        """Attempt each assigned platform once, independently. Raises only if every attempted platform failed."""
        self.config = config_for_run(self._base_config, claimed.run_dir)
        creator_name = str(read_creator(claimed.run_dir).get("display_name") or "")
        metadata = read_run_reporting(claimed.run_dir)
        state = load_json(self.config.queue_dir / "scheduler_state.json") or {}
        scheduled_for = metadata.get("scheduled_for") or state.get("scheduled_for") or datetime.now(ZoneInfo(self.config.timezone_name)).isoformat()
        results: dict[str, Any] = {}
        published_at: dict[str, str] = {}
        failures: dict[str, Exception] = {}
        skipped: list[str] = []
        for platform in platforms:
            try:
                if platform == INSTAGRAM:
                    publish_token = resolve_publish_access_token(self.config)
                    results[platform] = publish_to_instagram(self.config, claimed, access_token=publish_token)
                elif platform == YOUTUBE:
                    if not youtube_is_configured(self.config):
                        logger.warning("YouTube is assigned to %s but not configured (credentials or token file missing); skipping.", claimed.run_dir.name)
                        skipped.append(platform)
                        record_post(self.config.queue_dir, scheduled_for, [platform], "skipped", "YouTube is not configured", creator=creator_name)
                        continue
                    results[platform] = publish_to_youtube(self.config, claimed)
                else:
                    continue
                published_at[platform] = datetime.now(ZoneInfo(self.config.timezone_name)).isoformat()
                record_post(self.config.queue_dir, scheduled_for, [platform], "published", claimed.clip_id, creator=creator_name)
                logger.info("%s publish response for %s: %s", PLATFORM_LABELS.get(platform, platform), claimed.run_dir.name, results[platform])
            except Exception as error:
                failures[platform] = error
                logger.exception("%s publish failed for %s: %s", PLATFORM_LABELS.get(platform, platform), claimed.run_dir.name, error)
                record_post(self.config.queue_dir, scheduled_for, [platform], "failed", str(error), creator=creator_name)

        if not results:
            no_publish_error = next(iter(failures.values()), RuntimeError("No assigned platform completed a publish."))
            manifest = load_json(claimed.manifest_path)
            manifest = manifest if isinstance(manifest, dict) else {}
            failure_lines = [
                "Publish Failure",
                f"Stage: ClipPublisher",
                f"Scheduled for: {scheduled_for}",
                f"Platforms: {', '.join(PLATFORM_LABELS.get(name, name) for name in platforms)}",
                f"Creator: {creator_name or 'unknown'}",
                "",
                "Source",
                f"Clip ID: {claimed.clip_id}",
                f"Author: @{claimed.author_handle}",
                f"Link: {manifest.get('url') or 'unknown'}",
                "",
                "Error",
                f"Type: {type(no_publish_error).__name__}",
            ]
            failure_lines.extend(
                f"{PLATFORM_LABELS.get(name, name)}: {error}"
                for name, error in failures.items()
            )
            failure_lines.extend(
                f"{PLATFORM_LABELS.get(name, name)}: publish skipped because it is not configured"
                for name in skipped
            )
            if not failures and not skipped:
                failure_lines.append(f"Details: {no_publish_error}")
            failure_body = "\n".join(failure_lines) + "\n"
            email_platform = PLATFORM_LABELS.get(platforms[0], platforms[0]) if len(platforms) == 1 else ""
            send_email_alert(
                app_name="ClipPublisher",
                subject=alert_subject(self.config, claimed.clip_id, "Fail"),
                body=failure_body,
                html_body=render_alert_html(failure_body, section_titles={"Publish Failure", "Error"}),
                platform=email_platform,
            )
            setattr(no_publish_error, "_clipper_alert_sent", True)
            self._quarantine_failed_item(claimed, no_publish_error)
            raise no_publish_error

        publisher_dir = claimed.run_dir / "publisher"
        publisher_dir.mkdir(parents=True, exist_ok=True)
        (publisher_dir / "publish_state.json").write_text(
            json.dumps({
                "phase": "published",
                "status": "partial" if failures else "success" if results else "skipped",
                "result": results.get(INSTAGRAM) if len(results) == 1 and INSTAGRAM in results else results,
                "errors": {name: str(error) for name, error in failures.items()},
                "skipped": skipped,
                "published_at": published_at,
                "updated_at": datetime.now(ZoneInfo(self.config.timezone_name)).isoformat(),
            }, indent=2),
            encoding="utf-8",
        )
        if results:
            self._record_seen_id(claimed.clip_id)
            for platform, result in results.items():
                send_posted_alert(
                    clip_id=claimed.clip_id,
                    author_handle=claimed.author_handle,
                    platform=platform,
                    publish_result=result,
                    details=posted_alert_details(self.config, claimed),
                    subject=alert_subject(self.config, claimed.clip_id, "Success"),
                )
        self._archive_completed_item(claimed.run_dir)

    def process_one_ready_item(self, *, bypass_schedule: bool = False) -> bool:
        del bypass_schedule
        self.process_youtube_replay()
        ready_items = []
        for item in discover_queue_items(self.config):
            if is_item_ready(item.run_dir, self.config.ready_stability_seconds):
                ready_items.append(item)

        if not ready_items:
            logger.info("No ready queue item found.")
            return False

        item = ready_items[0]
        # Capture the slot's platforms before publishing updates its scheduler state.
        platforms = self._shared_slot_platforms()
        claimed = claim_item(self.config, item)
        logger.info("Claimed ready queue item %s for publishing to %s.", claimed.run_dir.name, ", ".join(platforms))
        self._publish_claimed(claimed, platforms)
        return True

    def run_forever(self) -> None:
        logger.info("ClipPublisher is watching %s for edited queue items.", self.config.queue_dir)
        while True:
            try:
                self.process_youtube_replay()
                ready_items = []
                for item in discover_queue_items(self.config):
                    if is_item_ready(item.run_dir, self.config.ready_stability_seconds):
                        ready_items.append(item)

                if not ready_items:
                    if self._has_pending_publish_stage():
                        logger.info("ClipPublisher is waiting for an edited queue item to become ready for publish.")
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                item = ready_items[0]
                platforms = self._shared_slot_platforms()
                claimed = claim_item(self.config, item)
                logger.info("Found ready queue item %s. Claiming and publishing to %s immediately.", claimed.run_dir.name, ", ".join(platforms))
                try:
                    self._publish_claimed(claimed, platforms)
                    logger.info("Completed publishing %s.", claimed.run_dir.name)
                except Exception:
                    logger.error("Publishing %s failed on every platform; it was quarantined.", claimed.run_dir.name)
                time.sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:
                logger.info("Shutting down ClipPublisher.")
                return
