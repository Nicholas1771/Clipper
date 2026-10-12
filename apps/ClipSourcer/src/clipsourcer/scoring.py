from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from clipsourcer.models import ClipCandidate


def normalize_term(value: str) -> str:
    return value.strip().lower().lstrip("#")


def matches_keyword(candidate: ClipCandidate, keyword: str) -> bool:
    normalized = normalize_term(keyword)
    if not normalized:
        return True

    text = candidate.text.lower()
    if normalized in text or f"#{normalized}" in text:
        return True

    return normalized in {normalize_term(tag) for tag in candidate.hashtags}


def is_recent(candidate: ClipCandidate, now: datetime, lookback_hours: int) -> bool:
    cutoff = now - timedelta(hours=lookback_hours)
    return candidate.created_at >= cutoff


def has_minimum_views(candidate: ClipCandidate, minimum_view_count: int) -> bool:
    return candidate.plays >= minimum_view_count


def viral_score(candidate: ClipCandidate, now: datetime) -> float:
    age_hours = max((now - candidate.created_at).total_seconds() / 3600, 0.25)
    weighted_engagement = (
        candidate.likes * 1.5
        + candidate.comments * 4.0
        + candidate.shares * 8.0
        + candidate.saves * 6.0
    )
    engagement_density = (weighted_engagement / max(candidate.plays, 1)) * 1000.0
    reach_signal = math.log1p(candidate.plays)
    recency_factor = 1.0 / (age_hours ** 0.6)
    score = (engagement_density * 2.5 + reach_signal * 12.0) * recency_factor
    return round(score, 2)


def rank_candidates(
    candidates: list[ClipCandidate],
    *,
    now: datetime | None = None,
    lookback_hours: int,
    keyword: str,
    minimum_view_count: int,
    seen_ids: set[str],
) -> list[tuple[ClipCandidate, float]]:
    del seen_ids
    current_time = now or datetime.now(timezone.utc)
    filtered = [
        candidate
        for candidate in candidates
        if is_recent(candidate, current_time, lookback_hours)
        and matches_keyword(candidate, keyword)
        and has_minimum_views(candidate, minimum_view_count)
    ]
    ranked = [(candidate, viral_score(candidate, current_time)) for candidate in filtered]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked
