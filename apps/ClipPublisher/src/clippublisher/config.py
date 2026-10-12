from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from common.env import load_app_env, looks_like_windows_path, parse_bool
from clippublisher.public_url import resolve_media_base_url

@dataclass(frozen=True)
class AppConfig:
    queue_dir: Path
    poll_interval_seconds: int
    ready_stability_seconds: int
    instagram_user_id: str
    instagram_access_token: str
    facebook_app_id: str
    facebook_app_secret: str
    token_state_file: Path
    auto_refresh_token: bool
    refresh_buffer_days: int
    media_base_url: str
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_public_base_url: str
    r2_key_prefix: str
    caption_prefix: str
    streamer_hashtag: str
    max_retries: int
    retry_delay_seconds: int
    max_posts_per_day: int
    max_posts_per_week: int
    min_post_interval_minutes: int
    posting_start_hour: int
    posting_end_hour: int
    posting_peak_hour: int
    posting_peak_minute: int
    posting_grace_minutes: int
    timezone_name: str
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_token_file: Path = Path("data/state/youtube_token.json")
    youtube_privacy_status: str = "public"
    youtube_category_id: str = "24"

    @classmethod
    def from_repo_root(cls, repo_root: Path, *, resolve_media_url: bool = True) -> "AppConfig":
        root_dir = load_app_env(repo_root)

        queue_raw = os.getenv("QUEUE_DIR", "data/queue").strip()
        if os.name != "nt" and looks_like_windows_path(queue_raw):
            queue_raw = "data/queue"
        queue_dir = Path(queue_raw).expanduser()
        if not queue_dir.is_absolute():
            queue_dir = root_dir / queue_dir

        token_state_raw = os.getenv("TOKEN_STATE_FILE", str(root_dir / "data" / "publisher_token_state.json")).strip()
        if os.name != "nt" and looks_like_windows_path(token_state_raw):
            token_state_raw = "data/publisher_token_state.json"
        token_state_file = Path(token_state_raw).expanduser()
        if not token_state_file.is_absolute():
            token_state_file = root_dir / token_state_file
        r2_public_base_url = os.getenv("R2_PUBLIC_BASE_URL", "").strip().rstrip("/")
        media_base_url = os.getenv("MEDIA_BASE_URL", "").strip().rstrip("/")
        if r2_public_base_url:
            media_base_url = r2_public_base_url
        elif resolve_media_url and not media_base_url:
            media_base_url = resolve_media_base_url(queue_dir)

        youtube_token_raw = os.getenv("YOUTUBE_TOKEN_FILE", "data/state/youtube_token.json").strip()
        if os.name != "nt" and looks_like_windows_path(youtube_token_raw):
            youtube_token_raw = "data/state/youtube_token.json"
        youtube_token_file = Path(youtube_token_raw).expanduser()
        if not youtube_token_file.is_absolute():
            youtube_token_file = root_dir / youtube_token_file

        return cls(
            youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", "").strip(),
            youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", "").strip(),
            youtube_token_file=youtube_token_file,
            youtube_privacy_status=os.getenv("YOUTUBE_PRIVACY_STATUS", "public").strip().lower() or "public",
            youtube_category_id=os.getenv("YOUTUBE_CATEGORY_ID", "24").strip() or "24",
            queue_dir=queue_dir,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            ready_stability_seconds=int(os.getenv("READY_STABILITY_SECONDS", "5")),
            instagram_user_id=os.getenv("INSTAGRAM_USER_ID", "").strip(),
            instagram_access_token=os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip(),
            facebook_app_id=os.getenv("FACEBOOK_APP_ID", "").strip(),
            facebook_app_secret=os.getenv("FACEBOOK_APP_SECRET", "").strip(),
            token_state_file=token_state_file,
            auto_refresh_token=parse_bool(os.getenv("AUTO_REFRESH_TOKEN", "true")),
            refresh_buffer_days=int(os.getenv("REFRESH_BUFFER_DAYS", "10")),
            media_base_url=media_base_url,
            r2_account_id=os.getenv("R2_ACCOUNT_ID", "").strip(),
            r2_access_key_id=os.getenv("R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY", "").strip(),
            r2_bucket_name=os.getenv("R2_BUCKET_NAME", "").strip(),
            r2_public_base_url=r2_public_base_url,
            r2_key_prefix=os.getenv("R2_KEY_PREFIX", "processing").strip().strip("/"),
            caption_prefix=os.getenv("CAPTION_PREFIX", "").strip(),
            streamer_hashtag=os.getenv("STREAMER_HASHTAG", "").strip().lstrip("#"),
            max_retries=int(os.getenv("MAX_RETRIES", "3")),
            retry_delay_seconds=int(os.getenv("RETRY_DELAY_SECONDS", "30")),
            max_posts_per_day=int(os.getenv("MAX_POSTS_PER_DAY", "1")),
            max_posts_per_week=int(os.getenv("MAX_POSTS_PER_WEEK", "7")),
            min_post_interval_minutes=int(os.getenv("MIN_POST_INTERVAL_MINUTES", "1200")),
            posting_start_hour=int(os.getenv("POSTING_START_HOUR", "18")),
            posting_end_hour=int(os.getenv("POSTING_END_HOUR", "21")),
            posting_peak_hour=int(os.getenv("POSTING_PEAK_HOUR", "19")),
            posting_peak_minute=int(os.getenv("POSTING_PEAK_MINUTE", "15")),
            posting_grace_minutes=int(os.getenv("POSTING_GRACE_MINUTES", "60")),
            timezone_name=os.getenv("TIMEZONE_NAME", "America/New_York").strip(),
        )
