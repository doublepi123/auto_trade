from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def one_side_fee_rate(market: str, fee_rate_us: Decimal, fee_rate_hk: Decimal) -> Decimal:
    return fee_rate_hk if market.upper() == "HK" else fee_rate_us


def estimate_round_trip_fee(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    one_side_rate: Decimal,
) -> Decimal:
    if quantity <= 0 or one_side_rate <= 0:
        if quantity <= 0:
            logger.warning("estimate_round_trip_fee called with quantity=%s; returning 0", quantity)
        if one_side_rate <= 0:
            logger.warning("estimate_round_trip_fee called with one_side_rate=%s; returning 0 (possible config error)", one_side_rate)
        return Decimal("0")
    return (entry_price + exit_price) * quantity * one_side_rate
