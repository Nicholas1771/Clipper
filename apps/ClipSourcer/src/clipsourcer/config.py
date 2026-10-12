from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from common.env import load_app_env, looks_like_windows_path, parse_bool, parse_csv


@dataclass(frozen=True)
class AppConfig:
    apify_token: str
    apify_actor_id: str
    source_mode: str
    source_hashtag: str
    source_profiles: list[str]
    keyword_filter: str
    poll_interval_minutes: float
    lookback_hours: int
    results_per_page: int
    minimum_view_count: int
    output_dir: Path
    state_file: Path
    source_history_file: Path
    source_schedule_file: Path
    download_video_files: bool
    authorized_download_profiles: list[str]
    max_source_runs_per_day: int
    min_source_gap_minutes: int
    source_target_posting_start_hour: int
    source_lead_minutes: int
    source_jitter_minutes: int
    source_grace_minutes: int
    source_attempts_per_slot: int
    source_timezone_name: str
    allow_download_bypass: bool = False

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)

        source_mode = os.getenv("SOURCE_MODE", "hashtag").strip().lower()
        if source_mode not in {"hashtag", "profiles"}:
            raise ValueError("SOURCE_MODE must be 'hashtag' or 'profiles'.")

        source_profiles = parse_csv(os.getenv("SOURCE_PROFILES", ""))
        if source_mode == "profiles" and not source_profiles:
            raise ValueError("SOURCE_PROFILES must contain at least one profile in profiles mode.")

        apify_token = os.getenv("APIFY_TOKEN", "").strip()
        if not apify_token:
            raise ValueError("APIFY_TOKEN is required.")

        output_default = os.getenv("QUEUE_DIR", "data/queue").strip()
        output_raw = os.getenv("OUTPUT_DIR", "").strip() or output_default
        if os.name != "nt" and looks_like_windows_path(output_raw):
            output_raw = output_default
        output_dir = Path(output_raw).expanduser()
        if not output_dir.is_absolute():
            output_dir = root_dir / output_dir
        state_raw = os.getenv("STATE_FILE", "data/state.json").strip()
        history_raw = os.getenv("SOURCE_HISTORY_FILE", "data/source_history.json").strip()
        schedule_raw = os.getenv("SOURCE_SCHEDULE_FILE", "data/source_schedule.json").strip()
        if os.name != "nt" and looks_like_windows_path(state_raw):
            state_raw = "data/state.json"
        if os.name != "nt" and looks_like_windows_path(history_raw):
            history_raw = "data/source_history.json"
        if os.name != "nt" and looks_like_windows_path(schedule_raw):
            schedule_raw = "data/source_schedule.json"
        state_file = Path(state_raw).expanduser()
        source_history_file = Path(history_raw).expanduser()
        source_schedule_file = Path(schedule_raw).expanduser()
        if not state_file.is_absolute():
            state_file = root_dir / state_file
        if not source_history_file.is_absolute():
            source_history_file = root_dir / source_history_file
        if not source_schedule_file.is_absolute():
            source_schedule_file = root_dir / source_schedule_file

        return cls(
            apify_token=apify_token,
            apify_actor_id=os.getenv("APIFY_ACTOR_ID", "clockworks~tiktok-scraper").strip(),
            source_mode=source_mode,
            source_hashtag=os.getenv("SOURCE_HASHTAG", "").strip().lstrip("#"),
            source_profiles=source_profiles,
            keyword_filter=os.getenv("KEYWORD_FILTER", "").strip(),
            poll_interval_minutes=float(os.getenv("POLL_INTERVAL_MINUTES", "60")),
            lookback_hours=int(os.getenv("LOOKBACK_HOURS", "4")),
            results_per_page=int(os.getenv("RESULTS_PER_PAGE", "25")),
            minimum_view_count=int(os.getenv("MINIMUM_VIEW_COUNT", "0")),
            output_dir=output_dir,
            state_file=state_file,
            source_history_file=source_history_file,
            source_schedule_file=source_schedule_file,
            download_video_files=parse_bool(os.getenv("DOWNLOAD_VIDEO_FILES", "false")),
            authorized_download_profiles=parse_csv(os.getenv("AUTHORIZED_DOWNLOAD_PROFILES", "")),
            max_source_runs_per_day=int(os.getenv("MAX_SOURCE_RUNS_PER_DAY", "1")),
            min_source_gap_minutes=int(os.getenv("MIN_SOURCE_GAP_MINUTES", "1200")),
            source_target_posting_start_hour=int(os.getenv("SOURCE_TARGET_POSTING_START_HOUR", "18")),
            source_lead_minutes=int(os.getenv("SOURCE_LEAD_MINUTES", "30")),
            source_jitter_minutes=int(os.getenv("SOURCE_JITTER_MINUTES", "15")),
            source_grace_minutes=int(os.getenv("SOURCE_GRACE_MINUTES", "90")),
            source_attempts_per_slot=int(os.getenv("SOURCE_ATTEMPTS_PER_SLOT", "3")),
            source_timezone_name=(
                os.getenv("SOURCE_TIMEZONE_NAME", "").strip()
                or os.getenv("TIMEZONE_NAME", "America/New_York").strip()
            ),
            allow_download_bypass=parse_bool(os.getenv("ALLOW_DOWNLOAD_BYPASS", "false")),
        )
