from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from common.env import load_app_env, looks_like_windows_path, read_dotenv, resolve_asset_path


@dataclass(frozen=True)
class AppConfig:
    queue_dir: Path
    poll_interval_seconds: int
    google_api_key: str
    gemini_model: str
    timeout_seconds: int
    timezone_name: str
    prompts_dir: Path | None = None
    overlay_reference_image_path: Path | None = None
    watermark_model: str = "gemini-2.5-flash"
    kick_placement_model: str = "gemini-2.5-flash"
    context_model: str = "gemini-2.5-flash"
    streamer_name: str = ""
    youtube_selection_mode: str = "shared"
    kick_overlay_top_y: float = 0.1
    kick_overlay_bottom_y: float = 0.8
    watermark_top_y: float = 0.5
    watermark_bottom_y: float = 0.5
    watermark_size: float = 0.02
    watermark_opacity: float = 0.42
    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)
        queue_raw = os.getenv("QUEUE_DIR", "data/queue").strip()
        if os.name != "nt" and looks_like_windows_path(queue_raw):
            queue_raw = "data/queue"
        queue_dir = Path(queue_raw).expanduser()
        if not queue_dir.is_absolute():
            queue_dir = root_dir / queue_dir
        overlay_reference_image_path = os.getenv("OVERLAY_IMAGE_PATH", "").strip()
        if os.name != "nt" and looks_like_windows_path(overlay_reference_image_path):
            overlay_reference_image_path = Path(overlay_reference_image_path.replace("\\", "/")).name
        overlay_path = resolve_asset_path(overlay_reference_image_path, root_dir, repo_root)
        prompts_dir = Path(os.getenv("AI_PROMPTS_DIR", "data/prompts")).expanduser()
        if not prompts_dir.is_absolute():
            prompts_dir = root_dir / prompts_dir
        return cls(
            queue_dir=queue_dir,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            google_api_key=os.getenv("GOOGLE_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").strip(),
            timeout_seconds=int(os.getenv("TIMEOUT_SECONDS", "180")),
            timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
            prompts_dir=prompts_dir if prompts_dir.exists() else None,
            overlay_reference_image_path=overlay_path if overlay_path.exists() else None,
            watermark_model=os.getenv("WATERMARK_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            kick_placement_model=os.getenv("KICK_PLACEMENT_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            streamer_name=os.getenv("STREAMER_NAME", "").strip(),
            context_model=os.getenv("CONTEXT_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
            youtube_selection_mode=os.getenv("YOUTUBE_SELECTION_MODE", "shared").strip().lower() or "shared",
            **_placement_settings(root_dir),
        )


def _placement_settings(root_dir: Path) -> dict[str, float]:
    """Kick strip and watermark placement, read the same way ClipEditor reads them (its .env backs up the process env)."""
    editor_env = read_dotenv(root_dir / "apps" / "ClipEditor" / ".env")

    def value(name: str, default: float) -> float:
        raw = os.getenv(name, "").strip() or str(editor_env.get(name, "")).strip()
        try:
            return max(0.0, min(1.0, float(raw))) if raw else default
        except ValueError:
            return default

    return {
        "kick_overlay_top_y": value("KICK_OVERLAY_TOP_Y", 0.1),
        "kick_overlay_bottom_y": value("KICK_OVERLAY_BOTTOM_Y", 0.8),
        "watermark_top_y": value("WATERMARK_TOP_Y", 0.5),
        "watermark_bottom_y": value("WATERMARK_BOTTOM_Y", 0.5),
        "watermark_size": value("WATERMARK_SIZE", 0.02),
        "watermark_opacity": value("WATERMARK_OPACITY", 0.42),
    }
