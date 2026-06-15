import logging
from decimal import Decimal

from app.core.fees import estimate_round_trip_fee, one_side_fee_rate


def test_estimate_round_trip_fee_uses_entry_and_exit_notional() -> None:
    assert estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("10"),
        one_side_rate=Decimal("0.001"),
    ) == Decimal("2.02")


def test_one_side_fee_rate_selects_market_setting() -> None:
    assert one_side_fee_rate("US", Decimal("0.0005"), Decimal("0.003")) == Decimal("0.0005")
    assert one_side_fee_rate("HK", Decimal("0.0005"), Decimal("0.003")) == Decimal("0.003")


def test_estimate_round_trip_fee_zero_for_non_positive_quantity() -> None:
    assert estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("0"),
        one_side_rate=Decimal("0.001"),
    ) == Decimal("0")
    assert estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("-1"),
        one_side_rate=Decimal("0.001"),
    ) == Decimal("0")


def test_estimate_round_trip_fee_warns_for_non_positive_quantity(caplog) -> None:
    caplog.set_level(logging.WARNING)
    estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("0"),
        one_side_rate=Decimal("0.001"),
    )
    assert "quantity=0" in caplog.text

    caplog.clear()

    estimate_round_trip_fee(
        entry_price=Decimal("100"),
        exit_price=Decimal("102"),
        quantity=Decimal("-5"),
        one_side_rate=Decimal("0.001"),
    )
    assert "quantity=-5" in caplog.text
