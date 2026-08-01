"""Fee drag analysis service.

Quantifies how much trading fees erode gross profits: total fees paid,
fees as a share of gross gains, per-symbol fee drag, and a daily fee
trend.  Read-only.

Inspired by Freqtrade's fee accounting and QuantStats' cost-of-trading
tear sheets.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
    trade_local_day,
)

__all__ = ["FeeDragService"]


class FeeDragService:
    """Aggregates realized fee drag across FIFO-paired closed trades."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            payload={"days": days, "sample_size": len(sample.trades)},
        )
        if mixed_error is not None:
            return mixed_error
        rows = sample.trades
        if not rows:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": 0,
                    "error": "No closed trades in window.",
                },
            )

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
            fee = row.est_fees
            total_fees += fee
            fee_sources[row.fee_source or "UNKNOWN"] += 1
            day = trade_local_day(row.symbol, row.exit_at).isoformat()
            by_day[day] += fee

            sym = by_symbol[row.symbol]
            sym["fees"] += fee
            sym["trades"] += 1

            net = row.net_pnl
            gross = row.gross_pnl
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

        return analytics_response(
            sample,
            {
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
            },
        )
