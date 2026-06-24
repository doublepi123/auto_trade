"""P203: Value-at-Risk and Conditional VaR (Expected Shortfall) estimators.

Risk metrics on a return series (or a portfolio of series). Both historical
(non-parametric, the empirical quantile) and parametric (Gaussian / normal
assumption) flavors are provided for VaR; CVaR / Expected Shortfall uses the
tail mean past the VaR threshold.

Reference: Jorion, "Value at Risk" (2007); McNeil, Frey & Embrechts
"Quantitative Risk Management" (2015). Mirrors ``empyrical.stats.ValueAtRisk``
and ``conditional_value_at_risk`` semantics — but in pure Python, with no
NumPy/scipy dependency, dict-based I/O, and explicit handling of single-asset
vs portfolio-of-assets inputs.

Sign convention: a return is *negative* for a loss. VaR is reported as a
**positive** number meaning the loss magnitude that is exceeded with
probability ``1 - confidence`` (so callers can write "1-day VaR95 = $1.2M"
without negative signs). Pass ``return_loss_as_negative=False`` to flip.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "historical_var",
    "historical_cvar",
    "parametric_var",
    "parametric_cvar",
    "portfolio_var",
    "risk_metrics",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mean(series: list[float]) -> float:
    return sum(series) / len(series) if series else 0.0


def _std(series: list[float], ddof: int = 1) -> float:
    if len(series) <= ddof:
        return 0.0
    m = _mean(series)
    return math.sqrt(sum((x - m) ** 2 for x in series) / (len(series) - ddof))


def _sorted(returns: list[float]) -> list[float]:
    return sorted(returns)


def _alpha_quantile_index(n: int, confidence: float) -> int:
    """Index of the worst observation in the (1 − confidence) tail of an ascending-sorted list.

    For confidence=0.95 and n=100 we want the worst 5% — index 4 (or thereabouts).
    The standard convention is ``k = ⌈(1 − confidence) · n⌉ − 1`` clamped to ``[0, n−1]``.
    """
    if n <= 0:
        return 0
    k = int(math.ceil((1.0 - confidence) * n)) - 1
    return max(0, min(n - 1, k))


def _to_loss(x: float, return_loss_as_negative: bool) -> float:
    """Return value ``x`` as a loss (positive) or keep sign per convention."""
    return -x if return_loss_as_negative else x


# ---------------------------------------------------------------------------
# Historical
# ---------------------------------------------------------------------------


def historical_var(
    returns: list[float],
    confidence: float = 0.95,
    return_loss_as_negative: bool = False,
) -> float:
    """Non-parametric (empirical-quantile) VaR.

    ``returns`` is a simple list of period returns. The VaR is reported as a
    loss magnitude (positive = loss) unless ``return_loss_as_negative`` is set.
    """
    if not returns or not 0.0 < confidence < 1.0:
        return 0.0
    s = _sorted(returns)
    idx = _alpha_quantile_index(len(s), confidence)
    var = -s[idx]  # loss = negative return
    return _to_loss(var, return_loss_as_negative)


def historical_cvar(
    returns: list[float],
    confidence: float = 0.95,
    return_loss_as_negative: bool = False,
) -> float:
    """Expected Shortfall / CVaR: mean of returns past the VaR tail.

    This is the "average loss conditional on exceeding VaR" — the integral of
    the tail distribution (a strictly *coherent* risk measure per Artzner et
    al., 1999, unlike VaR).
    """
    if not returns or not 0.0 < confidence < 1.0:
        return 0.0
    s = _sorted(returns)
    cutoff = _alpha_quantile_index(len(s), confidence)
    tail = s[: cutoff + 1]  # everything worse than or equal to VaR
    if not tail:
        return 0.0
    es = -_mean(tail)  # mean loss in the tail
    return _to_loss(es, return_loss_as_negative)


# ---------------------------------------------------------------------------
# Parametric (Gaussian)
# ---------------------------------------------------------------------------


def _normal_quantile(p: float) -> float:
    """Beasley-Springer/Moro approximation of the standard-normal inverse CDF.

    Accurate to ~1e-9 across the central range — more than enough for risk
    reporting at 90/95/99 % confidence levels.
    """
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0

    # Acklam's algorithm — coefficients for the inverse normal CDF.
    a = [
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
        3.754408661907416e00,
    ]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def parametric_var(
    returns: list[float],
    confidence: float = 0.95,
    return_loss_as_negative: bool = False,
) -> float:
    """Gaussian (mean + std) VaR.

    VaR_α = −(μ + σ · Φ⁻¹(1 − α)).
    """
    if not returns or not 0.0 < confidence < 1.0:
        return 0.0
    mu = _mean(returns)
    sigma = _std(returns)
    z = _normal_quantile(1.0 - confidence)  # negative number
    var = -(mu + sigma * z)  # loss = -return at the lower tail
    return _to_loss(var, return_loss_as_negative)


def parametric_cvar(
    returns: list[float],
    confidence: float = 0.95,
    return_loss_as_negative: bool = False,
) -> float:
    """Gaussian CVaR / Expected Shortfall.

    Under the normal assumption, CVaR_α = −(μ + σ · φ(Φ⁻¹(1−α)) / (1−α))
    where φ is the standard-normal PDF. This is the *parametric* counterpart
    to :func:`historical_cvar`.
    """
    if not returns or not 0.0 < confidence < 1.0:
        return 0.0
    mu = _mean(returns)
    sigma = _std(returns)
    z = _normal_quantile(1.0 - confidence)  # negative
    # Standard normal PDF at z
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    # CVaR_α = -(μ - σ · φ(z_α) / α). With z<0 and φ(z) > 0, the tail-mean is
    # a *negative* return, so negating it gives a *positive* loss.
    es = -(mu - sigma * pdf / (1.0 - confidence))
    return _to_loss(es, return_loss_as_negative)


# ---------------------------------------------------------------------------
# Portfolio-of-assets
# ---------------------------------------------------------------------------


def portfolio_var(
    asset_returns: dict[str, list[float]],
    weights: dict[str, float],
    confidence: float = 0.95,
    method: str = "historical",
) -> float:
    """VaR for a weighted portfolio of assets.

    Builds the portfolio return series ``r_p[t] = Σ w_s · r_s[t]`` (trimmed to
    the common minimum length) and delegates to the historical or parametric
    estimator. The portfolio return series is the natural "mark-to-market"
    object — no covariance inversion needed at the loss-aggregation step.
    """
    if not asset_returns or not weights:
        return 0.0
    common_symbols = [s for s in weights if s in asset_returns and asset_returns[s]]
    if not common_symbols:
        return 0.0
    n = min(len(asset_returns[s]) for s in common_symbols)
    if n < 1:
        return 0.0
    port_returns: list[float] = []
    for t in range(n):
        acc = 0.0
        for s in common_symbols:
            acc += weights[s] * asset_returns[s][t]
        port_returns.append(acc)
    if method == "parametric":
        return parametric_var(port_returns, confidence)
    return historical_var(port_returns, confidence)


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


def risk_metrics(
    returns: list[float],
    confidence_levels: list[float] | None = None,
) -> dict[str, Any]:
    """One-stop risk report: historical + parametric VaR/CVaR at multiple αs.

    Returns a flat dict suitable for JSON serialization. Includes the summary
    statistics needed to sanity-check the risk numbers (mean / std / min / max
    of the return series)."""
    if not returns:
        return {
            "n": 0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "var": {},
            "cvar": {},
        }
    if confidence_levels is None:
        confidence_levels = [0.90, 0.95, 0.99]
    var: dict[str, dict[str, float]] = {"historical": {}, "parametric": {}}
    cvar: dict[str, dict[str, float]] = {"historical": {}, "parametric": {}}
    for conf in confidence_levels:
        key = f"{int(conf * 100)}"
        var["historical"][key] = historical_var(returns, conf)
        var["parametric"][key] = parametric_var(returns, conf)
        cvar["historical"][key] = historical_cvar(returns, conf)
        cvar["parametric"][key] = parametric_cvar(returns, conf)
    return {
        "n": len(returns),
        "mean": _mean(returns),
        "std": _std(returns),
        "min": min(returns),
        "max": max(returns),
        "var": var,
        "cvar": cvar,
    }
