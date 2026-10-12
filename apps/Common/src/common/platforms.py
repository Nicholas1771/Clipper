from __future__ import annotations

import json
from pathlib import Path

INSTAGRAM = "instagram"
YOUTUBE = "youtube"
PLATFORMS = (INSTAGRAM, YOUTUBE)


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
        return [INSTAGRAM]
    return normalize_platforms(state.get("platforms") if isinstance(state, dict) else None)
