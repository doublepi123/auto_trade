"""Kelly criterion position sizing service.

Computes optimal bet fraction using the Kelly formula and its fractional
variants, based on historical win-rate and payoff ratio.  Read-only.

Inspired by Freqtrade's stake-amount management and QuantConnect's
portfolio construction models.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["KellyService"]


class KellyService:
    """Kelly criterion and fractional-Kelly position sizing estimates."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self,
        symbol: str | None = None,
        lookback_days: int = 90,
        fractions: list[float] | None = None,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 3:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 3 closed trades.",
            }

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / len(pnls)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
        payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # Full Kelly: f* = W - (1-W)/R
        if payoff_ratio > 0 and payoff_ratio != float("inf"):
            kelly_full = win_rate - (1.0 - win_rate) / payoff_ratio
        elif payoff_ratio == float("inf"):
            kelly_full = win_rate
        else:
            kelly_full = 0.0
        kelly_full = max(kelly_full, 0.0)

        fracs = fractions or [1.0, 0.5, 0.25, 0.1]
        variants: list[dict[str, Any]] = []
        for f in fracs:
            frac_kelly = kelly_full * f
            variants.append(
                {
                    "fraction": f,
                    "label": f"{'Full' if f == 1.0 else f'{int(f*100)}%'} Kelly",
                    "allocation_pct": round(frac_kelly * 100, 2),
                    "expected_growth": round(
                        win_rate * _safe_log(1 + frac_kelly * payoff_ratio)
                        + (1 - win_rate) * _safe_log(1 - frac_kelly),
                        6,
                    ),
                }
            )

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_ratio": round(payoff_ratio, 4),
            "kelly_full_pct": round(kelly_full * 100, 2),
            "variants": variants,
            "recommendation": _recommendation(kelly_full, win_rate, payoff_ratio),
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]


def _safe_log(x: float) -> float:
    import math

    return math.log(x) if x > 0 else -10.0


def _recommendation(kelly: float, wr: float, pr: float) -> str:
    if kelly <= 0:
        return "Negative or zero Kelly — no positive edge detected. Avoid sizing up."
    if kelly > 0.25:
        return "High Kelly fraction — consider quarter-Kelly to reduce variance."
    if wr < 0.4:
        return "Low win-rate edge — use fractional Kelly (≤25%) to manage drawdown."
    return "Moderate edge — half-Kelly is a reasonable default for live sizing."
