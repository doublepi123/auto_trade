"""P332: Reverse stress test — find the smallest shock that breaches a loss threshold.

Given a portfolio (positions × betas) and a loss threshold, compute the critical
scenario multiplier: the smallest factor by which a scenario's market return must
be scaled to cause the portfolio loss to exactly meet the threshold.

``critical_multiplier > 1`` means the scenario is "safe" (shock must be amplified
before the threshold is breached). ``critical_multiplier < 1`` means the threshold
is already breached at baseline. The critical scenario is the one most vulnerable
(i.e. smallest multiplier).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ReverseStressResult", "reverse_stress_report"]

_DEFAULT_SCENARIOS: list[dict[str, Any]] = [
    {"name": "equity_crash", "market_return": -0.10},
    {"name": "vol_spike", "market_return": 0.05},
    {"name": "corr_breakdown", "market_return": -0.05},
]


@dataclass(frozen=True)
class ReverseStressResult:
    critical_scenario_name: str
    critical_multiplier: float
    scenario_details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "critical_scenario_name": self.critical_scenario_name,
            "critical_multiplier": self.critical_multiplier,
            "scenario_details": self.scenario_details,
        }


def reverse_stress_report(
    positions: dict[str, float],
    betas: dict[str, float],
    loss_threshold: float,
    *,
    scenarios: list[dict[str, Any]] | None = None,
) -> ReverseStressResult:
    """Compute reverse stress test report.

    Args:
        positions: {symbol: notional} mapping.
        betas: {symbol: beta} mapping (market sensitivity).
        loss_threshold: Positive number representing the portfolio loss threshold.
        scenarios: Optional list of scenarios, each with ``name`` (str) and
            ``market_return`` (float). Defaults to equity_crash (-10%),
            vol_spike (+5%), corr_breakdown (-5%).

    Returns:
        ReverseStressResult with critical scenario, multiplier, and details.

    Raises:
        ValueError: If loss_threshold ≤ 0, or required keys are missing/invalid.
    """
    if not positions or not isinstance(positions, dict):
        raise ValueError("positions must be a non-empty dict")
    if not betas or not isinstance(betas, dict):
        raise ValueError("betas must be a non-empty dict")
    if not math.isfinite(loss_threshold) or loss_threshold <= 0:
        raise ValueError("loss_threshold must be a finite positive number")

    for sym, v in positions.items():
        if not math.isfinite(v):
            raise ValueError(f"position '{sym}' must be a finite number")
    for sym, v in betas.items():
        if not math.isfinite(v):
            raise ValueError(f"beta for '{sym}' must be a finite number")

    if scenarios is None:
        scenarios = _DEFAULT_SCENARIOS

    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("scenarios must be a non-empty list")

    details: list[dict[str, Any]] = []
    best_name = ""
    best_multiplier = float("inf")

    for sc in scenarios:
        if not isinstance(sc, dict):
            raise ValueError("each scenario must be a dict")
        name = str(sc.get("name", ""))
        if not name:
            raise ValueError("each scenario must have a 'name'")
        if "market_return" not in sc:
            raise ValueError(f"scenario '{name}' must have 'market_return'")
        mr = float(sc.get("market_return", 0.0))
        if not math.isfinite(mr):
            raise ValueError(f"scenario '{name}' market_return must be finite")

        # portfolio loss = sum(position_i * beta_i * market_return)
        portfolio_loss = 0.0
        for sym, notional in positions.items():
            beta = betas.get(sym, 1.0)
            portfolio_loss += notional * beta * mr

        # Treat loss as positive number: loss_threshold / abs(portfolio_loss)
        abs_loss = abs(portfolio_loss)
        if abs_loss < 1e-18:
            multiplier = float("inf")
        else:
            multiplier = loss_threshold / abs_loss

        detail = {
            "name": name,
            "market_return": mr,
            "portfolio_loss": portfolio_loss,
            "abs_portfolio_loss": abs_loss,
            "multiplier": multiplier if math.isfinite(multiplier) else float("inf"),
        }
        details.append(detail)

        if multiplier < best_multiplier:
            best_multiplier = multiplier
            best_name = name

    if best_multiplier == float("inf"):
        best_multiplier = 1.0  # degenerate case

    return ReverseStressResult(
        critical_scenario_name=best_name,
        critical_multiplier=best_multiplier,
        scenario_details=details,
    )
