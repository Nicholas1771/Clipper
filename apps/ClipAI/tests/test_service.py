from __future__ import annotations

import json
import sys
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

    def test_parse_watermark_box_validates_and_normalizes(self) -> None:
        service = ClipAIService(AppConfig(
            queue_dir=Path("."), poll_interval_seconds=60, google_api_key="k",
            gemini_model="m", timeout_seconds=30, timezone_name="America/New_York",
        ))
        self.assertEqual(
            {"x": 0.4, "y": 0.49, "w": 0.2, "h": 0.03},
            service._parse_watermark_box([490, 400, 520, 600]),
        )
        self.assertIsNone(service._parse_watermark_box([0, 0, 1000, 1000]))
        self.assertIsNone(service._parse_watermark_box([500, 600, 500, 400]))
        self.assertIsNone(service._parse_watermark_box("nope"))

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
                return_value={"has_kick_overlay": False, "has_caption": True, "caption_position": "BOTTOM"},
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


if __name__ == "__main__":
    unittest.main()
