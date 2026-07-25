from __future__ import annotations

from decimal import Decimal


DEFAULT_EDGE_SAFETY_BUFFER_BPS = 10.0


def estimated_round_trip_cost_pct(
    *,
    one_side_fee_rate: float,
    slippage_bps: float,
) -> float:
    """Return estimated round-trip fees and slippage in percent."""
    fee_rate = Decimal(str(one_side_fee_rate))
    slippage = Decimal(str(slippage_bps))
    if not all(value.is_finite() for value in (fee_rate, slippage)):
        raise ValueError("shadow cost assumptions must be finite")
    if fee_rate < 0 or slippage < 0:
        raise ValueError("shadow cost assumptions must be non-negative")
    return float(fee_rate * Decimal("200") + slippage * 2 / 100)


def minimum_profit_target_pct(
    *,
    one_side_fee_rate: float,
    slippage_bps: float,
    safety_buffer_bps: float = DEFAULT_EDGE_SAFETY_BUFFER_BPS,
) -> float:
    """Return the minimum gross target, in percent, after round-trip costs.

    The target must pay both entry and exit fees, both sides of configured
    slippage, and an explicit residual edge buffer.
    """
    buffer = Decimal(str(safety_buffer_bps))
    if not buffer.is_finite():
        raise ValueError("shadow cost assumptions must be finite")
    if buffer < 0:
        raise ValueError("shadow cost assumptions must be non-negative")
    round_trip_cost = Decimal(str(estimated_round_trip_cost_pct(
        one_side_fee_rate=one_side_fee_rate,
        slippage_bps=slippage_bps,
    )))
    return float(round_trip_cost + buffer / 100)


def estimated_net_reward_risk_ratio(
    *,
    profit_target_pct: float,
    stop_loss_pct: float,
    one_side_fee_rate: float,
    slippage_bps: float,
) -> float:
    """Return net target reward divided by cost-adjusted stop downside."""
    target = Decimal(str(profit_target_pct))
    stop = Decimal(str(stop_loss_pct))
    if not target.is_finite() or not stop.is_finite():
        raise ValueError("shadow exit thresholds must be finite")
    if target <= 0 or stop <= 0:
        raise ValueError("shadow exit thresholds must be positive")
    round_trip_cost = Decimal(str(estimated_round_trip_cost_pct(
        one_side_fee_rate=one_side_fee_rate,
        slippage_bps=slippage_bps,
    )))
    return float((target - round_trip_cost) / (stop + round_trip_cost))


__all__ = [
    "DEFAULT_EDGE_SAFETY_BUFFER_BPS",
    "estimated_net_reward_risk_ratio",
    "estimated_round_trip_cost_pct",
    "minimum_profit_target_pct",
]
