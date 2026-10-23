from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
import json
import os
from pathlib import Path
import re
from typing import Any

from common.env import _find_repo_root, parse_bool, parse_csv, read_dotenv, resolve_asset_path

active_creator_name: ContextVar[str] = ContextVar("active_creator_name", default="")


@dataclass(frozen=True)
class Creator:
    name: str
    instagram_posts_per_day: int
    youtube_posts_per_day: int
    settings: dict[str, str]

    @property
    def display_name(self) -> str:
        return self.settings["STREAMER_NAME"]


CREATOR_KEYS = {
    "STREAMER_NAME", "STREAMER_HASHTAG", "OVERLAY_IMAGE_PATH",
    "SOURCE_MODE", "SOURCE_HASHTAG", "SOURCE_PROFILES", "KEYWORD_FILTER",
    "DOWNLOAD_VIDEO_FILES", "AUTHORIZED_DOWNLOAD_PROFILES", "REQUIRE_SOURCE_KICK_BRANDING",
    "LOOKBACK_HOURS", "RESULTS_PER_PAGE", "MINIMUM_VIEW_COUNT",
}


def configured_creators(repo_root: Path | None = None, values: dict[str, str] | None = None) -> tuple[Creator, ...]:
    root = repo_root or _find_repo_root(Path(__file__).resolve())
    env = os.environ if values is None else values
    result: list[Creator] = []
    for entry in parse_csv(env.get("CREATORS", "")):
        parts = entry.split(":")
        if len(parts) != 3 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", parts[0]):
            raise ValueError("CREATORS must be creator:instagram_daily:youtube_daily entries separated by commas.")
        name, instagram, youtube = parts
        if any(creator.name == name for creator in result):
            raise ValueError(f"Duplicate creator in CREATORS: {name}")
        if not instagram.isdecimal() or not youtube.isdecimal():
            raise ValueError(f"Daily quotas for {name} must be non-negative integers.")
        path = root / "creators" / f"{name}.env"
        if not path.is_file():
            raise FileNotFoundError(f"Creator '{name}' has no config file at {path}.")
        settings = read_dotenv(path)
        unknown = settings.keys() - CREATOR_KEYS
        if unknown:
            raise ValueError(f"Unsupported settings in {path}: {', '.join(sorted(unknown))}")
        required = ("STREAMER_NAME", "STREAMER_HASHTAG", "OVERLAY_IMAGE_PATH", "REQUIRE_SOURCE_KICK_BRANDING")
        missing = [key for key in required if not settings.get(key, "").strip()]
        if missing:
            raise ValueError(f"Creator '{name}' is missing: {', '.join(missing)}")
        parse_bool(settings["REQUIRE_SOURCE_KICK_BRANDING"])
        settings = {
            "SOURCE_MODE": "hashtag",
            "SOURCE_HASHTAG": "",
            "SOURCE_PROFILES": "",
            "KEYWORD_FILTER": "",
            "DOWNLOAD_VIDEO_FILES": "false",
            "AUTHORIZED_DOWNLOAD_PROFILES": "",
            **settings,
        }
        mode = settings["SOURCE_MODE"]
        if mode not in {"hashtag", "profiles"}:
            raise ValueError(f"Creator '{name}': SOURCE_MODE must be hashtag or profiles.")
        target = settings["SOURCE_HASHTAG"] if mode == "hashtag" else settings["SOURCE_PROFILES"]
        if not target.strip():
            raise ValueError(f"Creator '{name}' requires a sourcing target for {mode} mode.")
        parse_bool(settings["DOWNLOAD_VIDEO_FILES"])
        for key in ("LOOKBACK_HOURS", "RESULTS_PER_PAGE", "MINIMUM_VIEW_COUNT"):
            if key in settings and (not settings[key].isdecimal() or (key != "MINIMUM_VIEW_COUNT" and int(settings[key]) == 0)):
                raise ValueError(f"Creator '{name}': {key} must be a valid {'non-negative' if key == 'MINIMUM_VIEW_COUNT' else 'positive'} integer.")
        result.append(Creator(name, int(instagram), int(youtube), settings))
    return tuple(result)


def creator_payload(creator: Creator) -> dict[str, Any]:
    return {"name": creator.name, "display_name": creator.display_name, "settings": creator.settings}


def read_creator(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "creator.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        raise ValueError(f"Invalid creator metadata: {path}")
    return payload


def config_for_creator(config: Any, payload: dict[str, Any]) -> Any:
    """Apply the queued creator snapshot, without changing shared process configuration."""
    settings = payload.get("settings", {})
    active_creator_name.set(str(payload.get("display_name") or ""))
    if not settings:
        return config
    names = {field.name for field in fields(config)}
    updates: dict[str, Any] = {}
    for key, value in settings.items():
        field = key.lower()
        if field in names:
            current = getattr(config, field)
            if isinstance(current, bool):
                updates[field] = parse_bool(value)
            elif isinstance(current, int):
                updates[field] = int(value)
            elif isinstance(current, list):
                updates[field] = parse_csv(value)
            elif isinstance(current, str):
                updates[field] = value.lstrip("#") if field in {"source_hashtag", "streamer_hashtag"} else value
    overlay = settings.get("OVERLAY_IMAGE_PATH")
    if overlay:
        path = resolve_asset_path(overlay, _find_repo_root(Path(__file__).resolve()))
        for field in ("overlay_image_path", "overlay_reference_image_path"):
            if field in names:
                updates[field] = path
    return replace(config, **updates)


def config_for_run(config: Any, run_dir: Path) -> Any:
    return config_for_creator(config, read_creator(run_dir))
