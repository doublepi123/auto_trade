from __future__ import annotations

from decimal import Decimal


DEFAULT_EDGE_SAFETY_BUFFER_BPS = 10.0


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
    fee_rate = Decimal(str(one_side_fee_rate))
    slippage = Decimal(str(slippage_bps))
    buffer = Decimal(str(safety_buffer_bps))
    if not all(value.is_finite() for value in (fee_rate, slippage, buffer)):
        raise ValueError("shadow cost assumptions must be finite")
    if fee_rate < 0 or slippage < 0 or buffer < 0:
        raise ValueError("shadow cost assumptions must be non-negative")
    return float(fee_rate * Decimal("200") + (slippage * 2 + buffer) / 100)


__all__ = ["DEFAULT_EDGE_SAFETY_BUFFER_BPS", "minimum_profit_target_pct"]
