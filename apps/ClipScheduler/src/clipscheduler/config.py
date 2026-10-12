from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from common.env import load_app_env, looks_like_windows_path


@dataclass(frozen=True)
class AppConfig:
    queue_dir: Path
    poll_interval_seconds: int
    schedule_start_hour: int
    schedule_end_hour: int
    schedule_peak_hour: int
    posts_per_day: int
    min_gap_minutes: int
    timezone_name: str

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)
        queue_raw = os.getenv("QUEUE_DIR", "data/queue").strip()
        if os.name != "nt" and looks_like_windows_path(queue_raw):
            queue_raw = "data/queue"
        queue_dir = Path(queue_raw).expanduser()
        if not queue_dir.is_absolute():
            queue_dir = root_dir / queue_dir
        return cls(
            queue_dir=queue_dir,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
            schedule_start_hour=int(os.getenv("SCHEDULE_START_HOUR", "17")),
            schedule_end_hour=int(os.getenv("SCHEDULE_END_HOUR", "19")),
            schedule_peak_hour=int(os.getenv("SCHEDULE_PEAK_HOUR", "18")),
            posts_per_day=int(os.getenv("POSTS_PER_DAY", "5")),
            min_gap_minutes=int(os.getenv("MIN_GAP_MINUTES", "75")),
            timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
        )
