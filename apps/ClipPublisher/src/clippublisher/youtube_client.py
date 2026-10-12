from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from clippublisher.config import AppConfig
from clippublisher.instagram_client import _split_trailing_hashtags
from clippublisher.models import QueueItem

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000
MAX_UPLOAD_ATTEMPTS = 5
VALID_PRIVACY = {"public", "unlisted", "private"}


class YouTubeError(RuntimeError):
    pass


def read_refresh_token(token_file: Path) -> str:
    try:
        payload = json.loads(token_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("refresh_token") or "").strip() if isinstance(payload, dict) else ""


def is_configured(config: AppConfig) -> bool:
    return bool(
        getattr(config, "youtube_client_id", "")
        and getattr(config, "youtube_client_secret", "")
        and read_refresh_token(config.youtube_token_file)
    )


def fetch_access_token(config: AppConfig) -> str:
    refresh_token = read_refresh_token(config.youtube_token_file)
    if not refresh_token:
        raise YouTubeError(f"No YouTube refresh token in {config.youtube_token_file}; run ClipPublisher with --youtube-auth.")
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": config.youtube_client_id,
            "client_secret": config.youtube_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise YouTubeError(f"YouTube token refresh failed ({response.status_code}): {response.text[:300]}")
    token = str(response.json().get("access_token") or "")
    if not token:
        raise YouTubeError("YouTube token refresh returned no access token.")
    return token


def build_description(config: AppConfig, item: QueueItem) -> str:
    """Description body followed by one space-delimited hashtag run: streamer tag first, then the AI tags, then #Shorts."""
    text = (item.youtube_description or item.description or item.caption).replace("\r\n", "\n").replace("\r", "\n")
    body, others = _split_trailing_hashtags(re.sub(r"\n{3,}", "\n\n", text).strip())
    streamer = getattr(config, "streamer_hashtag", "") or ""
    tags: list[str] = []
    for tag in ([f"#{streamer}"] if streamer else []) + others + ["#Shorts"]:
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    return f"{body} {' '.join(tags)}".strip()


def build_video_metadata(config: AppConfig, item: QueueItem) -> dict[str, Any]:
    title = (item.youtube_title or item.caption or f"@{item.author_handle} clip").replace("<", "").replace(">", "").strip()
    title = " ".join(title.split())[:MAX_TITLE_LENGTH] or "Clip"
    description = build_description(config, item)
    privacy = config.youtube_privacy_status if config.youtube_privacy_status in VALID_PRIVACY else "public"
    return {
        "snippet": {
            "title": title,
            "description": description.replace("<", "").replace(">", "")[:MAX_DESCRIPTION_LENGTH],
            "categoryId": config.youtube_category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


def _start_session(access_token: str, metadata: dict[str, Any], size: int) -> str:
    response = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        data=json.dumps(metadata),
        timeout=60,
    )
    if response.status_code != 200 or not response.headers.get("Location"):
        raise YouTubeError(f"YouTube upload session failed ({response.status_code}): {response.text[:500]}")
    return response.headers["Location"]


def _uploaded_offset(session_url: str, size: int) -> int | None:
    """Ask YouTube how many bytes it has. Returns None if the upload already completed."""
    response = requests.put(session_url, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}, timeout=60)
    if response.status_code in (200, 201):
        return None
    if response.status_code == 308:
        received = response.headers.get("Range", "")
        return int(received.rsplit("-", 1)[1]) + 1 if "-" in received else 0
    raise YouTubeError(f"YouTube upload status check failed ({response.status_code}): {response.text[:300]}")


def _send_bytes(session_url: str, video_path: Path, offset: int, size: int) -> requests.Response:
    with video_path.open("rb") as handle:
        handle.seek(offset)
        return requests.put(
            session_url,
            data=handle,
            headers={
                "Content-Length": str(size - offset),
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {offset}-{size - 1}/{size}",
            },
            timeout=600,
        )


def upload_video(access_token: str, video_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    size = video_path.stat().st_size
    if size <= 0:
        raise YouTubeError(f"Video file is empty: {video_path}")
    session_url = _start_session(access_token, metadata, size)
    offset = 0
    last_error = "unknown error"
    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
        try:
            response = _send_bytes(session_url, video_path, offset, size)
            if response.status_code in (200, 201):
                return response.json()
            if response.status_code < 500:
                raise YouTubeError(f"YouTube upload failed ({response.status_code}): {response.text[:500]}")
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as error:
            last_error = str(error)
        logger.warning("YouTube upload attempt %s/%s interrupted (%s); resuming.", attempt, MAX_UPLOAD_ATTEMPTS, last_error)
        time.sleep(min(2 ** attempt, 30))
        resumed = _uploaded_offset(session_url, size)
        if resumed is None:
            raise YouTubeError("YouTube reported the upload complete but returned no video details.")
        offset = resumed
    raise YouTubeError(f"YouTube upload failed after {MAX_UPLOAD_ATTEMPTS} attempts: {last_error}")


def publish_to_youtube(config: AppConfig, item: QueueItem) -> dict[str, Any]:
    access_token = fetch_access_token(config)
    video = upload_video(access_token, item.video_path, build_video_metadata(config, item))
    video_id = str(video.get("id") or "")
    status = video.get("status") or {}
    logger.info(
        "YouTube upload finished for %s: video %s (privacy=%s, channel=%s).",
        item.run_dir.name,
        video_id,
        status.get("privacyStatus"),
        (video.get("snippet") or {}).get("channelId"),
    )
    return {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}", "privacy": status.get("privacyStatus")}
