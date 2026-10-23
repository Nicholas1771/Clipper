from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from common.env import load_app_env, looks_like_windows_path, parse_bool
from common.creators import Creator, configured_creators


@dataclass(frozen=True)
class AppConfig:
    apify_token: str
    apify_actor_id: str
    source_mode: str
    source_hashtag: str
    source_profiles: list[str]
    keyword_filter: str
    candidate_cache_file: Path
    poll_interval_minutes: float
    lookback_hours: int
    results_per_page: int
    minimum_view_count: int
    output_dir: Path
    state_file: Path
    download_video_files: bool
    authorized_download_profiles: list[str]
    source_attempts_per_slot: int
    source_timezone_name: str
    allow_download_bypass: bool = False
    require_source_kick_branding: bool = False
    creators: tuple[Creator, ...] = ()

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "AppConfig":
        root_dir = load_app_env(repo_root)

        apify_token = os.getenv("APIFY_TOKEN", "").strip()
        if not apify_token:
            raise ValueError("APIFY_TOKEN is required.")

        output_raw = os.getenv("QUEUE_DIR", "data/queue").strip()
        if os.name != "nt" and looks_like_windows_path(output_raw):
            raise ValueError("QUEUE_DIR must be a Linux-compatible path on this host.")
        output_dir = Path(output_raw).expanduser()
        if not output_dir.is_absolute():
            output_dir = root_dir / output_dir
        state_raw = os.getenv("STATE_FILE", "data/state.json").strip()
        cache_raw = os.getenv("CANDIDATE_CACHE_FILE", "").strip() or "data/candidate_cache.json"
        if os.name != "nt" and looks_like_windows_path(state_raw):
            state_raw = "data/state.json"
        if os.name != "nt" and looks_like_windows_path(cache_raw):
            cache_raw = "data/candidate_cache.json"
        state_file = Path(state_raw).expanduser()
        candidate_cache_file = Path(cache_raw).expanduser()
        if not state_file.is_absolute():
            state_file = root_dir / state_file
        if not candidate_cache_file.is_absolute():
            candidate_cache_file = root_dir / candidate_cache_file

        return cls(
            apify_token=apify_token,
            apify_actor_id=os.getenv("APIFY_ACTOR_ID", "clockworks~tiktok-scraper").strip(),
            source_mode="hashtag",
            source_hashtag="",
            source_profiles=[],
            keyword_filter="",
            candidate_cache_file=candidate_cache_file,
            poll_interval_minutes=float(os.getenv("POLL_INTERVAL_MINUTES", "60")),
            lookback_hours=int(os.getenv("LOOKBACK_HOURS", "4")),
            results_per_page=int(os.getenv("RESULTS_PER_PAGE", "25")),
            minimum_view_count=int(os.getenv("MINIMUM_VIEW_COUNT", "0")),
            output_dir=output_dir,
            state_file=state_file,
            download_video_files=False,
            authorized_download_profiles=[],
            source_attempts_per_slot=int(os.getenv("SOURCE_ATTEMPTS_PER_SLOT", "3")),
            source_timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
            allow_download_bypass=parse_bool(os.getenv("ALLOW_DOWNLOAD_BYPASS", "false")),
            require_source_kick_branding=False,
            creators=configured_creators(root_dir),
        )
