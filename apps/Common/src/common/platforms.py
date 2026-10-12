from __future__ import annotations

import json
import os
from pathlib import Path

INSTAGRAM = "instagram"
YOUTUBE = "youtube"
PLATFORMS = (INSTAGRAM, YOUTUBE)
PLATFORM_LABELS = {INSTAGRAM: "Instagram", YOUTUBE: "YouTube"}
PUBLISH_ONLY_ENV = "CLIPPER_PUBLISH_ONLY"


def publish_only() -> str | None:
    """The single platform to publish to when the pipeline was launched with --publish, else None."""
    value = os.getenv(PUBLISH_ONLY_ENV, "").strip().lower()
    return value if value in PLATFORMS else None


def normalize_platforms(value: object) -> list[str]:
    """Known platform names in canonical order; a missing/invalid value means the legacy Instagram-only slot."""
    if not isinstance(value, list):
        return [INSTAGRAM]
    requested = {str(item).strip().lower() for item in value}
    return [platform for platform in PLATFORMS if platform in requested]


def read_slot_platforms(queue_dir: Path) -> list[str]:
    """Platforms the scheduler assigned to the slot that is currently moving through the pipeline."""
    try:
        state = json.loads((queue_dir / "scheduler_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    platforms = normalize_platforms(state.get("platforms") if isinstance(state, dict) else None)
    only = publish_only()
    return [only] if only else platforms
