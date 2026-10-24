from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock

APP_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = APP_ROOT / "src"
COMMON_SRC_ROOT = APP_ROOT.parent / "Common" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(COMMON_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(COMMON_SRC_ROOT))

from clipeditor.config import AppConfig, _float_env
from clipeditor.service import ClipEditorService


class ClipEditorConfigTests(unittest.TestCase):
    def test_float_env_parsing_and_clamping(self) -> None:
        with patch.dict("os.environ", {"TEST_VAR": "0.25"}):
            self.assertEqual(0.25, _float_env("TEST_VAR", 0.0, 0.0, 1.0))
        with patch.dict("os.environ", {"TEST_VAR": "-5.0"}):
            self.assertEqual(0.0, _float_env("TEST_VAR", 0.0, 0.0, 1.0))
        with patch.dict("os.environ", {"TEST_VAR": "2.5"}):
            self.assertEqual(1.0, _float_env("TEST_VAR", 0.0, 0.0, 1.0))
        with patch.dict("os.environ", {"ALIAS_VAR": "0.15"}):
            self.assertEqual(0.0, _float_env("PRIMARY_VAR", 0.0, 0.0, 1.0))
        with patch.dict("os.environ", {"TEST_VAR": "invalid"}):
            with self.assertRaises(ValueError):
                _float_env("TEST_VAR", 0.0, 0.0, 1.0)


class ClipEditorRenderTests(unittest.TestCase):
    def make_service(self, **kwargs) -> ClipEditorService:
        config_args = {
            "queue_dir": Path("."),
            "poll_interval_seconds": 60,
            "ready_stability_seconds": 5,
            "overlay_image_path": Path("overlay.png"),
            "watermark_font_file": None,
            "watermark_font_name": "Arial",
            "watermark_text": "@user",
            "title_prefix": "Clip",
            "timezone_name": "America/New_York",
            "trim_start_seconds": 0.0,
            "trim_end_seconds": 0.0,
            "audio_pitch_scale": 1.0,
        }
        config_args.update(kwargs)
        return ClipEditorService(AppConfig(**config_args))

    def test_process_records_actual_original_or_region_manual_branding(self) -> None:
        for original, overlay_y, method in [
            (True, None, None),
            (False, "(H-h)*0.8000", "protected_regions"),
            (False, "(H-h)*0.7000", "fallback"),
        ]:
            with self.subTest(original=original, overlay_y=overlay_y), TemporaryDirectory() as tmp:
                run = Path(tmp) / "clip"
                (run / "ai").mkdir(parents=True)
                (run / "ai" / "analysis.json").write_text(json.dumps({
                    "has_kick_overlay": original,
                    "kick_overlay_y": 0.8,
                    "kick_placement_method": "protected_regions",
                }))
                (run / "queue_state.json").write_text('{"phase": "ai_processed"}')
                service = self.make_service(queue_dir=Path(tmp))
                with patch.object(service, "_ready_items", return_value=[run]), \
                     patch.object(service, "_source_video_path", return_value=run / "source.mp4"), \
                     patch.object(service, "_read_source_manifest", return_value={}), \
                     patch.object(service, "_render_edited_video", return_value=overlay_y), \
                     patch.object(service, "_write_metadata_files"), \
                     patch.object(service, "_mark_edited"):
                    self.assertTrue(service.process_once())
                branding = json.loads((run / "editor" / "branding.json").read_text())
                self.assertEqual("Original" if original else "Manual", branding["mode"])
                self.assertEqual(method, branding.get("method"))
                if not original:
                    self.assertEqual(overlay_y, branding["overlay_y"])
                    self.assertEqual(0.8 if method == "protected_regions" else None, branding.get("fraction"))

    def test_probe_video_info_extracts_size_and_duration(self) -> None:
        service = self.make_service()
        fake_stderr = (
            "Input #0, mov,mp4,m4a,3gp,9ga,mp4v:\n"
            "  Duration: 00:01:23.45, start: 0.000000, bitrate: 1200 kb/s\n"
            "  Stream #0:0[0x1](und): Video: h264 (Main) (avc1), yuv420p, 1080x1920 [SAR 1:1 DAR 9:16], 30 fps\n"
        )
        fake_result = MagicMock(stderr=fake_stderr, returncode=0)
        with patch("subprocess.run", return_value=fake_result):
            size, duration = service._probe_video_info(Path("dummy.mp4"))
            self.assertEqual((1080, 1920), size)
            self.assertAlmostEqual(83.45, duration)

    def test_kick_overlay_moves_to_nearest_spot_clear_of_caption_text(self) -> None:
        service = self.make_service(kick_overlay_top_y=0.2, kick_overlay_bottom_y=0.8)
        sizes = {Path("video.mp4"): (1080, 1920), Path("overlay.png"): (1080, 108)}
        with patch.object(service, "_probe_video_size", side_effect=lambda path: sizes[path]):
            kwargs = {"target_width": 1080, "target_height": 1920}
            no_bands = service._kick_overlay_y(Path("video.mp4"), "bottom", [], **kwargs)
            self.assertEqual("(H-h)*0.8000", no_bands)
            clear_bands = service._kick_overlay_y(Path("video.mp4"), "bottom", [{"top": 0.05, "bottom": 0.14}], **kwargs)
            self.assertEqual("(H-h)*0.8000", clear_bands)
            # Title at the top and subtitles across the default spot: the bar sits just above the subtitles.
            bands = [{"top": 0.05, "bottom": 0.14}, {"top": 0.78, "bottom": 0.92}]
            moved = service._kick_overlay_y(Path("video.mp4"), "bottom", bands, **kwargs)
            self.assertEqual(f"(H-h)*{(0.78 * 1920 - 108) / 1812:.4f}", moved)
            covered = [{"top": 0.0, "bottom": 0.4}, {"top": 0.4, "bottom": 0.8}, {"top": 0.8, "bottom": 1.0}]
            self.assertEqual("(H-h)*0.8000", service._kick_overlay_y(Path("video.mp4"), "bottom", covered, **kwargs))
            # The only text-free spot is above KICK_OVERLAY_TOP_Y, so it is not used.
            high_only = [{"top": 0.12, "bottom": 1.0}]
            self.assertEqual("(H-h)*0.8000", service._kick_overlay_y(Path("video.mp4"), "bottom", high_only, **kwargs))

    def test_kick_overlay_uses_ai_spot_then_middle_for_both_and_stays_in_range(self) -> None:
        service = self.make_service(kick_overlay_top_y=0.2, kick_overlay_bottom_y=0.8, watermark_opacity=0.0)
        sizes = {Path("v.mp4"): (1080, 1920), Path("overlay.png"): (1080, 108)}
        kwargs = {"target_width": 1080, "target_height": 1920}
        with patch.object(service, "_probe_video_size", side_effect=lambda path: sizes[path]):
            self.assertEqual("(H-h)*0.6500", service._kick_overlay_y(Path("v.mp4"), "bottom", [], ai_fraction=0.65, **kwargs))
            self.assertEqual("(H-h)*0.8000", service._kick_overlay_y(Path("v.mp4"), "bottom", [], ai_fraction=0.95, **kwargs))
            self.assertEqual("(H-h)*0.2000", service._kick_overlay_y(Path("v.mp4"), "bottom", [], ai_fraction=0.05, **kwargs))
            both = service._kick_overlay_y(Path("v.mp4"), "bottom", [], caption_position="BOTH", ai_fraction=None, **kwargs)
            self.assertEqual("(H-h)*0.5000", both)

    def test_fallback_avoids_watermark_but_ai_selection_is_not_vetoed(self) -> None:
        # Watermark text 38px tall at the middle of the frame (y 941-979), plus a 19px gap.
        service = self.make_service(kick_overlay_top_y=0.2, kick_overlay_bottom_y=0.8, watermark_top_y=0.5, watermark_bottom_y=0.5)
        sizes = {Path("v.mp4"): (1080, 1920), Path("overlay.png"): (1080, 108)}
        kwargs = {"target_width": 1080, "target_height": 1920}

        def strip_rows(expr: str) -> tuple[float, float]:
            y = float(expr.split("*")[1]) * 1812
            return y, y + 108

        with patch.object(service, "_probe_video_size", side_effect=lambda path: sizes[path]):
            for result in (
                service._kick_overlay_y(Path("v.mp4"), "bottom", [], caption_position="BOTH", **kwargs),
            ):
                top, bottom = strip_rows(result)
                self.assertTrue(bottom <= 922 or top >= 998, result)
                self.assertTrue(0.2 <= float(result.split("*")[1]) <= 0.8)
            self.assertEqual(
                "(H-h)*0.5000",
                service._kick_overlay_y(Path("v.mp4"), "bottom", [{"top": 0, "bottom": 1}], ai_fraction=0.5, **kwargs),
            )
            # Blurred source watermark at 0.70-0.74 of the frame: our watermark sits there instead.
            box = {"x": 0.3, "y": 0.7, "w": 0.3, "h": 0.04}
            top, bottom = strip_rows(service._kick_overlay_y(Path("v.mp4"), "bottom", [], watermark_box=box, **kwargs))
            self.assertTrue(bottom <= 0.7 * 1920 - 19.2 or top >= 0.74 * 1920 + 19.2)

    def test_kick_strip_is_drawn_after_the_watermark(self) -> None:
        with TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "overlay.png"
            overlay.write_bytes(b"dummy")
            service = self.make_service(overlay_image_path=overlay)
            with patch.object(service, "_kick_overlay_y", return_value="(H-h)*0.8000"), \
                 patch.object(service, "_probe_video_duration", return_value=None), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="", stdout="")) as run:
                actual = service._render_edited_video(Path("in.mp4"), Path(tmp) / "out.mp4", add_kick_overlay=True)
                self.assertEqual("(H-h)*0.8000", actual)
            command = run.call_args[0][0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertLess(graph.index("drawtext"), graph.index("[marked][logo]overlay"))
            self.assertTrue(graph.endswith("[outv]"))

    def test_render_edited_video_applies_trim(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            overlay = tmp_path / "overlay.png"
            overlay.write_bytes(b"dummy")
            service = self.make_service(
                overlay_image_path=overlay,
                trim_start_seconds=0.2,
                trim_end_seconds=0.3,
            )
            with patch.object(service, "_probe_video_duration", return_value=10.0), \
                 patch.object(service, "_probe_video_size", return_value=(1080, 1920)), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                service._render_edited_video(
                    tmp_path / "source.mp4",
                    tmp_path / "out.mp4",
                    add_kick_overlay=True,
                )
                cmd = mock_run.call_args.args[0]
                # Check trim args
                self.assertIn("-ss", cmd)
                ss_idx = cmd.index("-ss")
                self.assertEqual("0.200", cmd[ss_idx + 1])
                self.assertIn("-t", cmd)
                t_idx = cmd.index("-t")
                self.assertEqual("9.500", cmd[t_idx + 1])

                # Verify drawbox is not in filter_complex
                filter_complex_idx = cmd.index("-filter_complex")
                fc = cmd[filter_complex_idx + 1]
                self.assertNotIn("drawbox", fc)

    def test_render_edited_video_applies_audio_pitch(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = self.make_service(
                audio_pitch_scale=0.97,
            )
            with patch.object(service, "_probe_video_duration", return_value=10.0), \
                 patch.object(service, "_probe_video_size", return_value=(1080, 1920)), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                service._render_edited_video(
                    tmp_path / "source.mp4",
                    tmp_path / "out.mp4",
                    add_kick_overlay=False,
                )
                cmd = mock_run.call_args.args[0]
                self.assertIn("-af", cmd)
                af_idx = cmd.index("-af")
                self.assertEqual("rubberband=pitch=0.9700", cmd[af_idx + 1])


if __name__ == "__main__":
    unittest.main()
