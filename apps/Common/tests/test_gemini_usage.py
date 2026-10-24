from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from common.gemini_usage import estimate_cost, gemini_usage, request_gemini
from common.reporting import _database, publish_usage_details, sourcing_run


class GeminiUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.queue = Path(self.directory.name) / "queue"
        self.run = self.queue / "clip"
        self.run.mkdir(parents=True)
        self.usage = {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 200}

    def response(self, usage=None, **extra):
        return Mock(status_code=200, json=lambda: {
            "usageMetadata": self.usage if usage is None else usage, **extra,
        })

    def call(self, response=None, run_dir=None):
        with patch("common.gemini_usage.requests.post", return_value=response or self.response()):
            return request_gemini(
                self.queue, model="gemini-2.5-flash", stage="test", run_dir=run_dir,
                url="https://example.invalid", json={"contents": []},
            )

    def test_text_output_and_thinking_counted_once(self):
        self.assertEqual(Decimal("0.00105"), estimate_cost("gemini-2.5-flash", self.usage))
        # totalTokenCount includes input and must not be charged again.
        self.assertEqual(Decimal("0.00105"), estimate_cost(
            "gemini-2.5-flash", {**self.usage, "totalTokenCount": 1300},
        ))

    def test_cad_conversion_is_saved_and_not_repriced(self):
        with sourcing_run() as run_id:
            self.call()
        usage = gemini_usage(self.queue, run_id=run_id)
        self.assertEqual(Decimal("0.001472205"), usage.cost_cad)
        with patch("common.gemini_usage.USD_TO_CAD", Decimal("9")):
            self.assertEqual(usage.cost_cad, gemini_usage(self.queue, run_id=run_id).cost_cad)
        text = usage.email_text("run")
        self.assertIn("$0.001472 CAD", text)
        self.assertEqual("1.4021", usage.rows[0]["fx_rate"])
        self.assertEqual("2026-10-22", usage.rows[0]["fx_date"])
        self.assertNotIn("1 USD", text)
        self.assertNotIn("CAD conversion", text)
        self.assertIn("thinking 200 tokens", text)

    def test_mixed_audio_and_cached_audio_are_priced_separately(self):
        usage = {
            **self.usage, "cachedContentTokenCount": 100,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 400}, {"modality": "AUDIO", "tokenCount": 600}],
            "cacheTokensDetails": [{"modality": "TEXT", "tokenCount": 40}, {"modality": "AUDIO", "tokenCount": 60}],
        }
        self.assertEqual(Decimal("0.0014052"), estimate_cost("gemini-2.5-flash", usage, has_audio=True))

    def test_flash_lite_and_pro_long_context_threshold(self):
        self.assertEqual(Decimal("0.00022"), estimate_cost("gemini-2.5-flash-lite", self.usage))
        for prompt, expected in ((200000, "0.4036"), (200001, "0.805404")):
            self.assertEqual(Decimal(expected), estimate_cost(
                "gemini-3.1-pro-preview", {**self.usage, "promptTokenCount": prompt},
            ))

    def test_incomplete_audio_usage_and_unknown_models_are_not_zero(self):
        for model, usage, audio in (
            ("unknown", self.usage, False),
            ("gemini-2.5-flash", self.usage, True),
            ("gemini-2.5-flash", {}, False),
            ("gemini-2.5-flash", {**self.usage, "promptTokenCount": -1}, False),
        ):
            with self.subTest(model=model, usage=usage), self.assertRaises(ValueError):
                estimate_cost(model, usage, has_audio=audio)

    def test_attempts_share_sourcing_run_then_survive_archival_and_retries(self):
        with sourcing_run() as run_id:
            self.call()
            self.call(self.response(candidates=[{"finishReason": "MAX_TOKENS"}]))
        (self.run / "reporting.json").write_text(json.dumps({"run_id": run_id}))
        self.call(run_dir=self.run)
        archived = self.queue / "Archive" / self.run.name
        archived.parent.mkdir()
        self.run.rename(archived)
        self.call(run_dir=archived)
        usage = gemini_usage(self.queue, run_id=run_id)
        self.assertEqual(4, len(usage.rows))
        self.assertEqual(Decimal("0.00420"), usage.cost_usd)
        self.assertEqual(0, usage.unknown_calls)
        self.assertIn("Gemini AI calls (run): 4", publish_usage_details(self.queue, archived))
        self.assertIn("Gemini AI calls (run): 4", publish_usage_details(self.queue, archived, replay=True))
        self.assertEqual("MAX_TOKENS", usage.rows[1]["finish_reason"])

    def test_timeout_is_persisted_unknown_without_credentials(self):
        with sourcing_run() as run_id, patch("common.gemini_usage.requests.post", side_effect=requests.Timeout("SECRET")):
            with self.assertRaises(requests.Timeout):
                request_gemini(self.queue, model="gemini-2.5-flash", stage="context", url="SECRET")
        usage = gemini_usage(self.queue, run_id=run_id)
        self.assertEqual(1, usage.unknown_calls)
        self.assertIn("unknown cost", usage.email_text("run"))
        self.assertNotIn("SECRET", json.dumps(usage.rows))

    def test_model_version_missing_usage_and_unknown_price_remain_visible(self):
        with sourcing_run() as run_id:
            self.call(self.response(modelVersion="unpriced-model"))
            self.call(Mock(status_code=503, json=lambda: {"error": {"message": "unavailable"}}))
        usage = gemini_usage(self.queue, run_id=run_id)
        self.assertEqual(2, usage.unknown_calls)
        self.assertEqual(Decimal(0), usage.cost_usd)
        text = usage.email_text("run")
        self.assertIn("unpriced-model", text)
        self.assertIn("unpriced model", text)
        self.assertIn("missing usage", text)

    def test_response_recording_failure_preserves_pending_unknown(self):
        from common import gemini_usage as module

        real_database = module._database
        with sourcing_run() as run_id, patch.object(module, "_database", side_effect=[
            real_database(self.queue), sqlite3.OperationalError("disk full"),
        ]), self.assertLogs(module.logger, level="ERROR"):
            self.call()
        self.assertEqual(1, gemini_usage(self.queue, run_id=run_id).unknown_calls)

    def test_daily_query_is_timezone_scoped_and_accounts_are_isolated(self):
        with sourcing_run() as run_id:
            self.call()
        with _database(self.queue) as db:
            db.execute("UPDATE gemini_attempts SET occurred_at=?", ("2026-10-23T02:00:00+00:00",))
        day = datetime(2026, 10, 22, tzinfo=timezone.utc).date()
        self.assertEqual(1, len(gemini_usage(self.queue, day=day, tz_name="America/New_York").rows))
        self.assertEqual(0, len(gemini_usage(self.queue, day=day, tz_name="UTC").rows))
        self.assertEqual(0, len(gemini_usage(self.queue.parent / "other", run_id=run_id).rows))


if __name__ == "__main__":
    unittest.main()
