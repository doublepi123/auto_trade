"""P309: Pareto Optimization — multi-objective frontier selection.

Given a list of strategy/parameter configs with named metrics and a set of
objectives to maximize, return the Pareto-optimal subset (non-dominated
configs). A config ``A`` *dominates* ``B`` when ``A`` is at least as good as
``B`` on every objective and strictly better on at least one.

Deterministic, pure Python. Reference: Pareto (1906), multi-objective
optimisation in quantitative finance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "ParetoOptimizeResult",
    "pareto_optimize_report",
]


def _is_dominated(
    candidate: dict[str, Any],
    others: Sequence[dict[str, Any]],
    objectives: Sequence[str],
) -> bool:
    """Return True if *candidate* is dominated by any config in *others*."""
    for other in others:
        if other is candidate:
            continue
        at_least_as_good = True
        strictly_better = False
        for obj in objectives:
            a = float(candidate.get(obj, float("-inf")))
            b = float(other.get(obj, float("-inf")))
            if b < a:
                at_least_as_good = False
                break
            if b > a:
                strictly_better = True
        if at_least_as_good and strictly_better:
            return True
    return False


@dataclass(frozen=True)
class ParetoOptimizeResult:
    frontier: list[dict[str, Any]]
    frontier_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "frontier": self.frontier,
            "frontier_size": self.frontier_size,
        }


def pareto_optimize_report(
    configs: list[dict[str, Any]],
    *,
    objectives: list[str],
) -> ParetoOptimizeResult:
    """Return the Pareto-optimal subset of *configs* under *objectives* (maximise).

    Raises ``ValueError`` when inputs are empty, objectives are missing, or
    any objective value is non-finite.
    """
    if not configs:
        raise ValueError("configs must be non-empty")
    if len(configs) > 50:
        raise ValueError("configs must contain at most 50 entries")
    if not objectives:
        raise ValueError("objectives must be non-empty")

    for cfg in configs:
        if not isinstance(cfg, dict):
            raise ValueError("configs must contain dicts")
        for obj in objectives:
            if obj not in cfg:
                raise ValueError(f"objective '{obj}' missing in config")
            val = cfg[obj]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"objective '{obj}' must be a finite number")
            num = float(val)
            if not math.isfinite(num):
                raise ValueError(f"objective '{obj}' must be finite, got {num}")

    frontier: list[dict[str, Any]] = []
    for cfg in configs:
        if not _is_dominated(cfg, configs, objectives):
            frontier.append(cfg)

    return ParetoOptimizeResult(frontier=frontier, frontier_size=len(frontier))
