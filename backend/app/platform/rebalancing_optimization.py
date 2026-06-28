"""P363: Rebalancing optimization — step-by-step cost / tracking-error frontier.

Pure-Python rebalancing optimizer: given current and target portfolio weights
along with a covariance matrix, computes the tracking error (TE) and
cumulative transaction cost for each integer number of rebalancing steps
up to ``max_steps``, then selects the optimal number of steps by minimising
``total_cost + TE * penalty``.

Public surface
--------------

* **rebalancing_optimization_report(current_weights, target_weights,
  covariance, cost_rate, max_steps)** → frozen
  :class:`RebalancingOptimizationResult` with ``frontier``, ``optimal_steps``,
  ``current_tracking_error``, and ``immediate_cost``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["RebalancingOptimizationResult", "rebalancing_optimization_report"]


def _validate_weights(weights: dict[str, float], field: str) -> dict[str, float]:
    if not isinstance(weights, dict) or not weights:
        raise ValueError(f"{field} must be a non-empty dict")
    out: dict[str, float] = {}
    for k, v in weights.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{field}['{k}'] must be a finite number")
        if not math.isfinite(float(v)):
            raise ValueError(f"{field}['{k}'] must be a finite number")
        out[str(k)] = float(v)
    return out


def _tracking_error(
    w_c: dict[str, float],
    w_t: dict[str, float],
    cov: dict[str, dict[str, float]],
) -> float:
    """Compute tracking error = sqrt((w_c - w_t)' Cov (w_c - w_t))."""
    diff = {k: w_c.get(k, 0.0) - w_t.get(k, 0.0) for k in set(w_c.keys()) | set(w_t.keys())}
    te2 = 0.0
    for i, di in diff.items():
        for j, dj in diff.items():
            ci = cov.get(i, {})
            cj = cov.get(j, {})
            cov_ij = ci.get(j, cj.get(i, 0.0))
            te2 += di * dj * cov_ij
    return math.sqrt(max(te2, 0.0))


@dataclass(frozen=True)
class RebalancingOptimizationResult:
    frontier: list[dict[str, Any]]
    optimal_steps: int
    current_tracking_error: float
    immediate_cost: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "frontier": [dict(f) for f in self.frontier],
            "optimal_steps": self.optimal_steps,
            "current_tracking_error": self.current_tracking_error,
            "immediate_cost": self.immediate_cost,
        }


def rebalancing_optimization_report(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    covariance: dict[str, dict[str, float]],
    *,
    cost_rate: float = 0.001,
    max_steps: int = 10,
) -> RebalancingOptimizationResult:
    current = _validate_weights(current_weights, "current_weights")
    target = _validate_weights(target_weights, "target_weights")
    if current.keys() != target.keys():
        raise ValueError("current_weights and target_weights must have the same assets")

    if not isinstance(covariance, dict) or not covariance:
        raise ValueError("covariance must be a non-empty dict")
    # Validate covariance matrix: each asset in weights must be present
    for asset in current:
        if asset not in covariance:
            raise ValueError(f"covariance matrix must include asset '{asset}'")

    if isinstance(cost_rate, bool) or not isinstance(cost_rate, (int, float)):
        raise ValueError("cost_rate must be a finite number")
    cost_rate_f = float(cost_rate)
    if not math.isfinite(cost_rate_f) or cost_rate_f < 0:
        raise ValueError("cost_rate must be a finite non-negative number")

    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise ValueError("max_steps must be a positive int")
    if max_steps < 1:
        raise ValueError("max_steps must be a positive int")

    # Current tracking error
    te_current = _tracking_error(current, target, covariance)

    # Weight difference per asset
    assets = list(current.keys())
    diff = {a: current[a] - target[a] for a in assets}
    total_abs_diff = sum(abs(d) for d in diff.values())

    # Immediate cost (single-step)
    immediate_cost = total_abs_diff * cost_rate_f

    frontier: list[dict[str, Any]] = []
    # TE penalty factor — scales TE to be comparable with cost (basis points)
    te_penalty = 0.01

    best_score = float("inf")
    optimal_steps = 1

    for k in range(1, max_steps + 1):
        # Partial weights through k equal steps
        partial = {a: current[a] - diff[a] / k for a in assets}
        te_k = _tracking_error(partial, target, covariance)
        # Cost: each of k steps incurs |diff|/k * cost_rate
        total_cost = sum(abs(diff[a]) / k * cost_rate_f for a in assets) * k
        # total_cost remains Σ|Δw_i| * cost_rate regardless of k

        frontier.append({
            "steps": k,
            "total_cost": round(total_cost, 8),
            "tracking_error": round(te_k, 8),
        })

        score = total_cost + te_k * te_penalty
        if score < best_score:
            best_score = score
            optimal_steps = k

    return RebalancingOptimizationResult(
        frontier=frontier,
        optimal_steps=optimal_steps,
        current_tracking_error=round(te_current, 8),
        immediate_cost=round(immediate_cost, 8),
    )
