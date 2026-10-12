from __future__ import annotations

import logging
import mimetypes
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from imageio_ffmpeg import get_ffmpeg_exe

from clipsourcer.apify_client import fetch_tiktok_items
from clipsourcer.config import AppConfig
from clipsourcer.models import ClipCandidate
from clipsourcer.storage import build_clip_base_path


logger = logging.getLogger(__name__)


def normalize_handle(value: str) -> str:
    return value.strip().lower().lstrip("@")


def is_download_authorized(candidate: ClipCandidate, config: AppConfig) -> bool:
    # NEVER USE THIS BYPASS UNLESS YOU ARE ABSOLUTELY CERTAIN YOU HAVE AUTHORIZATION TO DOWNLOAD THE CLIP.
    # This is a deliberate test-only escape hatch and should not be used in normal production runs.
    # IMPORTANT!!!: DO NOT REMOVE THIS BYPASS OR PRODUCTION TEST CASE WILL FAIL!!!
    if config.allow_download_bypass:
        return True

    if not config.download_video_files:
        return False

    authorized_handles = {normalize_handle(handle) for handle in config.authorized_download_profiles}
    if not authorized_handles:
        return False

    return normalize_handle(candidate.author_handle) in authorized_handles


def choose_media_url(candidate: ClipCandidate) -> str:
    if not candidate.media_urls:
        raise ValueError(
            "No downloadable media URL was returned for this clip. "
            "Enable DOWNLOAD_VIDEO_FILES and source owned-profile videos that expose mediaUrls."
        )
    return candidate.media_urls[0]


def infer_extension(media_url: str, content_type: str | None) -> str:
    if content_type:
        mime_type = content_type.split(";", 1)[0].strip()
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed

    suffix = Path(urlparse(media_url).path).suffix
    if suffix:
        return suffix
    return ".mp4"


def find_downloaded_file(destination_base: Path) -> Path | None:
    matches = [
        path
        for path in destination_base.parent.glob(f"{destination_base.name}.*")
        if path.is_file() and path.suffix.lower() not in {".json", ".part", ".tmp", ".ytdl"}
    ]
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_size, reverse=True)
    return matches[0]


def cleanup_download_artifacts(destination_base: Path) -> None:
    for path in destination_base.parent.glob(f"{destination_base.name}.*"):
        if path.is_file() and path.suffix.lower() != ".json":
            path.unlink()


def validate_download(download_path: Path) -> Path:
    if not download_path.exists():
        raise ValueError("Downloader reported success but no file was created.")
    if download_path.stat().st_size <= 0:
        raise ValueError(f"Downloaded file is empty: {download_path.name}")
    return download_path


def get_ffmpeg_binary() -> str:
    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved
    ffmpeg_path = Path(get_ffmpeg_exe())
    if ffmpeg_path.exists():
        return str(ffmpeg_path)
    raise FileNotFoundError("ffmpeg is not installed or available for MP4 normalization.")


def normalize_video_to_mp4(video_path: Path) -> Path:
    if not video_path.exists():
        raise ValueError(f"Cannot normalize missing video file: {video_path}")

    try:
        ffmpeg = get_ffmpeg_binary()
    except FileNotFoundError:
        logger.warning("No ffmpeg binary is available; leaving downloaded media as-is: %s", video_path)
        return video_path

    target_path = video_path.with_suffix(".mp4")
    temporary_path = video_path.with_name(f"{video_path.stem}.normalized.mp4")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(temporary_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(
            "Skipping MP4 normalization for %s because ffmpeg could not process it: %s",
            video_path,
            (result.stderr.strip() or result.stdout.strip() or "unknown ffmpeg error")[:200],
        )
        return video_path

    if temporary_path.exists():
        if target_path.exists() and target_path != temporary_path:
            target_path.unlink()
        temporary_path.replace(target_path)
    if video_path != target_path and video_path.exists():
        video_path.unlink()
    return target_path


def stream_to_destination(
    file_url: str,
    destination_base: Path,
    *,
    request_params: dict | None = None,
    request_headers: dict | None = None,
) -> Path:
    response = requests.get(
        file_url,
        params=request_params,
        headers=request_headers,
        timeout=(30, 240),
        stream=True,
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type")
    if content_type and content_type.split(";", 1)[0].strip().startswith("text/"):
        raise ValueError(f"Expected video content, got {content_type}.")

    extension = infer_extension(file_url, content_type)
    destination = destination_base.with_suffix(extension)
    temporary_path = destination.with_suffix(f"{destination.suffix}.tmp")

    with temporary_path.open("wb") as file_handle:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                file_handle.write(chunk)

    temporary_path.replace(destination)
    return normalize_video_to_mp4(destination)


def download_with_ytdlp(candidate: ClipCandidate, destination_base: Path) -> Path:
    with yt_dlp.YoutubeDL(
        {
            "outtmpl": str(destination_base) + ".%(ext)s",
            "format_sort": ["codec:h264"],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "overwrites": True,
        }
    ) as downloader:
        downloader.download([candidate.url])

    downloaded_file = find_downloaded_file(destination_base)
    if downloaded_file is None:
        raise ValueError("yt-dlp did not create a download file.")
    return normalize_video_to_mp4(downloaded_file)


def choose_tikwm_media_url(payload: dict) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("hdplay", "play", "wmplay"):
            value = str(data.get(key) or "").strip()
            if value:
                return value

    for key in ("hdplay", "play", "wmplay"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value

    raise ValueError("TikWM did not return a playable media URL.")


def download_with_tikwm(candidate: ClipCandidate, destination_base: Path) -> Path:
    response = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": candidate.url, "hd": "1"},
        headers={"User-Agent": "ClipSourcer/1.0"},
        timeout=(30, 120),
    )
    response.raise_for_status()
    payload = response.json()
    media_url = choose_tikwm_media_url(payload)
    return stream_to_destination(media_url, destination_base)


def get_apify_media_url(config: AppConfig, candidate: ClipCandidate) -> str:
    media_urls = candidate.media_urls
    if not media_urls:
        items = fetch_tiktok_items(
            config,
            include_video_downloads=True,
            post_urls=[candidate.url],
        )
        if not items:
            raise ValueError("Apify did not return any result for the fallback post URL.")
        media_urls = [
            str(url).strip()
            for url in items[0].get("mediaUrls", [])
            if str(url).strip()
        ]
    if not media_urls:
        raise ValueError("Apify did not return a downloadable media URL.")
    return media_urls[0]


def download_with_apify(config: AppConfig, candidate: ClipCandidate, destination_base: Path) -> Path:
    media_url = get_apify_media_url(config, candidate)
    parsed = urlparse(media_url)
    request_params = {"token": config.apify_token} if parsed.netloc.lower() == "api.apify.com" else None
    return stream_to_destination(media_url, destination_base, request_params=request_params)


def _download_all_video_files_for_test(config: AppConfig, candidate: ClipCandidate) -> dict[str, Path]:
    if not is_download_authorized(candidate, config):
        return {}

    results: dict[str, Path] = {}
    errors: list[str] = []
    shared_base = build_clip_base_path(config.output_dir, candidate)
    strategies = (
        (
            "yt-dlp",
            lambda strategy_base: download_with_ytdlp(candidate, strategy_base),
        ),
        (
            "TikWM",
            lambda strategy_base: download_with_tikwm(candidate, strategy_base),
        ),
        (
            "Apify",
            lambda strategy_base: download_with_apify(config, candidate, strategy_base),
        ),
    )

    for strategy_name, strategy in strategies:
        cleanup_download_artifacts(shared_base)
        try:
            results[strategy_name] = validate_download(strategy(shared_base))
        except Exception as error:
            cleanup_download_artifacts(shared_base)
            errors.append(f"{strategy_name}: {error}")

    if errors:
        raise RuntimeError("Test-only multi-strategy download failed. " + " | ".join(errors))
    return results


def download_video_file(config: AppConfig, candidate: ClipCandidate) -> Path | None:
    if not is_download_authorized(candidate, config):
        logger.info("Skipping video download for clip %s because download authorization did not pass.", candidate.clip_id)
        return None

    errors: list[str] = []
    strategies = (
        ("yt-dlp", lambda destination_base: download_with_ytdlp(candidate, destination_base)),
        ("TikWM", lambda destination_base: download_with_tikwm(candidate, destination_base)),
        ("Apify", lambda destination_base: download_with_apify(config, candidate, destination_base)),
    )
    destination_base = build_clip_base_path(config.output_dir, candidate)

    for strategy_name, strategy in strategies:
        cleanup_download_artifacts(destination_base)
        logger.info("Trying %s download for clip %s.", strategy_name, candidate.clip_id)
        try:
            download_path = validate_download(strategy(destination_base))
            logger.info("%s download succeeded: %s", strategy_name, download_path)
            return download_path
        except Exception as error:
            cleanup_download_artifacts(destination_base)
            logger.warning("%s download failed for clip %s: %s", strategy_name, candidate.clip_id, error)
            errors.append(f"{strategy_name}: {error}")

    raise RuntimeError("All download strategies failed. " + " | ".join(errors))
