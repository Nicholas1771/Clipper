from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from clippublisher.config import AppConfig
from common.alerts import send_email_alert, send_failure_alert
from clippublisher.instagram_client import publish_to_instagram
from clippublisher.queue import claim_item, discover_queue_items, is_item_ready
from clippublisher.youtube_client import is_configured as youtube_is_configured, publish_to_youtube
from common.platforms import INSTAGRAM, YOUTUBE, read_slot_platforms
from clippublisher.token_manager import resolve_publish_access_token


logger = logging.getLogger(__name__)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def next_run_text(queue_dir: Path, tz_name: str) -> str:
    state = load_json(queue_dir / "scheduler_state.json")
    now = datetime.now(ZoneInfo(tz_name))
    upcoming = []
    for raw in (state.get("slots") if isinstance(state, dict) else None) or []:
        try:
            slot = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if slot > now:
            upcoming.append(slot)
    if not upcoming:
        return "none left today (a new schedule is built tomorrow)"
    return min(upcoming).strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def original_title(manifest_path: Path, fallback: str = "") -> str:
    manifest = load_json(manifest_path)
    if isinstance(manifest, dict):
        text = str(manifest.get("text") or "").strip()
        if text:
            return text
    return fallback


def alert_details(config: AppConfig, item: Any) -> str:
    return (
        f"Author: @{item.author_handle}\n"
        f"Next scheduled run: {next_run_text(config.queue_dir, config.timezone_name)}\n"
    )


def alert_subject(config: AppConfig, clip_id: str, outcome: str) -> str:
    account = os.getenv("ACCOUNT_NAME", "").strip() or "Clipper"
    state = load_json(config.queue_dir / "scheduler_state.json")
    position = ""
    if isinstance(state, dict):
        slot_index = state.get("slot_index")
        total = state.get("total_slots") or len(state.get("slots") or [])
        if isinstance(slot_index, int) and total:
            position = f" [{slot_index + 1}/{total}]"
    return f"{account}{position} {outcome} - {clip_id}"


PLATFORM_LABELS = {INSTAGRAM: "Instagram", YOUTUBE: "YouTube"}


def send_posted_alert(*, clip_id: str, author_handle: str, publish_result: dict[str, Any] | None = None, details: str = "", title: str = "", subject: str | None = None, results: dict[str, Any] | None = None, failures: dict[str, Exception] | None = None) -> None:
    results = dict(results) if results else {INSTAGRAM: publish_result or {}}
    subject = subject or f"Clipper Success - {clip_id}"
    lines = []
    for platform, result in results.items():
        result = result if isinstance(result, dict) else {}
        if platform == YOUTUBE:
            lines.append(f"YouTube video: {result.get('url') or result.get('id') or 'unknown'}")
        else:
            lines.append(f"Instagram publish id: {result.get('id') or 'unknown'}")
    for platform, error in (failures or {}).items():
        lines.append(f"{PLATFORM_LABELS.get(platform, platform)} FAILED: {error}")
    body = (
        f"Clip posted successfully.\n"
        f"Clip ID: {clip_id}\n"
        f"{details or f'Author: @{author_handle}' + chr(10)}"
        + "\n".join(lines) + "\n"
    )
    if title:
        body += f"\n\nDetails\n{title}\n"
    send_email_alert(app_name="ClipPublisher", subject=subject, body=body)


class QueuePublisherRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _sourcer_seen_ids_path(self) -> Path:
        state_raw = os.getenv("STATE_FILE", "data/state.json").strip()
        if os.name != "nt" and state_raw.startswith("C:"):
            state_raw = "data/state.json"
        state_file = Path(state_raw).expanduser()
        if not state_file.is_absolute():
            state_file = Path(__file__).resolve().parents[4] / state_file
        return state_file

    def _record_seen_id(self, clip_id: str) -> None:
        if not clip_id:
            return
        state_file = self._sourcer_seen_ids_path()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = load_json(state_file) if state_file.exists() else {"seen_ids": []}
        if not isinstance(payload, dict):
            payload = {"seen_ids": []}
        seen_ids = {str(value) for value in payload.get("seen_ids", [])}
        seen_ids.add(clip_id)
        payload["seen_ids"] = sorted(seen_ids)
        payload["updated_at"] = datetime.now(ZoneInfo(self.config.timezone_name)).isoformat()
        save_json(state_file, payload)
        logger.info("Recorded successful published clip ID %s in seen history at %s.", clip_id, state_file)

    def _archive_completed_item(self, run_dir: Path) -> Path:
        archive_dir = self.config.queue_dir / "Archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / run_dir.name
        suffix = 1
        while destination.exists():
            destination = archive_dir / f"{run_dir.name}_{suffix}"
            suffix += 1
        try:
            run_dir.rename(destination)
        except (PermissionError, OSError):
            shutil.move(str(run_dir), str(destination))
        logger.info("Archived published clip %s to %s.", run_dir.name, destination)
        return destination

    def _quarantine_failed_item(self, claimed: Any, error: Exception) -> None:
        """A publish is attempted exactly once: park the failed item so it is never retried."""
        run_dir = claimed.run_dir
        try:
            publisher_dir = run_dir / "publisher"
            publisher_dir.mkdir(parents=True, exist_ok=True)
            (publisher_dir / "publish_state.json").write_text(
                json.dumps({
                    "phase": "failed",
                    "status": "error",
                    "error": str(error),
                    "updated_at": datetime.now(ZoneInfo(self.config.timezone_name)).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
            self._record_seen_id(claimed.clip_id)
            archive_dir = self.config.queue_dir / "Archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            destination = archive_dir / f"{run_dir.name}_failed"
            suffix = 1
            while destination.exists():
                destination = archive_dir / f"{run_dir.name}_failed_{suffix}"
                suffix += 1
            shutil.move(str(run_dir), str(destination))
            logger.error("Publish failed once for %s; moved to %s and will not be retried.", run_dir.name, destination)
        except Exception:
            logger.exception("Could not quarantine failed publish item %s.", run_dir.name)

    def _has_pending_publish_stage(self) -> bool:
        if not self.config.queue_dir.exists():
            return False
        for child in sorted(self.config.queue_dir.iterdir(), key=lambda path: path.name):
            if not child.is_dir() or child.name in {"processing", "Archive", "Test", "source"}:
                continue
            state_path = child / "queue_state.json"
            if not state_path.exists():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(state, dict) and state.get("phase") in {"edited", "published"}:
                return True
        return False

    def _publish_claimed(self, claimed: Any, platforms: list[str]) -> None:
        """Attempt each assigned platform once, independently. Raises only if every attempted platform failed."""
        results: dict[str, Any] = {}
        failures: dict[str, Exception] = {}
        skipped: list[str] = []
        for platform in platforms:
            try:
                if platform == INSTAGRAM:
                    publish_token = resolve_publish_access_token(self.config)
                    results[platform] = publish_to_instagram(self.config, claimed, access_token=publish_token)
                elif platform == YOUTUBE:
                    if not youtube_is_configured(self.config):
                        logger.warning("YouTube is assigned to %s but not configured (credentials or token file missing); skipping.", claimed.run_dir.name)
                        skipped.append(platform)
                        continue
                    results[platform] = publish_to_youtube(self.config, claimed)
                else:
                    continue
                logger.info("%s publish response for %s: %s", PLATFORM_LABELS.get(platform, platform), claimed.run_dir.name, results[platform])
            except Exception as error:
                failures[platform] = error
                logger.exception("%s publish failed for %s: %s", PLATFORM_LABELS.get(platform, platform), claimed.run_dir.name, error)
                outcome = "Fail" if platforms == [INSTAGRAM] else f"Fail {PLATFORM_LABELS.get(platform, platform)}"
                send_failure_alert(
                    app_name="ClipPublisher",
                    error=error,
                    context=f"{PLATFORM_LABELS.get(platform, platform)} publish failure for {claimed.run_dir.name}",
                    clip_id=claimed.clip_id,
                    details=alert_details(self.config, claimed),
                    subject=alert_subject(self.config, claimed.clip_id, outcome),
                    title=original_title(claimed.manifest_path, claimed.caption),
                )
                setattr(error, "_clipper_alert_sent", True)

        if failures and not results:
            first_error = next(iter(failures.values()))
            self._quarantine_failed_item(claimed, first_error)
            raise first_error

        publisher_dir = claimed.run_dir / "publisher"
        publisher_dir.mkdir(parents=True, exist_ok=True)
        (publisher_dir / "publish_state.json").write_text(
            json.dumps({
                "phase": "published",
                "status": "partial" if failures else "success" if results else "skipped",
                "result": results.get(INSTAGRAM) if len(results) == 1 and INSTAGRAM in results else results,
                "errors": {name: str(error) for name, error in failures.items()},
                "skipped": skipped,
                "updated_at": datetime.now(ZoneInfo(self.config.timezone_name)).isoformat(),
            }, indent=2),
            encoding="utf-8",
        )
        if results:
            send_posted_alert(
                clip_id=claimed.clip_id,
                author_handle=claimed.author_handle,
                results=results,
                failures=failures,
                details=alert_details(self.config, claimed),
                subject=alert_subject(self.config, claimed.clip_id, "Partial" if failures else "Success"),
                title=original_title(claimed.manifest_path, claimed.caption),
            )
            self._record_seen_id(claimed.clip_id)
        self._archive_completed_item(claimed.run_dir)

    def process_one_ready_item(self, *, bypass_schedule: bool = False) -> bool:
        del bypass_schedule
        ready_items = []
        for item in discover_queue_items(self.config):
            if is_item_ready(item.run_dir, self.config.ready_stability_seconds):
                ready_items.append(item)

        if not ready_items:
            logger.info("No ready queue item found.")
            return False

        item = ready_items[0]
        # Read before claiming: claiming marks this slot done and the scheduler may overwrite the state afterwards.
        platforms = read_slot_platforms(self.config.queue_dir)
        claimed = claim_item(self.config, item)
        logger.info("Claimed ready queue item %s for publishing to %s.", claimed.run_dir.name, ", ".join(platforms))
        self._publish_claimed(claimed, platforms)
        return True

    def run_forever(self) -> None:
        logger.info("ClipPublisher is watching %s for edited queue items.", self.config.queue_dir)
        while True:
            try:
                ready_items = []
                for item in discover_queue_items(self.config):
                    if is_item_ready(item.run_dir, self.config.ready_stability_seconds):
                        ready_items.append(item)

                if not ready_items:
                    if self._has_pending_publish_stage():
                        logger.info("ClipPublisher is waiting for an edited queue item to become ready for publish.")
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                item = ready_items[0]
                platforms = read_slot_platforms(self.config.queue_dir)
                claimed = claim_item(self.config, item)
                logger.info("Found ready queue item %s. Claiming and publishing to %s immediately.", claimed.run_dir.name, ", ".join(platforms))
                try:
                    self._publish_claimed(claimed, platforms)
                    logger.info("Completed publishing %s.", claimed.run_dir.name)
                except Exception:
                    logger.error("Publishing %s failed on every platform; it was quarantined.", claimed.run_dir.name)
                time.sleep(self.config.poll_interval_seconds)
            except KeyboardInterrupt:
                logger.info("Shutting down ClipPublisher.")
                return
