from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urljoin

import boto3

from clippublisher.config import AppConfig
from clippublisher.models import QueueItem


def _relative_media_path(config: AppConfig, item: QueueItem) -> str:
    try:
        return item.video_path.relative_to(config.queue_dir).as_posix()
    except ValueError:
        return item.video_path.relative_to(item.run_dir.parent).as_posix()


def is_r2_enabled(config: AppConfig) -> bool:
    return all(
        [
            config.r2_account_id,
            config.r2_access_key_id,
            config.r2_secret_access_key,
            config.r2_bucket_name,
            config.r2_public_base_url,
        ]
    )


def upload_video_and_get_url(config: AppConfig, item: QueueItem) -> str:
    if not is_r2_enabled(config):
        raise ValueError("R2 media storage is not configured.")

    relative_path = _relative_media_path(config, item)
    key = f"{config.r2_key_prefix}/{relative_path}" if config.r2_key_prefix else relative_path
    key = key.lstrip("/")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{config.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=config.r2_access_key_id,
        aws_secret_access_key=config.r2_secret_access_key,
        region_name="auto",
    )
    with item.video_path.open("rb") as handle:
        client.upload_fileobj(
            handle,
            config.r2_bucket_name,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

    encoded_key = "/".join(quote(part) for part in key.split("/"))
    return urljoin(f"{config.r2_public_base_url.rstrip('/')}/", encoded_key)

