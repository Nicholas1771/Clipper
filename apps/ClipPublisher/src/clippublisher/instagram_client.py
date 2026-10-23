from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from clippublisher.config import AppConfig
from clippublisher.media_storage import is_r2_enabled, upload_media_and_get_url, upload_video_and_get_url
from clippublisher.models import QueueItem
from clippublisher.public_url import ensure_media_base_url

logger = logging.getLogger(__name__)


def get_instagram_user_id(config: AppConfig) -> str:
    if not config.instagram_user_id:
        raise ValueError("INSTAGRAM_USER_ID is required for publishing.")
    return config.instagram_user_id


def _split_trailing_hashtags(caption: str) -> tuple[str, list[str]]:
    """Separate a final line made only of hashtags from the body so tags can be reordered."""
    body, _, last_line = caption.rpartition("\n")
    words = last_line.split()
    if words and all(re.fullmatch(r"#\w+", word) for word in words):
        return body.rstrip(), words
    return caption, []


def build_caption(config: AppConfig, item: QueueItem) -> str:
    if config.instagram_caption_file is not None:
        caption = config.instagram_caption_file.read_text(encoding="utf-8")
        if not caption.strip():
            raise ValueError("INSTAGRAM_CAPTION_FILE must not be empty.")
        if len(caption) > 2200:
            raise ValueError("INSTAGRAM_CAPTION_FILE exceeds Instagram's 2200-character limit.")
        return caption
    preferred = (item.description or item.caption).strip()
    caption = preferred.replace("\r\n", "\n").replace("\r", "\n")
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    if config.caption_prefix:
        caption = f"{config.caption_prefix} {caption}".strip()
    caption, others = _split_trailing_hashtags(caption)
    tags: list[str] = []
    for tag in ([f"#{config.streamer_hashtag}"] if config.streamer_hashtag else []) + others:
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    if caption and tags:
        caption = f"{caption}\n\n{' '.join(tags)}"
    if len(caption) > 2200:
        caption = caption[:2197].rstrip() + "..."
    return caption


def build_public_video_url(config: AppConfig, item: QueueItem) -> str:
    if is_r2_enabled(config):
        return upload_video_and_get_url(config, item)
    if not config.media_base_url:
        raise ValueError(
            "MEDIA_BASE_URL is required. It must be a public HTTPS URL that can reach the uploaded MP4 "
            "for Meta Graph API to download and publish."
        )

    try:
        relative_path = item.video_path.relative_to(config.queue_dir).as_posix()
    except ValueError:
        relative_path = item.video_path.relative_to(item.run_dir.parent).as_posix()
    base_url = ensure_media_base_url(config.media_base_url)
    return urljoin(f"{base_url.rstrip('/')}/", relative_path)


def build_public_cover_url(config: AppConfig, item: QueueItem) -> str:
    from PIL import Image

    if config.instagram_cover_image is None:
        raise ValueError("INSTAGRAM_COVER_IMAGE is required to build a reel cover.")
    cover = item.run_dir / "publisher" / "instagram_cover.jpg"
    with Image.open(config.instagram_cover_image) as image:
        if image.format != "JPEG":
            raise ValueError("INSTAGRAM_COVER_IMAGE must be a JPEG image.")
        image.verify()
    if config.instagram_cover_image.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Instagram reel cover exceeds the 8 MB limit.")
    cover.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.instagram_cover_image, cover)
    if is_r2_enabled(config):
        return upload_media_and_get_url(config, item, cover, content_type="image/jpeg")
    if not config.media_base_url:
        raise ValueError("MEDIA_BASE_URL is required to serve the Instagram reel cover.")
    try:
        relative_path = cover.relative_to(config.queue_dir).as_posix()
    except ValueError:
        relative_path = cover.relative_to(item.run_dir.parent).as_posix()
    base_url = ensure_media_base_url(config.media_base_url)
    return urljoin(f"{base_url.rstrip('/')}/", relative_path)


def get_media_status(config: AppConfig, media_id: str, *, access_token: str) -> str:
    response = requests.get(
        f"https://graph.facebook.com/v20.0/{media_id}",
        params={
            "fields": "status_code,status",
            "access_token": access_token,
        },
        timeout=(30, 120),
    )
    if response.status_code >= 400:
        body = response.text
        try:
            payload = response.json()
            body = json.dumps(payload, indent=2)
        except ValueError:
            pass
        raise RuntimeError(
            f"Instagram media status check failed for media {media_id}: HTTP {response.status_code}. Response: {body}"
        )
    payload = response.json()
    detail = payload.get("status")
    if detail:
        logger.info("Instagram media %s status_code=%s detail=%s", media_id, payload.get("status_code"), detail)
    return payload.get("status_code", "UNKNOWN")


def publish_to_instagram(config: AppConfig, item: QueueItem, *, access_token: str) -> dict:
    ig_user_id = get_instagram_user_id(config)
    caption = build_caption(config, item)
    video_url = build_public_video_url(config, item)

    media_payload = {
        "access_token": access_token,
        "video_url": video_url,
        "caption": caption,
        "media_type": "REELS",
    }
    if config.instagram_cover_image is not None:
        media_payload["cover_url"] = build_public_cover_url(config, item)

    response = requests.post(
        f"https://graph.facebook.com/v20.0/{ig_user_id}/media",
        data=media_payload,
        timeout=(30, 120),
    )
    if response.status_code >= 400:
        body = response.text
        try:
            payload = response.json()
            body = json.dumps(payload, indent=2)
        except ValueError:
            pass
        raise RuntimeError(
            f"Instagram media creation failed for IG user {ig_user_id}: HTTP {response.status_code}. Response: {body}"
        )
    payload = response.json()
    creation_id = payload.get("id")
    if not creation_id:
        raise RuntimeError(f"Instagram media creation did not return an id: {payload}")

    final_status = "UNKNOWN"
    for attempt in range(1, 21):
        final_status = get_media_status(config, creation_id, access_token=access_token)
        if final_status in {"PUBLISHED", "READY", "FINISHED"}:
            break
        if final_status in {"ERROR", "FAILED", "REJECTED", "EXPIRED"}:
            raise RuntimeError(
                f"Instagram media creation failed for IG user {ig_user_id}: status_code={final_status}. Media id: {creation_id}"
            )
        time.sleep(5)

    if final_status not in {"IN_PROGRESS", "PUBLISHED", "READY", "FINISHED", "UNKNOWN"}:
        raise RuntimeError(
            f"Instagram media was not ready for publishing for IG user {ig_user_id}. "
            f"Last status_code={final_status}. Media id: {creation_id}"
        )

    for attempt in range(1, 7):
        publish_response = requests.post(
            f"https://graph.facebook.com/v20.0/{ig_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
            timeout=(30, 120),
        )
        if publish_response.status_code < 400:
            return publish_response.json()

        body = publish_response.text
        payload = {}
        try:
            payload = publish_response.json()
            body = json.dumps(payload, indent=2)
        except ValueError:
            pass

        error_code = payload.get("error", {}).get("code")
        error_msg = payload.get("error", {}).get("message", "")
        if error_code == 9007 or "Media ID is not available" in error_msg or "not ready for publishing" in error_msg:
            time.sleep(10 * attempt)
            continue

        raise RuntimeError(
            f"Instagram publish failed for IG user {ig_user_id}: HTTP {publish_response.status_code}. Response: {body}"
        )

    raise RuntimeError(
        f"Instagram publish failed for IG user {ig_user_id} after retries: "
        f"media was still not ready for publishing. Final response: {body}"
    )
