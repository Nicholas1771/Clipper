import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clippublisher import runner as runner_module
from clippublisher import youtube_client
from clippublisher.config import AppConfig
from clippublisher.models import QueueItem

from test_single_attempt import make_item


def make_runner(tmp: str, platforms: list[str]):
    queue = Path(tmp) / "Queue"
    run = make_item(queue, "0001_x")
    (queue / "scheduler_state.json").write_text(json.dumps({"platforms": platforms}), encoding="utf-8")
    (run / "editor" / "youtube_title.txt").write_text("YT title", encoding="utf-8")
    config = mock.MagicMock(spec=AppConfig)
    config.queue_dir = queue
    config.timezone_name = "UTC"
    config.ready_stability_seconds = 0
    config.poll_interval_seconds = 0
    runner = runner_module.QueuePublisherRunner(config)
    seen = Path(tmp) / "state.json"
    return queue, runner, seen


class MultiPlatformTests(unittest.TestCase):
    def run_once(self, tmp, platforms, *, ig=None, yt=None, yt_configured=True):
        queue, runner, seen = make_runner(tmp, platforms)
        ig = ig or mock.Mock(return_value={"id": "ig1"})
        yt = yt or mock.Mock(return_value={"id": "yt1", "url": "https://youtube.com/shorts/yt1"})
        with mock.patch.object(runner_module, "publish_to_instagram", ig), \
             mock.patch.object(runner_module, "publish_to_youtube", yt), \
             mock.patch.object(runner_module, "youtube_is_configured", lambda c: yt_configured), \
             mock.patch.object(runner_module, "resolve_publish_access_token", lambda c: "t"), \
             mock.patch.object(runner_module, "send_failure_alert") as fail_alert, \
             mock.patch.object(runner_module, "send_email_alert") as email, \
             mock.patch.object(runner, "_sourcer_seen_ids_path", lambda: seen):
            try:
                runner.process_one_ready_item()
                error = None
            except RuntimeError as exc:
                error = exc
        return queue, seen, ig, yt, fail_alert, email, error

    def test_youtube_only_slot_skips_instagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, seen, ig, yt, _, email, error = self.run_once(tmp, ["youtube"])
            self.assertIsNone(error)
            ig.assert_not_called()
            yt.assert_called_once()
            self.assertTrue((queue / "Archive" / "0001_x").exists())
            self.assertIn("123", json.loads(seen.read_text())["seen_ids"])
            self.assertIn("youtube.com/shorts/yt1", email.call_args.kwargs["body"])

    def test_both_platforms_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, ig, yt, fail_alert, _, error = self.run_once(tmp, ["instagram", "youtube"])
            self.assertIsNone(error)
            ig.assert_called_once()
            yt.assert_called_once()
            fail_alert.assert_not_called()

    def test_youtube_failure_does_not_block_instagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            yt = mock.Mock(side_effect=RuntimeError("quota"))
            queue, seen, ig, _, fail_alert, email, error = self.run_once(tmp, ["instagram", "youtube"], yt=yt)
            self.assertIsNone(error)
            ig.assert_called_once()
            fail_alert.assert_called_once()
            self.assertIn("Fail YouTube", fail_alert.call_args.kwargs["subject"])
            self.assertTrue((queue / "Archive" / "0001_x").exists())
            self.assertIn("123", json.loads(seen.read_text())["seen_ids"])
            self.assertIn("YouTube FAILED", email.call_args.kwargs["body"])

    def test_instagram_failure_does_not_block_youtube(self):
        with tempfile.TemporaryDirectory() as tmp:
            ig = mock.Mock(side_effect=RuntimeError("ig down"))
            queue, _, _, yt, _, _, error = self.run_once(tmp, ["instagram", "youtube"], ig=ig)
            self.assertIsNone(error)
            yt.assert_called_once()
            self.assertTrue((queue / "Archive" / "0001_x").exists())

    def test_all_platforms_failing_quarantines_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ig = mock.Mock(side_effect=RuntimeError("ig down"))
            yt = mock.Mock(side_effect=RuntimeError("yt down"))
            queue, _, _, _, fail_alert, _, error = self.run_once(tmp, ["instagram", "youtube"], ig=ig, yt=yt)
            self.assertIsNotNone(error)
            self.assertEqual(2, fail_alert.call_count)
            self.assertTrue((queue / "Archive" / "0001_x_failed").exists())

    def test_unconfigured_youtube_is_skipped_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, seen, _, yt, fail_alert, email, error = self.run_once(tmp, ["youtube"], yt_configured=False)
            self.assertIsNone(error)
            yt.assert_not_called()
            fail_alert.assert_not_called()
            email.assert_not_called()
            self.assertFalse(seen.exists())
            self.assertTrue((queue / "Archive" / "0001_x").exists())


class YouTubeMetadataTests(unittest.TestCase):
    def item(self, **overrides):
        values = dict(run_dir=Path("r"), manifest_path=Path("m"), video_path=Path("v"), clip_id="1", caption="cap", author_handle="a")
        values.update(overrides)
        return QueueItem(**values)

    def config(self, **overrides):
        config = mock.MagicMock(spec=AppConfig)
        config.youtube_privacy_status = "public"
        config.youtube_category_id = "24"
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    def test_title_is_limited_and_shorts_tag_added(self):
        meta = youtube_client.build_video_metadata(
            self.config(), self.item(youtube_title="x" * 150, youtube_description="desc")
        )
        self.assertEqual(100, len(meta["snippet"]["title"]))
        self.assertTrue(meta["snippet"]["description"].endswith("#Shorts"))
        self.assertEqual("public", meta["status"]["privacyStatus"])
        self.assertFalse(meta["status"]["selfDeclaredMadeForKids"])

    def test_hashtags_follow_description_after_a_space(self):
        config = self.config(streamer_hashtag="kai")
        item = self.item(youtube_description="Great play.\n\n#clutch #KAI #fun")
        self.assertEqual("Great play. #kai #clutch #fun #Shorts", youtube_client.build_description(config, item))

    def test_streamer_tag_added_when_description_has_no_tags(self):
        config = self.config(streamer_hashtag="kai")
        self.assertEqual("Nice. #kai #Shorts", youtube_client.build_description(config, self.item(youtube_description="Nice.")))

    def test_falls_back_to_instagram_description_and_validates_privacy(self):
        meta = youtube_client.build_video_metadata(
            self.config(youtube_privacy_status="bogus"), self.item(description="ig text <b>")
        )
        self.assertIn("ig text b", meta["snippet"]["description"])
        self.assertEqual("public", meta["status"]["privacyStatus"])

    def test_configured_requires_credentials_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "t.json"
            config = self.config(youtube_client_id="i", youtube_client_secret="s", youtube_token_file=token)
            self.assertFalse(youtube_client.is_configured(config))
            token.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
            self.assertTrue(youtube_client.is_configured(config))


if __name__ == "__main__":
    unittest.main()
