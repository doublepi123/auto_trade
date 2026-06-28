"""P346: Option strategy payoff analysis.

Compute a combined payoff diagram for a multi-leg option strategy at expiry.
Supports calls and puts with positive (long) or negative (short) quantities.
Generates payoff over a configurable spot range with breakeven detection.

Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["OptionStrategyPayoffResult", "option_strategy_payoff_report"]


_VALID_TYPES = {"call", "put"}


@dataclass(frozen=True)
class OptionStrategyPayoffResult:
    payoff_points: list[dict[str, float]] = field(default_factory=list)
    breakeven_points: list[float] = field(default_factory=list)
    max_profit: float | None = None
    max_loss: float | None = None
    total_premium: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "payoff_points": self.payoff_points,
            "breakeven_points": self.breakeven_points,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "total_premium": self.total_premium,
        }


def _single_leg_payoff(spot: float, leg: dict[str, Any]) -> float:
    """Compute the expiry payoff (excluding premium) of a single option leg."""
    strike = leg["strike"]
    qty = leg["quantity"]
    opt_type = leg["type"]
    if opt_type == "call":
        intrinsic = max(spot - strike, 0.0)
    else:  # put
        intrinsic = max(strike - spot, 0.0)
    return qty * intrinsic


def option_strategy_payoff_report(
    legs: list[dict[str, Any]],
    *,
    spot_range: list[float] | None = None,
) -> OptionStrategyPayoffResult:
    """Compute multi-leg option strategy payoff at expiry.

    Args:
        legs: List of leg dicts with keys ``strike`` (float), ``type``
            (``"call"`` or ``"put"``), ``quantity`` (float, positive = long,
            negative = short), and ``premium`` (float, paid/received).
        spot_range: Optional list of spot prices at which to evaluate.
            Defaults to a linear grid from ``0.5 * min_strike`` to
            ``1.5 * max_strike`` with 100 points.

    Returns:
        OptionStrategyPayoffResult with payoff_points, breakeven_points,
        max_profit, max_loss, total_premium.

    Raises:
        ValueError: On invalid legs, missing keys, non-finite values,
            invalid option type, or empty legs.
    """
    if not legs:
        raise ValueError("legs must be a non-empty list")

    strikes: list[float] = []
    total_premium = 0.0

    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise ValueError(f"legs[{i}] must be a dict")
        for key in ("strike", "type", "quantity", "premium"):
            if key not in leg:
                raise ValueError(f"legs[{i}] missing '{key}'")
        opt_type = str(leg["type"])
        if opt_type not in _VALID_TYPES:
            raise ValueError(f"legs[{i}] type must be 'call' or 'put', got '{opt_type}'")
        strike = leg["strike"]
        if not isinstance(strike, (int, float)) or isinstance(strike, bool):
            raise ValueError(f"legs[{i}] strike must be a finite number")
        strike_f = float(strike)
        if not math.isfinite(strike_f) or strike_f <= 0:
            raise ValueError(f"legs[{i}] strike must be a finite positive number")
        qty = leg["quantity"]
        if not isinstance(qty, (int, float)) or isinstance(qty, bool):
            raise ValueError(f"legs[{i}] quantity must be a finite number")
        qty_f = float(qty)
        if not math.isfinite(qty_f):
            raise ValueError(f"legs[{i}] quantity must be a finite number")
        premium = leg["premium"]
        if not isinstance(premium, (int, float)) or isinstance(premium, bool):
            raise ValueError(f"legs[{i}] premium must be a finite number")
        premium_f = float(premium)
        if not math.isfinite(premium_f):
            raise ValueError(f"legs[{i}] premium must be a finite number")
        strikes.append(strike_f)
        total_premium += qty_f * premium_f

    min_strike = min(strikes)
    max_strike = max(strikes)

    if spot_range is None:
        low = 0.5 * min_strike
        high = 1.5 * max_strike
        spot_range = [low + (high - low) * i / 99.0 for i in range(100)]

    payoff_points: list[dict[str, float]] = []
    payoffs: list[float] = []
    for spot in spot_range:
        gross = sum(_single_leg_payoff(spot, leg) for leg in legs)
        net = gross - total_premium
        payoff_points.append({"spot": spot, "payoff": net})
        payoffs.append(net)

    # Find breakeven points (where payoff crosses zero)
    breakeven_points: list[float] = []
    for i in range(len(payoffs) - 1):
        p0 = payoffs[i]
        p1 = payoffs[i + 1]
        if p0 == 0.0:
            if i == 0 or (i > 0 and payoffs[i - 1] != 0.0):
                breakeven_points.append(spot_range[i])
        elif p0 * p1 < 0:
            # Linear interpolation
            s0 = spot_range[i]
            s1 = spot_range[i + 1]
            # find s where payoff = 0
            be = s0 - p0 * (s1 - s0) / (p1 - p0)
            breakeven_points.append(be)
    # Check last point
    if payoffs and payoffs[-1] == 0.0 and (len(payoffs) < 2 or payoffs[-2] != 0.0):
        breakeven_points.append(spot_range[-1])

    # Compute max_profit and max_loss analytically.
    # Puts are always bounded (max payoff when spot=0: qty*strike).
    # Calls are unbounded when net long, unbounded loss when net short.
    net_call_qty = sum(float(leg["quantity"]) for leg in legs if leg["type"] == "call")
    has_unbounded_upside = net_call_qty > 0
    has_unbounded_downside = net_call_qty < 0

    # Analytic payoff at spot=0 (for puts) and spot=∞ (for calls)
    spot_zero_payoff = sum(
        float(leg["quantity"]) * float(leg["strike"]) if leg["type"] == "put" else 0.0
        for leg in legs
    ) - total_premium

    # Also compute payoff at each strike (where kinks might occur that the grid misses)
    strike_payoffs = []
    for ks in strikes:
        strike_payoffs.append(
            sum(_single_leg_payoff(ks, leg) for leg in legs) - total_premium
        )

    if has_unbounded_upside:
        max_profit: float | None = None
    else:
        max_profit = max(payoffs + strike_payoffs + [spot_zero_payoff]) if payoffs else spot_zero_payoff

    if has_unbounded_downside:
        max_loss: float | None = None
    else:
        all_payoffs = payoffs + strike_payoffs + [spot_zero_payoff]
        max_loss = min(all_payoffs)

    return OptionStrategyPayoffResult(
        payoff_points=payoff_points,
        breakeven_points=breakeven_points,
        max_profit=max_profit,
        max_loss=max_loss,
        total_premium=-total_premium,  # negative = net debit, positive = net credit
    )
