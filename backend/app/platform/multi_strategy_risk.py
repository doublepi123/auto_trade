"""P335: Multi-Strategy Risk Report — portfolio volatility & risk decomposition.

Computes portfolio volatility, risk contributions, diversification ratio,
and concentration HHI from a panel of strategy returns and weights.

Reference: Litterman (2003), Qian (2005) risk parity. Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MultiStrategyRiskResult:
    """Aggregate multi-strategy risk diagnostics."""
    portfolio_vol: float
    risk_contributions: dict[str, float]
    diversification_ratio: float
    concentration_hhi: float
    covariance_matrix: list[list[float]]

    def to_dict(self) -> dict[str, object]:
        return {
            "portfolio_vol": self.portfolio_vol,
            "risk_contributions": self.risk_contributions,
            "diversification_ratio": self.diversification_ratio,
            "concentration_hhi": self.concentration_hhi,
            "covariance_matrix": self.covariance_matrix,
        }


def _validate_returns(returns: dict[str, list[float]]) -> None:
    if not returns:
        raise ValueError("strategy_returns must be a non-empty dict")
    length: int | None = None
    for name, series in returns.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"strategy_returns['{name}'] must be a non-empty list")
        for v in series:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
                raise ValueError(f"strategy_returns['{name}'] contains non-finite value: {v}")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise ValueError("all strategy return series must have equal length")


def _validate_weights(returns: dict[str, list[float]], weights: dict[str, float]) -> None:
    if not weights:
        raise ValueError("weights must be a non-empty dict")
    if set(weights.keys()) != set(returns.keys()):
        raise ValueError("weights keys must match strategy_returns keys exactly")
    for name, w in weights.items():
        if not isinstance(w, (int, float)) or isinstance(w, bool) or not math.isfinite(float(w)):
            raise ValueError(f"weights['{name}'] must be a finite number")
        if w < 0:
            raise ValueError(f"weights['{name}'] must be non-negative")


def _compute_covariance(returns: dict[str, list[float]], strategies: list[str]) -> list[list[float]]:
    """Compute sample covariance matrix for aligned return series."""
    n = len(strategies)
    T = len(returns[strategies[0]])

    # Compute means
    means = {s: sum(returns[s]) / T for s in strategies}

    # Compute covariance matrix
    cov: list[list[float]] = []
    for i in range(n):
        row: list[float] = []
        si = strategies[i]
        for j in range(n):
            sj = strategies[j]
            total = 0.0
            for t in range(T):
                total += (returns[si][t] - means[si]) * (returns[sj][t] - means[sj])
            row.append(total / (T - 1))
        cov.append(row)
    return cov


def _matrix_vector_multiply(cov: list[list[float]], weights: list[float]) -> list[float]:
    """Multiply covariance matrix by weight vector: Cov * w."""
    n = len(weights)
    result: list[float] = []
    for i in range(n):
        row = cov[i]
        total = 0.0
        for j in range(n):
            total += row[j] * weights[j]
        result.append(total)
    return result


def multi_strategy_risk_report(
    strategy_returns: dict[str, list[float]],
    weights: dict[str, float],
    *,
    periods_per_year: int = 252,
) -> MultiStrategyRiskResult:
    """Compute portfolio volatility, risk contributions, and diversification metrics.

    Args:
        strategy_returns: Dict mapping strategy name to list of period returns.
        weights: Dict mapping strategy name to portfolio weight (must be non-negative).
        periods_per_year: Annualization factor (default 252 for daily returns).

    Returns:
        MultiStrategyRiskResult with portfolio_vol (annualized), risk_contributions,
        diversification_ratio, concentration_hhi, and covariance_matrix.
    """
    _validate_returns(strategy_returns)
    _validate_weights(strategy_returns, weights)
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year < 1:
        raise ValueError("periods_per_year must be an int >= 1")

    strategies = list(strategy_returns.keys())
    cov = _compute_covariance(strategy_returns, strategies)
    w_list = [weights[s] for s in strategies]

    # Portfolio variance: w' * Cov * w
    cov_w = _matrix_vector_multiply(cov, w_list)
    port_var = 0.0
    for i, wi in enumerate(w_list):
        port_var += wi * cov_w[i]
    port_var = max(port_var, 0.0)

    # Annualized portfolio volatility
    portfolio_vol_period = math.sqrt(port_var)
    portfolio_vol = portfolio_vol_period * math.sqrt(periods_per_year)

    # Per-strategy individual volatility (annualized)
    individual_vols: list[float] = []
    for i, s in enumerate(strategies):
        ind_var = cov[i][i]
        individual_vols.append(math.sqrt(max(ind_var, 0.0)) * math.sqrt(periods_per_year))

    # Risk contributions: RC_i = w_i * (Cov*w)_i / portfolio_vol_period
    # Then annualize so they sum to portfolio_vol (annualized)
    ann_factor = math.sqrt(periods_per_year)
    risk_contributions: dict[str, float] = {}
    if portfolio_vol_period > 0:
        for i, s in enumerate(strategies):
            rc_period = w_list[i] * cov_w[i] / portfolio_vol_period
            risk_contributions[s] = rc_period * ann_factor

    # Weighted average individual volatility (annualized)
    weighted_avg_ind_vol = sum(w_list[i] * individual_vols[i] for i in range(len(strategies)))

    # Diversification ratio: portfolio_vol / weighted_avg_individual_vol
    if weighted_avg_ind_vol > 0:
        diversification_ratio = portfolio_vol / weighted_avg_ind_vol
    else:
        diversification_ratio = 0.0

    # Concentration HHI: sum((RC_i / sum(RC))^2)
    total_rc = sum(risk_contributions.values()) if risk_contributions else 0.0
    if total_rc > 0:
        concentration_hhi = sum((rc / total_rc) ** 2 for rc in risk_contributions.values())
    else:
        concentration_hhi = 0.0

    return MultiStrategyRiskResult(
        portfolio_vol=portfolio_vol,
        risk_contributions=risk_contributions,
        diversification_ratio=diversification_ratio,
        concentration_hhi=concentration_hhi,
        covariance_matrix=cov,
    )
