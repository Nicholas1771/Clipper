from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import random
import shutil
import time
from typing import Any
from zoneinfo import ZoneInfo

from clipsourcer.apify_client import fetch_tiktok_items
from clipsourcer.config import AppConfig
from clipsourcer.downloads import download_video_file
from clipsourcer.models import ClipCandidate
from clipsourcer.scoring import has_minimum_views, is_recent, matches_keyword, rank_candidates, viral_score
from clipsourcer.storage import load_seen_ids, run_timestamp, unique_run_dir, read_queue_state, write_manifest, write_queue_state


logger = logging.getLogger(__name__)


def _log_play_count_distribution(candidates: list[ClipCandidate]) -> None:
    buckets = (
        "<1,000",
        "1,000-9,999",
        "10,000-99,999",
        "100,000-999,999",
        "1,000,000-9,999,999",
        ">=10,000,000",
    )
    thresholds = (1_000, 10_000, 100_000, 1_000_000, 10_000_000)
    counts = [0] * len(buckets)
    for candidate in candidates:
        bucket_index = next(
            (index for index, threshold in enumerate(thresholds) if candidate.plays < threshold),
            len(thresholds),
        )
        counts[bucket_index] += 1

    distribution = ", ".join(f"{label}: {count}" for label, count in zip(buckets, counts))
    logger.info("Apify result play-count distribution (total=%s): %s", len(candidates), distribution)


def _log_candidate_diagnostics(
    candidates: list[ClipCandidate],
    *,
    origin: str,
    now: datetime,
    lookback_hours: int,
    keyword: str,
    minimum_view_count: int,
    seen_ids: set[str],
    ranked: list[tuple[ClipCandidate, float]],
    unseen_ranked: list[tuple[ClipCandidate, float]],
) -> None:
    eligible_positions = {candidate.clip_id: index for index, (candidate, _) in enumerate(ranked, start=1)}
    selection_positions = {
        candidate.clip_id: index for index, (candidate, _) in enumerate(unseen_ranked, start=1)
    }

    for candidate in candidates:
        recent = is_recent(candidate, now, lookback_hours)
        keyword_match = matches_keyword(candidate, keyword)
        meets_minimum_views = has_minimum_views(candidate, minimum_view_count)
        seen = candidate.clip_id in seen_ids
        rejected_by = [
            reason
            for condition, reason in (
                (not recent, "outside_lookback"),
                (not keyword_match, "keyword_mismatch"),
                (not meets_minimum_views, "below_min_views"),
                (seen, "already_seen"),
            )
            if condition
        ]
        text = " ".join(candidate.text.split())[:120]
        logger.debug(
            "%s candidate: id=%s author=%s views=%s likes=%s comments=%s shares=%s saves=%s "
            "posted=%s age_hours=%.2f viral_score=%.2f eligible_rank=%s selection_rank=%s "
            "recent=%s keyword_match=%s meets_min_views=%s (minimum=%s) seen=%s "
            "rejected_by=%s url=%s text=%r",
            origin,
            candidate.clip_id,
            candidate.author_handle or "unknown",
            candidate.plays,
            candidate.likes,
            candidate.comments,
            candidate.shares,
            candidate.saves,
            candidate.created_at.isoformat(),
            (now - candidate.created_at).total_seconds() / 3600,
            viral_score(candidate, now),
            eligible_positions.get(candidate.clip_id, "-"),
            selection_positions.get(candidate.clip_id, "-"),
            recent,
            keyword_match,
            meets_minimum_views,
            minimum_view_count,
            seen,
            ",".join(rejected_by) or "none",
            candidate.url,
            text,
        )


@dataclass(frozen=True)
class SourcingResult:
    manifest_path: Path
    downloaded_video_path: Path | None


@dataclass(frozen=True)
class SourcingCycleOutcome:
    status: str
    reason: str | None = None
    result: SourcingResult | None = None


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


class ClipSourcerService:
    def __init__(
        self,
        config: AppConfig,
        fetch_items: Callable[[AppConfig], list[dict]] = fetch_tiktok_items,
        *,
        test_mode: bool = False,
    ) -> None:
        self.config = config
        self.test_mode = test_mode
        self.fetch_items = fetch_items
        self._tz = ZoneInfo(config.source_timezone_name)

    def _local_now(self) -> datetime:
        return datetime.now(self._tz)

    def _attempted_candidates_path(self) -> Path:
        cache_path = self.config.candidate_cache_file
        return cache_path.with_name(f"{cache_path.stem}_attempted.json")

    def _load_attempted_candidates(self) -> set[str]:
        path = self._attempted_candidates_path()
        if not path.exists():
            return set()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("attempted_ids"), list):
            raise ValueError(f"Invalid attempted-candidate history: {path}")
        return set(payload["attempted_ids"])

    def _reserve_candidate(self, clip_id: str, attempted_ids: set[str]) -> None:
        path = self._attempted_candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f"{path.name}.tmp")
        temporary_path.write_text(
            json.dumps({"attempted_ids": sorted(attempted_ids | {clip_id})}, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)
        attempted_ids.add(clip_id)

    def _candidate_cache_scope(self) -> str:
        source_target = (
            self.config.source_hashtag
            if self.config.source_mode == "hashtag"
            else sorted(self.config.source_profiles)
        )
        scope = json.dumps(
            {
                "actor": self.config.apify_actor_id,
                "mode": self.config.source_mode,
                "target": source_target,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(scope.encode("utf-8")).hexdigest()

    def _load_candidate_cache(self, seen_ids: set[str], now: datetime) -> list[ClipCandidate]:
        cache_path = self.config.candidate_cache_file
        if not cache_path.exists():
            return []
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            logger.warning("Could not read candidate cache %s; fetching fresh results: %s", cache_path, error)
            return []
        if not isinstance(payload, dict) or payload.get("scope") != self._candidate_cache_scope():
            logger.info("Candidate cache is absent or belongs to a different source; fetching fresh results.")
            return []

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            logger.warning("Candidate cache %s has no valid items list; fetching fresh results.", cache_path)
            return []

        candidates: list[ClipCandidate] = []
        for item in raw_items:
            if not isinstance(item, dict):
                logger.warning("Ignoring malformed item in candidate cache %s.", cache_path)
                continue
            try:
                candidate = ClipCandidate.from_apify_item(item)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                logger.warning("Ignoring invalid cached candidate in %s: %s", cache_path, error)
                continue
            if candidate.clip_id in seen_ids or not is_recent(candidate, now, self.config.lookback_hours):
                continue
            candidates.append(candidate)
        return candidates

    def _save_candidate_cache(
        self, candidates: list[ClipCandidate], *, log_candidates: bool = True, required: bool = False
    ) -> None:
        cache_path = self.config.candidate_cache_file
        temporary_path = cache_path.with_name(f"{cache_path.name}.tmp")
        payload = {
            "scope": self._candidate_cache_scope(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": [candidate.raw_payload for candidate in candidates],
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary_path.replace(cache_path)
        except OSError as error:
            logger.warning("Could not persist candidate cache %s: %s", cache_path, error)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning("Could not remove temporary candidate cache %s: %s", temporary_path, cleanup_error)
            if required:
                raise
        else:
            logger.info("Candidate cache saved: size=%s clip(s).", len(candidates))
            if not log_candidates:
                return
            now = datetime.now(timezone.utc)
            for candidate in candidates:
                logger.info(
                    "Saved candidate to cache: id=%s author=%s views=%s viral_score=%.2f posted=%s url=%s",
                    candidate.clip_id,
                    candidate.author_handle or "unknown",
                    candidate.plays,
                    viral_score(candidate, now),
                    candidate.created_at.isoformat(),
                    candidate.url,
                )

    def _scheduler_ready(self) -> bool:
        state_path = self.config.output_dir / "scheduler_state.json"
        if not state_path.exists():
            return False
        payload = load_json(state_path)
        return isinstance(payload, dict) and payload.get("phase") == "scheduled"

    def _source_history(self) -> list[datetime]:
        payload = load_json(self.config.source_history_file)
        if not isinstance(payload, list):
            return []

        parsed: list[datetime] = []
        for raw in payload:
            if not isinstance(raw, str):
                continue
            try:
                parsed.append(datetime.fromisoformat(raw).astimezone(timezone.utc))
            except ValueError:
                continue
        return sorted(parsed)

    def _schedule_window_for_day(self, day: date) -> tuple[datetime, datetime, datetime]:
        day_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=self._tz)
        posting_start_minutes = self.config.source_target_posting_start_hour * 60
        if posting_start_minutes <= 0:
            raise ValueError("SOURCE_TARGET_POSTING_START_HOUR must be between 1 and 23.")

        base_minutes = posting_start_minutes - self.config.source_lead_minutes
        min_minutes = max(0, base_minutes - self.config.source_jitter_minutes)
        max_minutes = min(posting_start_minutes - 1, base_minutes + self.config.source_jitter_minutes)
        if max_minutes < min_minutes:
            max_minutes = max(0, posting_start_minutes - 1)
            min_minutes = max_minutes

        peak_minutes = max(min_minutes, min(max_minutes, base_minutes))
        return (
            day_start + timedelta(minutes=min_minutes),
            day_start + timedelta(minutes=max_minutes),
            day_start + timedelta(minutes=peak_minutes),
        )

    def _scheduled_time_for_day(self, day: date) -> datetime:
        window_start, window_end, window_peak = self._schedule_window_for_day(day)
        total_minutes = int((window_end - window_start).total_seconds() // 60)
        peak_offset = int((window_peak - window_start).total_seconds() // 60)
        offset_minutes = int(round(random.triangular(0, total_minutes, peak_offset)))
        return window_start + timedelta(minutes=offset_minutes)

    def _load_or_create_daily_schedule(self, now: datetime) -> datetime:
        today_key = now.date().isoformat()
        payload = load_json(self.config.source_schedule_file)
        if isinstance(payload, dict):
            scheduled_for_raw = payload.get("scheduled_for")
            if payload.get("date") == today_key and isinstance(scheduled_for_raw, str):
                try:
                    scheduled_for = datetime.fromisoformat(scheduled_for_raw).astimezone(self._tz)
                    window_start, window_end, _window_peak = self._schedule_window_for_day(now.date())
                    if window_start <= scheduled_for <= window_end:
                        return scheduled_for
                    logger.info(
                        "Replacing stale sourcing schedule %s because it no longer fits today's pre-post window (%s to %s).",
                        scheduled_for.isoformat(),
                        window_start.isoformat(),
                        window_end.isoformat(),
                    )
                except ValueError:
                    pass

        scheduled_for = self._scheduled_time_for_day(now.date())
        save_json(
            self.config.source_schedule_file,
            {
                "date": today_key,
                "scheduled_for": scheduled_for.isoformat(),
            },
        )
        logger.info("Scheduled today's sourcing run for %s.", scheduled_for.isoformat())
        return scheduled_for

    def _can_source_now(self, now: datetime) -> tuple[bool, str | None]:
        history_utc = self._source_history()
        history_local = [timestamp.astimezone(self._tz) for timestamp in history_utc]

        if history_local:
            last_source_local = history_local[-1]
            min_gap = timedelta(minutes=self.config.min_source_gap_minutes)
            if now - last_source_local < min_gap:
                next_allowed = last_source_local + min_gap
                return False, f"last sourcing run was too recent; wait until {next_allowed.isoformat()}"

        today_runs = [timestamp for timestamp in history_local if timestamp.date() == now.date()]
        if len(today_runs) >= self.config.max_source_runs_per_day:
            return False, "daily sourcing cap reached"

        scheduled_for = self._load_or_create_daily_schedule(now)
        if now < scheduled_for:
            return False, f"waiting for today's randomized sourcing time at {scheduled_for.isoformat()}"

        grace_deadline = scheduled_for + timedelta(minutes=self.config.source_grace_minutes)
        if now > grace_deadline:
            return False, f"today's sourcing window expired at {grace_deadline.isoformat()}"

        return True, None

    def _record_source_run(self, now: datetime) -> None:
        history = self._source_history()
        history.append(now.astimezone(timezone.utc))
        history = sorted(history)
        save_json(self.config.source_history_file, [item.isoformat() for item in history])

    def _update_scheduler_state(self, scheduler_state_path: Path, scheduler_state: dict[str, Any], **updates: Any) -> None:
        updated_scheduler_state = dict(scheduler_state)
        updated_scheduler_state.update(updates)
        scheduler_state_path.write_text(json.dumps(updated_scheduler_state, indent=2), encoding="utf-8")

    def _mark_scheduler_source_failed(
        self,
        scheduler_state_path: Path,
        scheduler_state: dict[str, Any],
        *,
        attempts: int,
        reason: str,
    ) -> None:
        self._update_scheduler_state(
            scheduler_state_path,
            scheduler_state,
            date=datetime.now(self._tz).date().isoformat(),
            phase="source_failed",
            next_stage="schedule_next",
            source_attempts=attempts,
            source_failure_reason=reason,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _source_from_test_folder(self, scheduler_state_path: Path, scheduler_state: dict[str, Any]) -> SourcingCycleOutcome:
        """Copy <test queue>/source into a fresh run folder instead of sourcing a new clip."""
        test_source = self.config.output_dir / "source"
        files = [path for path in test_source.iterdir() if path.is_file()] if test_source.is_dir() else []
        if not files:
            return SourcingCycleOutcome(status="no_match", reason=f"test mode: no files found in {test_source}")
        stem = next((path.stem for path in files if path.suffix.lower() == ".mp4"), files[0].stem)
        run_dir = unique_run_dir(self.config.output_dir, f"{run_timestamp()}_{stem}")
        source_dir = run_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, source_dir / path.name)
        manifest = next((source_dir / path.name for path in files if path.suffix.lower() == ".json"), None)
        video = next((source_dir / path.name for path in files if path.suffix.lower() == ".mp4"), None)
        write_queue_state(
            run_dir,
            phase="sourced",
            next_stage="ai",
            source_manifest=manifest.relative_to(run_dir).as_posix() if manifest else None,
            source_video=video.relative_to(run_dir).as_posix() if video else None,
            ready_for_next="ai",
        )
        if scheduler_state_path.exists() and scheduler_state.get("phase") == "scheduled":
            self._update_scheduler_state(
                scheduler_state_path,
                scheduler_state,
                date=datetime.now(self._tz).date().isoformat(),
                phase="source_consumed",
                next_stage="editor",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        logger.info("Test mode: copied %s into %s instead of sourcing a new clip.", test_source, source_dir)
        return SourcingCycleOutcome(
            status="sourced",
            result=SourcingResult(manifest_path=manifest or source_dir, downloaded_video_path=video),
        )

    def _attempt_sourcing(self, scheduler_state_path: Path, scheduler_state: dict[str, Any]) -> SourcingCycleOutcome:
        if self.test_mode:
            return self._source_from_test_folder(scheduler_state_path, scheduler_state)
        logger.info(
            "Starting sourcing cycle (mode=%s, lookback_hours=%s, results_per_page=%s, min_views=%s).",
            self.config.source_mode,
            self.config.lookback_hours,
            self.config.results_per_page,
            self.config.minimum_view_count,
        )
        seen_ids = load_seen_ids(self.config.state_file)
        logger.info("Loaded %s previously sourced clip ID%s.", len(seen_ids), "" if len(seen_ids) == 1 else "s")
        attempted_ids = self._load_attempted_candidates()
        excluded_ids = seen_ids | attempted_ids
        logger.info("Loaded %s previously attempted candidate IDs (separate from published seen history).", len(attempted_ids))
        now_utc = datetime.now(timezone.utc)
        candidates = self._load_candidate_cache(excluded_ids, now_utc)
        logger.info("Candidate cache size at sourcing start: %s unseen, unexpired clip(s).", len(candidates))
        origin = "Cached"
        if candidates:
            logger.info(
                "Using %s candidate%s from the persistent cache; skipping Apify fetch.",
                len(candidates),
                "" if len(candidates) == 1 else "s",
            )
        else:
            logger.info("Candidate source: Apify; fetching new results because the cache is empty or unavailable.")
            raw_items = self.fetch_items(self.config)
            candidates = [ClipCandidate.from_apify_item(item) for item in raw_items]
            origin = "Apify"
            _log_play_count_distribution(candidates)

        ranked = rank_candidates(
            candidates,
            now=now_utc,
            lookback_hours=self.config.lookback_hours,
            keyword=self.config.keyword_filter,
            minimum_view_count=self.config.minimum_view_count,
            seen_ids=seen_ids,
        )
        unseen_ranked = [(candidate, score) for candidate, score in ranked if candidate.clip_id not in excluded_ids]
        if not unseen_ranked and origin == "Cached":
            logger.info("Candidate source: Apify; fetching new results because cached clips no longer pass eligibility filters.")
            raw_items = self.fetch_items(self.config)
            candidates = [ClipCandidate.from_apify_item(item) for item in raw_items]
            origin = "Apify"
            _log_play_count_distribution(candidates)
            ranked = rank_candidates(
                candidates,
                now=now_utc,
                lookback_hours=self.config.lookback_hours,
                keyword=self.config.keyword_filter,
                minimum_view_count=self.config.minimum_view_count,
                seen_ids=seen_ids,
            )
            unseen_ranked = [(candidate, score) for candidate, score in ranked if candidate.clip_id not in excluded_ids]

        _log_candidate_diagnostics(
            candidates,
            origin=origin,
            now=now_utc,
            lookback_hours=self.config.lookback_hours,
            keyword=self.config.keyword_filter,
            minimum_view_count=self.config.minimum_view_count,
            seen_ids=seen_ids,
            ranked=ranked,
            unseen_ranked=unseen_ranked,
        )
        logger.info(
            "Candidate gates: raw=%s -> ranked_eligible=%s -> unseen_ranked=%s.",
            len(candidates),
            len(ranked),
            len(unseen_ranked),
        )
        if not unseen_ranked:
            logger.info("No eligible new clip found in this attempt because all matching clips are already seen or filtered out.")
            self._save_candidate_cache([])
            return SourcingCycleOutcome(
                status="no_match",
                reason="all matching clips were already seen or filtered out",
            )

        if origin == "Apify":
            self._save_candidate_cache([candidate for candidate, _score in unseen_ranked])
        for best_candidate, score in unseen_ranked:
            if best_candidate.clip_id in attempted_ids:
                continue
            self._reserve_candidate(best_candidate.clip_id, attempted_ids)
            self._save_candidate_cache(
                [candidate for candidate, _candidate_score in unseen_ranked if candidate.clip_id not in attempted_ids],
                log_candidates=False,
                required=True,
            )
            if origin == "Cached":
                logger.info(
                    "Using candidate from cache: id=%s author=%s views=%s viral_score=%.2f posted=%s url=%s",
                    best_candidate.clip_id,
                    best_candidate.author_handle or "unknown",
                    best_candidate.plays,
                    score,
                    best_candidate.created_at.isoformat(),
                    best_candidate.url,
                )
            logger.info(
                "Trying clip %s by @%s with %s plays and viral score %s.",
                best_candidate.clip_id,
                best_candidate.author_handle,
                best_candidate.plays,
                score,
            )
            try:
                downloaded_video_path = download_video_file(self.config, best_candidate)
            except Exception as error:
                logger.warning("Skipping clip %s because video download failed: %s", best_candidate.clip_id, error)
                continue

            if self.config.download_video_files and downloaded_video_path is None:
                logger.warning("Skipping clip %s because no source video file was produced.", best_candidate.clip_id)
                continue

            manifest_path = write_manifest(
                self.config.output_dir,
                best_candidate,
                score,
                downloaded_video_path=downloaded_video_path,
            )
            self._record_source_run(datetime.now(timezone.utc))
            run_dir = manifest_path.parent
            source_dir = run_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            if manifest_path.parent != source_dir:
                target_manifest = source_dir / manifest_path.name
                manifest_path.replace(target_manifest)
                manifest_path = target_manifest
            if downloaded_video_path is not None and downloaded_video_path.parent != source_dir:
                target_video = source_dir / downloaded_video_path.name
                downloaded_video_path.replace(target_video)
                downloaded_video_path = target_video
            write_queue_state(
                run_dir,
                phase="sourced",
                next_stage="ai",
                source_manifest=(manifest_path.relative_to(run_dir)).as_posix(),
                source_video=(downloaded_video_path.relative_to(run_dir)).as_posix() if downloaded_video_path is not None else None,
                ready_for_next="ai",
            )
            if scheduler_state_path.exists() and scheduler_state.get("phase") == "scheduled":
                self._update_scheduler_state(
                    scheduler_state_path,
                    scheduler_state,
                    date=datetime.now(self._tz).date().isoformat(),
                    phase="source_consumed",
                    next_stage="editor",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            logger.info("Saved manifest to %s.", manifest_path)
            if downloaded_video_path is not None:
                logger.info("Saved video to %s.", downloaded_video_path)
            logger.info("Completed sourcing cycle.")
            return SourcingCycleOutcome(
                status="sourced",
                result=SourcingResult(
                    manifest_path=manifest_path,
                    downloaded_video_path=downloaded_video_path,
                ),
            )

        return SourcingCycleOutcome(
            status="no_match",
            reason="ranked candidates were present but none produced a valid downloadable clip",
        )

    def run_once(self, *, bypass_schedule: bool = False) -> SourcingCycleOutcome:
        now_local = self._local_now()
        scheduler_state_path = self.config.output_dir / "scheduler_state.json"
        scheduler_state = {}
        if scheduler_state_path.exists():
            scheduler_state = load_json(scheduler_state_path)

        scheduler_ready = isinstance(scheduler_state, dict) and scheduler_state.get("phase") == "scheduled"
        if not bypass_schedule:
            if scheduler_ready:
                logger.info("ClipScheduler has marked the queue ready; proceeding with this sourcing cycle.")
            else:
                allowed, reason = self._can_source_now(now_local)
                if not allowed:
                    logger.info("Skipping sourcing cycle because the daily schedule has not opened: %s", reason)
                    return SourcingCycleOutcome(status="skipped", reason=reason)
                logger.info("No scheduler signal was present; falling back to the legacy local schedule gate for this source cycle.")
        else:
            logger.info("Running one-off sourcing cycle immediately; ignoring the scheduler gate.")
        attempts = self.config.source_attempts_per_slot if scheduler_ready and not bypass_schedule else 1
        last_outcome = SourcingCycleOutcome(status="no_match", reason="no sourcing attempt was executed")
        for attempt_number in range(1, attempts + 1):
            logger.info("Sourcing attempt %s/%s for the current scheduler slot.", attempt_number, attempts)
            try:
                outcome = self._attempt_sourcing(scheduler_state_path, scheduler_state if isinstance(scheduler_state, dict) else {})
            except Exception as error:
                logger.warning("Sourcing attempt %s/%s failed with an exception: %s", attempt_number, attempts, error)
                last_outcome = SourcingCycleOutcome(status="error", reason=str(error))
                continue
            last_outcome = outcome
            if outcome.status == "sourced":
                return outcome
            logger.info("Sourcing attempt %s/%s ended without a valid clip: %s", attempt_number, attempts, outcome.reason)

        if scheduler_ready and not bypass_schedule and isinstance(scheduler_state, dict) and scheduler_state_path.exists():
            failure_reason = last_outcome.reason or "no valid clip was available after the scheduled retries"
            self._mark_scheduler_source_failed(
                scheduler_state_path,
                scheduler_state,
                attempts=attempts,
                reason=failure_reason,
            )
            logger.info("No valid clip was sourced after %s attempts; ending this scheduler slot and waiting for the next one.", attempts)
            return SourcingCycleOutcome(status="source_failed", reason=failure_reason)

        return last_outcome

    def run_forever(self) -> None:
        cycle_count = 0
        while True:
            scheduler_state_path = self.config.output_dir / "scheduler_state.json"
            scheduler_state = load_json(scheduler_state_path)
            if not (isinstance(scheduler_state, dict) and scheduler_state.get("phase") == "scheduled"):
                logger.debug("Waiting for ClipScheduler to mark the queue ready.")
                time.sleep(self.config.poll_interval_minutes * 60)
                continue

            cycle_count += 1
            logger.info("Starting cycle %s after ClipScheduler signaled ready.", cycle_count)
            outcome = self.run_once()
            if outcome.status == "sourced" and outcome.result is not None:
                logger.info("Sourced manifest: %s", outcome.result.manifest_path)
                if outcome.result.downloaded_video_path is not None:
                    logger.info("Downloaded video: %s", outcome.result.downloaded_video_path)
            elif outcome.reason:
                logger.info("Sourcing cycle %s ended without a queued clip: %s", cycle_count, outcome.reason)
            else:
                logger.info("Sourcing cycle %s ended without a queued clip.", cycle_count)

            logger.info("Completed cycle %s. Polling again for the next scheduler signal.", cycle_count)
            time.sleep(self.config.poll_interval_minutes * 60)
