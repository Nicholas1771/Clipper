from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


@dataclass(frozen=True)
class ClipCandidate:
    clip_id: str
    url: str
    text: str
    created_at: datetime
    author_handle: str
    hashtags: list[str]
    plays: int
    likes: int
    shares: int
    comments: int
    saves: int
    media_urls: list[str]
    raw_payload: dict[str, Any]

    @classmethod
    def from_apify_item(cls, item: dict[str, Any]) -> "ClipCandidate":
        hashtags = []
        for hashtag in item.get("hashtags", []):
            if isinstance(hashtag, dict):
                name = str(hashtag.get("name", "")).strip()
            else:
                name = str(hashtag).strip()
            if name:
                hashtags.append(name)

        media_urls = [str(url).strip() for url in item.get("mediaUrls", []) if str(url).strip()]

        candidate_id = (
            item.get("postId")
            or item.get("videoId")
            or item.get("id")
            or (item.get("video") or {}).get("id")
            or ""
        )
        if not candidate_id:
            raise KeyError("No canonical TikTok clip identifier was found in the Apify item payload.")

        return cls(
            clip_id=str(candidate_id),
            url=str(item.get("webVideoUrl") or item.get("url") or ""),
            text=str(item.get("text") or ""),
            created_at=parse_iso8601(str(item["createTimeISO"])),
            author_handle=str(item.get("authorMeta", {}).get("name") or ""),
            hashtags=hashtags,
            plays=int(item.get("playCount") or 0),
            likes=int(item.get("diggCount") or 0),
            shares=int(item.get("shareCount") or 0),
            comments=int(item.get("commentCount") or 0),
            saves=int(item.get("collectCount") or 0),
            media_urls=media_urls,
            raw_payload=item,
        )
