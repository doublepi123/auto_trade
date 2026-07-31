"""PnL autocorrelation analysis service.

Measures serial dependence in the trade PnL sequence to detect momentum
(winning begets winning) or mean-reversion (losses follow wins) patterns.
Read-only.

Inspired by VectorBT's return autocorrelation analytics and QuantStats'
serial dependence tearsheet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["AutocorrelationService"]


class AutocorrelationService:
    """Lag-k autocorrelation of the PnL sequence."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        max_lag: int = 10,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 20:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 20 closed trades.",
            }

        n = len(pnls)
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / n

        lags: list[dict[str, Any]] = []
        for k in range(1, min(max_lag + 1, n)):
            if var == 0:
                acf = 0.0
            else:
                cov = sum(
                    (pnls[i] - mean) * (pnls[i - k] - mean)
                    for i in range(k, n)
                ) / (n - k)
                acf = cov / var
            # approximate 95% confidence band: ±1.96/sqrt(n)
            band = 1.96 / (n**0.5)
            lags.append(
                {
                    "lag": k,
                    "acf": round(acf, 4),
                    "significant": abs(acf) > band,
                }
            )

        # Ljung-Box Q statistic for overall serial independence
        q_stat = 0.0
        for lag_obj in lags:
            k = lag_obj["lag"]
            q_stat += (lag_obj["acf"] ** 2) / (n - k)
        q_stat *= n * (n + 2)

        sig_count = sum(1 for l in lags if l["significant"])
        pattern = _classify(lags[0]["acf"] if lags else 0, sig_count)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "lags": lags,
            "ljung_box_q": round(q_stat, 4),
            "significant_lags": sig_count,
            "confidence_band": round(1.96 / (n**0.5), 4),
            "pattern": pattern,
            "interpretation": _interpret(pattern, lags[0]["acf"] if lags else 0),
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]


def _classify(lag1: float, sig_count: int) -> str:
    if sig_count == 0:
        return "independent"
    if lag1 > 0.1:
        return "momentum"
    if lag1 < -0.1:
        return "mean-reversion"
    return "weak-dependence"


def _interpret(pattern: str, lag1: float) -> str:
    if pattern == "independent":
        return "No significant serial dependence — trade outcomes appear independent."
    if pattern == "momentum":
        return f"Positive autocorrelation (lag-1={lag1:.3f}) — wins tend to follow wins. Consider sizing up after wins."
    if pattern == "mean-reversion":
        return f"Negative autocorrelation (lag-1={lag1:.3f}) — losses tend to follow wins. Consider reducing size after wins."
    return "Weak serial dependence detected but lag-1 is near zero — higher-order patterns may exist."
