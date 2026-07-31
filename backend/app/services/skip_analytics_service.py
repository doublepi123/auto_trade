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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TradeEvent

__all__ = ["SkipAnalyticsService"]


class SkipAnalyticsService:
    """Aggregates skipped-order events into category analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 30) -> dict[str, Any]:
        rows = self._fetch(days)
        if not rows:
            return {"days": days, "sample_size": 0, "error": "No skipped-order events in window."}

        by_category: dict[str, int] = defaultdict(int)
        by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_day: dict[str, int] = defaultdict(int)
        reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        sides: dict[str, int] = defaultdict(int)

        for event in rows:
            category = _category_of(event)
            by_category[category] += 1
            sym = event.symbol or "unknown"
            by_symbol[sym][category] += 1
            if event.created_at:
                by_day[event.created_at.date().isoformat()] += 1
            sides[event.side or "UNKNOWN"] += 1
            message = (event.message or "").strip()
            if message:
                reasons[category][message[:120]] += 1

        total = len(rows)
        category_rows = [
            {"category": cat, "count": n, "share": round(n / total, 4)}
            for cat, n in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
        ]

        symbol_rows = [
            {
                "symbol": sym,
                "total": sum(cats.values()),
                "by_category": dict(sorted(cats.items(), key=lambda kv: kv[1], reverse=True)),
            }
            for sym, cats in sorted(by_symbol.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
        ]

        top_reasons = [
            {
                "category": cat,
                "reasons": [
                    {"message": msg, "count": n}
                    for msg, n in sorted(msgs.items(), key=lambda kv: kv[1], reverse=True)[:3]
                ],
            }
            for cat, msgs in sorted(reasons.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
        ]

        daily = [{"date": d, "count": n} for d, n in sorted(by_day.items())]

        return {
            "days": days,
            "sample_size": total,
            "by_category": category_rows,
            "by_symbol": symbol_rows,
            "by_side": dict(sorted(sides.items(), key=lambda kv: kv[1], reverse=True)),
            "top_reasons": top_reasons,
            "daily": daily,
        }

    def _fetch(self, days: int) -> list[TradeEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(TradeEvent)
            .where(TradeEvent.event_type == "ORDER_SKIPPED", TradeEvent.created_at >= cutoff)
            .order_by(TradeEvent.created_at.asc())
        )
        return list(self._db.execute(stmt).scalars().all())


def _category_of(event: TradeEvent) -> str:
    try:
        payload = json.loads(event.payload_json or "{}")
    except (TypeError, ValueError):
        return "UNKNOWN"
    category = payload.get("skip_category")
    return str(category) if category else "UNKNOWN"
