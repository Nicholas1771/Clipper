from contextlib import closing
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from common.gemini_usage import GeminiUsage
from common.reporting import APIFY_CAD_PER_1000, _database, _database_path


def initialize_monthly_tracking(repo_root: Path, now: datetime) -> datetime:
    path = repo_root / "data" / "monthly_costs.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS tracking (id INTEGER PRIMARY KEY CHECK(id=1), started_at TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO tracking VALUES (1, ?)", (now.astimezone(timezone.utc).isoformat(),))
        return datetime.fromisoformat(db.execute("SELECT started_at FROM tracking WHERE id=1").fetchone()[0])


def usage_between(queue: Path, start: datetime, end: datetime) -> tuple[list[dict], GeminiUsage]:
    if not _database_path(queue).exists():
        return [], GeminiUsage()
    args = (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
    with _database(queue) as db:
        apify = db.execute(
            "SELECT * FROM usage WHERE occurred_at >= ? AND occurred_at <= ?", args,
        ).fetchall()
        ai = db.execute(
            "SELECT * FROM gemini_attempts WHERE occurred_at >= ? AND occurred_at <= ?", args,
        ).fetchall()
    return [dict(row) for row in apify], GeminiUsage(tuple(dict(row) for row in ai))


def apify_cost(rows: list[dict]) -> Decimal:
    return Decimal(sum(row["results"] or 0 for row in rows)) * APIFY_CAD_PER_1000 / 1000


def cost_lines(apify: Decimal, ai: Decimal, hosting: Decimal) -> list[str]:
    return [
        f"Total cost: ${apify + ai + hosting:.2f} CAD",
        f"Apify cost: ${apify:.2f} CAD",
        f"AI cost: ${ai:.2f} CAD",
        f"Hosting cost: ${hosting:.2f} CAD",
    ]


def usage_lines(apify: list[dict], ai: GeminiUsage) -> list[str]:
    lines = [
        f"Apify requests: {sum(row['results'] or 0 for row in apify):,}",
        f"Apify cost: ${apify_cost(apify):.2f} CAD",
        f"AI calls: {len(ai.rows):,}",
        f"AI cost: ${ai.cost_cad:.2f} CAD",
    ]
    for model in sorted({row["actual_model"] for row in ai.rows}):
        usage = GeminiUsage(tuple(row for row in ai.rows if row["actual_model"] == model))
        lines.append(f"{model}: ${usage.cost_cad:.2f} CAD")
    return lines


def monthly_cost_lines(repo_root: Path, queues: list[Path], now: datetime, tz_name: str, daily_hosting: Decimal) -> list[str]:
    started = initialize_monthly_tracking(repo_root, now)
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    month_start = datetime.combine(local.date().replace(day=1), time.min, tz)
    start = max(started, month_start)
    apify_total = ai_total = Decimal(0)
    for queue in queues:
        apify, ai = usage_between(queue, start, now)
        apify_total += apify_cost(apify)
        ai_total += ai.cost_cad
    days = max(0, (local.date() - start.astimezone(tz).date()).days + 1)
    return cost_lines(apify_total, ai_total, daily_hosting * days)
