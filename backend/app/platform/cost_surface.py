"""P311: Cost Surface — trade-cost landscape across participation × quantity grid.

Models the expected transaction cost (in basis points) as a function of
participation rate and order quantity using a square-root impact model:
``cost_bps = σ × √(participation) × 10 000`` scaled by the square-root of the
normalised quantity relative to ADV.

Deterministic, pure Python. Reference: Almgren & Chriss (2000), Kissell &
Glantz (2003) square-root market impact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CostSurfaceResult",
    "cost_surface_report",
]


@dataclass(frozen=True)
class CostSurfaceResult:
    grid: list[dict[str, Any]]
    min_cost_bps: float
    max_cost_bps: float
    mean_cost_bps: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid,
            "min_cost_bps": self.min_cost_bps,
            "max_cost_bps": self.max_cost_bps,
            "mean_cost_bps": self.mean_cost_bps,
        }


def cost_surface_report(
    adv: float,
    volatility: float,
    *,
    participation_levels: list[float] | None = None,
    qty_levels: list[float] | None = None,
) -> CostSurfaceResult:
    """Compute the cost surface across a participation × quantity grid.

    *adv* — average daily volume (shares). Must be > 0.
    *volatility* — daily volatility (decimal, e.g. 0.02 for 2%). Must be > 0.
    *participation_levels* — list of participation rates as fractions of ADV
      (default: [0.01, 0.05, 0.1, 0.2]).
    *qty_levels* — list of order quantities in shares (default: same fractions
      scaled by *adv*).
    """
    if isinstance(adv, bool) or not isinstance(adv, (int, float)):
        raise ValueError("adv must be a finite positive number")
    adv_f = float(adv)
    if not math.isfinite(adv_f) or adv_f <= 0:
        raise ValueError("adv must be a finite positive number")

    if isinstance(volatility, bool) or not isinstance(volatility, (int, float)):
        raise ValueError("volatility must be a finite positive number")
    vol_f = float(volatility)
    if not math.isfinite(vol_f) or vol_f <= 0:
        raise ValueError("volatility must be a finite positive number")

    if participation_levels is None:
        participation_levels = [0.01, 0.05, 0.1, 0.2]
    if qty_levels is None:
        qty_levels = [frac * adv_f for frac in participation_levels]

    for p in participation_levels:
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            raise ValueError("participation_levels must be finite numbers")
        if not math.isfinite(float(p)):
            raise ValueError("participation_levels must be finite numbers")
        if float(p) <= 0 or float(p) > 1:
            raise ValueError("participation_levels must be in (0, 1]")

    for q in qty_levels:
        if isinstance(q, bool) or not isinstance(q, (int, float)):
            raise ValueError("qty_levels must be finite positive numbers")
        qf = float(q)
        if not math.isfinite(qf) or qf <= 0:
            raise ValueError("qty_levels must be finite positive numbers")

    grid: list[dict[str, Any]] = []
    costs: list[float] = []

    for participation in participation_levels:
        p = float(participation)
        for qty in qty_levels:
            q = float(qty)
            # Almgren-Chriss style: temporary impact ~ σ×√(q/ADV); permanent impact ~ σ×p
            # participation enters independently through the permanent term.
            temporary_bps = vol_f * math.sqrt(q / adv_f) * 10000.0 * 0.5
            permanent_bps = vol_f * p * 10000.0 * 0.5
            cost_bps = temporary_bps + permanent_bps
            grid.append({"participation": p, "qty": q, "cost_bps": cost_bps})
            costs.append(cost_bps)

    return CostSurfaceResult(
        grid=grid,
        min_cost_bps=min(costs) if costs else 0.0,
        max_cost_bps=max(costs) if costs else 0.0,
        mean_cost_bps=sum(costs) / len(costs) if costs else 0.0,
    )
