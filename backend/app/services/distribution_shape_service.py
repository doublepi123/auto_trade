"""PnL distribution shape analysis service.

Computes skewness, kurtosis, and a simple normality heuristic on the
realized PnL distribution to characterize tail risk.  Read-only.

Inspired by VectorBT's return distribution analytics and QuantStats'
distribution fitting tearsheet.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["DistributionShapeService"]


class DistributionShapeService:
    """Statistical shape descriptors of the PnL distribution."""

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
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "analysis_status": "INSUFFICIENT_SAMPLE",
                "error": "Need at least 10 closed trades.",
            })

        n = len(pnls)
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / n
        std = math.sqrt(var) if var > 0 else 0.0
        if std == 0:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": n,
                "analysis_status": "DEGENERATE",
                "mean": round(mean, 2),
                "std": 0.0,
                "tail_label": "degenerate",
                "asymmetry": "undefined",
                "error": (
                    "PnL distribution has zero variance; skewness, kurtosis, "
                    "and normality are undefined."
                ),
            })

        # skewness (Fisher)
        skew = sum(((p - mean) / std) ** 3 for p in pnls) / n
        kurt = sum(((p - mean) / std) ** 4 for p in pnls) / n - 3.0

        # R-7 / NumPy-default linear interpolation on rank (n - 1) * p.
        sorted_pnls = sorted(pnls)
        p5 = _linear_quantile(sorted_pnls, 0.05)
        p25 = _linear_quantile(sorted_pnls, 0.25)
        p50 = median(sorted_pnls)
        p75 = _linear_quantile(sorted_pnls, 0.75)
        p95 = _linear_quantile(sorted_pnls, 0.95)

        # simple normality heuristic (Jarque-Bera-like)
        jb = (n / 6.0) * (skew**2 + (kurt**2) / 4.0)
        is_normal_like = jb < 5.99  # chi2(2) 5% critical value

        # tail characterization
        tail_label = "fat-tailed" if kurt > 1.0 else "thin-tailed" if kurt < -1.0 else "near-normal"
        asymmetry = "right-skewed" if skew > 0.5 else "left-skewed" if skew < -0.5 else "symmetric"

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "analysis_status": "READY",
            "mean": round(mean, 2),
            "std": round(std, 2),
            "skewness": round(skew, 4),
            "kurtosis": round(kurt, 4),
            "jarque_bera": round(jb, 4),
            "is_normal_like": is_normal_like,
            "tail_label": tail_label,
            "asymmetry": asymmetry,
            "percentiles": {
                "p5": round(p5, 2),
                "p25": round(p25, 2),
                "p50": round(p50, 2),
                "p75": round(p75, 2),
                "p95": round(p95, 2),
            },
            "iqr": round(p75 - p25, 2),
            "interpretation": _interpret(skew, kurt, is_normal_like),
        })


def _linear_quantile(ordered: list[float], probability: float) -> float:
    """Return an R-7 linear-interpolated sample quantile."""

    if not ordered:
        raise ValueError("ordered sample must not be empty")
    rank = (len(ordered) - 1) * probability
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _interpret(skew: float, kurt: float, normal: bool) -> str:
    parts: list[str] = []
    if normal:
        parts.append("Distribution is approximately normal (JB test)")
    else:
        parts.append("Distribution deviates significantly from normal")
    if skew > 0.5:
        parts.append("positive skew — occasional large wins")
    elif skew < -0.5:
        parts.append("negative skew — occasional large losses (tail risk)")
    if kurt > 1.0:
        parts.append("excess kurtosis — fat tails, extreme outcomes more likely")
    return "; ".join(parts) + "."
