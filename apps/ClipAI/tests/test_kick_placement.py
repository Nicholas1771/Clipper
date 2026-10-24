from __future__ import annotations

import json
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


def response(regions=None):
    return {"regions": [{"frame": n, **region} for n in (1, 2, 3)
                        for region in (regions if regions is not None else [{"kind": "face", "top": 0, "bottom": 1}])]}


class KickPlacementTests(unittest.TestCase):
    def _service_and_run(self, temp):
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
            overlay_reference_image_path=overlay, kick_overlay_top_y=0.1, kick_overlay_bottom_y=0.8,
        )
        return ClipAIService(config), run_dir

    def test_one_flash_call_with_ruler_frames_and_saved_evidence(self):
        with TemporaryDirectory() as temp:
            service, run = self._service_and_run(temp)
            visual = {}
            with patch.object(service, "_call_gemini", return_value=response()) as call:
                picked = service._pick_kick_overlay_y(run, visual)
            self.assertEqual(0.8, picked)
            call.assert_called_once()
            self.assertEqual("gemini-2.5-flash", call.call_args.kwargs["model"])
            self.assertEqual(2048, call.call_args.kwargs["thinking_budget"])
            self.assertEqual(4096, call.call_args.kwargs["max_output_tokens"])
            self.assertEqual([run / "ai" / f"kick_regions_{n}.jpg" for n in (1, 2, 3)],
                             call.call_args.kwargs["image_paths"])
            with Image.open(run / "ai/kick_regions_1.jpg") as image:
                self.assertEqual((610, 960), image.size)
            self.assertEqual("protected_regions", visual["kick_placement_method"])
            evidence = json.loads((run / "ai/kick_placement_results.json").read_text())
            self.assertEqual("selected", evidence["status"])
            self.assertEqual(12, evidence["margin"])
            self.assertFalse(list((run / "ai").glob("kick_frame*")))
            self.assertNotIn("kick_overlay_candidates", visual)

    def test_ruler_preserves_source_aspect_and_original_file(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "source.jpg"
            target = Path(temp) / "ruler.jpg"
            Image.new("RGB", (1920, 1080), "blue").save(source)
            original = source.read_bytes()
            self.assertEqual(target, kick_placement.draw_region_ruler(source, target))
            self.assertEqual(original, source.read_bytes())
            with Image.open(target) as image:
                self.assertEqual((610, 304), image.size)

    def test_existing_overlay_skips_placement(self):
        with TemporaryDirectory() as temp:
            service, run = self._service_and_run(temp)
            with patch.object(service, "_detect_overlay_visual", return_value={"has_kick_overlay": True}), \
                 patch.object(service, "_detect_caption_position", return_value={}), \
                 patch.object(service, "_detect_source_watermark", return_value={}), \
                 patch.object(service, "_pick_kick_overlay_y") as pick:
                self.assertIsNone(service._analyze_visual({}, run_dir=run)["kick_overlay_y"])
                pick.assert_not_called()

    def test_missing_inputs_skip_paid_call(self):
        with TemporaryDirectory() as temp:
            service, run = self._service_and_run(temp)
            with patch.object(service, "_call_gemini") as call, self.assertLogs(level="WARNING"):
                self.assertIsNone(service._pick_kick_overlay_y(None, {}))
                call.assert_not_called()

    def test_invalid_or_no_fit_response_logs_fallback(self):
        invalid = {"regions": []}
        blocked = response([{"kind": "face", "top": 0, "bottom": 1000}])
        for payload, status in ((invalid, "invalid"), (blocked, "no_fit")):
            with self.subTest(status=status), TemporaryDirectory() as temp:
                service, run = self._service_and_run(temp)
                with patch.object(service, "_call_gemini", return_value=payload), self.assertLogs(level="WARNING"):
                    self.assertIsNone(service._pick_kick_overlay_y(run, {}))
                evidence = json.loads((run / "ai/kick_placement_results.json").read_text())
                self.assertEqual(status, evidence["status"])

    def test_source_intervals_map_through_letterboxing(self):
        layout = kick_placement.Layout(1920, 1080, 108)
        intervals = kick_placement.protected_intervals(
            {"regions": [{"frame": 1, "kind": "caption", "top": 0, "bottom": 1000}]},
            [layout],
        )
        self.assertEqual([(656.25, 1263.75)], intervals)

    def test_validation_requires_unique_frames_and_valid_coordinates(self):
        layout = kick_placement.Layout(1080, 1920, 108)
        bad = [
            {}, {"regions": None}, {"regions": [None] * 3}, {"regions": []},
            {"regions": [{"frame": True, "kind": "face", "top": 0, "bottom": 1}]},
            {"regions": [{"frame": 4, "kind": "face", "top": 0, "bottom": 1}]},
            {"regions": [{"frame": 1, "kind": "face", "top": 0, "bottom": 1}]},
        ]
        for top, bottom, kind in ((True, 900, "face"), (-1, 10, "face"), (5, 5, "face"),
                                  (0, 1001, "face"), (0.5, 100, "face"), (0, 100, "")):
            bad.append(response([{"kind": kind, "top": top, "bottom": bottom}]))
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                kick_placement.protected_intervals(payload, [layout] * 3)

    def test_position_avoids_union_and_maximizes_clearance(self):
        intervals = [(0, 600), (1100, 1920)]
        picked = kick_placement.choose_position(intervals, 108, 0.8, 0.1)
        self.assertIsNotNone(picked)
        self.assertEqual(12, picked.margin)
        top = round(1812 * picked.fraction)
        self.assertGreaterEqual(top, 612)
        self.assertLessEqual(top + 108, 1088)
        self.assertAlmostEqual((600 + 1100 - 108) / 2, top, delta=1)
        self.assertIsNone(kick_placement.choose_position([(0, 1920)], 108, 0.1, 0.8))

    def test_tight_fit_removes_only_extra_margin(self):
        intervals = [(0, 700), (810, 1920)]
        picked = kick_placement.choose_position(intervals, 108, 0.1, 0.8)
        self.assertIsNotNone(picked)
        self.assertEqual(0, picked.margin)
        top = round(1812 * picked.fraction)
        self.assertGreaterEqual(top, 700)
        self.assertLessEqual(top + 108, 810)
        self.assertIsNone(kick_placement.choose_position([(0, 700), (807, 1920)], 108, 0.1, 0.8))

    def test_geometry_rejects_invalid_values(self):
        for height, low, high in ((0, .1, .8), (1920, .1, .8), (108, float("nan"), .8),
                                  (108, -.1, .8), (108, .1, 1.1)):
            with self.subTest(height=height, low=low), self.assertRaises(ValueError):
                kick_placement.choose_position([], height, low, high)


if __name__ == "__main__":
    unittest.main()
