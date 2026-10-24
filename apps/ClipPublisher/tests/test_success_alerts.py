from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from clippublisher import runner
from clippublisher.config import AppConfig
from clippublisher.models import QueueItem
from common.reporting import record_apify_usage, sourcing_run


class SuccessAlertTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = self.root / "queue"
        self.run = self.queue / "clip"
        (self.run / "source").mkdir(parents=True)
        (self.run / "editor").mkdir()
        self.manifest = self.run / "source" / "clip.json"
        self.source = {
            "clip_id": "123", "author_handle": "original",
            "url": "https://www.tiktok.com/@original/video/123",
            "created_at": "2026-10-17T18:00:00Z",
            "engagement": {"plays": 105100},
            "text": "Original caption <not HTML> & hashtags",
        }
        self.manifest.write_text(json.dumps(self.source), encoding="utf-8")
        self.item = QueueItem(self.run, self.manifest, self.run / "editor" / "clip.mp4", "123", "Generated caption", "original")
        self.config = mock.Mock(spec=AppConfig, queue_dir=self.queue, timezone_name="America/New_York")
        self.branding = self.run / "editor" / "branding.json"
        self.branding.write_text(json.dumps({"mode": "Manual", "placement": 6}))
        (self.run / "creator.json").write_text(json.dumps({
            "name": "n3on", "display_name": "N3on", "settings": {"STREAMER_HASHTAG": "n3on"},
        }))
        cache = self.root / "cache.json"
        cache.write_text('{"items": []}')
        with sourcing_run() as run_id:
            record_apify_usage(self.queue, 25)
        (self.run / "reporting.json").write_text(json.dumps({"run_id": run_id, "cache_file": str(cache)}))
        (self.queue / "scheduler_state.json").write_text(json.dumps({
            "slots": ["2026-10-17T23:55:00+00:00"], "slot_index": 2, "total_slots": 5,
        }))

    def details(self, **kwargs):
        with mock.patch.object(runner, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 17, 23, tzinfo=timezone.utc)
            return runner.posted_alert_details(self.config, self.item, **kwargs)

    def test_success_body_matches_requested_layout_and_html_has_clickable_link(self):
        details = self.details()
        with mock.patch.object(runner, "send_email_alert") as send:
            runner.send_posted_alert(
                clip_id="123", author_handle="original", publish_result={"id": "ig123"},
                details=details, subject=runner.alert_subject(self.config, "123", "Success"),
            )
        call = send.call_args.kwargs
        self.assertEqual("Success [3/5] - 123", call["subject"])
        self.assertEqual(
            "Instagram publish id: ig123\n"
            "Next scheduled run: 2026-10-17 19:55:00\n\n"
            "Source\n"
            "Creator: N3on\n"
            "Clip ID: 123\n"
            "Author: @original\n"
            "Link: https://www.tiktok.com/@original/video/123\n"
            "Views: 105,100\n"
            "Age: 5.0 hours\n"
            "Caption: Original caption <not HTML> & hashtags\n\n"
            "Apify\n"
            "Apify requests: 25\n"
            "Apify cost: $0.1065 CAD\n"
            "Apify cache count: 0\n\n"
            "Gemini AI\n"
            "Gemini AI cost (run): unknown (no recorded Gemini usage; tracking is not retroactive)\n\n"
            "Editor\n"
            "Branding: Manual [6]\n",
            call["body"],
        )
        self.assertIn('<a href="https://www.tiktok.com/@original/video/123"', call["html_body"])
        self.assertIn("Original caption &lt;not HTML&gt; &amp; hashtags", call["html_body"])
        self.assertNotIn("Generated caption", call["body"])

    def test_original_and_unnumbered_manual_branding(self):
        self.branding.write_text('{"mode": "Original"}')
        self.assertIn("Branding: Original\n", self.details())
        self.branding.write_text('{"mode": "Manual", "placement": null}')
        self.assertIn("Branding: Manual [unavailable: unnumbered fallback]", self.details())

    def test_legacy_metadata_is_unknown_not_invented(self):
        self.manifest.write_text('{"clip_id": "123"}')
        self.branding.unlink()
        (self.run / "reporting.json").unlink()
        with self.assertLogs(runner.logger, level="WARNING"):
            details = self.details()
        for text in ["Views: unknown", "Age: unknown", "Link: unknown", "Branding: unknown", "Caption: unavailable",
                     "Apify requests: unknown", "Apify cost: unknown"]:
            self.assertIn(text, details)
        self.assertNotIn("Generated caption", details)

    def test_invalid_or_future_timestamp_is_reported_unknown(self):
        for value in ["not-a-date", "2026-10-17T18:00:00", "2026-10-18T18:00:00Z"]:
            with self.subTest(value=value):
                self.source["created_at"] = value
                self.manifest.write_text(json.dumps(self.source))
                with self.assertLogs(runner.logger, level="WARNING"):
                    self.assertIn("Age: unknown", self.details())

    def test_html_escapes_caption_and_rejects_unsafe_links(self):
        for link in ['javascript:alert(1)', 'https://[invalid']:
            html = runner.posted_alert_html(f"Source\nLink: {link}\nCaption: <script>alert(1)</script>")
            self.assertNotIn("<a ", html)
            self.assertNotIn("<script>", html)
        html = runner.posted_alert_html('Link: https://www.tiktok.com/video/123?a=1&b="quoted"')
        self.assertIn('a=1&amp;b=&quot;quoted&quot;', html)

    def test_youtube_replay_uses_zero_apify_and_same_sections(self):
        (self.queue / "youtube_plan.json").write_text(json.dumps({"entries": [
            {"scheduled_for": "2026-10-17T23:10:00+00:00", "status": "published"},
            {"scheduled_for": "2026-10-17T23:30:00+00:00", "status": "pending"},
        ]}))
        with mock.patch.dict("os.environ", {"CANDIDATE_CACHE_FILE": str(self.root / "cache.json")}):
            details = self.details(replay=True)
        self.assertIn("Next scheduled run: 2026-10-17 19:30:00", details)
        self.assertIn("Apify requests: 0\nApify cost: $0.0000 CAD", details)
        with mock.patch.object(runner, "send_email_alert") as send:
            runner.send_posted_alert(
                clip_id="123", author_handle="original", platform="youtube",
                publish_result={"id": "yt123", "url": "https://youtube.com/shorts/yt123"},
                details=details,
            )
        self.assertTrue(send.call_args.kwargs["body"].startswith("YouTube video: https://youtube.com/shorts/yt123\n"))

    def test_no_remaining_schedule(self):
        (self.queue / "scheduler_state.json").write_text('{"slots": []}')
        self.assertIn("Next scheduled run: none left today", self.details())


if __name__ == "__main__":
    unittest.main()
