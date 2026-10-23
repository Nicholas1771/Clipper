from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clipai.config import AppConfig
from clipai.service import ClipAIService


class ClipAITests(unittest.TestCase):
    def test_source_branding_requires_frames_and_uses_existing_strict_detector(self):
        service = ClipAIService(AppConfig(
            queue_dir=Path("."), poll_interval_seconds=1, google_api_key="k",
            gemini_model="m", timeout_seconds=30, timezone_name="UTC",
        ))
        with patch.object(service, "_extract_sample_frames", return_value=[]), \
             patch.object(service, "_detect_overlay_visual") as detector:
            with self.assertRaisesRegex(RuntimeError, "No source frames"):
                service.source_has_kick_branding(Path("clip.mp4"), {})
            detector.assert_not_called()
        frames = [Path("frame.jpg")]
        with patch.object(service, "_extract_sample_frames", return_value=frames), \
             patch.object(service, "_detect_overlay_visual", return_value={"has_kick_overlay": False}) as detector:
            self.assertFalse(service.source_has_kick_branding(Path("clip.mp4"), {"text": "caption"}))
            detector.assert_called_once_with({"text": "caption"}, image_paths=frames)

    def test_normalize_hashtags_excludes_streamer_and_caps_at_two(self) -> None:
        service = ClipAIService(AppConfig(
            queue_dir=Path("."), poll_interval_seconds=60, google_api_key="k",
            gemini_model="m", timeout_seconds=30, timezone_name="America/New_York", streamer_name="Sample",
        ))
        self.assertEqual(
            ["firsttag", "secondtag"],
            service._normalize_hashtags(["#FirstTag", "Sample", "firsttag", "Second Tag", "x"]),
        )
        self.assertEqual([], service._normalize_hashtags(None))

    def test_process_once_writes_analysis_and_summary(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            queue_dir = root / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            run_dir = queue_dir / "0001_sample_test"
            run_dir.mkdir(parents=True, exist_ok=True)
            source_dir = run_dir / "source"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "manifest.json").write_text(
                json.dumps({
                    "text": "sample best moments",
                    "author_handle": "sample",
                    "hashtags": ["sample"],
                }),
                encoding="utf-8",
            )
            (run_dir / "queue_state.json").write_text(
                json.dumps({"phase": "sourced", "next_stage": "ai"}),
                encoding="utf-8",
            )
            (queue_dir / "scheduler_state.json").write_text(
                json.dumps({"phase": "source_consumed", "slot_index": 3}),
                encoding="utf-8",
            )

            config = AppConfig(
                queue_dir=queue_dir,
                poll_interval_seconds=60,
                google_api_key="test-key",
                gemini_model="gemini-2.5-flash-lite",
                timeout_seconds=30,
                timezone_name="America/New_York",
            )
            service = ClipAIService(config)
            (run_dir / "creator.json").write_text(json.dumps({
                "name": "navaeh", "display_name": "Nevaeh",
                "settings": {"STREAMER_NAME": "Nevaeh", "OVERLAY_IMAGE_PATH": "iamnevaehakira.png"},
            }))

            with patch.object(
                service,
                "_analyze_visual",
                return_value={
                    "has_kick_overlay": False, "has_caption": True, "caption_position": "BOTTOM",
                    "caption_bands": [{"top": 78, "bottom": 92}, {"top": 0.1, "bottom": 0.05}, {"top": 0.0, "bottom": 0.9}, "bad"],
                },
            ), patch.object(
                service,
                "_analyze_clip_context",
                return_value={
                    "caption": "sample best moments",
                    "transcript": "",
                    "context_sufficient": False,
                    "hashtags": ["yera"],
                },
            ), patch.object(
                service,
                "_generate_summary",
                return_value="First paragraph with context.\n\nSecond paragraph with context.\n\nThird paragraph with context.",
            ):
                self.assertTrue(service.process_once())
                self.assertEqual(service.config.streamer_name, "Nevaeh")
                self.assertEqual(service.config.overlay_reference_image_path.name, "iamnevaehakira.png")

            analysis = json.loads((run_dir / "ai" / "analysis.json").read_text(encoding="utf-8"))
            self.assertFalse(analysis["has_kick_overlay"])
            self.assertTrue(analysis["has_caption"])
            self.assertEqual("BOTTOM", analysis["caption_position"])
            self.assertEqual([{"top": 0.05, "bottom": 0.1}, {"top": 0.78, "bottom": 0.92}], analysis["caption_bands"])
            self.assertFalse(analysis["context_sufficient"])
            self.assertEqual(
                "First paragraph with context.\n\nSecond paragraph with context.\n\nThird paragraph with context.\n\n#yera",
                (run_dir / "ai" / "summary.txt").read_text(encoding="utf-8"),
            )
            final_state = json.loads((run_dir / "queue_state.json").read_text(encoding="utf-8"))
            self.assertEqual("ai_processed", final_state["phase"])
            self.assertEqual("editor", final_state["next_stage"])
            scheduler_state = json.loads((queue_dir / "scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual("ai_processed", scheduler_state["phase"])
            self.assertEqual(3, scheduler_state["slot_index"])


class YouTubeDescriptionTests(unittest.TestCase):
    def make_service(self, tmp: str) -> ClipAIService:
        prompts = Path(tmp) / "prompts"
        prompts.mkdir(exist_ok=True)
        (prompts / "youtube_description.txt").write_text("YT __STREAMER__: __CAPTION__ | __TRANSCRIPT__ | __CONTEXT_SUFFICIENT__", encoding="utf-8")
        return ClipAIService(AppConfig(
            queue_dir=Path(tmp), poll_interval_seconds=60, google_api_key="k", prompts_dir=prompts,
            gemini_model="m", timeout_seconds=30, timezone_name="America/New_York", streamer_name="Sample",
        ))

    def context(self) -> dict:
        return {"caption": "Big play", "transcript": "wow", "context_sufficient": True}

    def test_writes_youtube_files_from_dedicated_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            run_dir = Path(tmp) / "run"
            (run_dir / "ai").mkdir(parents=True)
            with patch.object(service, "_call_gemini", return_value={"text": "A  desc\n\n\n#tag"}) as call:
                result = service._write_youtube_files(run_dir, self.context(), "ig summary")
            self.assertEqual("YT Sample: Big play | wow | yes", call.call_args.args[0])
            self.assertEqual("A desc\n\n#tag", result)
            self.assertEqual("A desc\n\n#tag", (run_dir / "ai" / "youtube_description.txt").read_text(encoding="utf-8"))
            self.assertEqual("Big play", (run_dir / "ai" / "youtube_title.txt").read_text(encoding="utf-8"))

    def test_falls_back_to_instagram_summary_when_generation_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            run_dir = Path(tmp) / "run"
            (run_dir / "ai").mkdir(parents=True)
            with patch.object(service, "_call_gemini", side_effect=RuntimeError("boom")):
                result = service._write_youtube_files(run_dir, self.context(), "ig summary")
            self.assertEqual("ig summary", result)
            self.assertEqual("ig summary", (run_dir / "ai" / "youtube_description.txt").read_text(encoding="utf-8"))

    def test_title_is_capped_on_a_word_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            title = self.make_service(tmp)._youtube_title("word " * 40)
            self.assertLessEqual(len(title), 100)
            self.assertTrue(title.endswith("word"))
            self.assertEqual("Sample clip", self.make_service(tmp)._youtube_title("  "))

    def archived(self, queue: Path, name: str, *, when: str, status: str = "success", existing: bool = False) -> Path:
        run = queue / "Archive" / name
        for sub in ("source", "editor", "ai", "publisher"):
            (run / sub).mkdir(parents=True)
        (run / "source" / "m.json").write_text(json.dumps({"text": f"Clip {name} #n3on"}), encoding="utf-8")
        (run / "ai" / "transcript.txt").write_text("hello", encoding="utf-8")
        (run / "ai" / "analysis.json").write_text(json.dumps({"context_sufficient": True, "summary": "ig summary"}), encoding="utf-8")
        (run / "publisher" / "publish_state.json").write_text(json.dumps(
            {"phase": "published", "status": status, "result": {"id": "m"}, "updated_at": when}), encoding="utf-8")
        if existing:
            (run / "editor" / "youtube_description.txt").write_text("keep", encoding="utf-8")
        return run

    def test_backfill_writes_missing_metadata_for_recent_published_clips_only(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            queue = Path(tmp)
            new = self.archived(queue, "new", when="2026-10-14T12:00:00-04:00")
            old = self.archived(queue, "old", when="2026-10-12T12:00:00-04:00")
            failed = self.archived(queue, "failed", when="2026-10-14T12:00:00-04:00", status="error")
            done = self.archived(queue, "done", when="2026-10-14T12:00:00-04:00", existing=True)
            with patch.object(service, "_call_gemini", return_value={"text": "Fresh desc"}) as call:
                self.assertEqual(1, service.backfill_youtube_metadata(since=date(2026, 10, 13)))
            self.assertEqual(1, call.call_count)
            self.assertEqual("YT Sample: Clip new | hello | yes", call.call_args.args[0])
            self.assertEqual("Fresh desc", (new / "editor" / "youtube_description.txt").read_text(encoding="utf-8"))
            self.assertEqual("Clip new", (new / "editor" / "youtube_title.txt").read_text(encoding="utf-8"))
            self.assertEqual("keep", (done / "editor" / "youtube_description.txt").read_text(encoding="utf-8"))
            for run in (old, failed):
                self.assertFalse((run / "editor" / "youtube_description.txt").exists())

    def test_instagram_top_mode_generates_youtube_files_without_youtube_slot(self) -> None:
        with TemporaryDirectory() as tmp:
            prompts = Path(tmp) / "prompts"
            prompts.mkdir()
            (prompts / "youtube_description.txt").write_text("x", encoding="utf-8")
            for mode, expected in (("instagram_top", True), ("shared", False)):
                service = ClipAIService(AppConfig(
                    queue_dir=Path(tmp), poll_interval_seconds=60, google_api_key="k", prompts_dir=prompts,
                    gemini_model="m", timeout_seconds=30, timezone_name="America/New_York", youtube_selection_mode=mode,
                ))
                run_dir = Path(tmp) / f"run_{mode}"
                (run_dir / "source").mkdir(parents=True)
                (run_dir / "source" / "m.json").write_text("{}", encoding="utf-8")
                (run_dir / "queue_state.json").write_text(json.dumps({"phase": "sourced"}), encoding="utf-8")
                context = {"caption": "c", "transcript": "t", "context_sufficient": True, "hashtags": []}
                with patch.object(service, "_ready_items", return_value=[run_dir]), \
                     patch.object(service, "_analyze_visual", return_value={}), \
                     patch.object(service, "_analyze_clip_context", return_value=context), \
                     patch.object(service, "_generate_summary", return_value="s"), \
                     patch("clipai.service.read_slot_platforms", return_value=["instagram"]), \
                     patch.object(service, "_write_youtube_files", return_value="d") as write:
                    service.process_once()
                self.assertEqual(expected, write.called, mode)


class OnScreenCaptionTests(unittest.TestCase):
    def make_service(self, root: Path) -> ClipAIService:
        return ClipAIService(AppConfig(
            queue_dir=root, poll_interval_seconds=60, google_api_key="k",
            prompts_dir=APP_ROOT.parents[1] / "data" / "prompts",
            gemini_model="m", timeout_seconds=30, timezone_name="America/New_York",
            streamer_name="N3on", youtube_selection_mode="instagram_top",
        ))

    def test_existing_caption_call_extracts_text_without_an_extra_call(self):
        with TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            frames = [Path(tmp) / "reference_frame_1.jpg"]
            payload = {
                "has_caption": True, "caption_position": "TOP",
                "confidence": 0.95, "text_bands": [{"top": 0.1, "bottom": 0.2}],
                "on_screen_caption": "  N3on meets Kai at the event  ",
            }
            with patch.object(service, "_collect_source_frames", return_value=frames), \
                 patch.object(service, "_call_gemini", return_value=payload) as call:
                result = service._detect_caption_position({"text": "#n3on"}, run_dir=Path(tmp))
            self.assertEqual("N3on meets Kai at the event", result["on_screen_caption"])
            self.assertEqual("TOP", result["caption_position"])
            call.assert_called_once()
            self.assertEqual(frames, call.call_args.kwargs["image_paths"])
            self.assertIn("on_screen_caption", call.call_args.kwargs["response_schema"]["required"])
            self.assertIn("never guess missing words", call.call_args.args[0])

    def test_absent_unreadable_or_low_confidence_text_is_empty(self):
        with TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            base = {
                "has_caption": True, "caption_position": "TOP",
                "confidence": 0.9, "on_screen_caption": "visible text",
            }
            for changes, frames in [
                ({"has_caption": False}, [Path("frame.jpg")]),
                ({"confidence": 0.49}, [Path("frame.jpg")]),
                ({"caption_position": "NONE"}, [Path("frame.jpg")]),
                ({"on_screen_caption": ""}, [Path("frame.jpg")]),
                ({"on_screen_caption": None}, [Path("frame.jpg")]),
                ({}, []),
            ]:
                with self.subTest(changes=changes, frames=frames), \
                     patch.object(service, "_collect_source_frames", return_value=frames), \
                     patch.object(service, "_call_gemini", return_value={**base, **changes}):
                    result = service._detect_caption_position({})
                self.assertEqual("", result["on_screen_caption"])

    def test_hashtag_only_post_uses_visual_text_through_context_and_generators(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root)
            run = root / "run"
            (run / "source").mkdir(parents=True)
            (run / "source" / "m.json").write_text(json.dumps({"text": "#n3on"}), encoding="utf-8")
            (run / "queue_state.json").write_text(json.dumps({"phase": "sourced"}), encoding="utf-8")
            text = "N3on meets Kai at the event"
            with patch.object(service, "_analyze_visual", return_value={
                "has_caption": True, "caption_position": "TOP", "on_screen_caption": text,
            }), patch.object(service, "_call_gemini", side_effect=[
                {"transcript": "hello", "context_sufficient": True, "person_hashtags": ["Kai"]},
                {"text": "Instagram summary"},
                {"text": "N3on MEETS Kai"},
            ]) as call:
                self.assertTrue(service.process_once())
            self.assertEqual(3, call.call_count)
            for args in call.call_args_list:
                prompt = args.args[0]
                self.assertIn(text, prompt)
                self.assertNotIn("__ON_SCREEN_CAPTION__", prompt)
            self.assertIn("No caption provided.", call.call_args_list[0].args[0])
            analysis = json.loads((run / "ai" / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(text, analysis["on_screen_caption"])
            self.assertEqual(["kai"], analysis["hashtags"])
            self.assertEqual("N3on MEETS Kai", (run / "ai" / "youtube_description.txt").read_text(encoding="utf-8"))
            self.assertEqual("#n3on", json.loads((run / "source" / "m.json").read_text())["text"])

    def test_all_prompt_builders_keep_original_text_and_fill_optional_context(self):
        with TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            caption = "Original TikTok caption"
            text = "Readable on-screen title"
            prompts = [
                service._build_context_prompt(caption, text),
                service._build_summary_prompt(caption, "audio transcript", True, text),
                service._build_youtube_prompt(caption, "audio transcript", True, text),
            ]
            for prompt in prompts:
                self.assertIn(text, prompt)
                self.assertNotIn("__ON_SCREEN_CAPTION__", prompt)
            self.assertIn(caption, prompts[0])
            self.assertIn(caption, prompts[1])
            self.assertIn(caption, prompts[2])
            streamer_prompt = service._build_summary_prompt(caption, "audio transcript", False, text)
            self.assertNotIn(text, streamer_prompt)
            self.assertNotIn("__ON_SCREEN_CAPTION__", streamer_prompt)
            self.assertIn("None detected.", service._build_youtube_prompt(caption, "", False))

    def test_backfill_reuses_saved_visual_caption_without_new_visual_analysis(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.make_service(root)
            run = YouTubeDescriptionTests().archived(root, "saved", when="2026-10-14T12:00:00-04:00")
            (run / "ai" / "analysis.json").write_text(json.dumps({
                "context_sufficient": True, "on_screen_caption": "Saved on-screen event", "summary": "summary",
            }))
            with patch.object(service, "_call_gemini", return_value={"text": "YouTube caption"}) as call, \
                 patch.object(service, "_analyze_visual") as visual:
                self.assertEqual(1, service.backfill_youtube_metadata(since=date(2026, 10, 13)))
            visual.assert_not_called()
            call.assert_called_once()
            self.assertIn("Saved on-screen event", call.call_args.args[0])
            self.assertIn("Clip saved", call.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
