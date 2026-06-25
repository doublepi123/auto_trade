"""P225: Volatility Forecasting Models.

Time-varying volatility estimates that a risk engine or sizer can subscribe
to. Three deterministic, fully-recursive estimators — no optimizer loop, no
randomness, no new dependencies.

* **EWMA** — RiskMetrics ™ decay. ``σ²_t = λ·σ²_{t−1} + (1−λ)·r²_{t−1}``,
  ``λ = 0.94`` daily by convention. Recursive, O(n).
* **GARCH(1,1)** — ``σ²_t = ω + α·r²_{t−1} + β·σ²_{t−1}`` with the stationarity
  constraint ``α + β < 1`` and ``ω = γ·V̄`` where ``V̄`` is the unconditional
  variance and ``γ = 1 − α − β``. The long-run variance is fit from the sample
  so the model reverts to a data-driven level.
* **Rolling Parkinson** — high/low range estimator
  ``σ² = (1/(4·ln2)) · (ln(H/L))²``, robust to microstructure noise in the
  close-to-close estimator. Averaged over a window.

All three return a per-period volatility series plus the latest forecast, so
a caller can plug them into VaR / sizing / regime detection.

Reference: RiskMetrics (1996), Bollerslev (1986) GARCH, Parkinson (1980).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "ewma_volatility",
    "garch11_volatility",
    "parkinson_volatility",
    "VolatilityReport",
    "volatility_report",
]


def ewma_volatility(returns: Sequence[float], lam: float = 0.94) -> list[float]:
    """EWMA (RiskMetrics) conditional variance series.

    Returns one variance per return (seed with the first squared return).
    """
    if not 0.0 < lam < 1.0:
        raise ValueError("lambda must be in (0, 1)")
    n = len(returns)
    if n == 0:
        return []
    var0 = returns[0] ** 2
    out = [var0]
    for i in range(1, n):
        v = lam * out[-1] + (1.0 - lam) * (returns[i - 1] ** 2)
        out.append(v)
    return out


def garch11_volatility(
    returns: Sequence[float],
    alpha: float = 0.10,
    beta: float = 0.85,
) -> list[float]:
    """GARCH(1,1) conditional variance series with data-driven long-run variance.

    ``σ²_t = ω + α·r²_{t−1} + β·σ²_{t−1}`` with ``ω = γ·V̄``,
    ``γ = 1 − α − β``, ``V̄ = sample variance``. Requires ``α + β < 1``.
    """
    if not 0.0 <= alpha < 1.0:
        raise ValueError("alpha must be in [0, 1)")
    if not 0.0 <= beta < 1.0:
        raise ValueError("beta must be in [0, 1)")
    if alpha + beta >= 1.0:
        raise ValueError("alpha + beta must be < 1 for stationarity")
    n = len(returns)
    if n < 2:
        raise ValueError("need >=2 returns")
    mu = sum(returns) / n
    var_bar = sum((r - mu) ** 2 for r in returns) / (n - 1)
    gamma = 1.0 - alpha - beta
    omega = gamma * var_bar
    # seed variance at the unconditional level
    out = [var_bar]
    for i in range(1, n):
        v = omega + alpha * (returns[i - 1] ** 2) + beta * out[-1]
        out.append(max(v, 1e-12))  # numerical guard
    return out


def parkinson_volatility(highs: Sequence[float], lows: Sequence[float], window: int | None = None) -> list[float]:
    """Parkinson high/low range volatility estimator.

    Per-period variance estimate ``(1/(4·ln2))·(ln(H/L))²``. If ``window`` is
    given, returns one rolling-average variance per period over that window;
    otherwise one variance per (H,L) pair.
    """
    n = len(highs)
    if n != len(lows) or n == 0:
        raise ValueError("highs and lows must be equal-length non-empty lists")
    factor = 1.0 / (4.0 * math.log(2.0))
    per_period = []
    for h, l in zip(highs, lows):
        if l <= 0 or h <= 0 or h < l:
            per_period.append(0.0)
            continue
        per_period.append(factor * (math.log(h / l)) ** 2)
    if window is None:
        return per_period
    if window < 1:
        raise ValueError("window must be >=1")
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - window + 1)
        seg = per_period[lo : i + 1]
        out.append(sum(seg) / len(seg))
    return out


@dataclass(frozen=True)
class VolatilityReport:
    ewma: list[float]
    garch: list[float]
    parkinson: list[float] | None
    latest_ewma: float
    latest_garch: float
    latest_parkinson: float | None
    long_run_variance: float

    def to_dict(self) -> dict:
        return {
            "ewma": self.ewma,
            "garch": self.garch,
            "parkinson": self.parkinson,
            "latest_ewma": self.latest_ewma,
            "latest_garch": self.latest_garch,
            "latest_parkinson": self.latest_parkinson,
            "long_run_variance": self.long_run_variance,
        }


def volatility_report(
    returns: Sequence[float],
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    lam: float = 0.94,
    alpha: float = 0.10,
    beta: float = 0.85,
) -> VolatilityReport:
    """Compute EWMA + GARCH(1,1) (+ Parkinson if H/L supplied) in one call."""
    ewma = ewma_volatility(returns, lam=lam)
    garch = garch11_volatility(returns, alpha=alpha, beta=beta)
    park = None
    latest_park = None
    if highs is not None and lows is not None:
        park = parkinson_volatility(highs, lows)
        latest_park = park[-1] if park else None
    n = len(returns)
    mu = sum(returns) / n if n else 0.0
    var_bar = sum((r - mu) ** 2 for r in returns) / (n - 1) if n > 1 else 0.0
    return VolatilityReport(
        ewma=ewma,
        garch=garch,
        parkinson=park,
        latest_ewma=ewma[-1] if ewma else 0.0,
        latest_garch=garch[-1] if garch else 0.0,
        latest_parkinson=latest_park,
        long_run_variance=var_bar,
    )