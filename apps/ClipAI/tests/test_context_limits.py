import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clipai.config import AppConfig
from clipai.service import ClipAIService, GeminiTruncatedResponseError
from common.gemini_usage import gemini_usage
from common.reporting import sourcing_run


class ContextLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = ClipAIService(AppConfig(
            queue_dir=Path(self.directory.name) / "queue", poll_interval_seconds=1, google_api_key="test",
            gemini_model="gemini-2.5-flash-lite", timeout_seconds=180,
            timezone_name="UTC",
        ))

    def response(self, reason="STOP", text='{"context_sufficient":true}'):
        return Mock(status_code=200, json=lambda: {
            "candidates": [{"finishReason": reason, "content": {"parts": [{"text": text}]}}],
        })

    def test_generation_settings_are_opt_in_and_timeout_is_unchanged(self):
        with patch("clipai.service.requests.post", return_value=self.response()) as post:
            self.service._call_gemini("visual")
            settings = post.call_args.kwargs["json"]["generationConfig"]
            self.assertNotIn("thinkingConfig", settings)
            self.assertNotIn("maxOutputTokens", settings)
            self.service._call_gemini("audio", max_output_tokens=4096, thinking_budget=0)
            settings = post.call_args.kwargs["json"]["generationConfig"]
            self.assertEqual(4096, settings["maxOutputTokens"])
            self.assertEqual({"thinkingBudget": 0}, settings["thinkingConfig"])
            self.assertEqual((10, 180), post.call_args.kwargs["timeout"])

    def test_truncated_response_is_rejected_even_with_valid_json(self):
        for text in ('{"context_sufficient":true}', '{"transcript":"unfinished'):
            with patch("clipai.service.requests.post", return_value=self.response("MAX_TOKENS", text)):
                with self.assertRaises(GeminiTruncatedResponseError):
                    self.service._call_gemini("audio")

    def test_truncated_usage_is_accounted_before_content_is_rejected(self):
        response = Mock(status_code=200, json=lambda: {
            "modelVersion": "gemini-2.5-flash",
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 200},
            "candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "incomplete"}]}}],
        })
        with sourcing_run() as run_id, patch("clipai.service.requests.post", return_value=response):
            with self.assertRaises(GeminiTruncatedResponseError):
                self.service._call_gemini("audio", stage="transcription_context")
        usage = gemini_usage(self.service.config.queue_dir, run_id=run_id)
        self.assertEqual(1, len(usage.rows))
        self.assertGreater(usage.cost_cad, 0)
        self.assertEqual("MAX_TOKENS", usage.rows[0]["finish_reason"])
        self.assertIn("transcription_context", usage.email_text("run"))

    def test_context_fields_and_audio_survive_truncation_retry(self):
        result = {"transcript": "Actual words", "context_sufficient": True, "person_hashtags": ["alice"]}
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"audio")
            with patch.object(self.service, "_source_video_path", return_value=Path("video.mp4")), \
                 patch.object(self.service, "_extract_audio", return_value=audio), \
                 patch.object(self.service, "_build_context_prompt", return_value="full context prompt"), \
                 patch.object(self.service, "_call_gemini", side_effect=[
                     GeminiTruncatedResponseError("truncated"), result,
                 ]) as call:
                context = self.service._analyze_clip_context({"text": "caption"}, run_dir=Path(tmp))
                self.assertEqual("Actual words", context["transcript"])
                self.assertTrue(context["context_sufficient"])
                self.assertEqual(["alice"], context["hashtags"])
                self.assertEqual(2, call.call_count)
                self.assertEqual(call.call_args_list[0], call.call_args_list[1])
                kwargs = call.call_args.kwargs
                self.assertEqual([audio], kwargs["audio_paths"])
                self.assertEqual(0, kwargs["thinking_budget"])
                self.assertEqual(4096, kwargs["max_output_tokens"])
                self.assertEqual(
                    {"transcript", "context_sufficient", "person_hashtags"},
                    set(kwargs["response_schema"]["properties"]),
                )
            self.assertFalse(audio.exists())

    def test_second_truncation_fails_and_cleans_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.touch()
            with patch.object(self.service, "_source_video_path", return_value=Path("video.mp4")), \
                 patch.object(self.service, "_extract_audio", return_value=audio), \
                 patch.object(self.service, "_build_context_prompt", return_value="prompt"), \
                 patch.object(self.service, "_call_gemini", side_effect=GeminiTruncatedResponseError("truncated")) as call:
                with self.assertRaises(GeminiTruncatedResponseError):
                    self.service._analyze_clip_context({}, run_dir=Path(tmp))
                self.assertEqual(2, call.call_count)
            self.assertFalse(audio.exists())

    def test_unsupported_model_does_not_receive_flash_thinking_setting(self):
        from dataclasses import replace

        self.service.config = replace(self.service.config, context_model="other-model")
        with patch.object(self.service, "_source_video_path", return_value=None), \
             patch.object(self.service, "_build_context_prompt", return_value="prompt"), \
             patch.object(self.service, "_call_gemini", return_value={"context_sufficient": False}) as call:
            self.service._analyze_clip_context({}, run_dir=Path("."))
        self.assertIsNone(call.call_args.kwargs["thinking_budget"])


if __name__ == "__main__":
    unittest.main()
