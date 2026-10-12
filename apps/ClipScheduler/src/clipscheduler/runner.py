from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from clipscheduler.config import AppConfig
from common.alerts import send_email_alert
from common.env import looks_like_windows_path
from common.platforms import INSTAGRAM, PLATFORMS, YOUTUBE, normalize_platforms

logger = logging.getLogger(__name__)

ACTIVE_PHASES = {"scheduled", "source_consumed", "ai_processed", "edited"}
FINISHED_PHASES = {"published", "source_failed"}
STALE_CYCLE_HOURS = 3
MIN_GAP_FLOOR_SECONDS = 60
STAGES = ("scheduler", "sourcer", "ai", "editor", "publisher")
STAGE_PHASE_RANK = {"scheduled": 0, "source_consumed": 1, "ai_processed": 2, "edited": 3, "published": 4}


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

    def _target_slot_count(self, *, remaining_fraction: float, rng: random.Random) -> int:
        """Posts for the remaining window, scaled proportionally with a tight random spread."""
        posts_per_day = self.config.slots_per_day
        target = posts_per_day * max(0.0, min(1.0, remaining_fraction))
        if target <= 0:
            return 0
        sigma = max(0.5, 0.1 * target)
        count = round(rng.gauss(target, sigma))
        low = max(0, math.floor(target) - 2)
        high = math.ceil(target) + 2
        return max(low, min(high, count))

    def _generate_daily_slots(self, now: datetime, rng: random.Random | None = None) -> list[datetime]:
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
        count = min(self._target_slot_count(remaining_fraction=remaining_fraction, rng=rng), max_count)
        if count <= 0:
            return []

        free = span - 1 - (count - 1) * gap
        slots: list[datetime] = []
        for index in range(count):
            compressed = int((index + rng.random()) / count * free)
            slots.append(datetime.fromtimestamp(base_ts + compressed + index * gap, tz=self._tz))
        return slots

    def _platform_quotas(self) -> dict[str, int]:
        return {
            INSTAGRAM: max(0, self.config.instagram_posts_per_day),
            YOUTUBE: max(0, self.config.youtube_posts_per_day),
        }

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
            share = min(count, max(1, round(count * quota / busiest)))
            for index in sorted(rng.sample(range(count), share)):
                assignments[index].append(platform)
        return assignments

    def ensure_plan(self, now: datetime, immediate: datetime | None = None) -> DayPlan:
        today = now.date()
        if self._plan is not None and self._plan.day == today:
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
            self._save_plan(now)
            logger.info(
                "ClipScheduler generated %s slot(s) for %s: %s",
                len(slots),
                today.isoformat(),
                [
                    f"{slot.strftime('%Y-%m-%d %H:%M:%S %Z')} [{'+'.join(self._plan.platforms_for(index))}]"
                    for index, slot in enumerate(slots)
                ],
            )
        if immediate is not None:
            logger.info("ClipScheduler immediate run (--now) at %s, outside the planned slots.", immediate.strftime("%Y-%m-%d %H:%M:%S %Z"))
        self._send_plan_alert(self._plan, now, immediate, started=started)
        return self._plan

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
        return DayPlan(day=today, slots=slots, next_index=max(0, min(next_index, len(slots))), platforms=platforms)

    def prepare_startup(self) -> None:
        """Keep today's saved plan across restarts; drop only the interrupted cycle so its slot runs again."""
        self.config.queue_dir.mkdir(parents=True, exist_ok=True)
        state = load_json(self._ready_marker_path())
        slot_index = state.get("slot_index") if isinstance(state, dict) else None
        if isinstance(state, dict) and state.get("phase") in ACTIVE_PHASES and isinstance(slot_index, int):
            self._resume_index = slot_index
        self._ready_marker_path().unlink(missing_ok=True)
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if child.name in {"processing", "Archive", "Test", "source", self._plan_path().name}:
                continue
            self._archive_stale_queue_item(child)
        self._plan = None

    def _send_plan_alert(
        self, plan: DayPlan, now: datetime, immediate: datetime | None = None, *, started: bool = False
    ) -> None:
        if os.getenv("CLIPPER_TEST_MODE"):
            return
        remaining = plan.slots[plan.next_index :]
        lines = []
        for index, slot in enumerate(remaining, start=plan.next_index + 1):
            overdue = " (overdue, runs on startup)" if slot <= now else ""
            targets = ", ".join(plan.platforms_for(index - 1)) or "none"
            lines.append(f"{index}. {slot.strftime('%Y-%m-%d %H:%M:%S %Z')} -> {targets}{overdue}")
        if immediate is not None:
            lines.insert(0, f"Immediate run (--now): {immediate.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        title = "Started" if started else "New schedule"
        body = (
            f"{title} for {plan.day.strftime('%Y%m%d')} ({self.config.timezone_name}).\n"
            f"Window: {self.config.schedule_start_hour:02d}:00-{self.config.schedule_end_hour:02d}:00, "
            f"posts planned: {len(plan.slots)}, remaining: {len(remaining)}\n\n"
            + ("\n".join(lines) if lines else "No runs left in today's window.")
            + "\n"
        )
        day_label = plan.day.strftime("%Y%m%d")
        subject = f"Started - {day_label}" if started else f"Schedule - {day_label}"
        try:
            send_email_alert(app_name="ClipScheduler", subject=subject, body=body)
        except Exception:
            logger.exception("Failed to send schedule alert email.")

    def _save_plan(self, now: datetime) -> None:
        plan = self._plan
        if plan is None:
            return
        save_json(
            self._plan_path(),
            {
                "date": plan.day.isoformat(),
                "posts_per_day": self.config.slots_per_day,
                "instagram_posts_per_day": max(0, self.config.instagram_posts_per_day),
                "youtube_posts_per_day": max(0, self.config.youtube_posts_per_day),
                "planned_slots": len(plan.slots),
                "slots": [slot.isoformat() for slot in plan.slots],
                "platforms": plan.platforms,
                "future_slots": [slot.isoformat() for slot in plan.slots[plan.next_index :]],
                "next_index": plan.next_index,
                "updated_at": now.isoformat(),
                "timezone_name": self.config.timezone_name,
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
        state_path = self._ready_marker_path()
        save_json(state_path, payload)
        logger.info(
            "ClipScheduler marked queue ready at %s for slot %s/%s (platforms: %s).",
            when.isoformat(),
            slot_index + 1,
            len(resolved_slots),
            ", ".join(payload["platforms"]) or "none",
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
            if child.name in {"processing", "Archive", "Test", "source"}:
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
            if child.name in {"processing", "Archive", "Test", "source"}:
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
        if phase == "source_failed":
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

    def run_once(self, now: datetime | None = None) -> str:
        now = now.astimezone(self._tz) if now is not None else self._local_now()
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

    def run_forever(self) -> None:
        logger.info("Watching queue %s for the scheduled source window.", self.config.queue_dir)
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval_seconds)
