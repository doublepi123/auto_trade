"""Fixed quantity and tick quantization (PREREGISTRATION §10.5).

``q = floor(min(100, 25000/P, 250/(0.0045×P)))``; ``q < 1`` means no entry.
``ceil_to_tick`` rounds a price UP to the legal US equity tick.

Assumption stated in config: sub-$1.00 names are unreachable because §10.2
floors the T−1 close at $20, so a single $0.01 tick applies everywhere in
this package.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING

from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)


def position_quantity(
    price: Decimal,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> int:
    """§10.5 ``q = floor(min(100, 25000/P, 250/(0.0045·P)))``; 0 if ``q < 1``."""
    if price <= 0:
        return 0
    notional_bound = config.notional_cap_usd / price
    risk_bound = config.risk_cap_usd / (config.stop_loss_pct * price)
    cap = min(
        Decimal(config.quantity_cap_shares),
        notional_bound,
        risk_bound,
    )
    if cap < 1:
        return 0
    return math.floor(cap)


def ceil_to_tick(
    price: Decimal,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> Decimal:
    """Round ``price`` UP to the legal tick (§10.5 / §10.4 limit price).

    This version supports only the ≥ $1.00 single-tick tier: a price
    below ``us_tick_size_min_price`` raises ``ValueError`` rather than
    silently rounding (SEC Rule 612 sub-dollar pricing is out of scope —
    the §10.2 universe floors T−1 close at $20, so reaching it indicates
    an upstream data error).
    """
    if price < config.us_tick_size_min_price:
        raise ValueError(
            f"price {price} below the supported tick tier minimum "
            f"{config.us_tick_size_min_price}"
        )
    tick = config.us_tick_size
    ticks = (price / tick).to_integral_value(rounding=ROUND_CEILING)
    return (ticks * tick).quantize(tick)
