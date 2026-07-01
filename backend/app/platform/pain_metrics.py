"""P209: Pain Index, Ulcer Index, and MAR-based ratios.

Downside-volatility ratios that focus on the *depth and duration* of losses
rather than their instantaneous magnitude. Originally proposed in the
1980s/1990s and rediscovered in the pyfolio/QuantStats era.

  - **Pain Index (PI)**  : mean of % drawdowns (mean underwater depth).
    Z. Pedar, "Pain Index" (1989, S&P Outlook).
  - **Ulcer Index (UI)** : RMS of % drawdowns, weighted toward larger
    drawdowns. Martin E. Burke, "Ulcer Index — An Alternative Approach
    to the Measurement of Investment Risk & Performance" (1994).
  - **MAR ratio**       : CAGR / |max drawdown| (annualized).
    A. Sedlacek, "MAR Ratio" (1980s), resurfaces in managed-futures world.
  - **Kestner ratio**   : CAGR / Ulcer Index. Kestner (1996).

Pure-Python, dict-friendly, no NumPy/scipy. The platform's existing
``analyzers.DrawDownAnalyzer`` covers simple max-Drawdown; this module
adds the *distribution* of drawdowns and the ratios that depend on it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

__all__ = [
    "pain_index",
    "ulcer_index",
    "mar_ratio",
    "kestner_ratio",
    "pain_metrics_report",
]


def _underwater(equity: Sequence[float]) -> list[float]:
    if not equity:
        return []
    peak = equity[0]
    out: list[float] = []
    for x in equity:
        if x > peak:
            peak = x
        out.append((x - peak) / peak if peak > 0 else 0.0)
    return out


def pain_index(equity: Sequence[float]) -> float:
    """Mean of the underwater curve: average % drawdown over the run.

    A 5 % Pain Index means the portfolio spent, on average, 5 % below its
    prior peak across the run. PI = 0 for a monotonically-rising curve.
    """
    uw = _underwater(equity)
    if not uw:
        return 0.0
    return -sum(uw) / len(uw)  # positive number


def ulcer_index(equity: Sequence[float]) -> float:
    """RMS of the underwater curve: sqrt(mean(underwater^2)).

    Weights deeper drawdowns quadratically, so UI ≥ PI in every case and
    UI is more sensitive to long, deep drawdowns.
    """
    uw = _underwater(equity)
    if not uw:
        return 0.0
    return math.sqrt(sum(u * u for u in uw) / len(uw))


def _cagr(equity: Sequence[float], periods_per_year: int = 252) -> float:
    if len(equity) < 2 or equity[0] <= 0 or equity[-1] <= 0:
        return 0.0
    n_periods = len(equity) - 1
    years = n_periods / periods_per_year
    if years <= 0:
        return 0.0
    return (equity[-1] / equity[0]) ** (1.0 / years) - 1.0


def mar_ratio(equity: Sequence[float], periods_per_year: int = 252) -> float:
    """MAR = CAGR / |max drawdown| (with drawdown as a positive loss)."""
    cagr = _cagr(equity, periods_per_year)
    uw = _underwater(equity)
    if not uw:
        return 0.0
    max_dd = -min(uw)  # loss magnitude
    if max_dd <= 0:
        return 0.0
    return cagr / max_dd


def kestner_ratio(equity: Sequence[float], periods_per_year: int = 252) -> float:
    """Kestner = CAGR / Ulcer Index.  More sensitive to drawdown distribution than MAR."""
    cagr = _cagr(equity, periods_per_year)
    ui = ulcer_index(equity)
    if ui <= 0:
        return 0.0
    return cagr / ui


def pain_metrics_report(
    equity: Sequence[float], periods_per_year: int = 252
) -> dict[str, Any]:
    """One-stop report: PI, UI, MAR, Kestner, plus underlying drawdown stats."""
    if not equity:
        return {
            "n": 0,
            "cagr": 0.0,
            "max_drawdown": 0.0,
            "pain_index": 0.0,
            "ulcer_index": 0.0,
            "mar_ratio": 0.0,
            "kestner_ratio": 0.0,
        }
    uw = _underwater(equity)
    return {
        "n": len(equity),
        "cagr": _cagr(equity, periods_per_year),
        "max_drawdown": -min(uw) if uw else 0.0,
        "pain_index": pain_index(equity),
        "ulcer_index": ulcer_index(equity),
        "mar_ratio": mar_ratio(equity, periods_per_year),
        "kestner_ratio": kestner_ratio(equity, periods_per_year),
    }
