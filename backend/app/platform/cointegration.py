"""P223: Cointegration & Pairs Trading Diagnostics.

Two-asset statistical-arb foundation. Given two price series of equal length,
fit the hedge ratio by OLS, form the spread, and quantify mean-reversion
strength so a strategy can size entries off the z-score and exits off the
half-life.

* **Engle-Granger 2-step** — regress y on x (y = β·x + α + ε), the residual ε
  is the cointegrating spread; a near-unit-root residual ⇒ cointegration.
  Stationarity of the residual is scored by the Durbin-Watson statistic and a
  simplified AR(1) coefficient (close to 0 ⇒ fast mean reversion ⇒ cointegrated).
* **Ornstein-Uhlenbeck half-life** — discretize the continuous OU process
  ``dx = κ(μ − x) dt + σ dW`` as ``Δs_t = −κ·(s_{t−1} − μ) + ε``; the OLS slope
  on ``(s_{t−1} − μ)`` gives κ and half-life ``ln(2)/κ``.
* **z-score** — current spread vs its rolling mean/std, the standard
  mean-reversion entry signal.

Deterministic, pure Python. Reference: statsmodels ``coint`` / ``OLS`` and
the pairs-trading literature (Vidyamurthy, Chan).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "CointegrationResult",
    "cointegration_analysis",
    "hedge_ratio_ols",
    "spread_series",
    "half_life_ou",
    "zscore",
    "durbin_watson",
]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: Sequence[float], mu: float | None = None) -> float:
    if len(xs) < 2:
        return 0.0
    m = mu if mu is not None else _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def hedge_ratio_ols(y: Sequence[float], x: Sequence[float]) -> tuple[float, float, float]:
    """OLS regression ``y = β·x + α + ε``. Returns ``(beta, alpha, r_squared)``.

    Beta is the hedge ratio (units of x to short per unit of y long). R² is the
    uncentered explained fraction (1 - SSR/SST).
    """
    n = len(y)
    if n != len(x) or n < 2:
        raise ValueError("y and x must be equal-length series with >=2 points")
    mx = _mean(x)
    my = _mean(y)
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx == 0.0:
        raise ValueError("x has zero variance; cannot fit hedge ratio")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    beta = sxy / sxx
    alpha = my - beta * mx
    # R^2 (centered)
    syy = sum((yi - my) ** 2 for yi in y)
    ssr = sum((yi - (beta * xi + alpha)) ** 2 for xi, yi in zip(x, y))
    r2 = 1.0 - (ssr / syy) if syy > 0 else 0.0
    return beta, alpha, r2


def spread_series(y: Sequence[float], x: Sequence[float], beta: float, alpha: float) -> list[float]:
    """Residual spread ``s_t = y_t − β·x_t − α``."""
    return [yi - beta * xi - alpha for xi, yi in zip(x, y)]


def durbin_watson(residuals: Sequence[float]) -> float:
    """Durbin-Watson statistic. ~2 ⇒ no autocorrelation; ~0 ⇒ strong positive
    autocorrelation (i.e. slowly mean-reverting / near-unit-root)."""
    n = len(residuals)
    if n < 2:
        return 2.0
    num = sum((residuals[i] - residuals[i - 1]) ** 2 for i in range(1, n))
    den = sum(r ** 2 for r in residuals)
    if den == 0.0:
        return 2.0
    return num / den


def half_life_ou(spread: Sequence[float]) -> float:
    """Ornstein-Uhlenbeck half-life of mean reversion (in bars).

    Regress ``Δs_t`` on ``−(s_{t−1} − mean(s))``: slope κ, half-life ``ln2/κ``.
    Returns ``+inf`` when κ ≤ 0 (non-mean-reverting / explosive) — caller should
    treat that as "no usable half-life".
    """
    n = len(spread)
    if n < 3:
        raise ValueError("spread needs >=3 points for half-life fit")
    mu = _mean(spread)
    # Δs_t = s_t - s_{t-1} = -κ (s_{t-1} - μ) + ε  → regress Δs on (s_{t-1}-μ)
    lhs = [spread[i] - spread[i - 1] for i in range(1, n)]
    rhs = [spread[i - 1] - mu for i in range(1, n)]
    sxx = sum(r * r for r in rhs)
    if sxx == 0.0:
        return math.inf
    sxy = sum(r * l for r, l in zip(rhs, lhs))
    # slope of Δs on (s_{t-1}-μ) is -κ
    slope = sxy / sxx
    kappa = -slope
    if kappa <= 0.0:
        return math.inf
    return math.log(2.0) / kappa


def zscore(spread: Sequence[float], window: int | None = None) -> list[float]:
    """Rolling z-score of the spread (or full-sample z-score if ``window`` is None)."""
    n = len(spread)
    if n == 0:
        return []
    if window is None or window >= n:
        mu = _mean(spread)
        sd = math.sqrt(_var(spread, mu))
        if sd == 0.0:
            return [0.0] * n
        return [(s - mu) / sd for s in spread]
    if window < 2:
        raise ValueError("window must be >=2")
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - window + 1)
        seg = spread[lo : i + 1]
        mu = _mean(seg)
        sd = math.sqrt(_var(seg, mu))
        out.append((spread[i] - mu) / sd if sd > 0 else 0.0)
    return out


@dataclass(frozen=True)
class CointegrationResult:
    beta: float
    alpha: float
    r_squared: float
    spread: list[float]
    current_zscore: float
    half_life: float  # +inf if non-mean-reverting
    durbin_watson: float
    spread_mean: float
    spread_std: float

    def to_dict(self) -> dict:
        return {
            "beta": self.beta,
            "alpha": self.alpha,
            "r_squared": self.r_squared,
            "spread": self.spread,
            "current_zscore": self.current_zscore,
            "half_life": self.half_life,
            "half_life_finite": math.isfinite(self.half_life),
            "durbin_watson": self.durbin_watson,
            "spread_mean": self.spread_mean,
            "spread_std": self.spread_std,
        }


def cointegration_analysis(
    y: Sequence[float],
    x: Sequence[float],
    zscore_window: int | None = None,
) -> CointegrationResult:
    """Full Engle-Granger style pairs diagnostics."""
    beta, alpha, r2 = hedge_ratio_ols(y, x)
    spread = spread_series(y, x, beta, alpha)
    mu = _mean(spread)
    sd = math.sqrt(_var(spread, mu))
    if zscore_window is not None:
        zs = zscore(spread, zscore_window)
        current_z = zs[-1] if zs else 0.0
    else:
        current_z = (spread[-1] - mu) / sd if sd > 0 else 0.0
    try:
        hl = half_life_ou(spread)
    except ValueError:
        hl = math.inf
    dw = durbin_watson(spread)
    return CointegrationResult(
        beta=beta,
        alpha=alpha,
        r_squared=r2,
        spread=spread,
        current_zscore=current_z,
        half_life=hl,
        durbin_watson=dw,
        spread_mean=mu,
        spread_std=sd,
    )