"""PnL autocorrelation analysis service.

Measures serial dependence in the trade PnL sequence to detect momentum
(winning begets winning) or mean-reversion (losses follow wins) patterns.
Read-only.

Inspired by VectorBT's return autocorrelation analytics and QuantStats'
serial dependence tearsheet.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

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
        if len(pnls) < 20:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "analysis_status": "INSUFFICIENT_SAMPLE",
                "error": "Need at least 20 closed trades.",
            })

        n = len(pnls)
        mean = sum(pnls) / n
        denominator = sum((p - mean) ** 2 for p in pnls)
        if denominator == 0:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": n,
                "analysis_status": "DEGENERATE",
                "pattern": "degenerate",
                "error": (
                    "PnL sequence has zero variance; autocorrelation and "
                    "serial-independence tests are undefined."
                ),
            })

        lags: list[dict[str, Any]] = []
        raw_acfs: list[float] = []
        for k in range(1, min(max_lag + 1, n)):
            numerator = sum(
                (pnls[i] - mean) * (pnls[i - k] - mean)
                for i in range(k, n)
            )
            acf = numerator / denominator
            raw_acfs.append(acf)
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
        for k, acf in enumerate(raw_acfs, start=1):
            q_stat += (acf**2) / (n - k)
        q_stat *= n * (n + 2)

        sig_count = sum(1 for l in lags if l["significant"])
        pattern = _classify(lags[0]["acf"] if lags else 0, sig_count)

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "analysis_status": "READY",
            "lags": lags,
            "ljung_box_q": round(q_stat, 4),
            "significant_lags": sig_count,
            "confidence_band": round(1.96 / (n**0.5), 4),
            "pattern": pattern,
            "interpretation": _interpret(pattern, lags[0]["acf"] if lags else 0),
        })


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
