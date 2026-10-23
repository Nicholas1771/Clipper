from datetime import date
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clippublisher import runner as runner_module
from clippublisher import youtube_client
from clippublisher.config import AppConfig
from clippublisher.models import QueueItem
from common.reporting import posts_for_day, record_apify_usage, sourcing_run

from test_single_attempt import make_item


def make_runner(tmp: str, platforms: list[str]):
    queue = Path(tmp) / "Queue"
    make_item(queue, "0001_x")
    (queue / "scheduler_state.json").write_text(json.dumps({"platforms": platforms}), encoding="utf-8")
    config = mock.MagicMock(spec=AppConfig)
    config.queue_dir = queue
    config.timezone_name = "UTC"
    config.ready_stability_seconds = 0
    config.poll_interval_seconds = 0
    runner = runner_module.QueuePublisherRunner(config)
    seen = Path(tmp) / "state.json"
    return queue, runner, seen


class MultiPlatformTests(unittest.TestCase):
    def test_success_and_failure_emails_include_run_usage_and_cache_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, runner, seen = make_runner(tmp, ["instagram", "youtube"])
            cache = Path(tmp) / "cache.json"
            cache.write_text(json.dumps({"items": [{"id": "cached"}]}))
            when = "2026-10-15T18:00:00+00:00"
            with sourcing_run() as run_id:
                record_apify_usage(queue, 1000)
            (queue / "0001_x" / "reporting.json").write_text(json.dumps({
                "scheduled_for": when, "run_id": run_id, "cache_file": str(cache),
            }))
            with mock.patch.object(runner_module, "publish_to_instagram", return_value={"id": "ig"}), \
                 mock.patch.object(runner_module, "publish_to_youtube", side_effect=RuntimeError("quota")), \
                 mock.patch.object(runner_module, "youtube_is_configured", return_value=True), \
                 mock.patch.object(runner_module, "resolve_publish_access_token", return_value="token"), \
                 mock.patch.object(runner_module, "send_failure_alert") as failure, \
                 mock.patch.object(runner_module, "send_email_alert") as email, \
                 mock.patch.object(runner, "_sourcer_seen_ids_path", return_value=seen):
                runner.process_one_ready_item()
            failure_text = failure.call_args.kwargs["details"]
            success_text = email.call_args.kwargs["body"]
            self.assertIn("scraped clips): 1,000", failure_text)
            self.assertIn("Apify requests: 1,000", success_text)
            self.assertIn("Apify cache count: 1", success_text)
            for text in [failure_text, success_text]:
                self.assertIn("$4.2600 CAD", text)
            self.assertIn("cache: 1", failure_text)
            rows = posts_for_day(queue, date(2026, 10, 15), "UTC")
            self.assertEqual(["published", "failed"], [row["status"] for row in rows])

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

    def test_instagram_only_slot_skips_youtube(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, ig, yt, _, _, error = self.run_once(tmp, ["instagram"])
            self.assertIsNone(error)
            ig.assert_called_once()
            yt.assert_not_called()

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
            self.assertEqual("YouTube", fail_alert.call_args.kwargs["platform"])
            self.assertTrue((queue / "Archive" / "0001_x").exists())
            self.assertIn("123", json.loads(seen.read_text())["seen_ids"])
            self.assertEqual("Instagram", email.call_args.kwargs["platform"])

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
            queue, seen, _, _, fail_alert, _, error = self.run_once(tmp, ["instagram", "youtube"], ig=ig, yt=yt)
            self.assertIsNotNone(error)
            self.assertEqual(2, fail_alert.call_count)
            self.assertTrue((queue / "Archive" / "0001_x_failed").exists())
            self.assertFalse(seen.exists())

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
        config.streamer_hashtag = ""
        config.google_api_key = "test-key"
        config.gemini_model = "gemini-2.5-flash-lite"
        config.timeout_seconds = 180
        config.prompts_dir = Path(__file__).resolve().parents[3] / "data" / "prompts"
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    def test_title_is_ai_text_plus_hashtags_with_no_description(self):
        config = self.config(streamer_hashtag="kai")
        item = self.item(caption="source caption", description="Wild clutch play.\n\nSecond paragraph.\n\n#clutch #KAI #fun")
        self.assertEqual("Wild clutch play. #kai #clutch #fun", youtube_client.build_title(config, item))
        meta = youtube_client.build_video_metadata(config, item)
        self.assertEqual("", meta["snippet"]["description"])
        self.assertEqual("public", meta["status"]["privacyStatus"])
        self.assertFalse(meta["status"]["selfDeclaredMadeForKids"])

    def test_title_uses_the_youtube_ai_caption_not_the_instagram_summary(self):
        config = self.config(streamer_hashtag="kai")
        item = self.item(description="Long instagram summary paragraph.\n\n#clutch #fun", youtube_description="Wild CLUTCH play 🔥")
        self.assertEqual("Wild CLUTCH play 🔥 #kai #clutch #fun", youtube_client.build_title(config, item))
    def test_hashtags_that_do_not_fit_are_dropped(self):
        config = self.config(streamer_hashtag="kai")
        text = "x" * 80
        item = self.item(description=f"{text}\n\n#abcdef #ghijklmnopqrstuvwxyz #zz")
        title = youtube_client.build_title(config, item)
        self.assertLessEqual(len(title), 100)
        self.assertEqual(f"{text} #kai #abcdef #zz", title)

    def test_long_text_is_cut_on_a_word_boundary_and_leaves_no_tags(self):
        config = self.config(streamer_hashtag="kai")
        title = youtube_client.build_title(config, self.item(description=("word " * 40).strip() + "\n\n#a"))
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("word"))

    def test_falls_back_to_caption_when_no_ai_text(self):
        self.assertEqual("cap", youtube_client.build_title(self.config(streamer_hashtag=""), self.item()))

    def test_invalid_privacy_falls_back_to_public(self):
        meta = youtube_client.build_video_metadata(self.config(youtube_privacy_status="bogus"), self.item())
        self.assertEqual("public", meta["status"]["privacyStatus"])

    def ai_response(self, decision="allow", reason="Ordinary gameplay commentary."):
        return mock.Mock(status_code=200, json=mock.Mock(return_value={
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": json.dumps({"decision": decision, "reason": reason})}]},
            }],
        }))

    def test_ai_screen_receives_title_caption_summary_and_transcript(self):
        item = self.item(
            caption="Source caption", description="Clip summary", youtube_description="Final title",
            transcript="Spoken words with context.",
        )
        with mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response()) as post:
            youtube_client.screen_youtube_content(self.config(), item)
        payload = post.call_args.kwargs["json"]
        content = json.loads(payload["contents"][0]["parts"][0]["text"])
        self.assertEqual("Final title", content["title"])
        self.assertEqual(item.caption, content["caption"])
        self.assertEqual(item.description, content["summary"])
        self.assertEqual(item.transcript, content["transcript"])
        self.assertIn("untrusted", payload["systemInstruction"]["parts"][0]["text"])
        self.assertEqual("test-key", post.call_args.kwargs["headers"]["x-goog-api-key"])
        self.assertEqual((10, 180), post.call_args.kwargs["timeout"])

    def test_ai_can_allow_quoted_language_previously_blocked_by_keywords(self):
        with mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response()):
            youtube_client.screen_youtube_content(
                self.config(), self.item(transcript="Never send me your password. That is a scam."),
            )

    def test_ai_screen_uses_prompt_file_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = Path(tmp)
            (prompts / "youtube_policy_screening.txt").write_text("Custom screening instructions.", encoding="utf-8")
            with mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response()) as post:
                youtube_client.screen_youtube_content(self.config(prompts_dir=prompts), self.item())
            self.assertEqual(
                "Custom screening instructions.",
                post.call_args.kwargs["json"]["systemInstruction"]["parts"][0]["text"],
            )

    def test_missing_or_empty_prompt_blocks_screening_without_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = Path(tmp)
            with mock.patch.object(youtube_client.requests, "post") as post:
                for directory in (None, prompts):
                    with self.subTest(directory=directory), self.assertRaises(youtube_client.YouTubeScreeningError):
                        youtube_client.screen_youtube_content(self.config(prompts_dir=directory), self.item())
                (prompts / "youtube_policy_screening.txt").write_text(" \n", encoding="utf-8")
                with self.assertRaises(youtube_client.YouTubeScreeningError):
                    youtube_client.screen_youtube_content(self.config(prompts_dir=prompts), self.item())
                post.assert_not_called()

    def test_unchanged_approval_avoids_duplicate_ai_call_but_prompt_change_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = Path(tmp)
            path = prompts / "youtube_policy_screening.txt"
            path.write_text("Original instructions.", encoding="utf-8")
            config = self.config(prompts_dir=prompts)
            item = self.item()
            with mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response()) as post, \
                 mock.patch.object(youtube_client, "fetch_access_token", return_value="token"), \
                 mock.patch.object(youtube_client, "upload_video", return_value={"id": "yt1"}):
                approval = youtube_client.screen_youtube_content(config, item)
                youtube_client.publish_to_youtube(config, item, approval=approval)
                self.assertEqual(1, post.call_count)
                path.write_text("Updated instructions.", encoding="utf-8")
                youtube_client.publish_to_youtube(config, item, approval=approval)
                self.assertEqual(2, post.call_count)

    def test_ai_block_and_review_prevent_authentication_and_upload(self):
        for decision in ("block", "review"):
            with self.subTest(decision=decision), \
                 mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response(decision, "Targeted threat.")), \
                 mock.patch.object(youtube_client, "fetch_access_token") as fetch_token, \
                 mock.patch.object(youtube_client, "upload_video") as upload:
                with self.assertRaisesRegex(youtube_client.YouTubePolicyError, "Targeted threat"):
                    youtube_client.publish_to_youtube(self.config(), self.item(transcript="Threatening speech."))
                fetch_token.assert_not_called()
                upload.assert_not_called()

    def test_ai_allow_proceeds_to_upload(self):
        with mock.patch.object(youtube_client.requests, "post", return_value=self.ai_response()), \
             mock.patch.object(youtube_client, "fetch_access_token", return_value="token"), \
             mock.patch.object(youtube_client, "upload_video", return_value={"id": "yt1"}) as upload:
            result = youtube_client.publish_to_youtube(self.config(), self.item())
        upload.assert_called_once()
        self.assertEqual("yt1", result["id"])

    def test_ai_screen_fails_closed_for_missing_key_http_and_network_errors(self):
        with mock.patch.object(youtube_client.requests, "post") as post:
            with self.assertRaises(youtube_client.YouTubeScreeningError):
                youtube_client.screen_youtube_content(self.config(google_api_key=""), self.item())
            post.assert_not_called()
        for response in (mock.Mock(status_code=429), mock.Mock(status_code=503)):
            with mock.patch.object(youtube_client.requests, "post", return_value=response):
                with self.assertRaises(youtube_client.YouTubeScreeningError):
                    youtube_client.screen_youtube_content(self.config(), self.item())
        with mock.patch.object(youtube_client.requests, "post", side_effect=youtube_client.requests.Timeout):
            with self.assertRaises(youtube_client.YouTubeScreeningError):
                youtube_client.screen_youtube_content(self.config(), self.item())

    def test_ai_screen_rejects_invalid_truncated_or_refused_responses(self):
        responses = [
            {}, {"candidates": []}, {"candidates": [{"finishReason": "SAFETY"}]},
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": '{"decision":"allow"}'}]}}]},
        ]
        for result in ("not JSON", "null", "[]", '{"decision":true,"reason":"ok"}',
                       '{"decision":"allow","reason":""}', '{"decision":"allow"}'):
            responses.append({"candidates": [{
                "finishReason": "STOP", "content": {"parts": [{"text": result}]},
            }]})
        for payload in responses:
            with self.subTest(payload=payload), \
                 mock.patch.object(youtube_client.requests, "post", return_value=mock.Mock(status_code=200, json=lambda: payload)):
                with self.assertRaises(youtube_client.YouTubeScreeningError):
                    youtube_client.screen_youtube_content(self.config(), self.item())

    def test_configured_requires_credentials_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "t.json"
            config = self.config(youtube_client_id="i", youtube_client_secret="s", youtube_token_file=token)
            self.assertFalse(youtube_client.is_configured(config))
            token.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
            self.assertTrue(youtube_client.is_configured(config))


if __name__ == "__main__":
    unittest.main()
