from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
for path in (APP_ROOT / "src", APP_ROOT.parent / "Common" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PIL import Image

from clipai import kick_placement
from clipai.config import AppConfig
from clipai.service import ClipAIService

LAYOUT = kick_placement.Layout(1080, 1920, 108.0)


class KickPlacementTests(unittest.TestCase):
    def test_candidates_stay_in_config_range_and_skip_caption_text(self) -> None:
        all_spots = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8, [])
        self.assertAlmostEqual(0.2, all_spots[0])
        self.assertAlmostEqual(0.8, all_spots[-1])
        self.assertTrue(all(0.2 <= f <= 0.8 for f in all_spots))
        steps = [(b - a) * LAYOUT.max_y for a, b in zip(all_spots, all_spots[1:])]
        self.assertTrue(all(step >= LAYOUT.strip_height for step in steps))
        clear = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8, [{"top": 0.78, "bottom": 0.92}])
        self.assertLess(len(clear), len(all_spots))
        self.assertTrue(all(f * LAYOUT.max_y + 108 <= 0.78 * 1920 - 48 for f in clear))

    def test_watermark_spots_are_always_dropped(self) -> None:
        keep_out = kick_placement.watermark_span(LAYOUT, box={"x": 0.3, "y": 0.6, "w": 0.3, "h": 0.04}, watermark_y=0.9, size=0.02, opacity=0.3)
        self.assertAlmostEqual(0.6 * 1920 - 19.2, keep_out[0])
        spots = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8, [], [keep_out])
        self.assertTrue(spots)
        self.assertTrue(all(kick_placement.is_clear(LAYOUT, f, [keep_out]) for f in spots))
        default = kick_placement.watermark_span(LAYOUT, box=None, watermark_y=0.5, size=0.02, opacity=0.3)
        self.assertAlmostEqual((1920 - 38) * 0.5 - 19.2, default[0])
        self.assertIsNone(kick_placement.watermark_span(LAYOUT, box=None, watermark_y=0.5, size=0.02, opacity=0.0))

    def test_default_fraction(self) -> None:
        self.assertEqual(0.5, kick_placement.default_fraction(0.2, 0.8, "BOTH"))
        self.assertEqual(0.2, kick_placement.default_fraction(0.2, 0.8, "BOTTOM"))
        self.assertEqual(0.8, kick_placement.default_fraction(0.2, 0.8, "TOP"))

    def _service_and_run(self, temp: str) -> tuple[ClipAIService, Path]:
        root = Path(temp)
        overlay = root / "overlay.png"
        Image.new("RGB", (1080, 108), (0, 200, 0)).save(overlay)
        run_dir = root / "run"
        (run_dir / "ai").mkdir(parents=True)
        for index in (1, 2, 3):
            Image.new("RGB", (540, 960), (90, 90, 90)).save(run_dir / "ai" / f"reference_frame_{index}.jpg")
        config = AppConfig(
            queue_dir=root, poll_interval_seconds=60, google_api_key="k", gemini_model="m", timeout_seconds=30,
            timezone_name="America/New_York", prompts_dir=APP_ROOT.parents[1] / "data" / "prompts",
            overlay_reference_image_path=overlay, kick_overlay_top_y=0.2, kick_overlay_bottom_y=0.8,
        )
        return ClipAIService(config), run_dir

    def test_pick_returns_clear_spot_nearest_the_configured_one(self) -> None:
        with TemporaryDirectory() as temp:
            service, run_dir = self._service_and_run(temp)
            count = len(kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8, []))
            boxes = [{"number": n, "covers": "x", "clear": n in (2, 3)} for n in range(1, count + 1)]
            with patch.object(service, "_call_gemini", return_value={"boxes": boxes, "confidence": 0.9}):
                picked = service._pick_kick_overlay_y(run_dir, {"caption_position": "TOP", "caption_bands": []})
            fractions = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8, [])
            self.assertAlmostEqual(round(fractions[2], 4), picked)
            self.assertTrue((run_dir / "ai" / "kick_candidates.jpg").exists())

    def test_pick_falls_back_when_nothing_clear_or_unsure(self) -> None:
        with TemporaryDirectory() as temp:
            service, run_dir = self._service_and_run(temp)
            caption = {"caption_position": "TOP", "caption_bands": []}
            blocked = {"boxes": [{"number": 1, "covers": "face", "clear": False}], "confidence": 0.95}
            with patch.object(service, "_call_gemini", return_value=blocked):
                self.assertIsNone(service._pick_kick_overlay_y(run_dir, caption))
            unsure = {"boxes": [{"number": 1, "covers": "wall", "clear": True}], "confidence": 0.5}
            with patch.object(service, "_call_gemini", return_value=unsure):
                self.assertIsNone(service._pick_kick_overlay_y(run_dir, caption))


if __name__ == "__main__":
    unittest.main()
