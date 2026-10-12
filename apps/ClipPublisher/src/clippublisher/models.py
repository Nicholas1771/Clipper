from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QueueItem:
    run_dir: Path
    manifest_path: Path
    video_path: Path
    clip_id: str
    caption: str
    author_handle: str
    description: str = ""
    youtube_title: str = ""
    youtube_description: str = ""
