from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
for path in (APP_ROOT / "src", APP_ROOT.parent / "Common" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from imageio_ffmpeg import get_ffmpeg_exe

from clipai import service as service_module
from clipai.config import AppConfig
from clipai.service import ClipAIService
from clipai.static_regions import StaticRegion, draw_candidates, draw_zoom_sheet, find_static_regions, is_candidate, union_box


def make_video(path: Path, static: str = "", moving: bool = True, seconds: int = 10) -> Path:
    base = f"[0]{static or 'null'}[b]"
    graph = f"{base};color=c=black:s=120x80:r=5[m];[b][m]overlay=x='mod(n*37,400)':y='mod(n*53,800)':shortest=1" if moving else base.replace("[b]", "")
    subprocess.run(
        [get_ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s=540x960:r=5:d={seconds}",
         "-filter_complex", graph, "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


STATIC_BOX = "drawbox=x=100:y=600:w=200:h=40:color=white:t=fill"


class StaticRegionTests(unittest.TestCase):
    def test_finds_the_still_box_and_ignores_the_moving_one(self) -> None:
        with TemporaryDirectory() as temp:
            video = make_video(Path(temp) / "v.mp4", STATIC_BOX)
            regions = find_static_regions(video)
        self.assertEqual(1, len(regions))
        region = regions[0]
        self.assertAlmostEqual(100 / 540, region.x, delta=0.03)
        self.assertAlmostEqual(600 / 960, region.y, delta=0.02)
        self.assertAlmostEqual(200 / 540, region.w, delta=0.04)
        self.assertAlmostEqual(40 / 960, region.h, delta=0.02)

    def test_mostly_still_video_has_no_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            video = make_video(Path(temp) / "v.mp4", "drawgrid=w=16:h=16:t=2:c=white")
            self.assertEqual([], find_static_regions(video))

    def test_video_without_still_parts_has_no_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            video = make_video(Path(temp) / "v.mp4")
            self.assertEqual([], find_static_regions(video))

    def test_union_and_drawing(self) -> None:
        regions = [StaticRegion(0.3, 0.6, 0.1, 0.04, 20), StaticRegion(0.45, 0.61, 0.2, 0.03, 40)]
        self.assertEqual({"x": 0.3, "y": 0.6, "w": 0.35, "h": 0.04}, union_box(regions))
        with TemporaryDirectory() as temp:
            from PIL import Image

            frame = Path(temp) / "f.jpg"
            Image.new("RGB", (1080, 1920), (40, 40, 40)).save(frame)
            output = draw_candidates(frame, regions, Path(temp) / "ai" / "c.jpg")
            self.assertTrue(output.exists())
            sheet = draw_zoom_sheet(frame, regions, Path(temp) / "ai" / "z.jpg")
            with Image.open(sheet) as image:
                self.assertEqual(2 * 120, image.height)

    def test_edge_to_edge_bars_are_not_candidates(self) -> None:
        self.assertFalse(is_candidate(StaticRegion(0.01, 0.88, 0.99, 0.12, 900)))
        self.assertTrue(is_candidate(StaticRegion(0.35, 0.61, 0.3, 0.04, 200)))


def labels(watermarks, confidence):
    boxes = [{"number": number, "content": "@someone", "kind": "watermark"} for number in watermarks]
    boxes.append({"number": 1, "content": "BIG TITLE", "kind": "caption"})
    return {"boxes": boxes, "confidence": confidence}


class DetectSourceWatermarkTests(unittest.TestCase):
    REGIONS = [
        StaticRegion(0.2, 0.05, 0.6, 0.08, 800),
        StaticRegion(0.35, 0.61, 0.1, 0.04, 30),
        StaticRegion(0.46, 0.61, 0.18, 0.04, 200),
        StaticRegion(0.1, 0.9, 0.1, 0.03, 20),
    ]

    def detect(self, payload=None, regions=None, scan_error: Exception | None = None):
        service = ClipAIService(AppConfig(
            queue_dir=Path("."), poll_interval_seconds=60, google_api_key="k",
            gemini_model="m", timeout_seconds=30, timezone_name="America/New_York",
            prompts_dir=APP_ROOT.parents[1] / "data" / "prompts",
        ))
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            (run_dir / "ai").mkdir(parents=True)
            (run_dir / "source").mkdir()
            (run_dir / "ai" / "reference_frame_2.jpg").write_bytes(b"jpg")
            (run_dir / "source" / "clip.mp4").write_bytes(b"mp4")
            find = patch.object(service_module, "find_static_regions",
                                side_effect=scan_error, return_value=self.REGIONS if regions is None else regions)
            draw = patch.object(service_module, "draw_candidates", side_effect=lambda frame, rs, out: out)
            zoom = patch.object(service_module, "draw_zoom_sheet", side_effect=lambda frame, rs, out: out)
            with find, draw, zoom, patch.object(service, "_call_gemini", return_value=payload or {}) as gemini:
                result = service._detect_source_watermark({}, run_dir=run_dir)
        return result, gemini

    def test_uses_measured_box_of_the_picked_candidate(self) -> None:
        result, gemini = self.detect(labels([3], 0.9))
        self.assertTrue(result["has_source_watermark"])
        self.assertEqual({"x": 0.46, "y": 0.61, "w": 0.18, "h": 0.04}, result["source_watermark_box"])
        self.assertIn("4 numbered", gemini.call_args.args[0])

    def test_neighbouring_picks_are_merged(self) -> None:
        result, _ = self.detect(labels([2, 3], 0.9))
        self.assertEqual({"x": 0.35, "y": 0.61, "w": 0.29, "h": 0.04}, result["source_watermark_box"])

    def test_unconfirmed_means_no_blur(self) -> None:
        for payload in (
            labels([], 0.95),
            labels([3], 0.5),
            labels([9], 0.9),
            labels(["x"], 0.9),
            labels([2, 4], 0.9),  # far apart -> ambiguous
            {},
        ):
            with self.subTest(payload=payload):
                result, _ = self.detect(payload)
                self.assertFalse(result["has_source_watermark"])
                self.assertIsNone(result["source_watermark_box"])

    def test_stream_branding_is_never_blurred(self) -> None:
        payload = {"boxes": [{"number": 3, "content": "KICK.COM/N3ON", "kind": "watermark"}], "confidence": 0.95}
        result, _ = self.detect(payload)
        self.assertFalse(result["has_source_watermark"])

    def test_no_candidates_or_scan_failure_skips_gemini(self) -> None:
        for kwargs in ({"regions": []}, {"scan_error": RuntimeError("ffmpeg")}):
            with self.subTest(kwargs=kwargs):
                result, gemini = self.detect(**kwargs)
                self.assertFalse(result["has_source_watermark"])
                gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
