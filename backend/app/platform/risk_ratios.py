"""P205: Risk-adjusted performance ratios.

A complete family of return/risk ratios on a return series, plus a rolling
Sharpe estimator for stability assessment.

Ratios implemented:
  - Sharpe  = (μ − rf) / σ
  - Sortino = (μ − rf) / downside_deviation
  - Information = (μ − benchmark) / tracking_error
  - Treynor  = (μ − rf) / β
  - Modigliani (M²) = (Sharpe × σ_benchmark) + rf
  - Omega    = ∫_threshold^∞ (1 − F(r)) dr / ∫_{−∞}^threshold F(r) dr
                (the probability-weighted gain/loss ratio above a threshold)

Reference: Sharpe (1966), Sortino & van der Meer (1991), Treynor (1965),
Modigliani & Modigliani (1997), Keating & Shadwick (2002) for Omega.
Pure-Python, dict-based I/O, no external deps.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "sharpe_ratio",
    "sortino_ratio",
    "information_ratio",
    "treynor_ratio",
    "modigliani_ratio",
    "omega_ratio",
    "rolling_sharpe",
    "all_ratios",
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


def _downside_deviation(returns: list[float], threshold: float = 0.0) -> float:
    """Root mean square of negative deviations below ``threshold``.

    Sortino's original definition. The full-sample denominator is ``N`` (not
    ``N−1``) so the metric is well-defined on a single observation.
    """
    if not returns:
        return 0.0
    diffs = [min(0.0, r - threshold) for r in returns]
    return math.sqrt(sum(d * d for d in diffs) / len(returns))


def _cov(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    ma, mb = _mean(a), _mean(b)
    return sum((a[i] - ma) * (b[i] - mb) for i in range(len(a))) / (len(a) - 1)


def _beta(returns: list[float], benchmark: list[float]) -> float:
    """CAPM beta = cov(r, b) / var(b)."""
    bvar = sum((x - _mean(benchmark)) ** 2 for x in benchmark) / (len(benchmark) - 1)
    if bvar <= 0:
        return 0.0
    return _cov(returns, benchmark) / bvar


def _annualize(value: float, periods_per_year: int) -> float:
    return value * math.sqrt(periods_per_year) if periods_per_year > 0 else value


# ---------------------------------------------------------------------------
# ratios
# ---------------------------------------------------------------------------


def sharpe_ratio(
    returns: list[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualized Sharpe ratio: (mean − rf) / σ × √periods_per_year."""
    if len(returns) < 2:
        return 0.0
    excess = _mean(returns) - risk_free
    sigma = _std(returns)
    if sigma <= 1e-12:
        return 0.0
    return _annualize(excess / sigma, periods_per_year)


def sortino_ratio(
    returns: list[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualized Sortino ratio: (mean − rf) / downside_deviation × √periods_per_year.

    Downside deviation uses Sortino's original ``min(0, r − threshold)`` with
    full-sample mean as the threshold (so the ratio penalizes only *negative*
    deviation from the mean, ignoring upside vol).
    """
    if len(returns) < 2:
        return 0.0
    excess = _mean(returns) - risk_free
    dd = _downside_deviation(returns, threshold=_mean(returns))
    if dd <= 0:
        return 0.0
    return _annualize(excess / dd, periods_per_year)


def information_ratio(
    returns: list[float],
    benchmark: list[float],
    periods_per_year: int = 252,
) -> float:
    """Annualized Information ratio: (mean(r − b)) / tracking_error × √periods_per_year."""
    n = min(len(returns), len(benchmark))
    if n < 2:
        return 0.0
    active = [returns[i] - benchmark[i] for i in range(n)]
    return _annualize(_mean(active) / _std(active), periods_per_year) if _std(active) > 0 else 0.0


def treynor_ratio(
    returns: list[float],
    benchmark: list[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualized Treynor ratio: (mean(r) − rf) / β × periods_per_year.

    Treynor uses a *linear* (not square-root) annualization because the
    numerator is a return, not a return-per-unit-vol.
    """
    if len(returns) < 2 or len(benchmark) < 2:
        return 0.0
    excess = _mean(returns) - risk_free
    b = _beta(returns, benchmark)
    if abs(b) < 1e-12:
        return 0.0
    return (excess / b) * periods_per_year


def modigliani_ratio(
    returns: list[float],
    benchmark: list[float],
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Modigliani–Modigliani (M²) risk-adjusted return (annualized).

    M² = ((Sharpe_p × σ_b) + rf) × periods_per_year, where Sharpe_p and σ_b
    are both *per-period* and the result is annualised by the linear factor.
    Equivalent to "what return would the strategy deliver if its vol were
    scaled to match the benchmark's vol" — making Sharpe-comparable
    strategies directly rankable on a return basis.
    """
    if len(returns) < 2 or len(benchmark) < 2:
        return 0.0
    sigma_b = _std(benchmark)
    if sigma_b <= 0:
        return 0.0
    s = (sharpe_ratio(returns, risk_free, periods_per_year=1))  # per-period Sharpe
    m2_per_period = s * sigma_b + risk_free
    return m2_per_period * periods_per_year


def omega_ratio(
    returns: list[float],
    threshold: float = 0.0,
) -> float:
    """Omega ratio: probability-weighted gains above ``threshold`` divided by
    probability-weighted losses below.

    A threshold of 0 measures pure gain/loss asymmetry; a higher threshold
    raises the bar for what counts as a "gain".
    """
    if not returns:
        return 0.0
    gains = sum(max(0.0, r - threshold) for r in returns)
    losses = sum(max(0.0, threshold - r) for r in returns)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


# ---------------------------------------------------------------------------
# rolling Sharpe
# ---------------------------------------------------------------------------


def rolling_sharpe(
    returns: list[float],
    window: int,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> list[float]:
    """Annualized rolling Sharpe ratio over ``window`` ticks.

    Returns a list of length ``len(returns)``; entries with fewer than
    ``window`` observations are ``0.0``.
    """
    if window < 1 or not returns:
        return []
    out: list[float] = []
    for i in range(len(returns)):
        if i + 1 < window:
            out.append(0.0)
            continue
        seg = returns[i + 1 - window : i + 1]
        s = sharpe_ratio(seg, risk_free=risk_free, periods_per_year=periods_per_year)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# all-in-one report
# ---------------------------------------------------------------------------


def all_ratios(
    returns: list[float],
    benchmark: list[float] | None = None,
    risk_free: float = 0.0,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """Compute every ratio in one call. Benchmark-dependent metrics are 0.0 when no benchmark."""
    rep: dict[str, Any] = {
        "n": len(returns),
        "mean": _mean(returns),
        "std": _std(returns),
        "downside_deviation": _downside_deviation(returns, threshold=_mean(returns)),
        "sharpe": sharpe_ratio(returns, risk_free, periods_per_year),
        "sortino": sortino_ratio(returns, risk_free, periods_per_year),
        "omega": omega_ratio(returns, 0.0),
    }
    if benchmark and len(benchmark) >= 2:
        rep["information"] = information_ratio(returns, benchmark, periods_per_year)
        rep["treynor"] = treynor_ratio(returns, benchmark, risk_free, periods_per_year)
        rep["modigliani"] = modigliani_ratio(returns, benchmark, risk_free, periods_per_year)
    else:
        rep["information"] = 0.0
        rep["treynor"] = 0.0
        rep["modigliani"] = 0.0
    return rep
