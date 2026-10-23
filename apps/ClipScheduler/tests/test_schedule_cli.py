import importlib.util
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import random
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent.parent


class ScheduleCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_module("schedule_launcher", REPO_ROOT / "run_all_apps.py")
        cls.scheduler = load_module("schedule_entrypoint", APP_ROOT / "main.py")

    def test_launcher_starts_only_scheduler_for_each_profile(self):
        for profile in ("carootclips", "vrlclipmoments"):
            with self.subTest(profile=profile), \
                 mock.patch.object(self.launcher, "discover_profiles", return_value=["carootclips", "vrlclipmoments"]), \
                 mock.patch("sys.argv", ["run_all_apps.py", f"--{profile}", "--schedule"]), \
                 mock.patch.object(self.launcher, "launch_processes", return_value=0) as launch:
                self.assertEqual(self.launcher.main(), 0)
                self.assertEqual(launch.call_args.args[0], [
                    ("ClipScheduler", REPO_ROOT / "apps" / "ClipScheduler", ["--schedule"])
                ])
                self.assertEqual(launch.call_args.kwargs["profile"], profile)
                self.assertFalse(launch.call_args.kwargs["test_mode"])

    def test_launcher_rejects_pipeline_flags(self):
        for flags in (["--now"], ["--stop-after=editor"], ["--test"]):
            with self.subTest(flags=flags), mock.patch("sys.argv", ["run_all_apps.py", "--schedule", *flags]), \
                 mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
                self.launcher.parse_args()
            self.assertEqual(caught.exception.code, 2)

    def test_schedule_simulates_without_persisting_or_starting_work(self):
        with mock.patch("sys.argv", ["main.py", "--schedule", "--schedule-date=2026-10-15"]), \
             mock.patch.object(self.scheduler, "configure_app_logging"), \
             mock.patch.object(self.scheduler, "is_test_mode", return_value=False), \
             mock.patch.object(self.scheduler.AppConfig, "from_repo_root"), \
             mock.patch.object(self.scheduler, "ClipSchedulerRunner") as runner:
            tz = ZoneInfo("America/New_York")
            runner.return_value._local_now.return_value = datetime(2026, 10, 14, 22, 56, tzinfo=tz)
            plan = SimpleNamespace(slots=[], youtube_slots=[datetime(2026, 10, 15, 10, 30, tzinfo=tz)], platforms_for=lambda i: [])
            youtube = {"source_date": "2026-10-14", "entries": [], "unscheduled": [], "excluded": []}
            runner.return_value.simulate_day.return_value = (plan, youtube)
            self.assertEqual(self.scheduler.main(), 0)
            runner.return_value.simulate_day.assert_called_once_with(datetime(2026, 10, 15, tzinfo=tz))
            for method in ("ensure_plan", "prepare_startup", "reset_state_files", "force_reset_current_cycle", "fire_now", "run_forever", "_send_plan_alert", "_save_plan"):
                getattr(runner.return_value, method).assert_not_called()

    def test_default_schedule_date_is_next_midnight(self):
        with mock.patch("sys.argv", ["main.py", "--schedule"]), \
             mock.patch.object(self.scheduler, "configure_app_logging"), \
             mock.patch.object(self.scheduler, "is_test_mode", return_value=False), \
             mock.patch.object(self.scheduler.AppConfig, "from_repo_root"), \
             mock.patch.object(self.scheduler, "ClipSchedulerRunner") as runner:
            tz = ZoneInfo("America/New_York")
            runner.return_value._local_now.return_value = datetime(2026, 10, 14, 22, 56, tzinfo=tz)
            runner.return_value.simulate_day.return_value = (SimpleNamespace(slots=[], youtube_slots=[], platforms_for=lambda i: []), None)
            self.assertEqual(self.scheduler.main(), 0)
            runner.return_value.simulate_day.assert_called_once_with(datetime(2026, 10, 15, tzinfo=tz))

    def test_schedule_date_requires_schedule(self):
        with mock.patch("sys.argv", ["main.py", "--schedule-date=2026-10-15"]), \
             mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.scheduler.parse_args()

    def test_direct_command_rejects_now_or_stop_after(self):
        for flag in ("--now", "--stop-after=editor"):
            with mock.patch("sys.argv", ["main.py", "--schedule", flag]), \
                 mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                self.scheduler.parse_args()

    def test_schedule_failure_is_not_success(self):
        with mock.patch("sys.argv", ["main.py", "--schedule"]), \
             mock.patch.object(self.scheduler, "configure_app_logging"), \
             mock.patch.object(self.scheduler, "is_test_mode", return_value=False), \
             mock.patch.object(self.scheduler.AppConfig, "from_repo_root"), \
             mock.patch.object(self.scheduler, "ClipSchedulerRunner") as runner, \
             mock.patch.object(self.scheduler, "send_failure_alert") as alert, \
             mock.patch.object(self.scheduler.logger, "exception"):
            runner.return_value.simulate_day.side_effect = OSError("cannot read archive")
            self.assertEqual(self.scheduler.main(), 1)
            alert.assert_called_once()

    def test_schedule_rejects_inherited_test_mode(self):
        with mock.patch("sys.argv", ["main.py", "--schedule"]), \
             mock.patch.object(self.scheduler, "configure_app_logging"), \
             mock.patch.object(self.scheduler, "is_test_mode", return_value=True), \
             mock.patch.object(self.scheduler, "ClipSchedulerRunner") as runner, \
             mock.patch.object(self.scheduler, "send_failure_alert"), \
             mock.patch.object(self.scheduler.logger, "exception"):
            self.assertEqual(self.scheduler.main(), 1)
            runner.assert_not_called()


class SimulateDayTests(unittest.TestCase):
    def test_simulation_generates_full_day_and_writes_nothing(self):
        sys.path[:0] = [str(APP_ROOT / "src"), str(APP_ROOT.parent / "Common" / "src"), str(APP_ROOT.parent / "ClipPublisher" / "src")]
        from clipscheduler.config import AppConfig
        from clipscheduler.runner import ClipSchedulerRunner

        tz = ZoneInfo("America/New_York")
        with TemporaryDirectory() as tmp:
            queue = Path(tmp)
            config = AppConfig(queue_dir=queue, poll_interval_seconds=60, schedule_start_hour=10, schedule_end_hour=22,
                               instagram_posts_per_day=6, min_gap_minutes=10,
                               timezone_name="America/New_York", youtube_selection_mode="instagram_top",
                               youtube_posts_per_day=1)
            runner = ClipSchedulerRunner(config, rng=random.Random(1))
            fake_replay = mock.Mock()
            fake_replay.return_value.simulate.return_value = {"entries": []}
            publisher_config = mock.Mock(queue_dir=queue)
            with mock.patch("clippublisher.youtube_replay.YouTubeReplay", fake_replay), \
                 mock.patch("clippublisher.config.AppConfig.from_repo_root", return_value=publisher_config), \
                 mock.patch.object(runner, "_send_plan_alert") as alert:
                plan, youtube = runner.simulate_day(datetime(2026, 10, 15, tzinfo=tz))
            self.assertGreaterEqual(len(plan.slots), 4)
            self.assertEqual(len(plan.youtube_slots), 1)
            self.assertTrue(10 <= plan.youtube_slots[0].hour < 12)
            fake_replay.return_value.simulate.assert_called_once()
            fake_replay.return_value.prepare.assert_not_called()
            alert.assert_not_called()
            self.assertEqual(list(queue.iterdir()), [])
            self.assertEqual(youtube, {"entries": []})