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
    def test_candidates_include_both_complete_grids(self) -> None:
        all_spots = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8)
        self.assertAlmostEqual(0.2, all_spots[0])
        self.assertLessEqual(all_spots[-1], 0.8)
        self.assertTrue(all(0.2 <= f <= 0.8 for f in all_spots))
        primary, staggered = kick_placement.placement_grids(LAYOUT, 0.2, 0.8)
        self.assertEqual(primary + staggered, all_spots)
        for grid in (primary, staggered):
            steps = [(b - a) * LAYOUT.max_y for a, b in zip(grid, grid[1:])]
            self.assertTrue(all(abs(step - LAYOUT.strip_height * 1.075) < 0.001 for step in steps))
        self.assertEqual([(a + b) / 2 for a, b in zip(primary, primary[1:])], staggered)

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

    def test_pick_uses_model_ranking_not_configured_preference(self) -> None:
        with TemporaryDirectory() as temp:
            service, run_dir = self._service_and_run(temp)
            count = len(kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8))
            boxes = [{"number": n, "covers": "x", "clear": n in (2, 3), "confidence": 0.9} for n in range(1, count + 1)]
            visual = {"caption_position": "TOP", "caption_bands": [{"top": 0, "bottom": 1}],
                      "has_source_watermark": True, "source_watermark_box": {"x": 0, "y": 0, "w": 1, "h": 1}}
            with patch.object(service, "_call_gemini", return_value={
                "boxes": boxes, "ranked_clear_boxes": [2, 3], "confidence": 0.9,
            }) as call:
                picked = service._pick_kick_overlay_y(run_dir, visual)
            self.assertEqual("gemini-3.1-pro-preview", call.call_args.kwargs["model"])
            self.assertEqual(3, call.call_count)
            self.assertTrue(all(len(c.kwargs["image_paths"]) == 2 for c in call.call_args_list))
            fractions = kick_placement.candidate_fractions(LAYOUT, 0.2, 0.8)
            self.assertEqual([round(fraction, 4) for fraction in fractions], visual["kick_overlay_candidates"])
            self.assertAlmostEqual(round(fractions[1], 4), picked)
            self.assertTrue((run_dir / "ai" / "kick_candidates.jpg").exists())
            with Image.open(run_dir / "ai" / "kick_candidates.jpg") as sheet:
                self.assertEqual((1644, 1932), sheet.size)

    def test_invalid_ranking_uses_existing_editor_fallback(self) -> None:
        with TemporaryDirectory() as temp:
            service, run_dir = self._service_and_run(temp)
            for ranking in (None, [], [1], [2, 2], [99], [True]):
                with self.subTest(ranking=ranking), patch.object(service, "_call_gemini", return_value={
                    "boxes": [{"number": 1, "covers": "face", "clear": False},
                              {"number": 2, "covers": "jacket", "clear": True}],
                    "ranked_clear_boxes": ranking, "confidence": 0.95,
                }):
                    self.assertIsNone(service._pick_kick_overlay_y(run_dir, {"caption_position": "BOTTOM"}))

    def test_aggregation_requires_all_frames_and_combines_rank_confidence(self) -> None:
        def response(order, confidence):
            return {"boxes": [{"number": n, "clear": n in order, "confidence": confidence[n - 1]} for n in (1, 2, 3)],
                    "ranked_clear_boxes": order}
        results = [response([1, 2], [.7, .99, .95]),
                   response([2, 1], [.7, .99, .95]),
                   response([2, 1, 3], [.7, .99, .95])]
        ranking = kick_placement.aggregate_rankings(results, 3)
        self.assertEqual([2, 1], [r["number"] for r in ranking])
        results[0]["boxes"][1]["confidence"] = .6
        self.assertEqual([1], [r["number"] for r in kick_placement.aggregate_rankings(results, 3)])
        results[0]["boxes"].append(results[0]["boxes"][0])
        with self.assertRaises(ValueError):
            kick_placement.aggregate_rankings(results, 3)
        self.assertEqual([], kick_placement.aggregate_rankings(
            [response([1], [.95, .95, .95]), response([2], [.95, .95, .95]),
             response([1, 2], [.95, .95, .95])], 3,
        ))

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
