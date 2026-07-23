from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LongRoundTripEdge:
    gross_profit: Decimal
    estimated_fees: Decimal
    extra_costs: Decimal
    total_costs: Decimal
    net_profit: Decimal
    required_profit: Decimal
    edge_cost_ratio: Decimal | None

    def meets(self, minimum_edge_cost_ratio: Decimal = Decimal("0")) -> bool:
        if self.net_profit < self.required_profit:
            return False
        return (
            self.edge_cost_ratio is None
            or self.edge_cost_ratio >= minimum_edge_cost_ratio
        )


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


def evaluate_long_round_trip_edge(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    one_side_rate: Decimal,
    minimum_profit_amount: Decimal = Decimal("0"),
    minimum_profit_pct: Decimal = Decimal("0"),
    extra_costs: Decimal = Decimal("0"),
) -> LongRoundTripEdge:
    values = (
        entry_price,
        exit_price,
        quantity,
        one_side_rate,
        minimum_profit_amount,
        minimum_profit_pct,
        extra_costs,
    )
    if any(not value.is_finite() for value in values):
        raise ValueError("round-trip edge inputs must be finite")
    if entry_price <= 0 or exit_price <= 0 or quantity <= 0:
        raise ValueError("round-trip prices and quantity must be positive")
    if one_side_rate < 0 or minimum_profit_amount < 0 or minimum_profit_pct < 0:
        raise ValueError("round-trip rates and profit thresholds must be non-negative")
    if extra_costs < 0:
        raise ValueError("round-trip extra costs must be non-negative")

    gross_profit = (exit_price - entry_price) * quantity
    estimated_fees = estimate_round_trip_fee(
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        one_side_rate=one_side_rate,
    )
    total_costs = estimated_fees + extra_costs
    net_profit = gross_profit - total_costs
    percentage_profit = (
        entry_price
        * quantity
        * minimum_profit_pct
        / Decimal("100")
    )
    required_profit = max(minimum_profit_amount, percentage_profit)
    edge_cost_ratio = (
        gross_profit / total_costs
        if total_costs > 0
        else None
    )
    return LongRoundTripEdge(
        gross_profit=gross_profit,
        estimated_fees=estimated_fees,
        extra_costs=extra_costs,
        total_costs=total_costs,
        net_profit=net_profit,
        required_profit=required_profit,
        edge_cost_ratio=edge_cost_ratio,
    )
