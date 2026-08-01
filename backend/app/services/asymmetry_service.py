"""Win/loss asymmetry analysis service.

Provides a detailed breakdown of the asymmetry between winning and losing
trades: magnitude distributions, tail behavior, and conditional patterns.
Read-only.

Inspired by QuantStats' win/loss analysis and Edgewonk's trade anatomy.
"""
from __future__ import annotations

from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["AsymmetryService"]


class AsymmetryService:
    """Detailed win/loss distribution asymmetry analysis."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            symbol=symbol,
            lookback_days=lookback_days,
        )
        if mixed_error is not None:
            return mixed_error
        pnls = [trade.net_pnl for trade in sample.trades]
        if len(pnls) < 10:
            return analytics_response(
                sample,
                {
                    "symbol": symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(pnls),
                    "error": "Need at least 10 closed trades.",
                },
            )

        wins = sorted([p for p in pnls if p > 0], reverse=True)
        losses = sorted([p for p in pnls if p < 0])
        zeros = sum(1 for p in pnls if p == 0)

        def _stats(vals: list[float], label: str) -> dict[str, Any]:
            if not vals:
                return {
                    "label": label,
                    "count": 0,
                    "total": 0,
                    "avg": 0,
                    "median": 0,
                    "max": 0,
                    "min": 0,
                    "largest_magnitude": 0,
                    "smallest_magnitude": 0,
                    "top3_share": 0,
                }
            total = sum(vals)
            n = len(vals)
            sorted_v = sorted(vals)
            by_magnitude = sorted(vals, key=abs, reverse=True)
            top3 = sum(by_magnitude[:3])
            return {
                "label": label,
                "count": n,
                "total": round(total, 2),
                "avg": round(total / n, 2),
                "median": round(median(sorted_v), 2),
                # Keep conventional signed numeric extrema, and expose
                # magnitude extrema explicitly so callers never have to infer
                # whether ``max`` means closest to zero or the worst loss.
                "max": round(max(vals), 2),
                "min": round(min(vals), 2),
                "largest_magnitude": round(by_magnitude[0], 2),
                "smallest_magnitude": round(by_magnitude[-1], 2),
                "top3_share": round(abs(top3) / abs(total), 4) if total != 0 else 0,
            }

        win_stats = _stats(wins, "wins")
        loss_stats = _stats(losses, "losses")

        # Calculate the ratio from unrounded means. Display rounding in the
        # side summaries must not alter the analytical result.
        avg_win_raw = sum(wins) / len(wins) if wins else None
        avg_loss_raw = abs(sum(losses) / len(losses)) if losses else None
        asymmetry_ratio = (
            avg_win_raw / avg_loss_raw
            if avg_win_raw is not None
            and avg_loss_raw is not None
            and avg_loss_raw > 0
            else None
        )

        # profit/loss concentration
        total_win = sum(wins) if wins else 0
        total_loss = abs(sum(losses)) if losses else 0

        # conditional: what happens after a big win/loss
        big_win_threshold = wins[len(wins) // 4] if len(wins) >= 4 else (wins[0] if wins else 0)
        big_loss_threshold = losses[len(losses) // 4] if len(losses) >= 4 else (losses[0] if losses else 0)

        after_big_win = [pnls[i + 1] for i in range(len(pnls) - 1) if pnls[i] >= big_win_threshold and big_win_threshold > 0]
        after_big_loss = [pnls[i + 1] for i in range(len(pnls) - 1) if pnls[i] <= big_loss_threshold and big_loss_threshold < 0]

        return analytics_response(
            sample,
            {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "win_stats": win_stats,
                "loss_stats": loss_stats,
                "breakeven_count": zeros,
                "asymmetry_ratio": (
                    round(asymmetry_ratio, 4)
                    if asymmetry_ratio is not None
                    else None
                ),
                "total_win": round(total_win, 2),
                "total_loss": round(total_loss, 2),
                "net_edge": round(total_win - total_loss, 2),
                "conditional": {
                    "after_big_win_avg": round(sum(after_big_win) / len(after_big_win), 2) if after_big_win else None,
                    "after_big_win_count": len(after_big_win),
                    "after_big_loss_avg": round(sum(after_big_loss) / len(after_big_loss), 2) if after_big_loss else None,
                    "after_big_loss_count": len(after_big_loss),
                },
                "assessment": _assess(asymmetry_ratio, win_stats["count"], loss_stats["count"]),
            },
        )


def _assess(ratio: float | None, wins: int, losses: int) -> str:
    if ratio is None:
        if wins == 0 and losses == 0:
            return (
                "No winning or losing trades recorded — asymmetry "
                "undefined."
            )
        if wins == 0:
            return "No winning trades recorded — asymmetry undefined."
        return "No losing trades recorded — asymmetry undefined."
    if ratio > 2.0:
        return f"Strong positive asymmetry (ratio={ratio:.2f}) — wins are much larger than losses. Classic trend-following profile."
    if ratio > 1.0:
        return f"Moderate positive asymmetry (ratio={ratio:.2f}) — wins exceed losses on average."
    if ratio > 0.5:
        return f"Negative asymmetry (ratio={ratio:.2f}) — losses exceed wins. High win-rate needed to compensate."
    return f"Severe negative asymmetry (ratio={ratio:.2f}) — losses dominate. Review stop-loss and exit logic."
