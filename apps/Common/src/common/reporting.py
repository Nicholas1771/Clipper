from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from common.env import _find_repo_root, looks_like_windows_path

logger = logging.getLogger(__name__)
APIFY_CAD_PER_1000 = Decimal("4.26")
_usage_run: ContextVar[str | None] = ContextVar("apify_usage_run", default=None)


@contextlib.contextmanager
def sourcing_run():
    run_id = uuid4().hex
    token = _usage_run.set(run_id)
    try:
        yield run_id
    finally:
        _usage_run.reset(token)


def _database_path(queue_dir: Path) -> Path:
    # Outside the queue so startup cleanup and archiving cannot move the journal.
    return queue_dir.parent / f"{queue_dir.name}_reporting.sqlite3"


@contextlib.contextmanager
def _database(queue_dir: Path):
    path = _database_path(queue_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS usage "
                "(run_id TEXT NOT NULL, occurred_at TEXT NOT NULL, results INTEGER)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS posts "
                "(scheduled_for TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, "
                "detail TEXT NOT NULL, creator TEXT NOT NULL DEFAULT '', PRIMARY KEY (scheduled_for, platform))"
            )
            if "creator" not in {row[1] for row in connection.execute("PRAGMA table_info(posts)")}:
                connection.execute("ALTER TABLE posts ADD COLUMN creator TEXT NOT NULL DEFAULT ''")
            yield connection
    finally:
        connection.close()


@dataclass(frozen=True)
class ApifyUsage:
    requests: int = 0
    unknown_calls: int = 0

    @property
    def cost_cad(self) -> Decimal:
        return Decimal(self.requests) * APIFY_CAD_PER_1000 / 1000

    def email_text(self, scope: str, *, compact: bool = False) -> str:
        text = (
            f"Apify requests: {self.requests:,}\n"
            f"Apify cost: ${self.cost_cad:.4f} CAD\n"
        ) if compact else (
            f"Apify requests ({scope}, scraped clips): {self.requests:,}\n"
            f"Estimated Apify cost ({scope}): ${self.cost_cad:.4f} CAD "
            f"(${APIFY_CAD_PER_1000} CAD / 1,000 scraped clips)\n"
        )
        if self.unknown_calls:
            text += (
                f"WARNING: {self.unknown_calls} Apify call(s) returned no usable dataset; "
                "their usage/cost is unknown and excluded from this estimate.\n"
            )
        return text


def record_apify_usage(queue_dir: Path, results: int | None, *, now: datetime | None = None) -> None:
    with _database(queue_dir) as db:
        db.execute(
            "INSERT INTO usage VALUES (?, ?, ?)",
            (_usage_run.get() or uuid4().hex, (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(), results),
        )


def apify_usage(
    queue_dir: Path, *, run_id: str | None = None, day: date | None = None, tz_name: str = "UTC"
) -> ApifyUsage:
    if run_id is not None:
        where, args = "run_id = ?", (run_id,)
    elif day is not None:
        tz = ZoneInfo(tz_name)
        start = datetime.combine(day, time.min, tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc).isoformat()
        where, args = "occurred_at >= ? AND occurred_at < ?", (start, end)
    else:
        raise ValueError("Apify usage requires a run_id or day.")
    if not _database_path(queue_dir).exists():
        return ApifyUsage()
    with _database(queue_dir) as db:
        row = db.execute(
            f"SELECT COALESCE(SUM(results), 0), COUNT(*) - COUNT(results) FROM usage WHERE {where}", args
        ).fetchone()
    return ApifyUsage(row[0], row[1])


def record_post(
    queue_dir: Path, scheduled_for: str, platforms: list[str], status: str, detail: str = "", *, creator: str = ""
) -> bool:
    if not scheduled_for:
        logger.warning("Cannot record %s post status without a scheduled time in %s.", status, queue_dir)
        return False
    try:
        with _database(queue_dir) as db:
            for platform in platforms:
                if status == "planned":
                    db.execute(
                        "INSERT OR IGNORE INTO posts (scheduled_for, platform, status, detail, creator) VALUES (?, ?, ?, ?, ?)",
                        (scheduled_for, platform, status, detail, creator),
                    )
                else:
                    condition = " WHERE posts.status NOT IN ('published', 'failed', 'skipped')" if status == "abandoned" else ""
                    db.execute(
                        "INSERT INTO posts (scheduled_for, platform, status, detail, creator) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(scheduled_for, platform) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                        "creator=CASE WHEN excluded.creator<>'' THEN excluded.creator ELSE posts.creator END" + condition,
                        (scheduled_for, platform, status, detail, creator),
                    )
        return True
    except (OSError, sqlite3.Error):
        # Accounting must not turn a successful external publish into a publish failure.
        logger.exception("Could not persist %s post status in %s.", status, queue_dir)
        return False


def record_stage_failure(queue_dir: Path, stage: str, error: Exception) -> None:
    path = queue_dir / "scheduler_state.json"
    try:
        if not path.exists():
            return
        state = json.loads(path.read_text(encoding="utf-8"))
        phases = {"ClipAI": {"scheduled", "source_consumed"}, "ClipEditor": {"ai_processed"}}
        if isinstance(state, dict) and state.get("phase") in phases[stage]:
            record_post(
                queue_dir, str(state.get("scheduled_for") or ""),
                state.get("platforms") or ["instagram"], "failed", f"{stage}: {error}",
                creator=str(state.get("creator_name") or ""),
            )
    except (OSError, ValueError):
        logger.exception("Could not record %s failure from %s.", stage, path)


def posts_for_day(queue_dir: Path, day: date, tz_name: str) -> list[dict]:
    if not _database_path(queue_dir).exists():
        return []
    with _database(queue_dir) as db:
        rows = db.execute("SELECT * FROM posts ORDER BY scheduled_for, platform").fetchall()
    return [
        dict(row) for row in rows
        if datetime.fromisoformat(row["scheduled_for"]).astimezone(ZoneInfo(tz_name)).date() == day
    ]


def read_run_reporting(run_dir: Path) -> dict:
    path = run_dir / "reporting.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid run reporting metadata: {path}")
    return payload


def cache_remaining_text(cache_file: Path, *, compact: bool = False) -> str:
    label = "Apify cache count" if compact else "Clips remaining in cache"
    try:
        if not cache_file.exists():
            count = 0
        else:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("cache has no items list")
            count = len(payload["items"])
        return f"{label}: {count:,}\n"
    except (OSError, ValueError) as error:
        logger.warning("Could not count cached clips in %s: %s", cache_file, error)
        return f"{label}: unknown (cache could not be read)\n"


def publish_usage_details(
    queue_dir: Path, run_dir: Path | None = None, *, replay: bool = False, compact: bool = False
) -> str:
    metadata = read_run_reporting(run_dir) if run_dir is not None and not replay else {}
    raw = str(metadata.get("cache_file") or os.getenv("CANDIDATE_CACHE_FILE") or "data/candidate_cache.json")
    if os.name != "nt" and looks_like_windows_path(raw):
        raw = "data/candidate_cache.json"
    cache_file = Path(raw).expanduser()
    if not cache_file.is_absolute():
        cache_file = _find_repo_root(Path(__file__).resolve()) / cache_file
    if replay:
        usage_text = ApifyUsage().email_text("run", compact=compact)
    elif metadata.get("run_id"):
        try:
            usage_text = apify_usage(queue_dir, run_id=metadata["run_id"]).email_text("run", compact=compact)
        except (OSError, sqlite3.Error):
            logger.exception("Could not read Apify usage for run %s.", metadata["run_id"])
            reason = "usage journal could not be read"
            usage_text = (
                f"Apify requests: unknown ({reason})\nApify cost: unknown ({reason})\n"
                if compact else f"Apify requests / estimated cost (run): unknown ({reason})\n"
            )
    else:
        reason = "run predates usage tracking"
        usage_text = (
            f"Apify requests: unknown ({reason})\nApify cost: unknown ({reason})\n"
            if compact else f"Apify requests / estimated cost (run): unknown ({reason})\n"
        )
    return usage_text + cache_remaining_text(cache_file, compact=compact)
