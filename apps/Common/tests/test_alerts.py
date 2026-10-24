import os
import unittest
from unittest import mock

from common.alerts import format_alert_subject, send_email_alert


class AlertFormattingTests(unittest.TestCase):
    def test_html_alert_keeps_plain_text_alternative(self):
        settings = {
            "ALERT_EMAIL_ENABLED": "true", "ALERT_EMAIL_SMTP_HOST": "smtp.example.com",
            "ALERT_EMAIL_FROM": "sender@example.com", "ALERT_EMAIL_TO": "recipient@example.com",
            "ACCOUNT_NAME": "TestAccount", "ENVIRONMENT": "LOCAL",
        }
        with mock.patch.dict(os.environ, settings), mock.patch("common.alerts.smtplib.SMTP") as smtp:
            self.assertTrue(send_email_alert(
                app_name="ClipPublisher", subject="Success - 123", body="Plain text",
                html_body="<p>Readable HTML</p>", platform="Instagram",
            ))
        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual("Clip Success IG TestAccount LOCAL", message["Subject"])
        self.assertEqual("multipart/alternative", message.get_content_type())
        self.assertEqual("Plain text\n", message.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual("<p>Readable HTML</p>\n", message.get_body(preferencelist=("html",)).get_content())

    def test_prod_instagram_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "Clip Success IG CarootClips PROD",
                format_alert_subject(subject="Success [1/5] - 7695703331800386838", platform="Instagram"),
            )

    def test_prod_youtube_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "Clip Success YT CarootClips PROD",
                format_alert_subject(subject="Success [1/1] - 7695703331800386838", platform="YouTube"),
            )

    def test_local_youtube_success_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "VrlClipMoments", "ENVIRONMENT": "LOCAL"}):
            self.assertEqual(
                "Clip Success YT VrlClipMoments LOCAL",
                format_alert_subject(subject="Success - 7695624055570943246", platform="YouTube"),
            )

    def test_prod_instagram_failure_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "Clip Fail IG CarootClips PROD",
                format_alert_subject(subject="Fail [2/5] - 7695703331800386838", platform="Instagram"),
            )

    def test_prod_schedule_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] Schedule - 20261012",
                format_alert_subject(subject="Schedule - 20261012", platform="Instagram"),
            )

    def test_prod_started_alert(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "[PROD] Started - 20261012",
                format_alert_subject(subject="Started - 20261012", platform="YouTube"),
            )

    def test_pipeline_failure_without_platform(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "Clip Fail IG/YT CarootClips PROD",
                format_alert_subject(subject="Fail - ClipAI", platform=""),
            )

    def test_strips_legacy_prefixes_cleanly(self):
        with mock.patch.dict(os.environ, {"ACCOUNT_NAME": "CarootClips", "ENVIRONMENT": "PROD"}):
            self.assertEqual(
                "Clip Fail IG CarootClips PROD",
                format_alert_subject(subject="Clipper CarootClips Fail - 123", platform="Instagram"),
            )


if __name__ == "__main__":
    unittest.main()
