from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clipeditor.config import AppConfig
from imageio_ffmpeg import get_ffmpeg_exe

logger = logging.getLogger(__name__)


def load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class ClipEditorService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._tz = ZoneInfo(config.timezone_name)

    def _queue_state_path(self, run_dir: Path) -> Path:
        return run_dir / "queue_state.json"

    def _first_media_file(self, directory: Path, suffix: str) -> Path | None:
        if not directory.exists():
            return None
        matches = sorted(directory.glob(f"*{suffix}"), key=lambda path: path.name)
        return matches[0] if matches else None

    def _read_source_manifest(self, run_dir: Path) -> dict[str, Any]:
        source_dir = run_dir / "source"
        manifest_path = self._first_media_file(source_dir, ".json")
        if manifest_path is None:
            manifest_path = self._first_media_file(run_dir, ".json")
        if manifest_path is None:
            return {}
        return load_json(manifest_path)

    def _source_video_path(self, run_dir: Path) -> Path | None:
        source_dir = run_dir / "source"
        video_path = self._first_media_file(source_dir, ".mp4")
        if video_path is None:
            video_path = self._first_media_file(run_dir, ".mp4")
        return video_path

    def _metadata_for_queue(self, run_dir: Path, manifest: dict[str, Any]) -> tuple[str, str, str]:
        author_handle = str(manifest.get("author_handle") or "clip").strip()
        text = str(manifest.get("text") or "").strip()
        hashtags = manifest.get("hashtags") or []
        tags = ", ".join(str(tag).strip() for tag in hashtags if str(tag).strip())
        caption = text or f"@{author_handle} clip"
        title = f"{self.config.title_prefix} - @{author_handle}".strip()
        ai_summary_path = run_dir / "ai" / "summary.txt"
        description = ai_summary_path.read_text(encoding="utf-8").strip() if ai_summary_path.exists() else (text or f"@{author_handle} clip")
        if not ai_summary_path.exists() and tags:
            description += f"\n\n#{tags}"
        return caption, title, description

    def _write_metadata_files(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        editor_dir = run_dir / "editor"
        editor_dir.mkdir(parents=True, exist_ok=True)
        caption, title, description = self._metadata_for_queue(run_dir, manifest)
        (editor_dir / "caption.txt").write_text(caption, encoding="utf-8")
        (editor_dir / "title.txt").write_text(title, encoding="utf-8")
        (editor_dir / "description.txt").write_text(description, encoding="utf-8")
        for name in ("youtube_title.txt", "youtube_description.txt"):
            ai_file = run_dir / "ai" / name
            if ai_file.exists():
                (editor_dir / name).write_text(ai_file.read_text(encoding="utf-8").strip(), encoding="utf-8")

    def _video_dimensions(self, source_video: Path) -> tuple[int, int] | None:
        ffmpeg_exe = str(get_ffmpeg_exe())
        probe = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-i", str(source_video), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        stderr = probe.stderr or ""
        match = re.search(r"Video:.*?,\s(\d{2,5})x(\d{2,5})[,\s]", stderr)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def _drawtext_font_argument(self) -> str:
        if self.config.watermark_font_file is not None:
            escaped = str(self.config.watermark_font_file).replace("\\", "/").replace(":", "\\:")
            return f"fontfile='{escaped}'"
        font_name = self.config.watermark_font_name.replace("'", "\\'")
        return f"font='{font_name}'"

    def _source_enhance_filters(self) -> str:
        filters = ""
        contrast = self.config.source_contrast
        sharpness = self.config.source_sharpness
        if abs(contrast - 1.0) > 1e-6:
            filters += f",eq=contrast={contrast:.3f}"
        if sharpness > 1e-6:
            filters += f",unsharp=5:5:{sharpness:.3f}:5:5:0.0"
        return filters

    def _probe_video_size(self, video_path: Path) -> tuple[int, int] | None:
        completed = subprocess.run([str(get_ffmpeg_exe()), "-i", str(video_path)], capture_output=True, text=True)
        match = re.search(r"Stream #.*Video:.*?[ ,](\d{2,5})x(\d{2,5})[ ,\[]", completed.stderr)
        return (int(match.group(1)), int(match.group(2))) if match else None

    def _plan_source_watermark_cover(
        self, video_path: Path, box: Any, *, target_width: int, target_height: int
    ) -> tuple[str, tuple[float, float]] | None:
        """Return (delogo filter, watermark center in output pixels) for a normalized x/y/w/h box."""
        if not isinstance(box, dict):
            return None
        size = self._probe_video_size(video_path)
        if size is None:
            logger.warning("Could not read the size of %s; skipping source watermark cover.", video_path)
            return None
        source_width, source_height = size
        try:
            bx, by, bw, bh = (float(box[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            return None
        pad_x, pad_y = bw * 0.15, bh * 0.25
        left = max(2, int((bx - pad_x) * source_width))
        top = max(2, int((by - pad_y) * source_height))
        right = min(source_width - 2, int((bx + bw + pad_x) * source_width))
        bottom = min(source_height - 2, int((by + bh + pad_y) * source_height))
        if right - left < 4 or bottom - top < 4:
            return None
        scale = min(target_width / source_width, target_height / source_height)
        offset_x = (target_width - source_width * scale) / 2
        offset_y = (target_height - source_height * scale) / 2
        center = (
            offset_x + scale * (left + right) / 2,
            offset_y + scale * (top + bottom) / 2,
        )
        width, height = right - left, bottom - top
        return f"delogo=x={left}:y={top}:w={width}:h={height}", center

    def _watermark_filter(
        self,
        input_label: str,
        overlay_position: str = "bottom",
        anchor: tuple[float, float] | None = None,
    ) -> str:
        """Drawtext for the watermark. Y: 0=top..1=bottom (separate values for the top/bottom Kick overlay placement), opacity: 0..1, size: fraction of frame height."""
        opacity = self.config.watermark_opacity
        font_size = round(self.config.watermark_size * 1920)
        if opacity <= 0 or font_size < 1:
            return f"[{input_label}]null[outv]"
        watermark_y = self.config.watermark_top_y if overlay_position == "top" else self.config.watermark_bottom_y
        if anchor is not None:
            # Sit on top of the blurred source watermark, kept inside the frame.
            x_expr = f"min(max({anchor[0]:.1f}-text_w/2,0),w-text_w)"
            y_expr = f"min(max({anchor[1]:.1f}-text_h/2,0),h-text_h)"
        else:
            x_expr = "(w-text_w)/2"
            y_expr = f"(h-text_h)*{watermark_y:.3f}"
        shadow_alpha = min(1.0, 0.6 * opacity)
        border_alpha = min(1.0, 0.24 * opacity)
        return (
            f"[{input_label}]drawtext=text='{self.config.watermark_text}':{self._drawtext_font_argument()}:"
            f"fontcolor=white@{opacity:.3f}:fontsize={font_size}:"
            f"x='{x_expr}':y='{y_expr}':"
            f"shadowcolor=black@{shadow_alpha:.3f}:shadowx=1:shadowy=1:bordercolor=black@{border_alpha:.3f}:borderw=1[outv]"
        )

    def _render_edited_video(
        self,
        source_video: Path,
        output_path: Path,
        *,
        add_kick_overlay: bool = True,
        overlay_position: str = "bottom",
        source_watermark_box: dict[str, Any] | None = None,
    ) -> None:
        ffmpeg_exe = str(get_ffmpeg_exe())
        target_width = 1080
        target_height = 1920
        source_enhance = self._source_enhance_filters()
        source_cover = ""
        watermark_anchor = None
        if source_watermark_box:
            plan = self._plan_source_watermark_cover(
                source_video, source_watermark_box, target_width=target_width, target_height=target_height
            )
            if plan is not None:
                source_cover = plan[0] + ","
                watermark_anchor = plan[1]
        if add_kick_overlay:
            if not self.config.overlay_image_path.exists():
                raise FileNotFoundError(f"Overlay image does not exist: {self.config.overlay_image_path}")
            overlay_fraction = (
                self.config.kick_overlay_top_y if overlay_position == "top" else self.config.kick_overlay_bottom_y
            )
            overlay_y = f"(H-h)*{overlay_fraction:.4f}"
            filter_complex = (
                f"[0:v]{source_cover}scale={target_width}:{target_height}:force_original_aspect_ratio=decrease{source_enhance},pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base];"
                f"[1:v]scale={target_width}:-1,setsar=1[logo];"
                f"[base][logo]overlay=x=(W-w)/2:y='{overlay_y}':format=auto[withlogo];"
                + self._watermark_filter("withlogo", overlay_position, watermark_anchor)
            )
            input_images = [
                "-i",
                str(self.config.overlay_image_path),
            ]
            output_map = [
                "-map",
                "[outv]",
            ]
        else:
            filter_complex = (
                f"[0:v]{source_cover}scale={target_width}:{target_height}:force_original_aspect_ratio=decrease{source_enhance},pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base];"
                + self._watermark_filter("base", overlay_position, watermark_anchor)
            )
            input_images = []
            output_map = [
                "-map",
                "[outv]",
            ]
        command = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(source_video),
        ]
        command.extend(input_images)
        command.extend([
            "-filter_complex",
            filter_complex,
        ])
        command.extend(output_map)
        command.extend([
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ])
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown ffmpeg error"
            raise RuntimeError(f"Failed to overlay clip: {stderr[-600:]}")

    def _ready_items(self) -> list[Path]:
        items: list[Path] = []
        if not self.config.queue_dir.exists():
            return items
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state = load_json(self._queue_state_path(child))
            if state.get("phase") != "ai_processed":
                continue
            ai_analysis_path = child / "ai" / "analysis.json"
            summary_path = child / "ai" / "summary.txt"
            if not ai_analysis_path.exists() or not summary_path.exists():
                logger.info("Skipping %s because ClipAI output is not complete yet.", child.name)
                continue
            if self._source_video_path(child) is None:
                continue
            items.append(child)
        return items

    def _mark_edited(self, run_dir: Path, edited_video_path: Path) -> None:
        payload = {
            "phase": "edited",
            "next_stage": "publish",
            "updated_at": datetime.now(self._tz).isoformat(),
            "editor_video": edited_video_path.relative_to(run_dir).as_posix(),
        }
        write_json(self._queue_state_path(run_dir), payload)

        scheduler_state_path = self.config.queue_dir / "scheduler_state.json"
        scheduler_state_path.parent.mkdir(parents=True, exist_ok=True)
        existing_scheduler_state = load_json(scheduler_state_path)
        scheduler_payload = existing_scheduler_state if isinstance(existing_scheduler_state, dict) else {}
        scheduler_payload.update(
            {
                "date": datetime.now(self._tz).date().isoformat(),
                "phase": "edited",
                "next_stage": "publish",
                "updated_at": datetime.now(self._tz).isoformat(),
            }
        )
        scheduler_state_path.write_text(json.dumps(scheduler_payload, indent=2), encoding="utf-8")

    def _has_pending_ai_stage(self) -> bool:
        if not self.config.queue_dir.exists():
            return False
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state = load_json(self._queue_state_path(child))
            if isinstance(state, dict) and state.get("phase") in {"ai_processed", "edited"}:
                return True
        return False

    def process_once(self) -> bool:
        for run_dir in self._ready_items():
            state = load_json(self._queue_state_path(run_dir))
            if state.get("phase") == "edited":
                continue
            ai_state = load_json(run_dir / "ai" / "analysis.json")
            if not isinstance(ai_state, dict):
                logger.info("Skipping %s because ClipAI analysis is missing.", run_dir.name)
                continue
            video_path = self._source_video_path(run_dir)
            if video_path is None:
                continue
            manifest = self._read_source_manifest(run_dir)
            add_kick_overlay = not bool(ai_state.get("has_kick_overlay", False))
            caption_position = str(ai_state.get("caption_position") or "").strip().upper()
            has_caption = bool(ai_state.get("has_caption", False))
            overlay_position = "top" if add_kick_overlay and has_caption and caption_position == "BOTTOM" else "bottom"
            editor_dir = run_dir / "editor"
            editor_dir.mkdir(parents=True, exist_ok=True)
            edited_path = editor_dir / f"{video_path.stem}_edited.mp4"
            self._render_edited_video(
                video_path,
                edited_path,
                add_kick_overlay=add_kick_overlay,
                overlay_position=overlay_position,
                source_watermark_box=ai_state.get("source_watermark_box") if ai_state.get("has_source_watermark") else None,
            )
            self._write_metadata_files(run_dir, manifest)
            self._mark_edited(run_dir, edited_path)
            logger.info(
                "Completed ClipEditor processing for %s. add_kick_overlay=%s, overlay_position=%s",
                run_dir.name,
                add_kick_overlay,
                overlay_position,
            )
            return True
        if self._has_pending_ai_stage():
            logger.info("ClipEditor is waiting for an ai_processed queue item to reach the editor stage.")
        return False

    def run_forever(self) -> None:
        logger.info("Watching %s for sourced queue items.", self.config.queue_dir)
        while True:
            self.process_once()
            time.sleep(self.config.poll_interval_seconds)
