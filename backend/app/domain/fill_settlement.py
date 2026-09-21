"""Pure booking arithmetic and comparison of cumulative terminal fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class FillFacts:
    symbol: str
    action: str
    quantity: Decimal
    price: Decimal
    quantity_source: str
    price_source: str


@dataclass(frozen=True)
class EntryBooking:
    quantity_after: Decimal
    cost_after: Decimal


@dataclass(frozen=True)
class ReductionBooking:
    consumed_quantity: Decimal
    quantity_after: Decimal
    cost_after: Decimal


class RepeatVerdict(str, Enum):
    MATCH = "MATCH"
    TOLERATED_FALLBACK = "TOLERATED_FALLBACK"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class RepeatComparison:
    verdict: RepeatVerdict
    reason: str


def settlement_key(broker_order_id: str) -> str | None:
    """Use only the broker order ID; terminal status cannot split a receipt."""
    return broker_order_id.strip() or None


def plan_entry_booking(
    current_quantity: Decimal,
    current_cost: Decimal,
    fill_quantity: Decimal,
    fill_price: Decimal,
) -> EntryBooking:
    return EntryBooking(
        current_quantity + fill_quantity,
        current_cost + fill_price * fill_quantity,
    )


def plan_reduction_booking(
    current_quantity: Decimal,
    current_cost: Decimal,
    fill_quantity: Decimal,
) -> ReductionBooking:
    """Consume tracked cost in the same Decimal order as live settlement."""
    zero = Decimal("0")
    if current_quantity <= zero:
        return ReductionBooking(zero, zero, zero)
    consumed = min(current_quantity, fill_quantity)
    average_price = current_cost / current_quantity
    quantity_after = current_quantity - consumed
    cost_after = current_cost - average_price * consumed
    if quantity_after <= zero:
        return ReductionBooking(consumed, zero, zero)
    return ReductionBooking(consumed, quantity_after, max(zero, cost_after))


def compare_repeat(stored: FillFacts, incoming: FillFacts) -> RepeatComparison:
    """Compare receipt facts without rebooking later broker corrections."""
    if stored.symbol != incoming.symbol:
        return RepeatComparison(RepeatVerdict.CONFLICT, "symbol differs")
    if stored.action != incoming.action:
        return RepeatComparison(RepeatVerdict.CONFLICT, "action differs")

    quantity_matches = stored.quantity == incoming.quantity
    price_matches = abs(stored.price - incoming.price) <= (
        Decimal("1e-9") * max(Decimal("1"), abs(incoming.price))
    )
    tolerated_fields: list[str] = []
    for field, matches, stored_source, incoming_source in (
        ("quantity", quantity_matches, stored.quantity_source, incoming.quantity_source),
        ("price", price_matches, stored.price_source, incoming.price_source),
    ):
        if matches:
            continue
        if (stored_source, incoming_source) != ("FALLBACK", "BROKER"):
            return RepeatComparison(RepeatVerdict.CONFLICT, f"{field} differs")
        tolerated_fields.append(field)
    if tolerated_fields:
        return RepeatComparison(
            RepeatVerdict.TOLERATED_FALLBACK,
            f"broker corrected fallback {', '.join(tolerated_fields)}",
        )
    return RepeatComparison(RepeatVerdict.MATCH, "fill facts match")
