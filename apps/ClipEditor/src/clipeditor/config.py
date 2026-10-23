from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import platform

from common.env import load_app_env, looks_like_windows_path, resolve_asset_path


@dataclass(frozen=True)
class AppConfig:
    queue_dir: Path
    poll_interval_seconds: int
    ready_stability_seconds: int
    overlay_image_path: Path
    watermark_font_file: Path | None
    watermark_font_name: str
    watermark_text: str
    title_prefix: str
    timezone_name: str
    source_contrast: float = 1.0
    source_sharpness: float = 0.0
    watermark_top_y: float = 0.5
    watermark_bottom_y: float = 0.5
    kick_overlay_top_y: float = 0.1
    kick_overlay_bottom_y: float = 0.8
    watermark_opacity: float = 0.42
    watermark_size: float = 0.02
    trim_start_seconds: float = 0.0
    trim_end_seconds: float = 0.0
    audio_pitch_scale: float = 1.0

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)
        queue_raw = os.getenv("QUEUE_DIR", "data/queue").strip()
        if os.name != "nt" and looks_like_windows_path(queue_raw):
            queue_raw = "data/queue"
        queue_dir = Path(queue_raw).expanduser()
        if not queue_dir.is_absolute():
            queue_dir = root_dir / queue_dir
        overlay_raw = os.getenv("OVERLAY_IMAGE_PATH", "").strip()
        if os.name != "nt" and looks_like_windows_path(overlay_raw):
            overlay_raw = Path(overlay_raw.replace("\\", "/")).name
        overlay_image_path = resolve_asset_path(overlay_raw, root_dir, repo_root)
        raw_font_file = os.getenv("WATERMARK_FONT_FILE", "").strip()
        watermark_font_file: Path | None = None
        if raw_font_file:
            candidate_font = Path(raw_font_file).expanduser()
            if not candidate_font.is_absolute():
                candidate_font = repo_root / candidate_font
            if candidate_font.exists():
                watermark_font_file = candidate_font
        if watermark_font_file is None:
            for candidate in _default_font_candidates():
                if candidate.exists():
                    watermark_font_file = candidate
                    break
        return cls(
            queue_dir=queue_dir,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            ready_stability_seconds=int(os.getenv("READY_STABILITY_SECONDS", "5")),
            overlay_image_path=overlay_image_path,
            watermark_font_file=watermark_font_file,
            watermark_font_name=os.getenv("WATERMARK_FONT_NAME", _default_font_name()).strip() or _default_font_name(),
            watermark_text=os.getenv("ACCOUNT_NAME", "").strip(),
            title_prefix=os.getenv("TITLE_PREFIX", "Clip").strip() or "Clip",
            timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
            source_contrast=_float_env("SOURCE_CONTRAST", 1.0, 0.0, 3.0),
            source_sharpness=_float_env("SOURCE_SHARPNESS", 0.0, 0.0, 5.0),
            watermark_top_y=_float_env("WATERMARK_TOP_Y", 0.5, 0.0, 1.0),
            watermark_bottom_y=_float_env("WATERMARK_BOTTOM_Y", 0.5, 0.0, 1.0),
            kick_overlay_top_y=_float_env("KICK_OVERLAY_TOP_Y", 0.1, 0.0, 1.0),
            kick_overlay_bottom_y=_float_env("KICK_OVERLAY_BOTTOM_Y", 0.8, 0.0, 1.0),
            watermark_opacity=_float_env("WATERMARK_OPACITY", 0.42, 0.0, 1.0),
            watermark_size=_float_env("WATERMARK_SIZE", 0.02, 0.0, 1.0),
            trim_start_seconds=_float_env("TRIM_START_SECONDS", 0.0, 0.0, 3600.0),
            trim_end_seconds=_float_env("TRIM_END_SECONDS", 0.0, 0.0, 3600.0),
            audio_pitch_scale=_float_env("AUDIO_PITCH_SCALE", 1.0, 0.5, 2.0),
        )


def _default_font_candidates() -> list[Path]:
    system_name = platform.system().lower()
    if system_name == "windows":
        windows_root = os.getenv("WINDIR", "").strip() or os.getenv("SystemRoot", "").strip()
        windows_dir = Path(windows_root) if windows_root else Path()
        return [
            windows_dir / "Fonts" / "ariali.ttf",
            windows_dir / "Fonts" / "arial.ttf",
        ]
    if system_name == "darwin":
        return [
            Path("/System/Library/Fonts/Supplemental/Arial Italic.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        ]
    return [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/opentype/dejavu/DejaVuSans-Oblique.ttf"),
    ]


def _default_font_name() -> str:
    system_name = platform.system().lower()
    if system_name == "darwin":
        return "Arial"
    if system_name == "windows":
        return "Arial"
    return "DejaVu Sans"


def _float_env(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    return max(low, min(high, value))
