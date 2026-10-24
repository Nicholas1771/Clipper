from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from common.cost_reports import initialize_monthly_tracking, monthly_cost_lines, usage_between, usage_lines
from common.creators import active_creator_name
from common.gemini_usage import request_gemini
from common.reporting import _database, record_apify_usage
from common.schedule_alerts import LIGHTSAIL_DAILY_CAD, ScheduleSlot, build_started_alert_body, build_schedule_alert_body
from common.alerts import format_alert_subject


class CostReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()

    def at(self, month, day, hour=12):
        return datetime(2026, month, day, hour, tzinfo=timezone.utc)

    def test_monthly_excludes_past_restarts_without_double_count_and_resets(self):
        record_apify_usage(self.queue, 1000, now=self.at(10, 22))
        started = initialize_monthly_tracking(self.root, self.at(10, 23))
        self.assertEqual(started, initialize_monthly_tracking(self.root, self.at(10, 30)))
        record_apify_usage(self.queue, 100, now=self.at(10, 24))
        response = Mock(json=lambda: {"usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100}})
        with patch("common.gemini_usage.requests.post", return_value=response):
            request_gemini(self.queue, model="gemini-2.5-flash", stage="summary", url="https://example.invalid")
        with _database(self.queue) as db:
            db.execute("UPDATE gemini_attempts SET occurred_at=?", (self.at(10, 24).isoformat(),))
        lines = monthly_cost_lines(self.root, [self.queue], self.at(10, 24), "UTC", LIGHTSAIL_DAILY_CAD)
        self.assertIn("Apify cost: $0.43 CAD", lines)
        self.assertIn("Hosting cost: $0.56 CAD", lines)
        self.assertEqual(lines, monthly_cost_lines(self.root, [self.queue], self.at(10, 24), "UTC", LIGHTSAIL_DAILY_CAD))
        record_apify_usage(self.queue, 200, now=self.at(11, 1))
        november = monthly_cost_lines(self.root, [self.queue], self.at(11, 1), "UTC", LIGHTSAIL_DAILY_CAD)
        self.assertIn("Apify cost: $0.85 CAD", november)
        self.assertIn("Hosting cost: $0.28 CAD", november)
        self.assertIn("AI cost: $0.00 CAD", november)

    def test_model_stages_collapsed_and_creator_saved_for_rejected_and_successful_calls(self):
        token = active_creator_name.set("Cuffem")
        self.addCleanup(active_creator_name.reset, token)
        record_apify_usage(self.queue, 25)
        response = Mock(json=lambda: {"usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100}})
        with patch("common.gemini_usage.requests.post", return_value=response):
            for stage in ("summary", "transcription_context"):
                request_gemini(self.queue, model="gemini-2.5-flash", stage=stage, url="https://example.invalid")
        with _database(self.queue) as db:
            db.execute("UPDATE gemini_attempts SET occurred_at=?", (self.at(10, 23).isoformat(),))
            db.execute("UPDATE usage SET occurred_at=?", (self.at(10, 23).isoformat(),))
        apify, ai = usage_between(self.queue, self.at(10, 1), self.at(11, 1))
        self.assertEqual("Cuffem", apify[0]["creator"])
        self.assertEqual({"Cuffem"}, {r["creator"] for r in ai.rows})
        text = "\n".join(usage_lines(apify, ai))
        self.assertEqual(1, text.count("gemini-2.5-flash:"))
        self.assertIn("AI calls: 2", text)
        self.assertNotIn("tokens", text)
        self.assertNotIn("summary", text)

    def test_plan_start_exact_subjects_fixed_est_and_five_status_rows(self):
        when = self.at(10, 23)
        slot = ScheduleSlot(when, "kickzsclipz", "Instagram", creator="Cuffem")
        statuses = {"CarootClips": {"Instagram": "OK", "YouTube": "OK"},
                    "VrlClipMoments": {"Instagram": "OK", "YouTube": "OK"},
                    "kickzsclipz": {"Instagram": "OK", "YouTube": "Disabled (0 posts/day)"}}
        started = build_started_alert_body(statuses, [slot], when.date(), "America/New_York", 8, 23)
        plan = build_schedule_alert_body([], when.date(), "America/New_York", 8, 23, slot)
        self.assertEqual(5, started.count(": OK"))
        for text in (started, plan):
            self.assertNotIn("Timezone", text)
            self.assertNotIn("EDT", text)
            self.assertNotIn("EST", text)
            self.assertNotIn("(--now)", text)
            self.assertNotIn("Disabled", text)
        self.assertIn("Run Now: 20261023 08:00:00", plan)
        for kind in ("Plan", "Start", "Report"):
            subject = f"Clip {kind} 20261023 PROD"
            self.assertEqual(subject, format_alert_subject(subject=subject))
            self.assertEqual(subject + " TEST", format_alert_subject(subject=subject + " TEST"))


if __name__ == "__main__":
    unittest.main()
