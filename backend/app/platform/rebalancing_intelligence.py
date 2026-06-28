"""P344: Rebalancing intelligence — compare rebalance-frequency strategies.

Simulates an equal-weighted (or target-weighted) portfolio under different
rebalancing frequencies (daily, weekly, monthly, quarterly) and reports
the cost-adjusted Sharpe, turnover, tracking error, and cost drag for each.
Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RebalancingIntelligenceResult", "rebalancing_intelligence_report"]

# Rebalance frequencies: (label, periods_per_rebalance)
_FREQUENCIES: list[tuple[str, int]] = [
    ("daily", 1),
    ("weekly", 5),
    ("monthly", 21),
    ("quarterly", 63),
]


def _annualized_sharpe(returns: list[float], periods_per_year: int, risk_free: float = 0.0) -> float:
    """Compute annualised Sharpe ratio from a return series."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_r = sum(returns) / n
    if mean_r == 0.0 and n == 0:
        return 0.0
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    if var_r <= 0.0:
        return 0.0 if mean_r >= 0.0 else float("-inf")
    annual_mean = mean_r * periods_per_year
    annual_std = math.sqrt(var_r) * math.sqrt(periods_per_year)
    excess = annual_mean - risk_free
    return excess / annual_std if annual_std > 0 else 0.0


def _tracking_error(portfolio_returns: list[float], benchmark_returns: list[float]) -> float:
    """Annualised tracking error (std of return differentials)."""
    n = len(portfolio_returns)
    if n < 2:
        return 0.0
    diffs = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
    mean_diff = sum(diffs) / n
    var = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    return math.sqrt(max(var, 0.0))


def _simulate_frequency(
    returns_panel: dict[str, list[float]],
    target_weights: dict[str, float],
    rebalance_period: int,
    cost_per_turnover: float,
    periods_per_year: int,
) -> dict[str, Any]:
    """Simulate a single rebalance frequency.

    Returns dict with sharpe, turnover, tracking_error, cost_drag.
    """
    assets = list(returns_panel.keys())
    n_periods = len(next(iter(returns_panel.values())))
    if n_periods < 2:
        return {"sharpe": 0.0, "turnover": 0.0, "tracking_error": 0.0, "cost_drag": 0.0}

    # Current weights (drift from target)
    current_weights = {a: target_weights.get(a, 0.0) for a in assets}
    portfolio_returns: list[float] = []
    total_turnover = 0.0

    # Benchmark: perfectly rebalanced every period
    benchmark_returns: list[float] = []

    for t in range(n_periods):
        # Portfolio return this period (using drifted weights)
        port_r = sum(current_weights.get(a, 0.0) * returns_panel[a][t] for a in assets)
        portfolio_returns.append(port_r)

        # Benchmark return (using target weights)
        bench_r = sum(target_weights.get(a, 0.0) * returns_panel[a][t] for a in assets)
        benchmark_returns.append(bench_r)

        # Update drifted weights: w_i_new = w_i_old * (1 + r_i) / sum(w_j * (1 + r_j))
        weighted_sum = 0.0
        raw_weights: dict[str, float] = {}
        for a in assets:
            raw = current_weights.get(a, 0.0) * (1.0 + returns_panel[a][t])
            raw_weights[a] = raw
            weighted_sum += raw
        if weighted_sum > 1e-12:
            for a in assets:
                current_weights[a] = raw_weights[a] / weighted_sum

        # Rebalance if this period is a rebalance point
        # Rebalance at t=0, and then every rebalance_period
        if t == 0 or (t + 1) % rebalance_period == 0:
            turnover = 0.5 * sum(abs(current_weights.get(a, 0.0) - target_weights.get(a, 0.0)) for a in assets)
            total_turnover += turnover
            # Reset weights to target
            current_weights = {a: target_weights.get(a, 0.0) for a in assets}

    avg_turnover = total_turnover / max(n_periods, 1)
    cost_drag = avg_turnover * cost_per_turnover * periods_per_year

    # Cost-adjusted returns: subtract cost_drag per period
    cost_drag_per_period = avg_turnover * cost_per_turnover
    net_returns = [r - cost_drag_per_period for r in portfolio_returns]

    sharpe = _annualized_sharpe(net_returns, periods_per_year)
    te = _tracking_error(portfolio_returns, benchmark_returns)
    annual_te = te * math.sqrt(periods_per_year)

    return {
        "sharpe": sharpe,
        "turnover": avg_turnover,
        "tracking_error": annual_te,
        "cost_drag": cost_drag,
    }


@dataclass(frozen=True)
class RebalancingIntelligenceResult:
    per_frequency: dict[str, dict[str, float]] = field(default_factory=dict)
    optimal_frequency: str = ""
    cost_drag_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_frequency": self.per_frequency,
            "optimal_frequency": self.optimal_frequency,
            "cost_drag_ratio": self.cost_drag_ratio,
        }


def rebalancing_intelligence_report(
    returns_panel: dict[str, list[float]],
    target_weights: dict[str, float],
    *,
    cost_per_turnover: float = 0.001,
    periods_per_year: int = 252,
) -> RebalancingIntelligenceResult:
    """Compare rebalancing frequencies on an equal-/target-weighted portfolio.

    Args:
        returns_panel: {asset: [period_returns]} mapping. All series must have
            equal length and be non-empty.
        target_weights: {asset: target_weight} mapping. Must match the asset
            keys in returns_panel.
        cost_per_turnover: Cost per unit of turnover (default 0.001 = 10 bps).
        periods_per_year: Periods per year for annualisation (default 252).

    Returns:
        RebalancingIntelligenceResult with per-frequency stats and optimal.

    Raises:
        ValueError: On invalid/missing/empty inputs.
    """
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year < 1:
        raise ValueError("periods_per_year must be an int >= 1")
    if not isinstance(target_weights, dict) or not target_weights:
        raise ValueError("target_weights must be a non-empty dict")

    # Validate all assets present in both
    panel_assets = set(returns_panel.keys())
    weight_assets = set(target_weights.keys())
    if panel_assets != weight_assets:
        missing_in_weights = panel_assets - weight_assets
        missing_in_panel = weight_assets - panel_assets
        msg_parts: list[str] = []
        if missing_in_weights:
            msg_parts.append(f"assets in returns_panel but not in target_weights: {missing_in_weights}")
        if missing_in_panel:
            msg_parts.append(f"assets in target_weights but not in returns_panel: {missing_in_panel}")
        raise ValueError("; ".join(msg_parts))

    # Validate equal-length series
    length: int | None = None
    for name, series in returns_panel.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
        for v in series:
            if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"returns_panel['{name}'] contains non-finite values")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise ValueError(f"returns_panel['{name}'] length {len(series)} != {length}")

    if length is None or length < 2:
        raise ValueError("returns_panel must have at least 2 periods")

    # Validate weights are finite
    for name, w in target_weights.items():
        if not math.isfinite(float(w)):
            raise ValueError(f"target_weights['{name}'] must be finite")

    if not math.isfinite(cost_per_turnover) or cost_per_turnover < 0:
        raise ValueError("cost_per_turnover must be a finite non-negative number")

    per_frequency: dict[str, dict[str, float]] = {}
    best_sharpe = float("-inf")
    optimal_freq = ""

    for label, period in _FREQUENCIES:
        result = _simulate_frequency(
            returns_panel, target_weights, period, cost_per_turnover, periods_per_year
        )
        per_frequency[label] = result
        if result["sharpe"] > best_sharpe:
            best_sharpe = result["sharpe"]
            optimal_freq = label

    # cost_drag_ratio: ratio of total cost drag to absolute sharpe for the optimal frequency
    opt_stats = per_frequency.get(optimal_freq, {})
    opt_sharpe = opt_stats.get("sharpe", 0.0)
    opt_cost = opt_stats.get("cost_drag", 0.0)
    cost_drag_ratio = opt_cost / abs(opt_sharpe) if abs(opt_sharpe) > 1e-12 else 0.0

    return RebalancingIntelligenceResult(
        per_frequency=per_frequency,
        optimal_frequency=optimal_freq,
        cost_drag_ratio=cost_drag_ratio,
    )
