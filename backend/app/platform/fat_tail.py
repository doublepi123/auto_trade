"""P210: Return distribution shape — fat-tail diagnostics.

Empirical tools to *characterize* the tail of a return series without
fitting a parametric distribution. Useful when the assumed normality
behind Sharpe / VaR underestimates risk, or when comparing strategies
on tail behavior rather than on aggregate stats.

  - **Excess kurtosis** : the fourth standardized moment beyond 3. A
    Gaussian is 0; equity returns are typically 3-30 (fatter tails).
  - **Tail asymmetry (skew)** : negative skew = left tail heavier.
  - **Hill estimator** : a non-parametric tail-index estimator using
    the top-k order statistics. ``α > 2`` implies finite variance;
    ``α ≈ 1`` is the Cauchy limit; ``α < 1`` is the "infinite-mean" zone.
  - **VaR tail ratio** : the empirical tail's VaR ratio vs the Gaussian
    VaR at the same confidence. A number > 1 means the empirical tail
    is heavier than Gaussian; < 1 means thinner.
  - **Stable-distribution fit** : a *rough* method-of-moments fit of the
    four stable-distribution parameters (α, β, σ, μ) using Hill's α,
    the empirical skew for β, an IQR-derived σ, and the sample mean for
    μ. The point isn't high accuracy — it's a quick-and-cheap "what
    stable distribution looks like this data?" sanity check.

Reference: Hill (1975); McCulloch "Simple Consistent Estimators of
Stable Distribution Parameters" (1986); Fama & Roll (1968, 1971).
Pure-Python — no scipy, no NumPy, no RNG.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "excess_kurtosis",
    "skewness",
    "hill_estimator",
    "tail_ratio",
    "stable_fit",
    "fat_tail_report",
]


# ---------------------------------------------------------------------------
# central moments
# ---------------------------------------------------------------------------


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float], ddof: int = 1) -> float:
    if len(xs) <= ddof:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - ddof))


def excess_kurtosis(returns: list[float]) -> float:
    """Sample excess kurtosis (Fisher's g₂). Gaussian = 0; fat tails > 0."""
    if len(returns) < 4:
        return 0.0
    n = len(returns)
    m = _mean(returns)
    s = _std(returns, ddof=0)
    if s <= 0:
        return 0.0
    m4 = sum((x - m) ** 4 for x in returns) / n
    return m4 / (s ** 4) - 3.0


def skewness(returns: list[float]) -> float:
    """Sample skewness (Fisher-Pearson). Negative = left-heavy tail."""
    if len(returns) < 3:
        return 0.0
    n = len(returns)
    m = _mean(returns)
    s = _std(returns, ddof=0)
    if s <= 0:
        return 0.0
    m3 = sum((x - m) ** 3 for x in returns) / n
    return m3 / (s ** 3)


# ---------------------------------------------------------------------------
# Hill estimator
# ---------------------------------------------------------------------------


def hill_estimator(returns: list[float], k: int | None = None) -> float:
    """Hill's tail-index estimator: ``α̂ = k / (Σ ln(X_(n-i+1)/X_(n-k)))``.

    ``returns`` is the (un-signed) magnitude series; for a *two-sided* tail
    estimate, pass ``abs(returns)`` (the usual convention for financial
    returns). The returned ``α̂`` is the tail index; the *tail exponent*
    (Pareto α) equals this same number.
    """
    if not returns:
        return 0.0
    x = sorted(abs(r) for r in returns if r != 0)
    n = len(x)
    if n < 4:
        return 0.0
    if k is None:
        # Standard Hill rule of thumb: k ≈ √n
        k = max(2, int(math.sqrt(n)))
    k = min(k, n - 1)
    threshold = x[n - k - 1]  # the (n-k)-th order stat
    if threshold <= 0:
        return 0.0
    log_sum = 0.0
    for i in range(n - k, n):
        if x[i] <= 0:
            continue
        log_sum += math.log(x[i] / threshold)
    if log_sum <= 0:
        return 0.0
    return k / log_sum


# ---------------------------------------------------------------------------
# VaR tail ratio (empirical vs Gaussian)
# ---------------------------------------------------------------------------


def tail_ratio(returns: list[float], confidence: float = 0.95) -> float:
    """Ratio of empirical VaR to Gaussian VaR at the same confidence.

    > 1 means the empirical tail is fatter than Gaussian's; < 1 means
    thinner. NaN-safe (returns 1.0 when Gaussian std is zero). Uses the same
    high-precision Acklam inverse-normal-CDF as :mod:`app.platform.risk_metrics`
    so the Gaussian benchmark is consistent across modules (no hardcoded
    z-table that diverges at off-the-grid confidence levels).
    """
    if not returns or not 0.0 < confidence < 1.0:
        return 1.0
    sorted_rets = sorted(returns)
    n = len(sorted_rets)
    cutoff = int(math.ceil((1.0 - confidence) * n)) - 1
    cutoff = max(0, min(n - 1, cutoff))
    empirical_loss = -sorted_rets[cutoff]
    mu = _mean(returns)
    sigma = _std(returns)
    if sigma <= 0:
        return 1.0
    from app.platform.risk_metrics import _normal_quantile

    z = _normal_quantile(1.0 - confidence)  # negative
    gaussian_loss = -(mu + sigma * z)  # loss magnitude, matches parametric_var
    if gaussian_loss <= 0:
        return 1.0
    return empirical_loss / gaussian_loss


# ---------------------------------------------------------------------------
# rough stable-distribution fit
# ---------------------------------------------------------------------------


def stable_fit(returns: list[float]) -> dict[str, float]:
    """Method-of-moments fit of a stable distribution.

    Returns ``{alpha, beta, sigma, mu}``:

    - ``alpha`` ∈ (0, 2] — characteristic exponent; 2 = Gaussian,
      ~1.4-1.7 for typical equity returns.
    - ``beta`` ∈ [-1, 1] — skewness parameter.
    - ``sigma`` > 0 — scale.
    - ``mu`` — location.

    This is a *rough* fit, intended for sanity-checking "is this series
    closer to Gaussian or closer to a fat-tailed stable law?" — not a
    substitute for scipy.stats.levy_stable.fit. We use Hill's α for
    the tail exponent, the sample skewness for β, an IQR-derived σ,
    and the sample mean for μ.
    """
    if len(returns) < 5:
        return {"alpha": 0.0, "beta": 0.0, "sigma": 0.0, "mu": 0.0}
    alpha = hill_estimator(returns)
    # Clamp α to (0, 2]; pure Pareto/α<1 case we cap at 1.0 for a stable law
    alpha = max(0.5, min(2.0, alpha))
    sk = skewness(returns)
    # Map skewness in roughly [-1, 1] to beta. (Very rough; stable skew
    # doesn't equal sample skew 1:1, but the sign and magnitude carry through.)
    beta = max(-1.0, min(1.0, sk / 3.0))
    # Scale from inter-quartile range (Gaussian-consistent; for stable laws
    # the exact σ depends on α & β, but IQR / 1.349 ≈ σ for Gaussian and is
    # a reasonable first-order estimator for any α).
    sorted_rets = sorted(returns)
    n = len(sorted_rets)
    q25 = sorted_rets[int(0.25 * n)]
    q75 = sorted_rets[int(0.75 * n)]
    iqr = q75 - q25
    sigma = iqr / 1.349 if iqr > 0 else _std(returns)
    mu = _mean(returns)
    return {"alpha": alpha, "beta": beta, "sigma": sigma, "mu": mu}


# ---------------------------------------------------------------------------
# one-stop report
# ---------------------------------------------------------------------------


def fat_tail_report(returns: list[float]) -> dict[str, Any]:
    """Aggregate: kurtosis, skewness, Hill α, tail ratio (95% + 99%), stable fit."""
    if not returns:
        return {
            "n": 0,
            "mean": 0.0,
            "std": 0.0,
            "excess_kurtosis": 0.0,
            "skewness": 0.0,
            "hill_alpha": 0.0,
            "tail_ratio_95": 1.0,
            "tail_ratio_99": 1.0,
            "stable": {"alpha": 0.0, "beta": 0.0, "sigma": 0.0, "mu": 0.0},
        }
    return {
        "n": len(returns),
        "mean": _mean(returns),
        "std": _std(returns),
        "excess_kurtosis": excess_kurtosis(returns),
        "skewness": skewness(returns),
        "hill_alpha": hill_estimator(returns),
        "tail_ratio_95": tail_ratio(returns, 0.95),
        "tail_ratio_99": tail_ratio(returns, 0.99),
        "stable": stable_fit(returns),
    }
