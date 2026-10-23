from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os

from common.env import load_app_env, looks_like_windows_path
from common.creators import Creator, configured_creators


@dataclass(frozen=True)
class AppConfig:
    queue_dir: Path
    poll_interval_seconds: int
    schedule_start_hour: int
    schedule_end_hour: int
    instagram_posts_per_day: int
    min_gap_minutes: int
    timezone_name: str
    youtube_posts_per_day: int = 0
    youtube_selection_mode: str = "shared"
    youtube_start_hour: int = 10
    youtube_end_hour: int = 12
    daily_report_time: str = "23:00"
    creators: tuple[Creator, ...] = ()

    def __post_init__(self) -> None:
        try:
            parsed = datetime.strptime(self.daily_report_time, "%H:%M")
            if parsed.strftime("%H:%M") != self.daily_report_time:
                raise ValueError
        except ValueError as error:
            raise ValueError("DAILY_REPORT_TIME must be HH:MM in 24-hour time (00:00-23:59).") from error

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)
        youtube_mode = os.getenv("YOUTUBE_SELECTION_MODE", "shared").strip()
        if youtube_mode not in {"shared", "instagram_top"}:
            raise ValueError("YOUTUBE_SELECTION_MODE must be shared or instagram_top.")
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
            instagram_posts_per_day=int(os.getenv("INSTAGRAM_POSTS_PER_DAY", "0")),
            min_gap_minutes=int(os.getenv("MIN_GAP_MINUTES", "75")),
            timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
            youtube_posts_per_day=int(os.getenv("YOUTUBE_POSTS_PER_DAY", "").strip() or "0"),
            youtube_selection_mode=youtube_mode,
            youtube_start_hour=int(os.getenv("YOUTUBE_SCHEDULE_START_HOUR", "10")),
            youtube_end_hour=int(os.getenv("YOUTUBE_SCHEDULE_END_HOUR", "12")),
            daily_report_time=os.getenv("DAILY_REPORT_TIME", "23:00").strip(),
            creators=configured_creators(root_dir),
        )
