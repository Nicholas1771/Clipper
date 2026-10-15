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


if __name__ == "__main__":
    unittest.main()
