"""Drawdown recovery timeline service.

Identifies each drawdown episode from cumulative PnL and measures how long
recovery took (or whether it is still underwater).  Read-only.

Inspired by QuantStats' underwater chart and recovery-time analytics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RecoveryService"]


class RecoveryService:
    """Drawdown episode detection and recovery-time measurement."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        # build cumulative PnL series
        cum = 0.0
        series: list[tuple[int, float]] = []
        for _, pnl in rows:
            cum += pnl
            series.append((len(series), cum))

        # detect drawdown episodes
        episodes: list[dict[str, Any]] = []
        peak = series[0][1]
        peak_idx = 0
        trough = peak
        trough_idx = 0
        in_dd = False

        for idx, val in series:
            if val >= peak:
                if in_dd:
                    # recovered
                    episodes.append(
                        _episode(
                            peak, trough, peak_idx, trough_idx, idx, len(series)
                        )
                    )
                    in_dd = False
                peak = val
                peak_idx = idx
                trough = val
                trough_idx = idx
            else:
                if not in_dd:
                    in_dd = True
                    trough = val
                    trough_idx = idx
                elif val < trough:
                    trough = val
                    trough_idx = idx

        # still underwater
        if in_dd:
            episodes.append(
                _episode(peak, trough, peak_idx, trough_idx, None, len(series))
            )

        recovered = [e for e in episodes if e["recovered"]]
        recovery_trades = [e["recovery_trades"] for e in recovered]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "total_episodes": len(episodes),
            "recovered_count": len(recovered),
            "underwater_count": len(episodes) - len(recovered),
            "avg_recovery_trades": (
                round(sum(recovery_trades) / len(recovery_trades), 1)
                if recovery_trades
                else None
            ),
            "max_recovery_trades": max(recovery_trades) if recovery_trades else None,
            "max_drawdown": round(
                min(e["drawdown"] for e in episodes) if episodes else 0, 2
            ),
            "episodes": episodes[-20:],  # most recent 20
        }

    def _fetch(
        self, symbol: str | None, days: int
    ) -> list[tuple[datetime, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.filled_at, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.execute(stmt).all()
        return [
            (r[0], float(r[1]))
            for r in rows
            if r[0] is not None and r[1] is not None
        ]


def _episode(
    peak: float,
    trough: float,
    peak_idx: int,
    trough_idx: int,
    recovery_idx: int | None,
    total: int,
) -> dict[str, Any]:
    dd = trough - peak
    dd_pct = (dd / abs(peak) * 100) if peak != 0 else 0.0
    recovered = recovery_idx is not None
    return {
        "peak_trade_index": peak_idx,
        "trough_trade_index": trough_idx,
        "drawdown": round(dd, 2),
        "drawdown_pct": round(dd_pct, 2),
        "recovered": recovered,
        "recovery_trades": (recovery_idx - trough_idx) if recovered else None,
        "duration_trades": trough_idx - peak_idx,
        "still_underwater": not recovered,
    }
