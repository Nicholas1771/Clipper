from collections import Counter
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from common.creators import Creator
from clipscheduler.config import AppConfig
from clipscheduler.runner import ClipSchedulerRunner


class CreatorScheduleTests(unittest.TestCase):
    def config(self, queue, mode="instagram_top"):
        return AppConfig(
            queue_dir=queue, poll_interval_seconds=1, schedule_start_hour=8, schedule_end_hour=23,
            instagram_posts_per_day=7, youtube_posts_per_day=3, min_gap_minutes=10,
            timezone_name="UTC", youtube_selection_mode=mode,
            creators=(
                Creator("one", 4, 1, {"STREAMER_NAME": "One"}),
                Creator("two", 3, 2, {"STREAMER_NAME": "Two"}),
            ),
        )

    def test_random_mixed_account_schedule_preserves_exact_quotas_and_restart(self):
        with TemporaryDirectory() as tmp:
            config = self.config(Path(tmp))
            now = datetime(2026, 10, 19, tzinfo=ZoneInfo("UTC"))
            orders = set()
            for seed in range(15):
                runner = ClipSchedulerRunner(config, random.Random(seed))
                with patch.object(runner, "_ensure_youtube_selection"), patch.object(runner, "_send_plan_alert"):
                    plan = runner.ensure_plan(now)
                self.assertEqual(Counter(plan.creators), {"one": 4, "two": 3})
                self.assertEqual(plan.platforms, [["instagram"]] * 7)
                orders.add(tuple(plan.creators))
                resumed = ClipSchedulerRunner(config)
                saved = resumed._load_saved_plan(now.date())
                self.assertEqual(saved.creators, plan.creators)
                self.assertEqual(saved.creator_names, plan.creator_names)
                (Path(tmp) / "scheduler_plan.json").unlink()
            self.assertGreater(len(orders), 1)

    def test_shared_slots_enforce_each_platform_quota_for_each_creator(self):
        with TemporaryDirectory() as tmp:
            runner = ClipSchedulerRunner(self.config(Path(tmp), "shared"), random.Random(4))
            now = datetime(2026, 10, 19, tzinfo=ZoneInfo("UTC"))
            with patch.object(runner, "_send_plan_alert"):
                plan = runner.ensure_plan(now)
            quotas = Counter((creator, platform) for creator, platforms in zip(plan.creators, plan.platforms) for platform in platforms)
            self.assertEqual(quotas, {("one", "instagram"): 4, ("one", "youtube"): 1, ("two", "instagram"): 3, ("two", "youtube"): 2})
            runner._mark_ready(when=plan.slots[0], platforms=plan.platforms[0])
            state = json.loads((Path(tmp) / "scheduler_state.json").read_text())
            self.assertEqual(state["creator"], plan.creators[0])
            self.assertEqual(state["creator_name"], plan.creator_names[0])

    def test_full_day_quotas_cannot_be_silently_reduced_by_minimum_gap(self):
        with TemporaryDirectory() as tmp:
            config = replace(self.config(Path(tmp)), min_gap_minutes=180)
            runner = ClipSchedulerRunner(config)
            now = datetime(2026, 10, 19, tzinfo=ZoneInfo("UTC"))
            with self.assertRaisesRegex(ValueError, "daily creator slots do not fit"):
                runner._generate_daily_slots(now)
            with self.assertRaisesRegex(ValueError, "daily YouTube creator slots do not fit"):
                runner._generate_youtube_slots(now)

    def test_immediate_run_reports_and_sources_the_same_creator(self):
        with TemporaryDirectory() as tmp:
            runner = ClipSchedulerRunner(self.config(Path(tmp), "shared"), random.Random(2))
            now = datetime(2026, 10, 19, tzinfo=ZoneInfo("UTC"))
            with patch.object(runner, "_local_now", return_value=now), \
                 patch.object(runner, "_send_plan_alert") as alert:
                runner.fire_now()
            state = json.loads((Path(tmp) / "scheduler_state.json").read_text())
            self.assertEqual(state["creator_name"], runner._immediate_creator[1])
            self.assertEqual(state["platforms"], runner._immediate_creator[2])
            self.assertEqual(alert.call_args.args[2], now)

    def test_shared_slot_count_is_sum_of_creator_maxima_not_max_of_account_totals(self):
        with TemporaryDirectory() as tmp:
            config = replace(self.config(Path(tmp), "shared"), creators=(
                Creator("one", 4, 0, {"STREAMER_NAME": "One"}),
                Creator("two", 0, 3, {"STREAMER_NAME": "Two"}),
            ), instagram_posts_per_day=4, youtube_posts_per_day=3)
            runner = ClipSchedulerRunner(config)
            self.assertEqual(runner._target_slot_count(remaining_fraction=1), 7)
