"""Trade tag analytics service.

Aggregates performance by user-assigned trade-note tags to surface which
qualitative labels correlate with better or worse outcomes.  Read-only.

Inspired by Freqtrade's trade-tag grouping and Edgewonk's journal analytics.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord, TradeNote

__all__ = ["TagAnalyticsService"]


class TagAnalyticsService:
    """Performance breakdown by trade-note tags."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(self, min_trades: int = 2) -> dict[str, Any]:
        notes = self._fetch_notes()
        if not notes:
            return {
                "total_notes": 0,
                "tags": [],
                "error": "No trade notes with tags found.",
            }

        # group by tag
        by_tag: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for order_id, tags, pnl, rating in notes:
            for tag in tags:
                tag_clean = tag.strip().lower()
                if tag_clean:
                    by_tag[tag_clean].append({"pnl": pnl, "rating": rating})

        tag_stats: list[dict[str, Any]] = []
        for tag, trades in by_tag.items():
            if len(trades) < min_trades:
                continue
            pnls = [t["pnl"] for t in trades]
            ratings = [t["rating"] for t in trades if t["rating"] is not None]
            wins = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            tag_stats.append(
                {
                    "tag": tag,
                    "trade_count": len(trades),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(total_pnl / len(trades), 2),
                    "win_rate": round(wins / len(trades), 4),
                    "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                }
            )

        tag_stats.sort(key=lambda x: x["total_pnl"], reverse=True)

        best = tag_stats[0] if tag_stats else None
        worst = tag_stats[-1] if tag_stats else None

        return {
            "total_notes": len(notes),
            "unique_tags": len(by_tag),
            "qualifying_tags": len(tag_stats),
            "tags": tag_stats,
            "best_tag": best,
            "worst_tag": worst,
        }

    def _fetch_notes(self) -> list[tuple[int, list[str], float, int | None]]:
        import json

        stmt = (
            select(
                TradeNote.order_id,
                TradeNote.tags_json,
                OrderRecord.net_pnl,
                TradeNote.rating,
            )
            .join(OrderRecord, TradeNote.order_id == OrderRecord.id)
            .where(
                TradeNote.tags_json.is_not(None),
                OrderRecord.net_pnl.is_not(None),
            )
        )
        rows = self._db.execute(stmt).all()
        results: list[tuple[int, list[str], float, int | None]] = []
        for r in rows:
            tags_raw = r[1]
            try:
                parsed = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
                tags = [str(t) for t in parsed] if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                continue
            if tags and r[2] is not None:
                results.append((r[0], tags, float(r[2]), r[3]))
        return results
