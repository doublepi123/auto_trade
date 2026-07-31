"""Fee drag analysis service.

Quantifies how much trading fees erode gross profits: total fees paid,
fees as a share of gross gains, per-symbol fee drag, and a daily fee
trend.  Read-only.

Inspired by Freqtrade's fee accounting and QuantStats' cost-of-trading
tear sheets.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["FeeDragService"]


class FeeDragService:
    """Aggregates realized fee drag across filled orders."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if not rows:
            return {"days": days, "sample_size": 0, "error": "No filled orders in window."}

        total_fees = 0.0
        total_gross = 0.0
        total_net = 0.0
        gross_wins = 0.0
        fee_sources: dict[str, int] = defaultdict(int)
        by_symbol: dict[str, dict[str, float]] = defaultdict(
            lambda: {"fees": 0.0, "gross": 0.0, "net": 0.0, "trades": 0.0}
        )
        by_day: dict[str, float] = defaultdict(float)

        for row in rows:
            fee = _row_fee(row)
            total_fees += fee
            fee_sources[row.pnl_fee_source or row.fee_source or "UNKNOWN"] += 1
            day = row.filled_at.date().isoformat() if row.filled_at else "unknown"
            by_day[day] += fee

            sym = by_symbol[row.symbol]
            sym["fees"] += fee
            sym["trades"] += 1

            if row.net_pnl is not None:
                net = float(row.net_pnl)
                gross = float(row.gross_pnl) if row.gross_pnl is not None else net
                total_net += net
                total_gross += gross
                if gross > 0:
                    gross_wins += gross
                sym["net"] += net
                sym["gross"] += gross

        n = len(rows)
        fee_per_trade = total_fees / n if n else 0.0
        fee_to_gross = (total_fees / gross_wins) if gross_wins > 0 else None

        symbol_rows = [
            {
                "symbol": sym,
                "trades": int(v["trades"]),
                "fees": round(v["fees"], 2),
                "gross_pnl": round(v["gross"], 2),
                "net_pnl": round(v["net"], 2),
                "fee_share_of_gross": round(v["fees"] / v["gross"], 4) if v["gross"] > 0 else None,
            }
            for sym, v in by_symbol.items()
        ]
        symbol_rows.sort(key=lambda r: r["fees"], reverse=True)

        daily = [
            {"date": d, "fees": round(f, 2)} for d, f in sorted(by_day.items()) if d != "unknown"
        ]

        return {
            "days": days,
            "sample_size": n,
            "total_fees": round(total_fees, 2),
            "total_gross_pnl": round(total_gross, 2),
            "total_net_pnl": round(total_net, 2),
            "avg_fee_per_trade": round(fee_per_trade, 4),
            "fee_to_gross_win_ratio": round(fee_to_gross, 4) if fee_to_gross is not None else None,
            "fee_sources": dict(sorted(fee_sources.items(), key=lambda kv: kv[1], reverse=True)),
            "by_symbol": symbol_rows,
            "daily_fees": daily,
        }

    def _fetch(self, days: int) -> list[OrderRecord]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.filled_at.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return list(self._db.execute(stmt).scalars().all())


def _row_fee(row: OrderRecord) -> float:
    """Best-effort fee for one filled order."""
    for value in (row.pnl_fee, row.actual_fee, row.estimated_fee):
        if value is not None:
            return float(value)
    return 0.0
