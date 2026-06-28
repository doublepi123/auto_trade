"""P339: CVaR optimization via simplified Rockafellar-Uryasev approach.

Computes historical CVaR for equal-weight, minimum-variance, and risk-parity
approximations, then returns the candidate with the lowest CVaR.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class CvarOptimizationResult:
    optimal_weights: dict[str, float]
    cvar: float
    var: float
    weights_candidates: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "optimal_weights": dict(self.optimal_weights),
            "cvar": self.cvar,
            "var": self.var,
            "weights_candidates": self.weights_candidates,
        }


def _historical_cvar(portfolio_returns: list[float], confidence: float) -> tuple[float, float]:
    """Return (var, cvar) for a sorted list of returns (worst first)."""
    if not portfolio_returns:
        return 0.0, 0.0
    sorted_returns = sorted(portfolio_returns)
    n = len(sorted_returns)
    # VaR at given confidence: the (1-confidence) quantile
    var_index = max(0, int(math.ceil(n * (1.0 - confidence))) - 1)
    # Clamp to valid range
    var_index = min(var_index, n - 1)
    var_value = sorted_returns[var_index]
    # CVaR: mean of returns <= VaR
    tail = [r for r in sorted_returns if r <= var_value]
    cvar = mean(tail) if tail else var_value
    return var_value, cvar


def _portfolio_returns(returns_panel: dict[str, list[float]], weights: dict[str, float]) -> list[float]:
    """Compute weighted portfolio return series."""
    assets = sorted(returns_panel.keys())
    n = len(next(iter(returns_panel.values())))
    result: list[float] = []
    for i in range(n):
        total = 0.0
        for a in assets:
            total += weights.get(a, 0.0) * returns_panel[a][i]
        result.append(total)
    return result


def cvar_optimization_report(
    returns_panel: dict[str, list[float]],
    *,
    confidence: float = 0.95,
    target_return: float | None = None,
) -> CvarOptimizationResult:
    """Select portfolio weights that minimise historical CVaR.

    Candidates considered:
      - equal weight
      - minimum variance (inverse-vol weighted)
      - risk parity approximation (inverse-vol² weighted)

    Returns the candidate with the lowest CVaR.
    """
    if not isinstance(returns_panel, dict):
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) < 2:
        raise ValueError("returns_panel must contain at least 2 assets")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")

    assets = sorted(returns_panel.keys())
    series_map: dict[str, list[float]] = {}
    for asset in assets:
        series_map[asset] = validate_series(
            returns_panel[asset], name=f"returns_panel['{asset}']", min_len=2
        )

    # Check equal length
    lengths = {len(s) for s in series_map.values()}
    if len(lengths) != 1:
        raise ValueError("returns_panel series must have equal length")

    # Equal weight
    eq_w = {a: 1.0 / len(assets) for a in assets}

    # Minimum variance: weight ∝ 1/vol
    vols: dict[str, float] = {}
    for a in assets:
        s = std(series_map[a])
        vols[a] = s if s > 1e-12 else 1e-12
    inv_vol_sum = sum(1.0 / vols[a] for a in assets)
    mv_w = {a: (1.0 / vols[a]) / inv_vol_sum for a in assets}

    # Risk parity approximation: weight ∝ 1/vol²
    inv_vol2_sum = sum(1.0 / (vols[a] ** 2) for a in assets)
    rp_w = {a: (1.0 / (vols[a] ** 2)) / inv_vol2_sum for a in assets}

    candidates: list[dict[str, Any]] = []
    best_cvar = float("inf")
    best_var = 0.0
    best_weights = eq_w

    for method, w in [("equal_weight", eq_w), ("min_variance", mv_w), ("risk_parity", rp_w)]:
        port_ret = _portfolio_returns(series_map, w)
        var_val, cvar_val = _historical_cvar(port_ret, confidence)
        candidates.append({"method": method, "weights": dict(w), "cvar": cvar_val})
        if cvar_val < best_cvar:
            best_cvar = cvar_val
            best_var = var_val
            best_weights = w

    return CvarOptimizationResult(
        optimal_weights=best_weights,
        cvar=best_cvar,
        var=best_var,
        weights_candidates=candidates,
    )
