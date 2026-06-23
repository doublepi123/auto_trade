from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.latency import FixedLatencyModel
from app.platform.paper_broker import PaperBroker, PaperBrokerConfig
from app.platform.sdk import OrderIntent


def _bar(low="144", high="160", minute=0):
    return BarEvent(
        timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="A",
        open=Decimal("150"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("145"),
        volume=10000,
    )


def test_submit_latency_defers_eligibility():
    # With submit_bars=3 and same-bar promotion-then-match wiring:
    #   bar 0: submit_due 3 -> 2 (QUEUED, not matchable)
    #   bar 1: submit_due 2 -> 1 (QUEUED, not matchable)
    #   bar 2: submit_due 1 -> 0 -> SUBMITTED, then matching fires on bar 2
    # So exactly two non-filling bars precede the fill on the third bar.
    cfg = PaperBrokerConfig(latency_model=FixedLatencyModel(submit_bars=3))
    broker = PaperBroker(config=cfg)
    ev = broker.submit(
        OrderIntent(
            symbol="A",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=Decimal("145"),
            reason="t",
        )
    )
    assert ev.status == "QUEUED"
    assert broker.on_bar(_bar(minute=0)) == []
    assert broker.on_bar(_bar(minute=1)) == []
    fills = broker.on_bar(_bar(minute=2))
    assert len(fills) == 1


def test_fill_latency_holds_fill_then_emits():
    cfg = PaperBrokerConfig(latency_model=FixedLatencyModel(submit_bars=0, fill_bars=1))
    broker = PaperBroker(config=cfg)
    broker.submit(
        OrderIntent(
            symbol="A",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=Decimal("145"),
            reason="t",
        )
    )
    # triggering bar: fill is held, not emitted this bar
    assert broker.on_bar(_bar(minute=0)) == []
    # next bar: held fill emits
    fills = broker.on_bar(_bar(minute=1))
    assert len(fills) == 1


def test_no_latency_backward_compat_fills_immediately():
    broker = PaperBroker()
    broker.submit(
        OrderIntent(
            symbol="A",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=Decimal("145"),
            reason="t",
        )
    )
    fills = broker.on_bar(_bar(minute=0))
    assert len(fills) == 1
