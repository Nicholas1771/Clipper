from __future__ import annotations

from pathlib import Path
import sys


def has_source_kick_branding(video_path: Path, manifest: dict) -> bool:
    ai_root = Path(__file__).resolve().parents[3] / "ClipAI"
    source_root = str(ai_root / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    from clipai.config import AppConfig
    from clipai.service import ClipAIService

    return ClipAIService(AppConfig.from_repo_root(ai_root)).source_has_kick_branding(video_path, manifest)
