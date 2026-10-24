from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from common.reporting import _database, _database_path, _usage_run, read_run_reporting

logger = logging.getLogger(__name__)
PRICING_DATE = "2026-10-22"
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
USD_TO_CAD = Decimal("1.4021")
FX_DATE = "2026-10-22"
FX_SOURCE = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"
# Standard paid-tier USD per million tokens: input, audio input, output, cached input, cached audio.
RATES = {
    "gemini-2.5-flash-lite": ("0.10", "0.30", "0.40", "0.01", "0.03"),
    "gemini-2.5-flash": ("0.30", "1.00", "2.50", "0.03", "0.10"),
    "gemini-3.1-pro-preview": ("2.00", "2.00", "12.00", "0.20", "0.20"),
}


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid token count")
    return value


def estimate_cost(model: str, usage: dict, *, has_audio: bool = False) -> Decimal:
    """Price one text-output generateContent response; unsupported billing stays unknown."""
    normalized = model.removeprefix("models/")
    # Accept dated revisions of known aliases, not arbitrary preview models.
    normalized = re.sub(r"-\d{2}-\d{2}$", "", normalized)
    if normalized not in RATES:
        raise ValueError("unpriced model")
    prompt = _count(usage.get("promptTokenCount"))
    output = _count(usage.get("candidatesTokenCount", 0))
    thoughts = _count(usage.get("thoughtsTokenCount", 0))
    if "candidatesTokenCount" not in usage and "thoughtsTokenCount" not in usage:
        raise ValueError("missing output usage")
    cached = _count(usage.get("cachedContentTokenCount", 0))
    if cached > prompt:
        raise ValueError("cached tokens exceed input")
    if usage.get("toolUsePromptTokenCount"):
        raise ValueError("tool billing is not supported")
    input_rate, audio_rate, output_rate, cache_rate, audio_cache_rate = map(Decimal, RATES[normalized])
    if normalized == "gemini-3.1-pro-preview" and prompt > 200_000:
        input_rate, audio_rate, output_rate, cache_rate, audio_cache_rate = map(
            Decimal, ("4", "4", "18", "0.40", "0.40")
        )

    def audio_tokens(key: str, total: int, required: bool) -> int:
        details = usage.get(key)
        if details is None:
            if required:
                raise ValueError("missing audio modality usage")
            return 0
        if not isinstance(details, list):
            raise ValueError("invalid modality usage")
        counts: dict[str, int] = {}
        for detail in details:
            if not isinstance(detail, dict) or detail.get("modality") not in {"TEXT", "IMAGE", "VIDEO", "AUDIO"}:
                raise ValueError("unsupported modality")
            modality = detail["modality"]
            counts[modality] = counts.get(modality, 0) + _count(detail.get("tokenCount"))
        if sum(counts.values()) != total:
            raise ValueError("incomplete modality usage")
        return counts.get("AUDIO", 0)

    audio = audio_tokens("promptTokensDetails", prompt, has_audio and input_rate != audio_rate)
    cached_audio = audio_tokens("cacheTokensDetails", cached, bool(cached and audio and cache_rate != audio_cache_rate))
    if cached_audio > audio or cached - cached_audio > prompt - audio:
        raise ValueError("inconsistent cached modality usage")
    return (
        Decimal(prompt - audio - cached + cached_audio) * input_rate
        + Decimal(audio - cached_audio) * audio_rate
        + Decimal(cached - cached_audio) * cache_rate
        + Decimal(cached_audio) * audio_cache_rate
        + Decimal(output + thoughts) * output_rate
    ) / Decimal(1_000_000)


def request_gemini(
    queue_dir: Path, *, model: str, stage: str, run_dir: Path | None = None, **kwargs
) -> requests.Response:
    """Journal before sending; interrupted, timed-out and missing-usage calls remain unknown."""
    from common.creators import active_creator_name, read_creator

    run_id = _usage_run.get()
    creator = active_creator_name.get()
    if run_dir is not None:
        run_id = read_run_reporting(run_dir).get("run_id") or f"clip:{run_dir.name}"
        creator = read_creator(run_dir).get("display_name") or creator
    attempt_id = uuid4().hex
    with _database(queue_dir) as db:
        db.execute(
            "INSERT INTO gemini_attempts "
            "(id, run_id, occurred_at, stage, requested_model, actual_model, unknown_reason, pricing_date, finish_reason, fx_rate, fx_date, creator) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (attempt_id, run_id or f"unattributed:{attempt_id}", datetime.now(timezone.utc).isoformat(),
             stage, model, model, "request interrupted or no response received", PRICING_DATE, str(USD_TO_CAD), FX_DATE, creator),
        )
    # Never journal prompts, audio, API keys, response text or exception URLs.
    response = requests.post(**kwargs)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    usage = data.get("usageMetadata")
    actual = data.get("modelVersion") or model
    if not isinstance(actual, str):
        actual = model
    cost = None
    reason = "missing usage metadata"
    if isinstance(usage, dict):
        try:
            if any(isinstance(candidate, dict) and candidate.get("groundingMetadata") for candidate in data.get("candidates", [])):
                raise ValueError("grounding charges are not supported")
            parts = kwargs.get("json", {}).get("contents", [])
            has_audio = any(
                str(part.get("inline_data", part.get("inlineData", {})).get("mime_type",
                    part.get("inlineData", {}).get("mimeType", ""))).startswith("audio/")
                for content in parts for part in content.get("parts", [])
            )
            cost = estimate_cost(actual, usage, has_audio=has_audio)
            reason = ""
        except ValueError as error:
            reason = str(error)
    candidates = data.get("candidates") or []
    finish = str(candidates[0].get("finishReason") or "") if candidates and isinstance(candidates[0], dict) else ""
    try:
        with _database(queue_dir) as db:
            db.execute(
                "UPDATE gemini_attempts SET actual_model=?, usage_json=?, cost_usd=?, unknown_reason=?, "
                "finish_reason=? WHERE id=?",
                (actual, json.dumps(usage) if isinstance(usage, dict) else None,
                 str(cost) if cost is not None else None, reason, finish, attempt_id),
            )
    except (OSError, sqlite3.Error):
        # The pending entry is deliberately unknown; never turn an external success into a retry.
        logger.exception("Could not save Gemini usage; attempt %s remains unknown.", attempt_id)
    return response


@dataclass(frozen=True)
class GeminiUsage:
    rows: tuple[dict, ...] = ()

    @property
    def cost_usd(self) -> Decimal:
        return sum((Decimal(row["cost_usd"]) for row in self.rows if row["cost_usd"] is not None), Decimal(0))

    @property
    def unknown_calls(self) -> int:
        return sum(row["cost_usd"] is None for row in self.rows)

    @property
    def cost_cad(self) -> Decimal:
        return sum((
            Decimal(row["cost_usd"]) * Decimal(row["fx_rate"])
            for row in self.rows if row["cost_usd"] is not None
        ), Decimal(0))

    def email_text(self, scope: str) -> str:
        lines = [
            f"Gemini AI calls ({scope}): {len(self.rows):,}",
            f"Estimated Gemini AI cost ({scope}, known subtotal): ${self.cost_cad:.6f} CAD",
        ]
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in self.rows:
            groups.setdefault((row["actual_model"], row["stage"]), []).append(row)
        for (model, stage), rows in sorted(groups.items()):
            subtotal = sum((Decimal(r["cost_usd"]) * Decimal(r["fx_rate"]) for r in rows if r["cost_usd"] is not None), Decimal(0))
            tokens = {"promptTokenCount": 0, "candidatesTokenCount": 0, "thoughtsTokenCount": 0}
            for row in rows:
                usage = json.loads(row["usage_json"]) if row["usage_json"] else {}
                for key in tokens:
                    value = usage.get(key, 0)
                    if type(value) is int and value >= 0:
                        tokens[key] += value
            lines.append(
                f"  {model} / {stage}: {len(rows)} call(s), "
                f"input {tokens['promptTokenCount']:,}, output {tokens['candidatesTokenCount']:,}, "
                f"thinking {tokens['thoughtsTokenCount']:,} tokens; ${subtotal:.6f} CAD"
            )
        if self.unknown_calls:
            reasons = sorted({row["unknown_reason"] for row in self.rows if row["cost_usd"] is None})
            lines.append(
                f"WARNING: {self.unknown_calls} Gemini AI attempt(s) have unknown cost and are excluded "
                f"from the subtotal ({'; '.join(reasons)})."
            )
        pricing_dates = sorted({row["pricing_date"] for row in self.rows}) or [PRICING_DATE]
        lines.append(f"Standard paid-tier estimate; pricing snapshot(s) {', '.join(pricing_dates)}; not an invoice.")
        if self.rows:
            lines.append("Run costs are cumulative across platforms/replays; do not add repeated run totals.")
        return "\n".join(lines) + "\n"


def gemini_usage(
    queue_dir: Path, *, run_id: str | None = None, day: date | None = None, tz_name: str = "UTC"
) -> GeminiUsage:
    if run_id is not None:
        where, args = "run_id = ?", (run_id,)
    elif day is not None:
        tz = ZoneInfo(tz_name)
        start = datetime.combine(day, time.min, tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc).isoformat()
        where, args = "occurred_at >= ? AND occurred_at < ?", (start, end)
    else:
        raise ValueError("Gemini usage requires a run_id or day.")
    if not _database_path(queue_dir).exists():
        return GeminiUsage()
    with _database(queue_dir) as db:
        rows = db.execute(f"SELECT * FROM gemini_attempts WHERE {where} ORDER BY occurred_at, id", args).fetchall()
    return GeminiUsage(tuple(dict(row) for row in rows))


def gemini_publish_details(queue_dir: Path, run_dir: Path | None) -> str:
    if run_dir is None:
        return "Gemini AI cost (run): unknown (no clip attribution available)\n"
    try:
        run_id = read_run_reporting(run_dir).get("run_id") or f"clip:{run_dir.name}"
        usage = gemini_usage(queue_dir, run_id=run_id)
        if not usage.rows:
            return "Gemini AI cost (run): unknown (no recorded Gemini usage; tracking is not retroactive)\n"
        return usage.email_text("run")
    except (OSError, ValueError, sqlite3.Error):
        logger.exception("Could not read Gemini usage for %s.", run_dir)
        return "Gemini AI cost (run): unknown (usage journal could not be read)\n"
