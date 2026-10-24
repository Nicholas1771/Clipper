from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from clipscheduler.config import AppConfig
from common.reporting import posts_for_day
from test_runner import at, make_runner


class DailyReportTests(unittest.TestCase):
    def test_time_is_configurable_and_rejects_invalid_values(self):
        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue"
            self.assertEqual("23:00", make_runner(queue).config.daily_report_time)
            self.assertEqual("22:30", make_runner(queue, daily_report_time="22:30").config.daily_report_time)
            for value in ["24:00", "23:60", "11PM", "9:00", ""]:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "DAILY_REPORT_TIME"):
                    make_runner(queue, daily_report_time=value)
            with mock.patch("clipscheduler.config.load_app_env", return_value=Path(tmp)), \
                 mock.patch.dict("os.environ", {"DAILY_REPORT_TIME": "22:45", "QUEUE_DIR": str(queue)}):
                self.assertEqual("22:45", AppConfig.from_repo_root(Path(tmp)).daily_report_time)

    def test_report_runs_at_configured_time_even_with_no_slots_and_only_once(self):
        with TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"CLIPPER_TEST_MODE": "false"}):
            runner = make_runner(Path(tmp) / "queue", daily_report_time="22:30")
            with mock.patch.object(runner, "_send_plan_alert"), \
                 mock.patch("clipscheduler.runner.send_consolidated_daily_report", return_value=True) as send:
                runner.run_once(at(22, 29))
                send.assert_not_called()
                runner.run_once(at(22, 30))
                runner.run_once(at(23, 30))
                send.assert_called_once()
                runner.run_once(at(22, 30, day=7))
                self.assertEqual(2, send.call_count)

    def test_failed_delivery_retries_after_one_minute(self):
        with TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"CLIPPER_TEST_MODE": ""}):
            runner = make_runner(Path(tmp) / "queue")
            with mock.patch.object(runner, "_send_plan_alert"), \
                 mock.patch("clipscheduler.runner.send_consolidated_daily_report", side_effect=[False, True]) as send:
                runner.run_once(at(23))
                runner.run_once(at(23, second=30))
                self.assertEqual(1, send.call_count)
                runner.run_once(at(23, 1))
                self.assertEqual(2, send.call_count)

    def test_test_mode_suppresses_daily_reports(self):
        with TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"CLIPPER_TEST_MODE": "1"}):
            runner = make_runner(Path(tmp) / "queue")
            with mock.patch("clipscheduler.runner.send_consolidated_daily_report") as send:
                runner.run_once(at(23))
            send.assert_not_called()

    def test_overdue_skips_and_stale_cycles_are_recorded(self):
        with TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue"
            runner = make_runner(queue)
            with mock.patch.object(runner, "_send_plan_alert"):
                plan = runner.ensure_plan(at(6))
                runner.run_once(plan.slots[2])
                rows = posts_for_day(queue, at(6).date(), "America/New_York")
                self.assertEqual(["skipped", "skipped", "in progress"], [row["status"] for row in rows[:3]])
                runner.run_once(plan.slots[2] + timedelta(hours=3, seconds=1))
            rows = posts_for_day(queue, at(6).date(), "America/New_York")
            self.assertEqual("failed", rows[2]["status"])
