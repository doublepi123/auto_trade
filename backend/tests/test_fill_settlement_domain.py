from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.fill_settlement import (
    EntryBooking,
    FillFacts,
    ReductionBooking,
    RepeatVerdict,
    compare_repeat,
    plan_entry_booking,
    plan_reduction_booking,
    settlement_key,
)


@pytest.mark.parametrize(
    ("quantity", "cost", "fill", "price", "expected"),
    [
        ("0", "0", "100", "100", EntryBooking(Decimal("100"), Decimal("10000"))),
        ("100", "10000", "50", "110", EntryBooking(Decimal("150"), Decimal("15500"))),
    ],
)
def test_plan_entry_booking_accumulates_weighted_cost(
    quantity: str, cost: str, fill: str, price: str, expected: EntryBooking,
) -> None:
    # Given / When
    result = plan_entry_booking(Decimal(quantity), Decimal(cost), Decimal(fill), Decimal(price))
    # Then
    assert result == expected


def test_plan_entry_booking_is_associative_for_two_fills() -> None:
    # Given
    zero = Decimal("0")
    first = plan_entry_booking(zero, zero, Decimal("100"), Decimal("100"))
    second = plan_entry_booking(zero, zero, Decimal("50"), Decimal("110"))
    # When
    forward = plan_entry_booking(first.quantity_after, first.cost_after, Decimal("50"), Decimal("110"))
    reverse = plan_entry_booking(second.quantity_after, second.cost_after, Decimal("100"), Decimal("100"))
    # Then
    assert forward == reverse == EntryBooking(Decimal("150"), Decimal("15500"))


@pytest.mark.parametrize(
    ("fill", "consumed", "quantity", "cost"),
    [("50", "50", "50", "5000"), ("150", "100", "0", "0")],
)
def test_plan_reduction_booking_consumes_min_and_clamps(
    fill: str, consumed: str, quantity: str, cost: str,
) -> None:
    # Given / When
    result = plan_reduction_booking(Decimal("100"), Decimal("10000"), Decimal(fill))
    # Then
    assert result == ReductionBooking(Decimal(consumed), Decimal(quantity), Decimal(cost))


@pytest.mark.parametrize("cost", ["-1", "0", "0.00000000000000000001"])
def test_plan_reduction_booking_never_produces_negative_cost(cost: str) -> None:
    # Given / When
    result = plan_reduction_booking(Decimal("3"), Decimal(cost), Decimal("1"))
    # Then
    assert result.quantity_after == Decimal("2")
    assert result.cost_after >= Decimal("0")


@pytest.mark.parametrize("quantity", ["0", "-1"])
def test_plan_reduction_booking_without_position(quantity: str) -> None:
    # Given / When
    result = plan_reduction_booking(Decimal(quantity), Decimal("10"), Decimal("5"))
    # Then
    assert result == ReductionBooking(Decimal("0"), Decimal("0"), Decimal("0"))


def test_plan_reduction_booking_preserves_decimal_operation_order() -> None:
    # Given / When
    result = plan_reduction_booking(Decimal("3"), Decimal("1"), Decimal("1"))
    # Then
    assert result.cost_after == Decimal("1") - Decimal("1") / Decimal("3")


def test_settlement_key_ignores_terminal_status() -> None:
    # Given: successive terminal observations of one cumulative fill.
    observations = [(" order-42 ", "CANCELED"), ("order-42", "FILLED")]
    # When
    keys = [settlement_key(order_id) for order_id, _status in observations]
    # Then
    assert keys == ["order-42", "order-42"]
    assert settlement_key("") is None
    assert settlement_key(" \t\n") is None


def test_compare_repeat_match_on_identical_facts() -> None:
    # Given
    facts = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), "BROKER", "BROKER")
    # When
    result = compare_repeat(facts, facts)
    # Then
    assert result.verdict is RepeatVerdict.MATCH


@pytest.mark.parametrize("field", ["quantity", "price", "both"])
def test_compare_repeat_tolerates_broker_correcting_a_fallback_estimate(field: str) -> None:
    # Given
    stored = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), "FALLBACK", "FALLBACK")
    incoming = FillFacts(
        "AAPL.US", "BUY", Decimal("90" if field != "price" else "100"),
        Decimal("101" if field != "quantity" else "100"), "BROKER", "BROKER",
    )
    # When
    result = compare_repeat(stored, incoming)
    # Then
    assert result.verdict is RepeatVerdict.TOLERATED_FALLBACK


def test_compare_repeat_conflicts_on_broker_quantity_mismatch() -> None:
    # Given
    stored = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), "BROKER", "BROKER")
    # When
    result = compare_repeat(stored, replace(stored, quantity=Decimal("100.00000000001")))
    # Then
    assert result.verdict is RepeatVerdict.CONFLICT
    assert "quantity" in result.reason


@pytest.mark.parametrize(
    ("symbol", "action", "field"),
    [("MSFT.US", "BUY", "symbol"), ("AAPL.US", "SELL", "action")],
)
def test_compare_repeat_conflicts_on_symbol_or_action_mismatch(
    symbol: str, action: str, field: str,
) -> None:
    # Given
    stored = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), "FALLBACK", "FALLBACK")
    incoming = replace(stored, symbol=symbol, action=action, quantity_source="BROKER", price_source="BROKER")
    # When
    result = compare_repeat(stored, incoming)
    # Then
    assert result.verdict is RepeatVerdict.CONFLICT
    assert field in result.reason


@pytest.mark.parametrize("price", ["100", "0.5", "-100"])
@pytest.mark.parametrize(
    ("factor", "expected"),
    [("0.999", RepeatVerdict.MATCH), ("1", RepeatVerdict.MATCH), ("1.001", RepeatVerdict.CONFLICT)],
)
def test_compare_repeat_price_tolerance_boundary(
    price: str, factor: str, expected: RepeatVerdict,
) -> None:
    # Given: incoming price is b in abs(a-b) <= 1e-9 * max(1, abs(b)).
    incoming = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal(price), "BROKER", "BROKER")
    tolerance = Decimal("1e-9") * max(Decimal("1"), abs(incoming.price))
    stored = replace(incoming, price=incoming.price + tolerance * Decimal(factor))
    # When
    result = compare_repeat(stored, incoming)
    # Then
    assert result.verdict is expected
    assert expected is not RepeatVerdict.CONFLICT or "price" in result.reason


@pytest.mark.parametrize("field", ["quantity", "price"])
@pytest.mark.parametrize(("stored_source", "incoming_source"), [("BROKER", "FALLBACK"), ("FALLBACK", "FALLBACK")])
def test_compare_repeat_conflicts_without_broker_correction(
    field: str, stored_source: str, incoming_source: str,
) -> None:
    # Given
    stored = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), stored_source, stored_source)
    incoming = FillFacts(
        "AAPL.US", "BUY", Decimal("90" if field == "quantity" else "100"),
        Decimal("101" if field == "price" else "100"), incoming_source, incoming_source,
    )
    # When
    result = compare_repeat(stored, incoming)
    # Then
    assert result.verdict is RepeatVerdict.CONFLICT
    assert field in result.reason


@pytest.mark.parametrize(("quantity_source", "price_source", "field"), [("FALLBACK", "BROKER", "price"), ("BROKER", "FALLBACK", "quantity")])
def test_compare_repeat_conflict_overrides_tolerated_field(
    quantity_source: str, price_source: str, field: str,
) -> None:
    # Given
    stored = FillFacts("AAPL.US", "BUY", Decimal("100"), Decimal("100"), quantity_source, price_source)
    incoming = FillFacts("AAPL.US", "BUY", Decimal("90"), Decimal("101"), "BROKER", "BROKER")
    # When
    result = compare_repeat(stored, incoming)
    # Then
    assert result.verdict is RepeatVerdict.CONFLICT
    assert field in result.reason
