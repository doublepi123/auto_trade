from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.strategy_v2.signal_edge import ClusteredTTestResult, clustered_t_test
from app.platform.events import EventSource, FillEvent
from app.platform.round_trips import OpenLot, RoundTrip, pair_round_trips


_T0 = datetime(2026, 6, 22, 14, 30, tzinfo=timezone.utc)


def _fill(
    *,
    offset_seconds: int,
    symbol: str,
    side: str,
    quantity: int,
    price: str,
    commission: str,
    broker_order_id: str,
    fee: str = "0",
) -> FillEvent:
    return FillEvent(
        timestamp=_T0 + timedelta(seconds=offset_seconds),
        source=EventSource.BROKER,
        symbol=symbol,
        broker_order_id=broker_order_id,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        commission=Decimal(commission),
        fee=Decimal(fee),
    )


def test_pairs_simple_buy_then_sell_into_one_round_trip() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="0.50",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="sell-1",
        ),
    ]

    trips, open_lots = pair_round_trips(fills)

    assert open_lots == []
    assert len(trips) == 1
    trip = trips[0]
    assert isinstance(trip, RoundTrip)
    assert trip.symbol == "AAPL.US"
    assert trip.entry_at == _T0
    assert trip.exit_at == _T0 + timedelta(seconds=60)
    assert trip.entry_price == Decimal("10.00")
    assert trip.exit_price == Decimal("10.50")
    assert trip.quantity == 100
    assert trip.gross_pnl == Decimal("50.00")
    assert trip.fees == Decimal("1.00")
    assert trip.net_pnl == Decimal("49.00")
    assert trip.entry_notional == Decimal("1000.00")
    assert trip.net_return_pct == float(Decimal("49.00") / Decimal("1000.00") * 100)


def test_partial_exits_split_fifo_and_apportion_entry_commission() -> None:
    # One BUY lot split across two SELLs: each trip keeps the same entry price;
    # entry commission is pro-rata by quantity so the two shares sum to the original.
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="1.00",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=60,
            price="10.50",
            commission="0.30",
            broker_order_id="sell-60",
        ),
        _fill(
            offset_seconds=120,
            symbol="AAPL.US",
            side="SELL",
            quantity=40,
            price="9.00",
            commission="0.20",
            broker_order_id="sell-40",
        ),
    ]

    trips, open_lots = pair_round_trips(fills)

    assert open_lots == []
    assert len(trips) == 2

    first, second = trips
    assert first.quantity == 60
    assert first.entry_price == Decimal("10.00")
    assert first.exit_price == Decimal("10.50")
    assert first.gross_pnl == Decimal("30.00")
    assert first.fees == Decimal("0.90")
    assert first.net_pnl == Decimal("29.10")
    assert first.entry_notional == Decimal("600.00")
    assert first.net_return_pct == float(Decimal("29.10") / Decimal("600.00") * 100)

    assert second.quantity == 40
    assert second.entry_price == Decimal("10.00")
    assert second.exit_price == Decimal("9.00")
    assert second.gross_pnl == Decimal("-40.00")
    assert second.fees == Decimal("0.60")
    assert second.net_pnl == Decimal("-40.60")
    assert second.entry_notional == Decimal("400.00")
    assert second.net_return_pct == float(Decimal("-40.60") / Decimal("400.00") * 100)

    entry_share_first = first.fees - Decimal("0.30")
    entry_share_second = second.fees - Decimal("0.20")
    assert entry_share_first + entry_share_second == Decimal("1.00")


def test_one_exit_against_two_entry_lots_emits_two_fifo_trips() -> None:
    """FIFO per entry lot, not one blended trip.

    DailyPnlService blends multiple entry lots consumed by one SELL into a
    single ClosedRoundTrip (qty-weighted avg entry). This module emits one
    RoundTrip per consumed lot so the signal-edge gate counts TRADES, not
    closing fills. Exit commission is still pro-rata by quantity.
    """
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=50,
            price="10.00",
            commission="0.25",
            broker_order_id="buy-10",
        ),
        _fill(
            offset_seconds=30,
            symbol="AAPL.US",
            side="BUY",
            quantity=50,
            price="11.00",
            commission="0.25",
            broker_order_id="buy-11",
        ),
        _fill(
            offset_seconds=90,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="12.00",
            commission="1.00",
            broker_order_id="sell-100",
        ),
    ]

    trips, open_lots = pair_round_trips(fills)

    assert open_lots == []
    assert len(trips) == 2

    first, second = trips
    assert first.quantity == 50
    assert first.entry_at == _T0
    assert first.entry_price == Decimal("10.00")
    assert first.exit_price == Decimal("12.00")
    assert first.gross_pnl == Decimal("100.00")
    assert first.fees == Decimal("0.75")
    assert first.net_pnl == Decimal("99.25")
    assert first.entry_notional == Decimal("500.00")

    assert second.quantity == 50
    assert second.entry_at == _T0 + timedelta(seconds=30)
    assert second.entry_price == Decimal("11.00")
    assert second.exit_price == Decimal("12.00")
    assert second.gross_pnl == Decimal("50.00")
    assert second.fees == Decimal("0.75")
    assert second.net_pnl == Decimal("49.25")
    assert second.entry_notional == Decimal("550.00")

    assert first.exit_at == second.exit_at == _T0 + timedelta(seconds=90)


def test_unclosed_entry_returns_open_lot_not_a_round_trip() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="1.00",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=40,
            price="10.50",
            commission="0.20",
            broker_order_id="sell-40",
        ),
    ]

    trips, open_lots = pair_round_trips(fills)

    assert len(trips) == 1
    trip = trips[0]
    assert trip.quantity == 40
    assert trip.gross_pnl == Decimal("20.00")
    assert trip.fees == Decimal("0.60")
    assert trip.net_pnl == Decimal("19.40")
    assert trip.entry_notional == Decimal("400.00")

    assert open_lots == [
        OpenLot(
            symbol="AAPL.US",
            entry_at=_T0,
            entry_price=Decimal("10.00"),
            quantity=60,
            remaining_fees=Decimal("0.60"),
        )
    ]


def test_interleaved_symbols_pair_independently() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=10,
            price="100.00",
            commission="0.10",
            broker_order_id="aapl-buy",
        ),
        _fill(
            offset_seconds=10,
            symbol="MSFT.US",
            side="BUY",
            quantity=20,
            price="200.00",
            commission="0.40",
            broker_order_id="msft-buy",
        ),
        _fill(
            offset_seconds=20,
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price="110.00",
            commission="0.11",
            broker_order_id="aapl-sell",
        ),
        _fill(
            offset_seconds=30,
            symbol="MSFT.US",
            side="SELL",
            quantity=20,
            price="190.00",
            commission="0.38",
            broker_order_id="msft-sell",
        ),
    ]

    trips, open_lots = pair_round_trips(fills)

    assert open_lots == []
    assert len(trips) == 2
    aapl, msft = trips
    assert aapl.symbol == "AAPL.US"
    assert aapl.quantity == 10
    assert aapl.entry_price == Decimal("100.00")
    assert aapl.exit_price == Decimal("110.00")
    assert aapl.gross_pnl == Decimal("100.00")
    assert aapl.fees == Decimal("0.21")
    assert msft.symbol == "MSFT.US"
    assert msft.quantity == 20
    assert msft.entry_price == Decimal("200.00")
    assert msft.exit_price == Decimal("190.00")
    assert msft.gross_pnl == Decimal("-200.00")
    assert msft.fees == Decimal("0.78")


def test_empty_fills_returns_empty_trips_and_lots() -> None:
    assert pair_round_trips([]) == ([], [])


def test_round_trip_net_return_pct_feeds_clustered_t_test() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="0.50",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="sell-1",
        ),
    ]

    trips, _open_lots = pair_round_trips(fills)
    observations = [(rt.exit_at.date(), rt.net_return_pct) for rt in trips]
    result = clustered_t_test(observations)

    assert isinstance(result, ClusteredTTestResult)
    assert result.observations == len(trips)
    assert trips[0].net_return_pct == float(Decimal("49.00") / Decimal("1000.00") * 100)


def test_sell_without_open_entry_raises_value_error() -> None:
    """Unmatched SELL is a data error in long-only paper fills: raise, do not skip.

    DailyPnlService records a PnlReplayIssue because live ledgers can start
    mid-position. PaperBroker screening emits a complete stream; a SELL with
    no open lot means the input is inconsistent, and swallowing it would drop
    trades from the edge test.
    """
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="orphan-sell",
        ),
    ]

    with pytest.raises(ValueError):
        pair_round_trips(fills)


def test_sell_that_exceeds_open_quantity_raises_value_error() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=50,
            price="10.00",
            commission="0.25",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="oversell",
        ),
    ]

    with pytest.raises(ValueError):
        pair_round_trips(fills)


def test_unsorted_fills_are_sorted_by_timestamp_before_pairing() -> None:
    # Callers are not required to pre-sort; pairing sorts by timestamp (stable).
    buy = _fill(
        offset_seconds=0,
        symbol="AAPL.US",
        side="BUY",
        quantity=100,
        price="10.00",
        commission="0.50",
        broker_order_id="buy-1",
    )
    sell = _fill(
        offset_seconds=60,
        symbol="AAPL.US",
        side="SELL",
        quantity=100,
        price="10.50",
        commission="0.50",
        broker_order_id="sell-1",
    )

    trips, open_lots = pair_round_trips([sell, buy])

    assert open_lots == []
    assert len(trips) == 1
    assert trips[0].entry_at == buy.timestamp
    assert trips[0].exit_at == sell.timestamp
    assert trips[0].gross_pnl == Decimal("50.00")


def test_money_fields_are_decimal_and_net_return_pct_is_float() -> None:
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="0.50",
            broker_order_id="buy-1",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="sell-1",
        ),
    ]

    trips, _open_lots = pair_round_trips(fills)
    trip = trips[0]
    assert type(trip.entry_price) is Decimal
    assert type(trip.exit_price) is Decimal
    assert type(trip.gross_pnl) is Decimal
    assert type(trip.fees) is Decimal
    assert type(trip.net_pnl) is Decimal
    assert type(trip.entry_notional) is Decimal
    assert type(trip.net_return_pct) is float
    assert type(trip.quantity) is int


def test_fees_use_commission_field_not_fee() -> None:
    # PaperBroker writes FillEvent.commission; the leftover `fee` field is ignored.
    fills = [
        _fill(
            offset_seconds=0,
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            price="10.00",
            commission="0.50",
            broker_order_id="buy-1",
            fee="99.00",
        ),
        _fill(
            offset_seconds=60,
            symbol="AAPL.US",
            side="SELL",
            quantity=100,
            price="10.50",
            commission="0.50",
            broker_order_id="sell-1",
            fee="99.00",
        ),
    ]

    trips, _open_lots = pair_round_trips(fills)
    assert trips[0].fees == Decimal("1.00")


def test_round_trip_and_open_lot_are_frozen() -> None:
    assert RoundTrip.__dataclass_params__.frozen is True
    assert OpenLot.__dataclass_params__.frozen is True
