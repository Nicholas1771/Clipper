from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from common.reporting import (
    ApifyUsage, apify_usage, cache_remaining_text, posts_for_day,
    publish_usage_details, record_apify_usage, record_post, record_stage_failure, sourcing_run,
)
from common.schedule_alerts import build_daily_report_body, send_consolidated_daily_report


class ReportingTests(unittest.TestCase):
    def test_creator_survives_status_updates_and_appears_for_unscheduled_daily_rows(self):
        when = "2026-10-15T18:00:00+00:00"
        record_post(self.queue, when, ["instagram"], "in progress", creator="Nevaeh")
        record_post(self.queue, when, ["instagram"], "published", "clip123")
        rows = posts_for_day(self.queue, date(2026, 10, 15), "UTC")
        self.assertEqual(rows[0]["creator"], "Nevaeh")
        config = SimpleNamespace(queue_dir=self.queue, timezone_name="UTC")
        with mock.patch("common.schedule_alerts.discover_profile_configs", return_value=[]):
            body = build_daily_report_body(self.root, datetime(2026, 10, 15, 23, tzinfo=timezone.utc), config)
        self.assertIn("Nevaeh | Instagram | published", body)

    def test_existing_status_journal_migrates_without_losing_rows(self):
        path = self.queue.parent / f"{self.queue.name}_reporting.sqlite3"
        when = "2026-10-15T18:00:00+00:00"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE posts (scheduled_for TEXT, platform TEXT, status TEXT, detail TEXT, PRIMARY KEY (scheduled_for, platform))")
            db.execute("INSERT INTO posts VALUES (?, 'instagram', 'published', 'original')", (when,))
        rows = posts_for_day(self.queue, date(2026, 10, 15), "UTC")
        self.assertEqual(rows[0]["status"], "published")
        self.assertEqual(rows[0]["detail"], "original")
        self.assertEqual(rows[0]["creator"], "")

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()

    def test_usage_is_per_scraped_clip_and_per_run_including_fallbacks(self):
        with sourcing_run() as first:
            record_apify_usage(self.queue, 999)
            record_apify_usage(self.queue, 1)
        with sourcing_run() as second:
            record_apify_usage(self.queue, 0)
        usage = apify_usage(self.queue, run_id=first)
        self.assertEqual(1000, usage.requests)
        self.assertEqual(Decimal("4.26"), usage.cost_cad)
        self.assertEqual(ApifyUsage(), apify_usage(self.queue, run_id=second))
        self.assertIn("$4.2600 CAD", usage.email_text("run"))

    def test_local_day_boundaries_and_profile_isolation(self):
        day = date(2026, 10, 15)
        for at, count in [
            ("2026-10-15T03:59:59+00:00", 100),
            ("2026-10-15T04:00:00+00:00", 10),
            ("2026-10-16T03:59:59+00:00", 20),
            ("2026-10-16T04:00:00+00:00", 200),
        ]:
            record_apify_usage(self.queue, count, now=datetime.fromisoformat(at))
        record_apify_usage(self.root / "other_queue", 300)
        self.assertEqual(30, apify_usage(self.queue, day=day, tz_name="America/New_York").requests)

    def test_unknown_actor_usage_is_not_silently_counted_as_free(self):
        with sourcing_run() as run_id:
            record_apify_usage(self.queue, None)
        usage = apify_usage(self.queue, run_id=run_id)
        self.assertEqual(1, usage.unknown_calls)
        self.assertIn("usage/cost is unknown", usage.email_text("run"))

    def test_outcomes_persist_and_plan_resave_cannot_erase_them(self):
        when = "2026-10-15T18:00:00+00:00"
        record_post(self.queue, when, ["instagram", "youtube"], "planned")
        record_post(self.queue, when, ["instagram"], "published", "clip123")
        record_post(self.queue, when, ["youtube"], "failed", "quota")
        record_post(self.queue, when, ["instagram", "youtube"], "planned")
        rows = posts_for_day(self.queue, date(2026, 10, 15), "UTC")
        self.assertEqual(["published", "failed"], [row["status"] for row in rows])
        self.assertEqual("quota", rows[1]["detail"])
        self.assertEqual([], posts_for_day(self.queue, date(2026, 10, 16), "UTC"))
        record_post(self.queue, when, ["instagram", "youtube"], "abandoned")
        self.assertEqual(rows, posts_for_day(self.queue, date(2026, 10, 15), "UTC"))

    def test_stage_crashes_are_recorded_but_unrelated_idle_errors_are_not(self):
        when = "2026-10-15T18:00:00+00:00"
        marker = self.queue / "scheduler_state.json"
        for stage, phase in [("ClipAI", "source_consumed"), ("ClipEditor", "ai_processed")]:
            marker.write_text(json.dumps({"scheduled_for": when, "platforms": ["instagram"], "phase": phase}))
            record_stage_failure(self.queue, stage, RuntimeError("service down"))
            row = posts_for_day(self.queue, date(2026, 10, 15), "UTC")[0]
            self.assertEqual("failed", row["status"])
            self.assertEqual(f"{stage}: service down", row["detail"])
        marker.write_text(json.dumps({"scheduled_for": when, "phase": "complete"}))
        record_stage_failure(self.queue, "ClipAI", RuntimeError("unrelated"))
        self.assertEqual(row, posts_for_day(self.queue, date(2026, 10, 15), "UTC")[0])

    def test_status_journal_failure_is_logged_without_reclassifying_publish(self):
        with mock.patch("common.reporting.sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")), \
             self.assertLogs("common.reporting", level="ERROR"):
            self.assertFalse(record_post(self.queue, "2026-10-15T18:00:00+00:00", ["instagram"], "published"))

    def test_publish_details_use_stable_run_id_and_live_cache(self):
        run = self.queue / "processing" / "clip"
        run.mkdir(parents=True)
        cache = self.root / "cache.json"
        cache.write_text(json.dumps({"items": [{"id": "a"}, {"id": "b"}]}))
        with sourcing_run() as run_id:
            record_apify_usage(self.queue, 25)
        (run / "reporting.json").write_text(json.dumps({"run_id": run_id, "cache_file": str(cache)}))
        body = publish_usage_details(self.queue, run)
        self.assertIn("scraped clips): 25", body)
        self.assertIn("$0.1065 CAD", body)
        self.assertIn("cache: 2", body)
        compact = publish_usage_details(self.queue, run, compact=True)
        self.assertEqual("Apify requests: 25\nApify cost: $0.1065 CAD\nApify cache count: 2\n", compact)
        self.assertIn("predates usage tracking", publish_usage_details(self.queue))
        with mock.patch.dict(os.environ, {"CANDIDATE_CACHE_FILE": str(cache)}):
            replay = publish_usage_details(self.queue, run, replay=True)
        self.assertIn("scraped clips): 0", replay)
        self.assertIn("$0.0000 CAD", replay)
        self.assertIn("cache: 2", replay)

    def test_missing_and_unreadable_cache(self):
        cache = self.root / "cache.json"
        self.assertIn("cache: 0", cache_remaining_text(cache))
        cache.write_text("bad JSON")
        with self.assertLogs("common.reporting", level="WARNING"):
            self.assertIn("cache: unknown", cache_remaining_text(cache))

    def test_daily_report_includes_platform_outcomes_replays_and_total_cost(self):
        other = self.root / "other" / "queue"
        other.mkdir(parents=True)
        now = datetime(2026, 10, 15, 23, tzinfo=ZoneInfo("America/New_York"))
        when = "2026-10-15T18:00:00-04:00"
        record_post(self.queue, when, ["instagram"], "published", "a")
        record_post(self.queue, when, ["youtube"], "failed", "quota")
        record_post(other, when, ["instagram"], "skipped", "Overdue slot coalesced")
        record_apify_usage(self.queue, 1000, now=now)
        record_apify_usage(other, 500, now=now)
        record_apify_usage(other, None, now=now)
        (self.queue / "youtube_plan.json").write_text(json.dumps({
            "date": "2026-10-15", "entries": [{
                "scheduled_for": "2026-10-15T11:00:00-04:00", "status": "published", "clip_id": "replay",
                "creator_name": "Nevaeh",
            }],
        }))
        profiles = [
            SimpleNamespace(queue_dir=self.queue, account_name="One", timezone_name="America/New_York"),
            SimpleNamespace(queue_dir=other, account_name="Two", timezone_name="America/New_York"),
        ]
        with mock.patch("common.schedule_alerts.discover_profile_configs", return_value=profiles):
            body = build_daily_report_body(self.root, now, profiles[0])
        self.assertIn("One | Instagram | published - a", body)
        self.assertIn("One | YouTube | failed - quota", body)
        self.assertIn("One | Nevaeh | YouTube | published - replay", body)
        self.assertIn("Two | Instagram | skipped", body)
        self.assertIn("scraped clips): 1,500", body)
        self.assertIn("$6.3900 CAD", body)
        self.assertIn("usage/cost is unknown", body)

    def test_daily_report_deduplicates_across_restarts_and_retries_failed_delivery(self):
        config = SimpleNamespace(queue_dir=self.queue, timezone_name="UTC")
        now = datetime(2026, 10, 15, 23, tzinfo=timezone.utc)
        sender = mock.Mock(side_effect=[False, True])
        with mock.patch.dict(os.environ, {"CLIPPER_TEST_MODE": "false"}), \
             mock.patch("common.schedule_alerts.discover_profile_configs", return_value=[]):
            self.assertFalse(send_consolidated_daily_report(self.root, now, config, sender))
            self.assertTrue(send_consolidated_daily_report(self.root, now, config, sender))
            self.assertTrue(send_consolidated_daily_report(self.root, now, config, sender))
        self.assertEqual(2, sender.call_count)
        self.assertFalse(sender.call_args.kwargs["include_account"])
        self.assertFalse(sender.call_args.kwargs["include_platform"])
