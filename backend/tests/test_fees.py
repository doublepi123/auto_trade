import logging
from decimal import Decimal

import pytest

from app.core.fees import (
    estimate_round_trip_fee,
    evaluate_long_round_trip_edge,
    one_side_fee_rate,
)


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


def test_evaluate_long_round_trip_edge_reports_fee_adjusted_profit() -> None:
    edge = evaluate_long_round_trip_edge(
        entry_price=Decimal("199.75"),
        exit_price=Decimal("200.25"),
        quantity=Decimal("1000"),
        one_side_rate=Decimal("0.0005"),
        minimum_profit_pct=Decimal("0.2"),
        extra_costs=Decimal("79.9"),
    )

    assert edge.gross_profit == Decimal("500.00")
    assert edge.estimated_fees == Decimal("200.00000")
    assert edge.total_costs == Decimal("279.90000")
    assert edge.net_profit == Decimal("220.10000")
    assert edge.required_profit == Decimal("399.5000")
    assert edge.meets(Decimal("2")) is False


def test_evaluate_long_round_trip_edge_accepts_sufficient_net_edge() -> None:
    edge = evaluate_long_round_trip_edge(
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("100"),
        one_side_rate=Decimal("0.0005"),
        minimum_profit_amount=Decimal("10"),
        minimum_profit_pct=Decimal("0.2"),
        extra_costs=Decimal("4"),
    )

    assert edge.net_profit == Decimal("85.9500")
    assert edge.required_profit == Decimal("20.0")
    assert edge.meets(Decimal("2")) is True


def test_evaluate_long_round_trip_edge_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="positive"):
        evaluate_long_round_trip_edge(
            entry_price=Decimal("100"),
            exit_price=Decimal("101"),
            quantity=Decimal("0"),
            one_side_rate=Decimal("0.0005"),
        )
