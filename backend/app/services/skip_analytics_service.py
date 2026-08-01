"""Skip reason analytics service.

Aggregates ORDER_SKIPPED trade events by skip_category (FEE / RISK /
PENDING / POSITION / SESSION / COOLDOWN / REPRICING) to show why the
engine refrained from trading, per symbol and over time.  Read-only.

Inspired by Freqtrade's enter/exit reason tagging and its rejected-signal
analysis tooling.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradeEvent
from app.services.analytics_trade_sample_service import trade_local_day

__all__ = ["SkipAnalyticsService"]

_SKIP_CATEGORIES = frozenset({
    "FEE",
    "REPRICING",
    "COOLDOWN",
    "RISK",
    "PENDING",
    "POSITION",
    "SESSION",
})


@dataclass(frozen=True)
class _ParsedCategory:
    category: str
    issue_code: str | None = None


class SkipAnalyticsService:
    """Aggregates skipped-order events into category analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 30) -> dict[str, Any]:
        rows = self._fetch(days)
        if not rows:
            return {
                "days": days,
                "sample_size": 0,
                "event_quality": _event_quality(0, {}),
                "error": "No skipped-order events in window.",
            }

        by_category: dict[str, int] = defaultdict(int)
        by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_day: dict[str, int] = defaultdict(int)
        reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        sides: dict[str, int] = defaultdict(int)
        quality_issues: dict[str, int] = defaultdict(int)

        for event in rows:
            parsed = _category_of(event)
            category = parsed.category
            if parsed.issue_code is not None:
                quality_issues[parsed.issue_code] += 1
            by_category[category] += 1
            sym = event.symbol or "unknown"
            by_symbol[sym][category] += 1
            if event.created_at:
                event_day = (
                    trade_local_day(event.symbol, event.created_at)
                    if event.symbol
                    else _utc_date(event.created_at)
                )
                by_day[event_day.isoformat()] += 1
            sides[(event.side or "UNKNOWN").strip().upper()] += 1
            message = (event.message or "").strip()
            if message:
                reasons[category][message[:120]] += 1

        total = len(rows)
        category_rows = [
            {"category": cat, "count": n, "share": round(n / total, 4)}
            for cat, n in sorted(
                by_category.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]

        symbol_rows = [
            {
                "symbol": sym,
                "total": sum(cats.values()),
                "by_category": dict(
                    sorted(
                        cats.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ),
            }
            for sym, cats in sorted(
                by_symbol.items(),
                key=lambda item: (-sum(item[1].values()), item[0]),
            )
        ]

        top_reasons = [
            {
                "category": cat,
                "reasons": [
                    {"message": msg, "count": n}
                    for msg, n in sorted(
                        msgs.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:3]
                ],
            }
            for cat, msgs in sorted(
                reasons.items(),
                key=lambda item: (-sum(item[1].values()), item[0]),
            )
        ]

        daily = [{"date": d, "count": n} for d, n in sorted(by_day.items())]

        return {
            "days": days,
            "sample_size": total,
            "event_quality": _event_quality(total, quality_issues),
            "by_category": category_rows,
            "by_symbol": symbol_rows,
            "by_side": dict(
                sorted(sides.items(), key=lambda item: (-item[1], item[0]))
            ),
            "top_reasons": top_reasons,
            "daily": daily,
        }

    def _fetch(self, days: int) -> list[TradeEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(TradeEvent)
            .where(TradeEvent.event_type == "ORDER_SKIPPED", TradeEvent.created_at >= cutoff)
            .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
        )
        return list(self._db.execute(stmt).scalars().all())


def _category_of(event: TradeEvent) -> _ParsedCategory:
    try:
        payload = json.loads(event.payload_json or "{}")
    except (TypeError, ValueError):
        return _ParsedCategory("UNKNOWN", "MALFORMED_JSON")
    if not isinstance(payload, dict):
        return _ParsedCategory("UNKNOWN", "PAYLOAD_NOT_OBJECT")
    raw_category = payload.get("skip_category")
    if not isinstance(raw_category, str) or not raw_category.strip():
        return _ParsedCategory("UNKNOWN", "SKIP_CATEGORY_MISSING")
    category = raw_category.strip().upper()
    if category not in _SKIP_CATEGORIES:
        return _ParsedCategory(category, "SKIP_CATEGORY_UNKNOWN")
    return _ParsedCategory(category)


def _event_quality(total: int, issues: dict[str, int]) -> dict[str, Any]:
    invalid = sum(issues.values())
    return {
        "status": "COMPLETE" if invalid == 0 else "DEGRADED",
        "total_event_count": total,
        "valid_event_count": max(0, total - invalid),
        "invalid_event_count": invalid,
        "issues": [
            {"code": code, "count": count}
            for code, count in sorted(issues.items())
        ],
    }


def _utc_date(value: datetime):
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return normalized.date()
