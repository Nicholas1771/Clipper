from __future__ import annotations

from common.creators import config_for_run

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
        self._base_config = config
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

    def _probe_video_info(self, video_path: Path) -> tuple[tuple[int, int] | None, float | None]:
        completed = subprocess.run([str(get_ffmpeg_exe()), "-i", str(video_path)], capture_output=True, text=True)
        size = None
        duration = None
        size_match = re.search(r"Stream #.*Video:.*?[ ,](\d{2,5})x(\d{2,5})[ ,\[]", completed.stderr)
        if size_match:
            size = (int(size_match.group(1)), int(size_match.group(2)))
        dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr)
        if dur_match:
            hours, minutes, seconds = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
            duration = hours * 3600 + minutes * 60 + seconds
        return size, duration

    def _probe_video_size(self, video_path: Path) -> tuple[int, int] | None:
        size, _ = self._probe_video_info(video_path)
        return size

    def _probe_video_duration(self, video_path: Path) -> float | None:
        _, duration = self._probe_video_info(video_path)
        return duration

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
        # The box is measured from the video and already includes a few pixels of slack; only a hairline
        # margin is added so delogo samples its border colours from outside the watermark's soft edges.
        margin = 3
        left = max(2, int(bx * source_width) - margin)
        top = max(2, int(by * source_height) - margin)
        right = min(source_width - 2, int((bx + bw) * source_width) + margin)
        bottom = min(source_height - 2, int((by + bh) * source_height) + margin)
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

    def _kick_overlay_y(
        self,
        source_video: Path,
        overlay_position: str,
        caption_bands: Any,
        *,
        target_width: int,
        target_height: int,
        caption_position: str = "",
        ai_fraction: Any = None,
        watermark_box: dict[str, Any] | None = None,
    ) -> str:
        """Honor AI's position within the configured range without content-based vetoes.

        Fallback only: the target spot (middle of the range when captions sit at
        top and bottom, else the configured top/bottom spot) moved to the nearest place clear of the caption text
        bands; otherwise the nearest place clear of the watermark only; otherwise the target spot.
        """
        low = min(self.config.kick_overlay_top_y, self.config.kick_overlay_bottom_y)
        high = max(self.config.kick_overlay_top_y, self.config.kick_overlay_bottom_y)
        if caption_position == "BOTH":
            target = (low + high) / 2
        else:
            target = self.config.kick_overlay_top_y if overlay_position == "top" else self.config.kick_overlay_bottom_y
        target_expr = f"(H-h)*{target:.4f}"
        ai_pick = None
        if isinstance(ai_fraction, (int, float)) and not isinstance(ai_fraction, bool):
            ai_pick = max(low, min(high, float(ai_fraction)))
        if ai_pick is not None:
            logger.info("Placing the Kick overlay at ClipAI's selected spot %.4f.", ai_pick)
            return f"(H-h)*{ai_pick:.4f}"

        source_size = self._probe_video_size(source_video)
        overlay_size = self._probe_video_size(self.config.overlay_image_path)
        if source_size is None or overlay_size is None:
            return f"(H-h)*{ai_pick:.4f}" if ai_pick is not None else target_expr
        overlay_height = round(target_width * overlay_size[1] / overlay_size[0])
        scale = min(target_width / source_size[0], target_height / source_size[1])
        offset_y = (target_height - source_size[1] * scale) / 2
        max_y = target_height - overlay_height
        if max_y <= 0:
            return target_expr

        def to_output(fraction: float) -> float:
            return offset_y + fraction * source_size[1] * scale

        keep_out = self._watermark_rows(overlay_position, watermark_box, to_output, target_height)
        text_rows: list[tuple[float, float]] = []
        bands = caption_bands if isinstance(caption_bands, list) else []
        for band in bands:
            try:
                text_rows.append((to_output(float(band["top"])), to_output(float(band["bottom"]))))
            except (KeyError, TypeError, ValueError):
                continue

        def clear_of(y: float, rows: list[tuple[float, float]]) -> bool:
            return max_y * low - 0.01 <= y <= max_y * high + 0.01 and all(
                y + overlay_height <= top or y >= bottom for top, bottom in rows
            )

        def nearest(goal: float, rows: list[tuple[float, float]]) -> float | None:
            options = [goal * max_y, max_y * low, max_y * high]
            options += [edge for top, bottom in rows for edge in (top - overlay_height, bottom)]
            clear = [y for y in options if clear_of(y, rows)]
            return min(clear, key=lambda y: abs(y - goal * max_y)) / max_y if clear else None

        chosen = nearest(target, keep_out + text_rows)
        if chosen is None:
            chosen = nearest(target, keep_out)
        if chosen is None:
            logger.info("No spot in range is clear of the watermark and captions; using the configured Kick position.")
            return target_expr
        if abs(chosen - target) * max_y > 0.5:
            logger.info("Moved the Kick overlay from %.4f to %.4f to avoid captions and the watermark.", target, chosen)
        elif caption_position == "BOTH":
            logger.info("Captions at top and bottom; placing the Kick overlay in the middle (%.4f).", target)
        return f"(H-h)*{chosen:.4f}"

    def _watermark_rows(
        self, overlay_position: str, watermark_box: dict[str, Any] | None, to_output: Any, target_height: int
    ) -> list[tuple[float, float]]:
        """Output rows used by our account watermark (and the blurred source watermark it sits on)."""
        font_size = round(self.config.watermark_size * target_height)
        has_text = self.config.watermark_opacity > 0 and font_size >= 1
        margin = 0.01 * target_height
        if isinstance(watermark_box, dict):
            try:
                top = to_output(float(watermark_box["y"]))
                bottom = to_output(float(watermark_box["y"]) + float(watermark_box["h"]))
            except (KeyError, TypeError, ValueError):
                top = bottom = None
            if top is not None:
                center = (top + bottom) / 2
                if has_text:
                    top, bottom = min(top, center - font_size / 2), max(bottom, center + font_size / 2)
                return [(top - margin, bottom + margin)]
        if not has_text:
            return []
        watermark_y = self.config.watermark_top_y if overlay_position == "top" else self.config.watermark_bottom_y
        y = (target_height - font_size) * watermark_y
        return [(y - margin, y + font_size + margin)]

    def _watermark_filter(
        self,
        input_label: str,
        overlay_position: str = "bottom",
        anchor: tuple[float, float] | None = None,
        output_label: str = "outv",
    ) -> str:
        """Drawtext for the watermark. Y: 0=top..1=bottom (separate values for the top/bottom Kick overlay placement), opacity: 0..1, size: fraction of frame height."""
        opacity = self.config.watermark_opacity
        font_size = round(self.config.watermark_size * 1920)
        if opacity <= 0 or font_size < 1:
            return f"[{input_label}]null[{output_label}]"
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
            f"shadowcolor=black@{shadow_alpha:.3f}:shadowx=1:shadowy=1:bordercolor=black@{border_alpha:.3f}:borderw=1[{output_label}]"
        )

    def _render_edited_video(
        self,
        source_video: Path,
        output_path: Path,
        *,
        add_kick_overlay: bool = True,
        overlay_position: str = "bottom",
        source_watermark_box: dict[str, Any] | None = None,
        caption_bands: Any = None,
        caption_position: str = "",
        kick_overlay_fraction: Any = None,
    ) -> str | None:
        ffmpeg_exe = str(get_ffmpeg_exe())
        target_width = 1080
        target_height = 1920
        source_enhance = self._source_enhance_filters()
        source_cover = ""
        watermark_anchor = None
        overlay_y = None
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
            overlay_y = self._kick_overlay_y(
                source_video,
                overlay_position,
                caption_bands,
                target_width=target_width,
                target_height=target_height,
                caption_position=caption_position,
                ai_fraction=kick_overlay_fraction,
                watermark_box=source_watermark_box if watermark_anchor is not None else None,
            )
            # The Kick strip goes on last so it is always the top layer.
            filter_complex = (
                f"[0:v]{source_cover}scale={target_width}:{target_height}:force_original_aspect_ratio=decrease{source_enhance},pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base];"
                + self._watermark_filter("base", overlay_position, watermark_anchor, output_label="marked")
                + f";[1:v]scale={target_width}:-1,setsar=1[logo];"
                f"[marked][logo]overlay=x=(W-w)/2:y='{overlay_y}':format=auto[outv]"
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

        trim_start = max(0.0, self.config.trim_start_seconds)
        trim_end = max(0.0, self.config.trim_end_seconds)
        trim_args: list[str] = []
        if trim_start > 0 or trim_end > 0:
            source_duration = self._probe_video_duration(source_video)
            if source_duration is not None:
                if source_duration > (trim_start + trim_end):
                    target_duration = source_duration - trim_start - trim_end
                    if trim_start > 0:
                        trim_args.extend(["-ss", f"{trim_start:.3f}"])
                    trim_args.extend(["-t", f"{target_duration:.3f}"])
                else:
                    logger.warning(
                        "Video duration (%.2fs) shorter than requested trims (start=%.2fs, end=%.2fs); skipping trim.",
                        source_duration,
                        trim_start,
                        trim_end,
                    )
            elif trim_start > 0:
                trim_args.extend(["-ss", f"{trim_start:.3f}"])

        command = [
            ffmpeg_exe,
            "-y",
        ]
        command.extend(trim_args)
        command.extend([
            "-i",
            str(source_video),
        ])
        command.extend(input_images)
        command.extend([
            "-filter_complex",
            filter_complex,
        ])
        audio_args: list[str] = ["-map", "0:a?"]
        pitch = self.config.audio_pitch_scale
        if abs(pitch - 1.0) > 1e-4:
            audio_args.extend(["-af", f"rubberband=pitch={pitch:.4f}"])

        command.extend(output_map)
        command.extend(audio_args)
        command.extend([
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
        return overlay_y

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
            self.config = config_for_run(self._base_config, run_dir)
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
            overlay_y = self._render_edited_video(
                video_path,
                edited_path,
                add_kick_overlay=add_kick_overlay,
                overlay_position=overlay_position,
                source_watermark_box=ai_state.get("source_watermark_box") if ai_state.get("has_source_watermark") else None,
                caption_bands=ai_state.get("caption_bands") if has_caption else None,
                caption_position=caption_position if has_caption else "",
                kick_overlay_fraction=ai_state.get("kick_overlay_y"),
            )
            branding: dict[str, Any] = {"mode": "Original" if not add_kick_overlay else "Manual"}
            if add_kick_overlay:
                branding["overlay_y"] = overlay_y
                candidates = ai_state.get("kick_overlay_candidates") or []
                branding["placement"] = next(
                    (
                        index for index, fraction in enumerate(candidates, start=1)
                        if isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
                        and overlay_y == f"(H-h)*{fraction:.4f}"
                    ),
                    None,
                )
                if branding["placement"] is None:
                    logger.warning("Kick branding for %s used an unnumbered fallback position.", run_dir.name)
            write_json(editor_dir / "branding.json", branding)
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
