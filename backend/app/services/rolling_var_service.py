"""Rolling VaR/CVaR service.

Computes historical Value-at-Risk and Conditional VaR (Expected Shortfall)
over a rolling window of trade PnLs.  Read-only.

Inspired by VectorBT's risk analytics and QuantStats' VaR tearsheet.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    market_local_datetime,
    mixed_currency_error,
)

__all__ = ["RollingVarService"]


class RollingVarService:
    """Rolling historical VaR and CVaR computation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        window: int = 30,
        confidence: float = 0.95,
    ) -> dict[str, Any]:
        normalized_symbol = (
            symbol.strip().upper() if symbol and symbol.strip() else None
        )
        sample = load_analytics_trade_sample(
            self._db,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        trades = sample.trades
        base_payload = {
            "symbol": normalized_symbol or "ALL",
            "lookback_days": lookback_days,
            "window": window,
            "confidence": confidence,
            "sample_size": len(trades),
        }
        currency_error = mixed_currency_error(
            sample,
            payload=base_payload,
        )
        if currency_error is not None:
            return currency_error
        if len(trades) < window:
            return analytics_response(
                sample,
                {
                    **base_payload,
                    "error": f"Need at least {window} trades.",
                },
            )

        points: list[dict[str, Any]] = []
        tail_count = max(
            1,
            int(math.ceil((1.0 - confidence) * window - 1e-12)),
        )
        for i in range(window - 1, len(trades)):
            outcomes = sorted(
                trade.net_pnl
                for trade in trades[i - window + 1 : i + 1]
            )
            tail = outcomes[:tail_count]
            var = max(0.0, -tail[-1])
            cvar = max(var, 0.0, -sum(tail) / len(tail))
            endpoint = trades[i]
            points.append(
                {
                    "index": i,
                    "at": market_local_datetime(
                        endpoint.symbol,
                        endpoint.exit_at,
                    ).isoformat(),
                    "var": round(var, 2),
                    "cvar": round(cvar, 2),
                }
            )

        latest = points[-1] if points else None
        vars_list = [p["var"] for p in points]
        cvars_list = [p["cvar"] for p in points]

        return analytics_response(
            sample,
            {
                **base_payload,
                "tail_count": tail_count,
                "points": points[-50:],
                "summary": {
                    "latest_var": latest["var"] if latest else 0,
                    "latest_cvar": latest["cvar"] if latest else 0,
                    "var_mean": round(sum(vars_list) / len(vars_list), 2),
                    "var_max": round(max(vars_list), 2),
                    "cvar_mean": round(
                        sum(cvars_list) / len(cvars_list),
                        2,
                    ),
                    "cvar_max": round(max(cvars_list), 2),
                },
            },
        )
