from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import re

from clipsourcer.models import ClipCandidate


def load_seen_ids(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()

    data = json.loads(state_file.read_text(encoding="utf-8"))
    return set(data.get("seen_ids", []))


def save_seen_ids(state_file: Path, seen_ids: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"seen_ids": sorted(seen_ids), "updated_at": datetime.now(timezone.utc).isoformat()}
    state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return collapsed or "clip"


def run_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def unique_run_dir(output_dir: Path, name: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = output_dir / name
    suffix = 1
    while candidate.exists():
        candidate = output_dir / f"{name}_{suffix}"
        suffix += 1
    return candidate


def build_clip_base_path(output_dir: Path, candidate: ClipCandidate, *, source_name: str = "source") -> Path:
    del source_name
    output_dir.mkdir(parents=True, exist_ok=True)
    author_slug = slugify(candidate.author_handle)
    clip_marker = f"_{candidate.clip_id}"

    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        if author_slug in child.name and clip_marker in child.name:
            return child / f"{author_slug}_{candidate.clip_id}"

    run_dir = unique_run_dir(output_dir, f"{run_timestamp()}_{author_slug}_{candidate.clip_id}")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / f"{author_slug}_{candidate.clip_id}"


def write_manifest(output_dir: Path, candidate: ClipCandidate, score: float, downloaded_video_path: Path | None = None) -> Path:
    if downloaded_video_path is not None:
        manifest_dir = downloaded_video_path.parent
        manifest_path = manifest_dir / f"{downloaded_video_path.stem}.json"
    else:
        manifest_path = build_clip_base_path(output_dir, candidate).with_suffix(".json")
    payload = {
        "clip_id": candidate.clip_id,
        "url": candidate.url,
        "text": candidate.text,
        "author_handle": candidate.author_handle,
        "hashtags": candidate.hashtags,
        "created_at": candidate.created_at.isoformat(),
        "engagement": {
            "plays": candidate.plays,
            "likes": candidate.likes,
            "shares": candidate.shares,
            "comments": candidate.comments,
            "saves": candidate.saves,
        },
        "viral_score": score,
        "provider": "tiktok-apify",
        "downloaded_video_path": str(downloaded_video_path) if downloaded_video_path is not None else None,
        "raw_payload": candidate.raw_payload,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def read_queue_state(run_dir: Path) -> dict:
    state_path = run_dir / "queue_state.json"
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def write_queue_state(run_dir: Path, *, phase: str, **extra: object) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "queue_state.json"
    payload = {
        "phase": phase,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return state_path
