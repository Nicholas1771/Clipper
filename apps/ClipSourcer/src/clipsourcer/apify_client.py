from __future__ import annotations

import logging
from typing import Any

import requests

from clipsourcer.config import AppConfig


API_BASE = "https://api.apify.com/v2/acts"
logger = logging.getLogger(__name__)


def build_actor_input(
    config: AppConfig,
    *,
    include_video_downloads: bool = False,
    post_urls: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "excludePinnedPosts": True,
        "proxyCountryCode": "None",
        "resultsPerPage": config.results_per_page,
        "scrapeRelatedVideos": False,
        "shouldDownloadAvatars": False,
        "shouldDownloadCovers": False,
        "shouldDownloadMusicCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadVideos": include_video_downloads,
        "profileScrapeSections": ["videos"],
        "profileSorting": "latest",
        "maxProfilesPerQuery": max(len(config.source_profiles), 10),
    }

    if post_urls:
        payload["postURLs"] = post_urls
    elif config.source_mode == "hashtag":
        payload["hashtags"] = [config.source_hashtag]
    else:
        payload["profiles"] = config.source_profiles

    return payload


def fetch_tiktok_items(
    config: AppConfig,
    *,
    include_video_downloads: bool = False,
    post_urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/{config.apify_actor_id}/run-sync-get-dataset-items"
    source_kind = "postURLs" if post_urls else config.source_mode
    source_count = len(post_urls) if post_urls else (1 if config.source_mode == "hashtag" else len(config.source_profiles))
    logger.info(
        "Calling Apify actor for %s (%s target%s, video downloads=%s).",
        source_kind,
        source_count,
        "" if source_count == 1 else "s",
        include_video_downloads,
    )
    response = requests.post(
        url,
        params={"token": config.apify_token},
        json=build_actor_input(
            config,
            include_video_downloads=include_video_downloads,
            post_urls=post_urls,
        ),
        timeout=(30, 240),
    )
    response.raise_for_status()

    items = response.json()
    if not isinstance(items, list):
        raise ValueError("Apify response did not return a dataset item list.")
    logger.info("Apify returned %s item%s.", len(items), "" if len(items) == 1 else "s")
    return items
