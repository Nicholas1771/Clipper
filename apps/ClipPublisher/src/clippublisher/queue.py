from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clippublisher.config import AppConfig
from clippublisher.models import QueueItem


def _read_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_queue_state(run_dir: Path) -> dict:
    state_path = run_dir / "queue_state.json"
    if not state_path.exists():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _ready_video_path(run_dir: Path) -> Path | None:
    editor_dir = run_dir / "editor"
    if editor_dir.exists():
        candidates = sorted(editor_dir.glob("*.mp4"), key=lambda path: path.name)
        if candidates:
            return candidates[0]
    candidates = sorted(run_dir.glob("*.mp4"), key=lambda path: path.name)
    return candidates[0] if candidates else None


def _ready_manifest_path(run_dir: Path) -> Path | None:
    source_dir = run_dir / "source"
    if source_dir.exists():
        candidates = sorted(source_dir.glob("*.json"), key=lambda path: path.name)
        if candidates:
            return candidates[0]
    candidates = sorted(run_dir.glob("*.json"), key=lambda path: path.name)
    return candidates[0] if candidates else None


def discover_queue_items(config: AppConfig) -> list[QueueItem]:
    config.queue_dir.mkdir(parents=True, exist_ok=True)

    items: list[QueueItem] = []
    for child in sorted(config.queue_dir.iterdir(), key=lambda path: (path.stat().st_mtime, path.name), reverse=True):
        if not child.is_dir():
            continue
        if child.name in {"processing", "Archive", "Test", "source"}:
            continue

        state = _read_queue_state(child)
        if state and state.get("phase") != "edited":
            continue

        manifest_path = _ready_manifest_path(child)
        video_path = _ready_video_path(child)
        if manifest_path is None or video_path is None:
            continue

        if state.get("phase") == "edited" and not (child / "editor").exists():
            continue

        data = _read_manifest(manifest_path)
        clip_id = str(data.get("clip_id") or child.name)
        author_handle = str(data.get("author_handle") or "unknown")
        text = str(data.get("text") or "")
        caption = text.strip() or f"{author_handle} clip"
        caption_file = child / "editor" / "caption.txt"
        if caption_file.exists():
            caption = caption_file.read_text(encoding="utf-8").strip() or caption
        description = ""
        description_file = child / "editor" / "description.txt"
        if description_file.exists():
            description = description_file.read_text(encoding="utf-8").strip()
        items.append(
            QueueItem(
                run_dir=child,
                manifest_path=manifest_path,
                video_path=video_path,
                clip_id=clip_id,
                caption=caption,
                author_handle=author_handle,
                description=description,
            )
        )
    return items


def is_item_ready(path: Path, required_stability_seconds: int, *, now: float | None = None) -> bool:
    if not path.exists():
        return False

    started_at = now or time.time()
    files = sorted(path.glob("*.json")) + sorted(path.glob("*.mp4"))
    if not files:
        return False

    stable_since = None
    for candidate in files:
        if not candidate.exists():
            return False
        candidate_mtime = candidate.stat().st_mtime
        if stable_since is None or candidate_mtime < stable_since:
            stable_since = candidate_mtime

    if stable_since is None:
        return False

    final_stable_at = stable_since + required_stability_seconds
    return final_stable_at <= started_at


def claim_item(config: AppConfig, item: QueueItem) -> QueueItem:
    # Claimed in place: flipping the phase hides the item from discovery, so it is never picked twice.
    updated_run_dir = item.run_dir
    new_manifest = item.manifest_path
    new_video = item.video_path
    state_path = updated_run_dir / "queue_state.json"
    payload: dict = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            payload = {}
    payload["phase"] = "published"
    payload["updated_at"] = time.time()
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    publisher_dir = updated_run_dir / "publisher"
    publisher_dir.mkdir(parents=True, exist_ok=True)
    (publisher_dir / "publish_state.json").write_text(
        json.dumps(
            {
                "phase": "publishing",
                "status": "claimed",
                "updated_at": datetime.now(ZoneInfo(config.timezone_name)).isoformat(),
                "run_dir": updated_run_dir.name,
                "clip_id": item.clip_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    scheduler_state_path = config.queue_dir / "scheduler_state.json"
    now = datetime.now(ZoneInfo(config.timezone_name))
    current_scheduler_state = {}
    if scheduler_state_path.exists():
        try:
            candidate = json.loads(scheduler_state_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                current_scheduler_state = candidate
        except json.JSONDecodeError:
            current_scheduler_state = {}
    merged = dict(current_scheduler_state)
    slot_index = merged.get("slot_index")
    completed_slots = int(merged.get("completed_slots") or 0)
    if isinstance(slot_index, int):
        completed_slots = max(completed_slots, slot_index + 1)
    merged.update(
        {
            "date": now.date().isoformat(),
            "phase": "published",
            "next_stage": "schedule_next",
            "completed_slots": completed_slots,
            "updated_at": now.isoformat(),
        }
    )
    scheduler_state_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return QueueItem(
        run_dir=updated_run_dir,
        manifest_path=new_manifest,
        video_path=new_video,
        clip_id=item.clip_id,
        caption=item.caption,
        author_handle=item.author_handle,
        description=item.description,
    )
