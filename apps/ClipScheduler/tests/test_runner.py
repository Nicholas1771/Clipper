from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
for path in (APP_ROOT, SRC_ROOT, COMMON_SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from clipscheduler.config import AppConfig
from clipscheduler.runner import ClipSchedulerRunner
from main import run_until_stage

TZ = ZoneInfo("America/New_York")


def make_runner(queue_dir: Path, seed: int = 1, **overrides) -> ClipSchedulerRunner:
    values = dict(
        queue_dir=queue_dir,
        poll_interval_seconds=60,
        schedule_start_hour=10,
        schedule_end_hour=22,
        instagram_posts_per_day=12,
        min_gap_minutes=10,
        timezone_name="America/New_York",
    )
    values.update(overrides)
    return ClipSchedulerRunner(AppConfig(**values), rng=random.Random(seed))


def at(hour: int, minute: int = 0, day: int = 6, month: int = 10, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=TZ)


class SlotGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name)

    def assert_valid(self, slots, lo: datetime, hi: datetime, gap_seconds: int = 600) -> None:
        self.assertEqual(sorted(slots), slots)
        self.assertEqual(len(set(slots)), len(slots))
        for slot in slots:
            self.assertGreaterEqual(slot, lo)
            self.assertLess(slot, hi)
        for earlier, later in zip(slots, slots[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds(), gap_seconds)

    def test_before_window_generates_exact_full_day(self) -> None:
        for seed in range(300):
            slots = make_runner(self.queue, seed)._generate_daily_slots(at(6))
            self.assert_valid(slots, at(10), at(22))
            self.assertEqual(12, len(slots))

    def test_midway_generates_exactly_half(self) -> None:
        for seed in range(300):
            slots = make_runner(self.queue, seed)._generate_daily_slots(at(16))
            self.assert_valid(slots, at(16), at(22))
            self.assertEqual(6, len(slots))

    def test_distribution_is_even(self) -> None:
        for seed in range(200):
            slots = make_runner(self.queue, seed)._generate_daily_slots(at(0, 5))
            thirds = [0, 0, 0]
            for slot in slots:
                thirds[min(2, int((slot - at(10)).total_seconds() // (4 * 3600)))] += 1
            self.assertTrue(all(count >= 2 for count in thirds), (seed, thirds))

    def test_at_or_after_window_end_generates_nothing(self) -> None:
        runner = make_runner(self.queue)
        self.assertEqual([], runner._generate_daily_slots(at(22)))
        self.assertEqual([], runner._generate_daily_slots(at(23, 30)))

    def test_near_end_respects_gap_and_bounds(self) -> None:
        now = at(21, 52)
        for seed in range(200):
            slots = make_runner(self.queue, seed)._generate_daily_slots(now)
            self.assert_valid(slots, now, at(22))

    def test_boundary_start_times(self) -> None:
        for now in (at(10), at(10, 0, second=1), at(9, 59, second=59), at(21, 59, second=30)):
            for seed in range(50):
                slots = make_runner(self.queue, seed)._generate_daily_slots(now)
                self.assert_valid(slots, max(now, at(10)), at(22))

    def test_large_gap_reduces_count(self) -> None:
        slots = make_runner(self.queue, min_gap_minutes=120)._generate_daily_slots(at(6))
        self.assertLessEqual(len(slots), 7)
        self.assert_valid(slots, at(10), at(22), gap_seconds=7200)

    def test_zero_posts(self) -> None:
        self.assertEqual([], make_runner(self.queue, instagram_posts_per_day=0)._generate_daily_slots(at(6)))

    def test_dst_days_keep_gap_and_window(self) -> None:
        for day, month in ((8, 3), (1, 11)):
            for seed in range(50):
                slots = make_runner(self.queue, seed)._generate_daily_slots(at(0, 30, day=day, month=month))
                self.assert_valid(slots, at(10, day=day, month=month), at(22, day=day, month=month))

    def test_end_hour_24(self) -> None:
        slots = make_runner(self.queue, schedule_end_hour=24)._generate_daily_slots(at(6))
        self.assert_valid(slots, at(10), at(0, day=7))

    def test_invalid_window_raises(self) -> None:
        with self.assertRaises(ValueError):
            make_runner(self.queue, schedule_start_hour=12, schedule_end_hour=12)._generate_daily_slots(at(6))


class PlatformAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name)

    def state(self) -> dict:
        return json.loads((self.queue / "scheduler_state.json").read_text(encoding="utf-8"))

    def test_instagram_only_by_default(self) -> None:
        runner = make_runner(self.queue)
        plan = runner.ensure_plan(at(6))
        self.assertEqual([["instagram"]] * len(plan.slots), plan.platforms)

    def test_ranked_youtube_has_independent_exact_morning_quota(self) -> None:
        runner = make_runner(self.queue, youtube_posts_per_day=1, youtube_selection_mode="instagram_top")
        with patch.object(runner, "_prepare_youtube_selection", return_value=True) as prepare:
            plan = runner.ensure_plan(at(0))
            runner.ensure_plan(at(1))
        self.assertEqual([["instagram"]] * len(plan.slots), plan.platforms)
        self.assertEqual(1, len(plan.youtube_slots))
        self.assertGreaterEqual(plan.youtube_slots[0], at(10))
        self.assertLess(plan.youtube_slots[0], at(12))
        prepare.assert_called_once_with(at(0), plan.youtube_slots)

    def test_youtube_plan_generation_after_window_does_not_schedule_backdated_upload(self) -> None:
        runner = make_runner(self.queue, youtube_posts_per_day=1, youtube_selection_mode="instagram_top")
        with patch.object(runner, "_prepare_youtube_selection") as prepare:
            plan = runner.ensure_plan(at(13))
        self.assertEqual([], plan.youtube_slots)
        prepare.assert_not_called()
        self.assertTrue(plan.slots)

    def test_failed_youtube_selection_retries_without_blocking_instagram(self) -> None:
        runner = make_runner(self.queue, youtube_posts_per_day=1, youtube_selection_mode="instagram_top")
        with patch.object(runner, "_prepare_youtube_selection", side_effect=[False, True]) as prepare:
            first = runner.ensure_plan(at(0))
            runner.ensure_plan(at(0, 1))
            second = runner.ensure_plan(at(0, 5))
        self.assertIs(first, second)
        self.assertEqual(2, prepare.call_count)
        self.assertTrue(first.slots)

    def test_ranked_youtube_slots_and_history_survive_startup(self) -> None:
        first = make_runner(self.queue, youtube_posts_per_day=1, youtube_selection_mode="instagram_top")
        with patch.object(first, "_prepare_youtube_selection", return_value=True):
            original = first.ensure_plan(at(0))
        for name in ("youtube_plan.json", "youtube_history.json"):
            (self.queue / name).write_text("{}", encoding="utf-8")
        restarted = make_runner(self.queue, youtube_posts_per_day=1, youtube_selection_mode="instagram_top")
        restarted.prepare_startup()
        with patch.object(restarted, "_prepare_youtube_selection", return_value=True):
            plan = restarted.ensure_plan(at(1))
        self.assertEqual(original.youtube_slots, plan.youtube_slots)
        self.assertEqual(original.slots, plan.slots)
        self.assertTrue((self.queue / "youtube_history.json").exists())
        self.assertTrue((self.queue / "youtube_plan.json").exists())

    def test_slot_count_follows_the_busier_platform(self) -> None:
        for seed in range(200):
            runner = make_runner(self.queue, seed, instagram_posts_per_day=3, youtube_posts_per_day=12)
            self.assertEqual(12, len(runner._generate_daily_slots(at(6))))

    def test_every_slot_is_used_and_the_lighter_platform_gets_a_random_subset(self) -> None:
        youtube_positions = set()
        for seed in range(200):
            runner = make_runner(self.queue, seed, instagram_posts_per_day=12, youtube_posts_per_day=4)
            platforms = runner._assign_platforms(12)
            self.assertEqual(12, len(platforms))
            self.assertTrue(all("instagram" in slot for slot in platforms))
            youtube = [index for index, slot in enumerate(platforms) if "youtube" in slot]
            self.assertEqual(4, len(youtube))
            youtube_positions.update(youtube)
        self.assertEqual(set(range(12)), youtube_positions)

    def test_youtube_can_be_the_busier_platform(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=2, youtube_posts_per_day=8)
        platforms = runner._assign_platforms(8)
        self.assertTrue(all("youtube" in slot for slot in platforms))
        self.assertEqual(2, sum("instagram" in slot for slot in platforms))

    def test_enabled_platform_always_gets_at_least_one_slot(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=20, youtube_posts_per_day=1)
        platforms = runner._assign_platforms(3)
        self.assertEqual(1, sum("youtube" in slot for slot in platforms))

    def test_disabled_platform_is_never_assigned(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=0, youtube_posts_per_day=5)
        platforms = runner._assign_platforms(5)
        self.assertEqual([["youtube"]] * 5, platforms)
        self.assertEqual([], make_runner(self.queue, instagram_posts_per_day=0)._assign_platforms(4)[0])

    def test_fired_slot_publishes_its_assigned_platforms(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=6, youtube_posts_per_day=3)
        plan = runner.ensure_plan(at(6))
        runner.run_once(plan.slots[0])
        self.assertEqual(plan.platforms[0], self.state()["platforms"])
        plan_file = json.loads((self.queue / "scheduler_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan.platforms, plan_file["platforms"])
        self.assertEqual(3, plan_file["youtube_posts_per_day"])

    def test_assignment_survives_a_restart(self) -> None:
        first = make_runner(self.queue, instagram_posts_per_day=6, youtube_posts_per_day=3)
        original = first.ensure_plan(at(6)).platforms
        second = make_runner(self.queue, 77, instagram_posts_per_day=6, youtube_posts_per_day=3)
        self.assertEqual(original, second.ensure_plan(at(7)).platforms)

    def test_old_plan_without_platforms_is_upgraded(self) -> None:
        first = make_runner(self.queue)
        plan = first.ensure_plan(at(6))
        plan_path = self.queue / "scheduler_plan.json"
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        del saved["platforms"]
        plan_path.write_text(json.dumps(saved), encoding="utf-8")
        resumed = make_runner(self.queue).ensure_plan(at(7))
        self.assertEqual(plan.slots, resumed.slots)
        self.assertEqual(len(plan.slots), len(resumed.platforms))

    def test_fire_now_after_the_window_still_starts_an_immediate_cycle(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=6, youtube_posts_per_day=3)
        late = at(23, 30)
        runner._local_now = lambda: late
        marker = runner.fire_now()
        state = json.loads(Path(marker).read_text(encoding="utf-8"))
        self.assertEqual("scheduled", state["phase"])
        self.assertEqual(-1, state["slot_index"])
        self.assertEqual(["instagram", "youtube"], state["platforms"])
        self.assertEqual(late.isoformat(), state["scheduled_for"])
        self.assertEqual(marker, runner.run_once(late))
        self.assertEqual("scheduled", self.state()["phase"])

    def test_zero_youtube_quota_schedules_only_instagram(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=8, youtube_posts_per_day=0)
        self.assertEqual({"instagram": 8, "youtube": 0}, runner._platform_quotas())
        self.assertEqual([["instagram"]] * 8, runner._assign_platforms(8))
        runner._mark_ready(when=at(12))
        self.assertEqual(["instagram"], self.state()["platforms"])

    def test_zero_instagram_quota_schedules_only_youtube(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=0, youtube_posts_per_day=2)
        self.assertEqual({"instagram": 0, "youtube": 2}, runner._platform_quotas())
        self.assertEqual([["youtube"]] * 2, runner._assign_platforms(2))

    def test_zero_youtube_quota_has_no_replay_slots(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=8, youtube_posts_per_day=0,
                             youtube_selection_mode="instagram_top")
        with patch.object(runner, "_prepare_youtube_selection") as prepare:
            plan = runner.ensure_plan(at(0))
        self.assertEqual([], plan.youtube_slots)
        self.assertTrue(all(slot == ["instagram"] for slot in plan.platforms))
        prepare.assert_not_called()

    def test_forced_cycle_targets_every_enabled_platform(self) -> None:
        runner = make_runner(self.queue, instagram_posts_per_day=2, youtube_posts_per_day=1)
        runner._mark_ready(when=at(12))
        self.assertEqual(["instagram", "youtube"], self.state()["platforms"])


class RunOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name)
        self.state_path = self.queue / "scheduler_state.json"

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_phase(self, phase: str) -> None:
        payload = self.state()
        payload["phase"] = phase
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_every_slot_fires_exactly_once_over_a_simulated_day(self) -> None:
        for seed in range(25):
            (self.queue / "scheduler_plan.json").unlink(missing_ok=True)
            runner = make_runner(self.queue, seed)
            now = at(0, 1)
            fired: list[str] = []
            while now < at(23, 59):
                result = runner.run_once(now)
                if result != "waiting" and self.state()["phase"] == "scheduled":
                    fired.append(self.state()["scheduled_for"])
                    self.set_phase("published")
                now += timedelta(seconds=30)
            self.assertEqual([slot.isoformat() for slot in runner._plan.slots], fired, seed)
            self.assertEqual("complete", self.state()["phase"])

    def test_slot_not_fired_early_and_fires_when_due(self) -> None:
        runner = make_runner(self.queue)
        first = runner.ensure_plan(at(6)).slots[0]
        self.assertEqual("waiting", runner.run_once(first - timedelta(seconds=1)))
        self.assertNotEqual("waiting", runner.run_once(first))
        self.assertEqual(0, self.state()["slot_index"])
        self.assertEqual("scheduled", self.state()["phase"])

    def test_active_cycle_blocks_new_slot_without_repeat(self) -> None:
        runner = make_runner(self.queue)
        plan = runner.ensure_plan(at(6))
        runner.run_once(plan.slots[0])
        late = plan.slots[1] + timedelta(minutes=1)
        self.assertEqual(str(self.state_path), runner.run_once(late))
        self.assertEqual(0, self.state()["slot_index"])
        self.set_phase("published")
        runner.run_once(late)
        self.assertEqual(1, self.state()["slot_index"])
        self.set_phase("published")
        self.assertEqual("waiting", runner.run_once(late + timedelta(seconds=30)))
        self.assertEqual(2, runner._plan.next_index)
        self.assertEqual(2, self.state()["completed_slots"])

    def test_missed_slots_are_coalesced_not_burst_fired(self) -> None:
        runner = make_runner(self.queue)
        plan = runner.ensure_plan(at(6))
        wake = plan.slots[3] + timedelta(seconds=5)
        runner.run_once(wake)
        self.assertEqual(3, self.state()["slot_index"])
        self.assertEqual(4, runner._plan.next_index)
        self.set_phase("published")
        self.assertEqual("waiting", runner.run_once(wake + timedelta(seconds=30)))

    def test_source_failed_advances_to_next_slot(self) -> None:
        runner = make_runner(self.queue)
        plan = runner.ensure_plan(at(6))
        runner.run_once(plan.slots[0])
        self.set_phase("source_failed")
        runner.run_once(plan.slots[0] + timedelta(seconds=30))
        self.assertEqual("waiting_next_slot", self.state()["phase"])
        runner.run_once(plan.slots[1])
        self.assertEqual(1, self.state()["slot_index"])
        self.assertEqual("scheduled", self.state()["phase"])

    def test_new_day_generates_new_plan(self) -> None:
        runner = make_runner(self.queue)
        day1 = runner.ensure_plan(at(6))
        self.assertTrue(all(slot.day == 6 for slot in day1.slots))
        runner.run_once(at(23, 0))
        runner.run_once(at(0, 0, day=7))
        self.assertEqual(7, runner._plan.day.day)
        self.assertTrue(runner._plan.slots)
        self.assertTrue(all(slot.day == 7 for slot in runner._plan.slots))
        self.assertEqual("idle", self.state()["phase"])
        self.assertEqual("2026-10-07", self.state()["date"])

    def test_stale_active_cycle_is_abandoned_and_next_slot_fires(self) -> None:
        runner = make_runner(self.queue)
        plan = runner.ensure_plan(at(6))
        runner.run_once(plan.slots[0])
        payload = self.state()
        payload["updated_at"] = plan.slots[0].isoformat()
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        later = plan.slots[0] + timedelta(hours=4)
        runner.run_once(later)
        self.assertGreater(self.state()["slot_index"], 0)
        self.assertEqual("scheduled", self.state()["phase"])

    def test_startup_mid_window_plans_only_future_slots(self) -> None:
        runner = make_runner(self.queue)
        now = at(16)
        runner.run_once(now)
        self.assertTrue(all(slot >= now for slot in runner._plan.slots))
        plan_file = json.loads((self.queue / "scheduler_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(len(runner._plan.slots), plan_file["planned_slots"])
        self.assertEqual(plan_file["slots"], plan_file["future_slots"])

    def test_startup_after_window_waits(self) -> None:
        runner = make_runner(self.queue)
        self.assertEqual("waiting", runner.run_once(at(23)))
        self.assertEqual([], runner._plan.slots)

    def test_state_contract_fields_for_downstream_apps(self) -> None:
        runner = make_runner(self.queue)
        runner.run_once(runner.ensure_plan(at(6)).slots[0])
        state = self.state()
        for key in ("date", "phase", "next_stage", "scheduled_for", "slot_index", "completed_slots", "total_slots", "slots"):
            self.assertIn(key, state)
        self.assertEqual("scheduled", state["phase"])
        self.assertEqual("source", state["next_stage"])
        self.assertEqual("2026-10-06", state["date"])


class RestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name)
        patcher = patch("clipscheduler.runner.send_email_alert")
        self.alert = patcher.start()
        self.addCleanup(patcher.stop)

    def restart(self, seed: int = 99) -> ClipSchedulerRunner:
        runner = make_runner(self.queue, seed)
        runner.prepare_startup()
        return runner

    def test_restart_keeps_same_slots_and_sends_started_alert(self) -> None:
        first = make_runner(self.queue)
        original = first.ensure_plan(at(6)).slots
        self.alert.reset_mock()
        second = self.restart()
        plan = second.ensure_plan(at(11))
        self.assertEqual(original, plan.slots)
        subject = self.alert.call_args.kwargs["subject"]
        self.assertTrue(subject.startswith("Clip Start "))
        self.assertIn("remaining", self.alert.call_args.kwargs["body"])

    def test_restart_after_completed_slots_only_lists_remaining(self) -> None:
        first = make_runner(self.queue)
        plan = first.ensure_plan(at(6))
        first.run_once(plan.slots[2])
        self.assertEqual(3, first._plan.next_index)
        state_path = self.queue / "scheduler_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["phase"] = "published"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        second = self.restart()
        resumed = second.ensure_plan(plan.slots[2] + timedelta(minutes=1))
        self.assertEqual(3, resumed.next_index)
        self.assertEqual(plan.slots, resumed.slots)

    def test_restart_mid_cycle_retries_interrupted_slot(self) -> None:
        first = make_runner(self.queue)
        plan = first.ensure_plan(at(6))
        first.run_once(plan.slots[1])
        interrupted_item = self.queue / "interrupted_run"
        interrupted_item.mkdir()
        (interrupted_item / "queue_state.json").write_text('{"phase":"sourced"}', encoding="utf-8")
        self.assertEqual("scheduled", json.loads((self.queue / "scheduler_state.json").read_text(encoding="utf-8"))["phase"])
        second = self.restart()
        resumed = second.ensure_plan(plan.slots[1] + timedelta(minutes=1))
        self.assertEqual(plan.slots, resumed.slots)
        self.assertEqual(first._plan.slots.index(plan.slots[1]), resumed.next_index)
        self.assertFalse(interrupted_item.exists())
        self.assertTrue((self.queue / "Archive" / "interrupted_run").exists())

    def test_restart_after_window_has_nothing_left(self) -> None:
        first = make_runner(self.queue)
        plan = first.ensure_plan(at(6))
        first._plan.next_index = len(plan.slots)
        first._save_plan(at(21))
        second = self.restart()
        resumed = second.ensure_plan(at(23))
        self.assertEqual(plan.slots, resumed.slots)
        self.assertIn("No runs planned.", self.alert.call_args.kwargs["body"])

    def test_restart_next_day_generates_new_plan(self) -> None:
        make_runner(self.queue).ensure_plan(at(6))
        second = self.restart()
        plan = second.ensure_plan(at(6, day=7))
        self.assertTrue(all(slot.day == 7 for slot in plan.slots))
        self.assertTrue(self.alert.call_args.kwargs["subject"].startswith("Clip Start "))

    def test_stop_after_archives_stale_queue_before_firing_fresh_cycle(self) -> None:
        runner = make_runner(self.queue)
        stale_item = self.queue / "old_clip"
        stale_item.mkdir(parents=True)
        (stale_item / "queue_state.json").write_text('{"phase":"ai_processed"}', encoding="utf-8")

        with patch.object(runner, "stage_status", return_value="reached") as stage_status:
            result = run_until_stage(runner, "editor")

        self.assertEqual(0, result)
        self.assertFalse(stale_item.exists())
        self.assertTrue((self.queue / "Archive" / "old_clip").exists())
        state = json.loads((self.queue / "scheduler_state.json").read_text(encoding="utf-8"))
        self.assertEqual("scheduled", state["phase"])
        stage_status.assert_called_once_with("editor")

    def test_restart_with_corrupt_plan_generates_new_plan(self) -> None:
        (self.queue / "scheduler_plan.json").write_text("{not json", encoding="utf-8")
        plan = self.restart().ensure_plan(at(12))
        self.assertTrue(plan.slots)

    def test_overdue_slot_after_downtime_is_flagged_and_fires_once(self) -> None:
        first = make_runner(self.queue)
        plan = first.ensure_plan(at(6))
        second = self.restart()
        late = plan.slots[3] + timedelta(minutes=5)
        second.run_once(late)
        self.assertIn("Remaining Schedule", self.alert.call_args.kwargs["body"])
        self.assertEqual(4, second._plan.next_index)


if __name__ == "__main__":
    unittest.main()
