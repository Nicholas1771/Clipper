from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from clippublisher.config import AppConfig
from clippublisher.instagram_client import _split_trailing_hashtags
from clippublisher.models import QueueItem
from common.creators import read_creator

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
MAX_TITLE_LENGTH = 100
MAX_UPLOAD_ATTEMPTS = 5
VALID_PRIVACY = {"public", "unlisted", "private"}


class YouTubeError(RuntimeError):
    pass


class YouTubePolicyError(YouTubeError):
    """AI identified a policy concern; another replay candidate may be selected."""


class YouTubeScreeningError(YouTubeError):
    """AI review could not complete; publishing must not proceed unchecked."""


@dataclass(frozen=True)
class YouTubeApproval:
    fingerprint: str


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


def build_title(config: AppConfig, item: QueueItem) -> str:
    """AI text plus hashtags, all in the title. Hashtags that would push it past 100 characters are dropped."""
    source = (item.description or item.caption or f"@{item.author_handle} clip").replace("\r\n", "\n").replace("\r", "\n")
    body, others = _split_trailing_hashtags(source.strip())
    ai_text = item.youtube_description.strip()
    if ai_text:
        text = " ".join(ai_text.split())
    else:
        text = " ".join(re.split(r"\n\s*\n", body.strip())[0].split()) if body.strip() else ""
    text = re.sub(r"(?<!\w)#\S+", " ", text).replace("<", "").replace(">", "")
    text = " ".join(text.split()) or "Clip"
    if len(text) > MAX_TITLE_LENGTH:
        text = text[:MAX_TITLE_LENGTH].rsplit(" ", 1)[0].rstrip(" ,;:-") or text[:MAX_TITLE_LENGTH]
    creator = read_creator(item.run_dir)
    streamer = str(creator.get("settings", {}).get("STREAMER_HASHTAG") or getattr(config, "streamer_hashtag", "") or "").strip().lstrip("#")
    seen: set[str] = set()
    title = text
    for tag in ([f"#{streamer}"] if streamer else []) + others:
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        if len(title) + 1 + len(tag) <= MAX_TITLE_LENGTH:
            title = f"{title} {tag}"
    return title


def build_video_metadata(config: AppConfig, item: QueueItem) -> dict[str, Any]:
    privacy = config.youtube_privacy_status if config.youtube_privacy_status in VALID_PRIVACY else "public"
    return {
        "snippet": {"title": build_title(config, item), "description": "", "categoryId": config.youtube_category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }


def _screening_content(config: AppConfig, item: QueueItem) -> dict[str, str]:
    return {
        "title": build_title(config, item),
        "caption": item.caption,
        "summary": item.description,
        "youtube_title": item.youtube_title,
        "youtube_description": item.youtube_description,
        "transcript": item.transcript,
    }


def _read_screening_prompt(config: AppConfig) -> str:
    if config.prompts_dir is None:
        raise YouTubeScreeningError("AI_PROMPTS_DIR is required for YouTube AI policy screening.")
    path = config.prompts_dir / "youtube_policy_screening.txt"
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise YouTubeScreeningError(f"Cannot read YouTube AI policy prompt {path}: {error}") from error
    if not prompt:
        raise YouTubeScreeningError(f"YouTube AI policy prompt is empty: {path}")
    return prompt


def _screening_fingerprint(config: AppConfig, item: QueueItem, prompt: str) -> str:
    return hashlib.sha256(json.dumps(
        [config.gemini_model, prompt, _screening_content(config, item)],
        sort_keys=True,
    ).encode("utf-8")).hexdigest()


def screen_youtube_content(config: AppConfig, item: QueueItem) -> YouTubeApproval:
    """Require an explicit contextual AI approval before uploading to YouTube."""
    if not config.google_api_key:
        raise YouTubeScreeningError("GOOGLE_API_KEY is required for YouTube AI policy screening.")
    prompt = _read_screening_prompt(config)
    content = _screening_content(config, item)
    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent",
            headers={"x-goog-api-key": config.google_api_key},
            json={
                "systemInstruction": {"parts": [{"text": prompt}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(content)}]}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseSchema": {
                        "type": "OBJECT",
                        "properties": {
                            "decision": {"type": "STRING", "enum": ["allow", "block", "review"]},
                            "reason": {"type": "STRING"},
                        },
                        "required": ["decision", "reason"],
                    },
                },
            },
            timeout=(10, config.timeout_seconds),
        )
    except requests.RequestException:
        raise YouTubeScreeningError("YouTube AI policy screening request failed; upload blocked.") from None
    if response.status_code != 200:
        raise YouTubeScreeningError(
            f"YouTube AI policy screening failed (HTTP {response.status_code}); upload blocked."
        )
    try:
        payload = response.json()
        candidate = payload["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise ValueError("AI review did not finish normally")
        result = json.loads("".join(part.get("text", "") for part in candidate["content"]["parts"]))
        if (
            not isinstance(result, dict)
            or result.get("decision") not in ("allow", "block", "review")
            or not isinstance(result.get("reason"), str)
            or not result["reason"].strip()
        ):
            raise ValueError("Invalid AI review decision")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise YouTubeScreeningError("YouTube AI policy screening returned no valid decision; upload blocked.") from None
    if result["decision"] != "allow":
        raise YouTubePolicyError(f"YouTube AI policy screening ({result['decision']}): {result['reason']}")
    logger.info("YouTube AI policy screening approved clip %s: %s", item.clip_id, result["reason"])
    return YouTubeApproval(_screening_fingerprint(config, item, prompt))


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


def publish_to_youtube(
    config: AppConfig, item: QueueItem, *, approval: YouTubeApproval | None = None,
) -> dict[str, Any]:
    if approval is None or approval.fingerprint != _screening_fingerprint(config, item, _read_screening_prompt(config)):
        screen_youtube_content(config, item)
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
