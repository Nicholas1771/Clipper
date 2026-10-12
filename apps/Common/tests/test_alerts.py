import os
import unittest
from unittest import mock

from common.alerts import format_alert_subject


class AlertFormattingTests(unittest.TestCase):
    def test_prod_instagram_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [IG] [CarootClips] Success [1/5] - 7695703331800386838",
                format_alert_subject(subject="Success [1/5] - 7695703331800386838", platform="Instagram"),
            )

    def test_prod_youtube_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [YT] [CarootClips] Success [1/1] - 7695703331800386838",
                format_alert_subject(subject="Success [1/1] - 7695703331800386838", platform="YouTube"),
            )

    def test_local_youtube_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "VrlClipMoments", "ENVIRONMENT": "LOCAL"}):
            self.assertEqual(
                "[LOCAL] [YT] [VrlClipMoments] Success - 7695624055570943246",
                format_alert_subject(subject="Success - 7695624055570943246", platform="YouTube"),
            )

    def test_prod_instagram_failure_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [IG] [CarootClips] Fail [2/5] - 7695703331800386838",
                format_alert_subject(subject="Fail [2/5] - 7695703331800386838", platform="Instagram"),
            )

    def test_prod_schedule_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [IG] [CarootClips] Schedule - 20261012",
                format_alert_subject(subject="Schedule - 20261012", platform="Instagram"),
            )

    def test_prod_started_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [YT] [CarootClips] Started - 20261012",
                format_alert_subject(subject="Started - 20261012", platform="YouTube"),
            )

    def test_pipeline_failure_without_platform(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [IG/YT] [CarootClips] Fail - ClipAI",
                format_alert_subject(subject="Fail - ClipAI", platform=""),
            )

    def test_strips_legacy_prefixes_cleanly(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] [IG] [CarootClips] Fail - 123",
                format_alert_subject(subject="Clipper CarootClips Fail - 123", platform="Instagram"),
            )


if __name__ == "__main__":
    unittest.main()
