from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from clipscheduler.config import AppConfig
from common.alerts import send_email_alert
from common.env import _find_repo_root, is_test_mode, looks_like_windows_path
from common.platforms import INSTAGRAM, PLATFORM_LABELS, PLATFORMS, YOUTUBE, normalize_platforms
from common.schedule_alerts import send_consolidated_daily_report, send_consolidated_schedule_alert, send_consolidated_started_alert
from common.reporting import record_post, record_stage_failure
from common.creators import Creator

logger = logging.getLogger(__name__)

ACTIVE_PHASES = {"scheduled", "source_consumed", "ai_processed", "edited", "publishing"}
FINISHED_PHASES = {"published", "source_failed", "publish_failed"}
STALE_CYCLE_HOURS = 3
MIN_GAP_FLOOR_SECONDS = 60
STAGES = ("scheduler", "sourcer", "ai", "editor", "publisher")
STAGE_PHASE_RANK = {"scheduled": 0, "source_consumed": 1, "ai_processed": 2, "edited": 3, "publishing": 3, "published": 4}
YOUTUBE_STATE_FILES = {"youtube_plan.json", "youtube_history.json"}


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class DayPlan:
    day: date
    slots: list[datetime]
    next_index: int = 0
    platforms: list[list[str]] = field(default_factory=list)
    youtube_slots: list[datetime] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    creator_names: list[str] = field(default_factory=list)

    def platforms_for(self, index: int) -> list[str]:
        return list(self.platforms[index]) if 0 <= index < len(self.platforms) else [INSTAGRAM]


class ClipSchedulerRunner:
    def __init__(self, config: AppConfig, rng: random.Random | None = None) -> None:
        self.config = config
        self._tz = ZoneInfo(config.timezone_name)
        self._rng = rng or random.Random()
        self._plan: DayPlan | None = None
        self._startup_alert_sent = False
        self._resume_index: int | None = None
        self._youtube_retry_at: datetime | None = None
        self._report_retry_at: datetime | None = None
        self._report_sent_day: date | None = None
        self._immediate_creator: tuple[str, str, list[str]] | None = None

    def _local_now(self) -> datetime:
        return datetime.now(self._tz)

    def _schedule_window(self, day: date) -> tuple[datetime, datetime]:
        start_hour = self.config.schedule_start_hour
        end_hour = self.config.schedule_end_hour
        if not (0 <= start_hour < end_hour <= 24):
            raise ValueError("SCHEDULE_END_HOUR must be later than SCHEDULE_START_HOUR (hours 0-24).")
        midnight = datetime(day.year, day.month, day.day, tzinfo=self._tz)
        start = midnight.replace(hour=start_hour)
        if end_hour == 24:
            next_day = day + timedelta(days=1)
            end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=self._tz)
        else:
            end = midnight.replace(hour=end_hour)
        return start, end

    def _plan_path(self) -> Path:
        return self.config.queue_dir / "scheduler_plan.json"

    def _target_slot_count(self, *, remaining_fraction: float) -> int:
        """Exactly posts-per-day for a full window, scaled down proportionally when starting mid-window."""
        posts_per_day = (
            sum(max(self._creator_quotas(creator)) for creator in self.config.creators)
            if self.config.creators else max(self._platform_quotas().values())
        )
        return max(0, round(posts_per_day * max(0.0, min(1.0, remaining_fraction))))

    def _creator_quotas(self, creator: Creator) -> tuple[int, int]:
        ig, yt = creator.instagram_posts_per_day, creator.youtube_posts_per_day
        if self.config.youtube_selection_mode == "instagram_top":
            return ig, 0
        return ig, yt

    def _creator_tickets(self) -> list[tuple[str, str, list[str]]]:
        tickets: list[tuple[str, str, list[str]]] = []
        for creator in self.config.creators:
            ig, yt = self._creator_quotas(creator)
            count = max(ig, yt)
            ig_indices = set(self._rng.sample(range(count), ig))
            yt_indices = set(self._rng.sample(range(count), yt))
            for index in range(count):
                platforms = ([INSTAGRAM] if index in ig_indices else []) + ([YOUTUBE] if index in yt_indices else [])
                tickets.append((creator.name, creator.display_name, platforms))
        return tickets

    def _assign_creators(self, plan: DayPlan) -> None:
        if not self.config.creators:
            return
        tickets = self._rng.sample(self._creator_tickets(), len(plan.slots))
        plan.creators = [ticket[0] for ticket in tickets]
        plan.creator_names = [ticket[1] for ticket in tickets]
        plan.platforms = [ticket[2] for ticket in tickets]

    def _generate_daily_slots(
        self, now: datetime, rng: random.Random | None = None, *, exact_count: int | None = None
    ) -> list[datetime]:
        """Generate slots in [max(now, window start), window end) for now's local day.

        Slots are stratified across the span, with the minimum gap guaranteed by
        distributing random positions over the span left after reserving the gaps.
        All math is done on epoch seconds so DST changes cannot skew spacing.
        """
        rng = rng or self._rng
        start, end = self._schedule_window(now.date())
        if now >= end:
            return []
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        window_start_ts = max(start_ts, now.timestamp())
        remaining_fraction = (end_ts - window_start_ts) / (end_ts - start_ts)

        base_ts = math.ceil(window_start_ts)
        span = int(end_ts) - base_ts
        if span < 1:
            return []

        gap = max(MIN_GAP_FLOOR_SECONDS, self.config.min_gap_minutes * 60)
        max_count = (span - 1) // gap + 1
        target_count = exact_count if exact_count is not None else self._target_slot_count(remaining_fraction=remaining_fraction)
        if self.config.creators and remaining_fraction >= 1 and target_count > max_count:
            raise ValueError(
                f"{target_count} daily creator slots do not fit the account window with MIN_GAP_MINUTES="
                f"{self.config.min_gap_minutes}; widen the window or reduce the minimum gap."
            )
        count = min(target_count, max_count)
        if count <= 0:
            return []

        free = span - 1 - (count - 1) * gap
        slots: list[datetime] = []
        for index in range(count):
            compressed = int((index + rng.random()) / count * free)
            slots.append(datetime.fromtimestamp(base_ts + compressed + index * gap, tz=self._tz))
        return slots

    def _platform_quotas(self) -> dict[str, int]:
        quotas = {
            INSTAGRAM: max(0, self.config.instagram_posts_per_day),
            YOUTUBE: max(0, self.config.youtube_posts_per_day),
        }
        if self.config.youtube_selection_mode == "instagram_top":
            return {INSTAGRAM: quotas[INSTAGRAM], YOUTUBE: 0}
        return quotas

    def _assign_platforms(self, count: int, rng: random.Random | None = None) -> list[list[str]]:
        """Randomly pick which slots publish to each platform.

        The busiest platform takes every slot; the others take a random subset sized in
        proportion to their posts-per-day (at least one while they are enabled).
        """
        rng = rng or self._rng
        assignments: list[list[str]] = [[] for _ in range(max(0, count))]
        quotas = self._platform_quotas()
        busiest = max(quotas.values())
        if count <= 0 or busiest <= 0:
            return assignments
        for platform in PLATFORMS:
            quota = quotas.get(platform, 0)
            if quota <= 0:
                continue
            share = min(count, max(1, count if quota == busiest else round(quota * min(1.0, count / busiest))))
            for index in sorted(rng.sample(range(count), share)):
                assignments[index].append(platform)
        return assignments

    def ensure_plan(self, now: datetime, immediate: datetime | None = None) -> DayPlan:
        today = now.date()
        if self._plan is not None and self._plan.day == today:
            self._ensure_youtube_selection(now)
            return self._plan
        started = not self._startup_alert_sent
        self._startup_alert_sent = True
        plan = self._load_saved_plan(today)
        if plan is not None:
            if self._resume_index is not None:
                plan.next_index = min(plan.next_index, self._resume_index)
            self._plan = plan
            self._save_plan(now)
            logger.info(
                "ClipScheduler resumed the saved plan for %s: %s of %s slot(s) remaining: %s",
                today.isoformat(),
                len(plan.slots) - plan.next_index,
                len(plan.slots),
                [slot.strftime("%Y-%m-%d %H:%M:%S %Z") for slot in plan.slots[plan.next_index :]],
            )
        else:
            slots = self._generate_daily_slots(now)
            self._plan = DayPlan(day=today, slots=slots, platforms=self._assign_platforms(len(slots)))
            self._assign_creators(self._plan)
            self._plan.youtube_slots = self._generate_youtube_slots(now)
            self._save_plan(now)
            logger.info(
                "ClipScheduler generated %s slot(s) for %s: %s",
                len(slots),
                today.isoformat(),
                [
                    f"{slot.strftime('%Y-%m-%d %H:%M:%S %Z')} [{'+'.join(self._plan.platforms_for(index))}]"
                    f" {self._plan.creator_names[index] if self._plan.creator_names else ''}"
                    for index, slot in enumerate(slots)
                ],
            )
        if immediate is not None:
            logger.info("ClipScheduler immediate run (--now) at %s, outside the planned slots.", immediate.strftime("%Y-%m-%d %H:%M:%S %Z"))
        self._ensure_youtube_selection(now)
        self._send_plan_alert(self._plan, now, immediate, started=started)
        return self._plan

    def _generate_youtube_slots(self, now: datetime) -> list[datetime]:
        if self.config.youtube_selection_mode != "instagram_top":
            return []
        quota = max(0, self.config.youtube_posts_per_day)
        if not quota:
            return []
        window_config = replace(
            self.config,
            schedule_start_hour=self.config.youtube_start_hour,
            schedule_end_hour=self.config.youtube_end_hour,
            instagram_posts_per_day=quota,
            youtube_posts_per_day=0,
            youtube_selection_mode="shared",
            creators=(),
        )
        window_runner = ClipSchedulerRunner(window_config, rng=self._rng)
        slots = window_runner._generate_daily_slots(now, exact_count=quota)
        start, _ = window_runner._schedule_window(now.date())
        if self.config.creators and now <= start and len(slots) < quota:
            raise ValueError(
                f"{quota} daily YouTube creator slots do not fit the YouTube window with "
                f"MIN_GAP_MINUTES={self.config.min_gap_minutes}; widen the window or reduce the minimum gap."
            )
        return slots

    def _prepare_youtube_selection(self, now: datetime, slots: list[datetime]) -> bool:
        publisher_root = Path(__file__).resolve().parents[3] / "ClipPublisher"
        source_root = str(publisher_root / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from clippublisher.config import AppConfig as PublisherConfig
        from clippublisher.youtube_replay import YouTubeReplay

        config = PublisherConfig.from_repo_root(publisher_root, resolve_media_url=False)
        if config.queue_dir.resolve() != self.config.queue_dir.resolve():
            raise ValueError("Scheduler and publisher must use the same queue for YouTube selection.")
        return YouTubeReplay(config).prepare(now, slots)

    def simulate_day(self, now: datetime) -> tuple[DayPlan, dict | None]:
        """Same slot/platform/YouTube logic as a fresh start-of-day plan; writes nothing and sends no alerts."""
        slots = self._generate_daily_slots(now)
        plan = DayPlan(day=now.date(), slots=slots, platforms=self._assign_platforms(len(slots)),
                       youtube_slots=self._generate_youtube_slots(now))
        self._assign_creators(plan)
        if self.config.youtube_selection_mode != "instagram_top" or not plan.youtube_slots or now.hour >= self.config.youtube_end_hour:
            return plan, None
        publisher_root = Path(__file__).resolve().parents[3] / "ClipPublisher"
        source_root = str(publisher_root / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from clippublisher.config import AppConfig as PublisherConfig
        from clippublisher.youtube_replay import YouTubeReplay

        config = PublisherConfig.from_repo_root(publisher_root, resolve_media_url=False)
        if config.queue_dir.resolve() != self.config.queue_dir.resolve():
            raise ValueError("Scheduler and publisher must use the same queue for YouTube selection.")
        return plan, YouTubeReplay(config).simulate(now, plan.youtube_slots)

    def _ensure_youtube_selection(self, now: datetime) -> None:
        if (
            self.config.youtube_selection_mode != "instagram_top"
            or os.getenv("CLIPPER_TEST_MODE")
            or self._plan is None
            or not self._plan.youtube_slots
            or now.hour >= self.config.youtube_end_hour
            or (self._youtube_retry_at is not None and now < self._youtube_retry_at)
        ):
            return
        self._youtube_retry_at = now + timedelta(minutes=5)
        try:
            if self._prepare_youtube_selection(now, self._plan.youtube_slots):
                self._youtube_retry_at = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), self._tz)
        except Exception:
            logger.exception("Could not prepare YouTube ranking; Instagram scheduling continues unchanged.")

    def _load_saved_plan(self, today: date) -> DayPlan | None:
        saved = load_json(self._plan_path())
        if not isinstance(saved, dict) or saved.get("date") != today.isoformat():
            return None
        slots = [self._parse_time(value) for value in saved.get("slots") or []]
        if not isinstance(saved.get("slots"), list) or any(slot is None for slot in slots):
            return None
        next_index = saved.get("next_index")
        next_index = next_index if isinstance(next_index, int) else 0
        saved_platforms = saved.get("platforms")
        if isinstance(saved_platforms, list) and len(saved_platforms) == len(slots) and all(isinstance(item, list) for item in saved_platforms):
            platforms = [normalize_platforms(item) for item in saved_platforms]
        else:
            platforms = self._assign_platforms(len(slots))
        if self.config.youtube_selection_mode == "instagram_top":
            platforms = [[platform for platform in assignment if platform != YOUTUBE] for assignment in platforms]
        raw_youtube_slots = saved.get("youtube_slots", [])
        youtube_slots: list[datetime] = []
        if not isinstance(raw_youtube_slots, list):
            logger.warning("Invalid saved YouTube slots; skipping YouTube for this plan.")
        else:
            for value in raw_youtube_slots:
                parsed = self._parse_time(value)
                if parsed is None:
                    logger.warning("Invalid saved YouTube slots; skipping YouTube for this plan.")
                    youtube_slots = []
                    break
                youtube_slots.append(parsed)
        creators = saved.get("creators", [])
        creator_names = saved.get("creator_names", [])
        if self.config.creators and not creators:
            if len(self.config.creators) != 1:
                raise ValueError("Saved account plan has no creator assignments; finish the existing plan before adding creators.")
            creators = [self.config.creators[0].name] * len(slots)
            creator_names = [self.config.creators[0].display_name] * len(slots)
        if creators and (len(creators) != len(slots) or len(creator_names) != len(slots)):
            raise ValueError("Saved schedule has invalid creator assignments.")
        if self.config.creators and any(name not in {c.name for c in self.config.creators} for name in creators):
            raise ValueError("Saved schedule references a creator no longer configured on this account.")
        return DayPlan(day=today, slots=slots, next_index=max(0, min(next_index, len(slots))), platforms=platforms,
                       youtube_slots=youtube_slots, creators=creators, creator_names=creator_names)

    def prepare_startup(self) -> None:
        """Keep today's saved plan across restarts; drop only the interrupted cycle so its slot runs again."""
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)
        state = load_json(self._ready_marker_path())
        slot_index = state.get("slot_index") if isinstance(state, dict) else None
        if isinstance(state, dict) and state.get("phase") in ACTIVE_PHASES and isinstance(slot_index, int):
            self._resume_index = slot_index
        self._ready_marker_path().unlink(missing_ok=True)
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if child.name in {"processing", "Archive", "Test", "source", self._plan_path().name} | YOUTUBE_STATE_FILES:
                continue
            self._archive_stale_queue_item(child)
        self._plan = None

    def _send_plan_alert(
        self, plan: DayPlan, now: datetime, immediate: datetime | None = None, *, started: bool = False
    ) -> None:
        if os.getenv("CLIPPER_TEST_MODE"):
            return
        repo_root = _find_repo_root(Path(__file__).resolve())
        alert_config: AppConfig | SimpleNamespace = self.config
        if immediate is not None and self._immediate_creator is not None:
            alert_config = SimpleNamespace(
                **vars(self.config),
                immediate_creator_name=self._immediate_creator[1],
                immediate_platforms=self._immediate_creator[2],
            )
        try:
            if started:
                send_consolidated_started_alert(
                    repo_root=repo_root,
                    now=now,
                    fallback_plan=plan,
                    fallback_config=alert_config,
                    immediate=immediate,
                    send_alert_fn=send_email_alert,
                )
            else:
                send_consolidated_schedule_alert(
                    repo_root=repo_root,
                    now=now,
                    fallback_plan=plan,
                    fallback_config=alert_config,
                    immediate=immediate,
                    send_alert_fn=send_email_alert,
                )
        except Exception:
            logger.exception("Failed to send consolidated schedule alert email.")

    def _save_plan(self, now: datetime) -> None:
        plan = self._plan
        if plan is None:
            return
        for index, slot in enumerate(plan.slots):
            record_post(
                self.config.queue_dir, slot.isoformat(), plan.platforms_for(index), "planned",
                creator=plan.creator_names[index] if plan.creator_names else "",
            )
        save_json(
            self._plan_path(),
            {
                "date": plan.day.isoformat(),
                "posts_per_day": self._target_slot_count(remaining_fraction=1.0),
                "instagram_posts_per_day": max(0, self.config.instagram_posts_per_day),
                "youtube_posts_per_day": max(0, self.config.youtube_posts_per_day),
                "planned_slots": len(plan.slots),
                "slots": [slot.isoformat() for slot in plan.slots],
                "platforms": plan.platforms,
                "creators": plan.creators,
                "creator_names": plan.creator_names,
                "future_slots": [slot.isoformat() for slot in plan.slots[plan.next_index :]],
                "next_index": plan.next_index,
                "updated_at": now.isoformat(),
                "timezone_name": self.config.timezone_name,
                "youtube_selection_mode": self.config.youtube_selection_mode,
                "youtube_slots": [slot.isoformat() for slot in plan.youtube_slots],
            },
        )

    def _ready_marker_path(self) -> Path:
        return self.config.queue_dir / "scheduler_state.json"

    def _mark_ready(
        self,
        *,
        when: datetime,
        slot_index: int = 0,
        completed_slots: int = 0,
        all_slots: list[datetime] | None = None,
        now: datetime | None = None,
        platforms: list[str] | None = None,
    ) -> Path:
        resolved_slots = list(all_slots or [when])
        resolved_platforms = platforms if platforms is not None else [name for name, quota in self._platform_quotas().items() if quota > 0]
        payload = {
            "date": when.date().isoformat(),
            "phase": "scheduled",
            "next_stage": "source",
            "scheduled_for": when.isoformat(),
            "slot_index": slot_index,
            "completed_slots": completed_slots,
            "total_slots": len(resolved_slots),
            "slots": [slot.isoformat() for slot in resolved_slots],
            "platforms": normalize_platforms(resolved_platforms),
            "updated_at": (now or datetime.now(self._tz)).isoformat(),
        }
        if self.config.creators:
            if slot_index >= 0 and self._plan is not None and self._plan.creators:
                payload["creator"] = self._plan.creators[slot_index]
                payload["creator_name"] = self._plan.creator_names[slot_index]
            else:
                creator = self._immediate_creator or self._rng.choice(self._creator_tickets())
                payload["creator"], payload["creator_name"] = creator[0], creator[1]
                payload["platforms"] = creator[2]
        state_path = self._ready_marker_path()
        record_post(
            self.config.queue_dir, when.isoformat(), payload["platforms"], "in progress",
            creator=payload.get("creator_name", ""),
        )
        save_json(state_path, payload)
        logger.info(
            "ClipScheduler marked queue ready at %s for %s (platforms: %s; creator: %s).",
            when.isoformat(),
            f"slot {slot_index + 1}/{len(resolved_slots)}" if slot_index >= 0 else "an immediate (--now) run",
            ", ".join(payload["platforms"]) or "none",
            payload.get("creator_name") or "legacy",
        )
        return state_path
    def clear_schedule_artifacts(self) -> None:
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)

        state_path = self._ready_marker_path()
        if state_path.exists():
            state_path.unlink()
            logger.info("Cleared stale scheduler state at %s.", state_path)
        plan_path = self._plan_path()
        if plan_path.exists():
            plan_path.unlink()
            logger.info("Cleared stale scheduler plan at %s.", plan_path)

        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if child.name in {"processing", "Archive", "Test", "source"} | YOUTUBE_STATE_FILES:
                continue
            if child.is_dir() or child.is_file():
                self._archive_stale_queue_item(child)

        self._plan = None

    def _archive_stale_queue_item(self, child: Path) -> Path:
        archive_dir = self.config.queue_dir / "Archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / child.name
        suffix = 1
        while destination.exists():
            destination = archive_dir / f"{child.name}_{suffix}"
            suffix += 1
        try:
            if child.is_dir():
                shutil.move(str(child), str(destination))
            else:
                child.rename(destination)
        except (PermissionError, OSError):
            if child.is_dir():
                shutil.move(str(child), str(destination))
            else:
                shutil.move(str(child), str(destination))
        logger.info("Archived unfinished queue item %s to %s during startup cleanup.", child, destination)
        return destination

    def force_reset_current_cycle(self) -> None:
        self.clear_schedule_artifacts()

        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if child.name in {"processing", "Archive", "Test", "source"} | YOUTUBE_STATE_FILES:
                continue
            self._archive_stale_queue_item(child)

    def reset_state_files(self) -> None:
        """Remove stale scheduler files without touching or archiving queue items."""
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)
        self._ready_marker_path().unlink(missing_ok=True)
        self._plan_path().unlink(missing_ok=True)
        self._plan = None

    def stage_status(self, stage: str) -> str:
        """Return 'reached', 'failed' or 'pending' based on the shared scheduler state."""
        state = load_json(self._ready_marker_path())
        phase = state.get("phase") if isinstance(state, dict) else None
        if phase in {"source_failed", "publish_failed"}:
            return "failed"
        return "reached" if STAGE_PHASE_RANK.get(phase, -1) >= STAGES.index(stage) else "pending"

    def _parse_time(self, value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=self._tz)
        return parsed.astimezone(self._tz)

    def _read_today_state(self, today: date) -> dict | None:
        state = load_json(self._ready_marker_path())
        if isinstance(state, dict) and state.get("date") == today.isoformat():
            return state
        return None

    def _state_payload(self, plan: DayPlan, now: datetime, *, phase: str, completed: int, previous: dict | None = None) -> dict:
        payload = dict(previous or {})
        payload.update(
            {
                "date": plan.day.isoformat(),
                "phase": phase,
                "completed_slots": completed,
                "total_slots": len(plan.slots),
                "slots": [slot.isoformat() for slot in plan.slots],
                "updated_at": now.isoformat(),
            }
        )
        return payload

    def fire_now(self) -> str:
        """Start one cycle immediately for every enabled platform, outside the planned slots (works after the window closed)."""
        now = self._local_now()
        if self.config.creators:
            tickets = self._creator_tickets()
            if not tickets:
                self.ensure_plan(now)
                logger.info("No fresh-source creator slots are enabled; only the YouTube replay plan will run.")
                return "waiting"
            self._immediate_creator = self._rng.choice(tickets)
        plan = self.ensure_plan(now, immediate=now)
        marker = self._mark_ready(
            when=now,
            slot_index=-1,
            completed_slots=plan.next_index,
            all_slots=plan.slots,
            now=now,
        )
        return str(marker)

    def run_once(self, now: datetime | None = None) -> str:
        now = now.astimezone(self._tz) if now is not None else self._local_now()
        result = self._run_once(now)
        self._send_daily_report_if_due(now)
        return result

    def _run_once(self, now: datetime) -> str:
        plan = self.ensure_plan(now)
        state_path = self._ready_marker_path()
        state = self._read_today_state(now.date())

        if state is None:
            save_json(state_path, self._state_payload(plan, now, phase="idle", completed=0))
            state = self._read_today_state(now.date()) or {}

        phase = str(state.get("phase") or "idle")
        slot_index = state.get("slot_index")
        completed = int(state.get("completed_slots") or 0)

        if phase in FINISHED_PHASES:
            if isinstance(slot_index, int):
                completed = max(completed, slot_index + 1)
            phase = "waiting_next_slot"
            save_json(state_path, self._state_payload(plan, now, phase=phase, completed=completed, previous=state))
        elif phase in ACTIVE_PHASES:
            last_update = self._parse_time(state.get("updated_at")) or self._parse_time(state.get("scheduled_for"))
            if last_update is not None and now - last_update > timedelta(hours=STALE_CYCLE_HOURS):
                logger.warning("Abandoning stale scheduler cycle (phase=%s, last update %s).", phase, last_update.isoformat())
                stage = {
                    "scheduled": "ClipSourcer",
                    "source_consumed": "ClipAI",
                    "ai_processed": "ClipEditor",
                    "edited": "ClipPublisher",
                    "publishing": "ClipPublisher",
                }.get(phase, "ClipScheduler")
                record_stage_failure(
                    self.config.queue_dir,
                    stage,
                    RuntimeError(f"No publish completed within {STALE_CYCLE_HOURS} hours while the slot was at {phase}."),
                    final=True,
                )
                record_post(
                    self.config.queue_dir, str(state.get("scheduled_for") or ""),
                    state.get("platforms") or [INSTAGRAM], "abandoned", f"No progress for {STALE_CYCLE_HOURS} hours",
                )
                if isinstance(slot_index, int):
                    completed = max(completed, slot_index + 1)
                phase = "waiting_next_slot"
                save_json(state_path, self._state_payload(plan, now, phase=phase, completed=completed, previous=state))
            else:
                logger.info("ClipScheduler already has an active cycle for today (%s).", phase)
                return str(state_path)

        if plan.next_index >= len(plan.slots):
            if plan.slots and phase != "complete":
                save_json(state_path, self._state_payload(plan, now, phase="complete", completed=len(plan.slots), previous=state))
                logger.info("All %s daily slots are complete; waiting for next day.", len(plan.slots))
            return "waiting"

        due = [index for index in range(plan.next_index, len(plan.slots)) if plan.slots[index] <= now]
        if not due:
            logger.info(
                "ClipScheduler is waiting for slot %s/%s at %s.",
                plan.next_index + 1,
                len(plan.slots),
                plan.slots[plan.next_index].isoformat(),
            )
            return "waiting"

        # Fire only the latest due slot; earlier overdue ones are skipped so slots never fire back to back.
        fire_index = due[-1]
        skipped = fire_index - plan.next_index
        if skipped:
            logger.warning("Skipping %s overdue slot(s) before slot %s.", skipped, fire_index + 1)
            for index in range(plan.next_index, fire_index):
                record_post(self.config.queue_dir, plan.slots[index].isoformat(), plan.platforms_for(index), "skipped", "Overdue slot coalesced")
        marker = self._mark_ready(
            when=plan.slots[fire_index],
            slot_index=fire_index,
            completed_slots=fire_index,
            all_slots=plan.slots,
            now=now,
            platforms=plan.platforms_for(fire_index),
        )
        plan.next_index = fire_index + 1
        self._save_plan(now)
        return str(marker)

    def _send_daily_report_if_due(self, now: datetime) -> None:
        if (
            now.strftime("%H:%M") < self.config.daily_report_time
            or self._report_sent_day == now.date()
            or (self._report_retry_at is not None and now < self._report_retry_at)
            or is_test_mode()
        ):
            return
        self._report_retry_at = now + timedelta(minutes=1)
        try:
            if send_consolidated_daily_report(
                _find_repo_root(Path(__file__).resolve()), now, self.config, send_alert_fn=send_email_alert,
            ):
                self._report_sent_day = now.date()
        except Exception:
            logger.exception("Failed to send consolidated daily report email; will retry.")

    def run_forever(self) -> None:
        logger.info("Watching queue %s for the scheduled source window.", self.config.queue_dir)
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval_seconds)
