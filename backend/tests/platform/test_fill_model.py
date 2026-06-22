from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.fill_model import (
    FillModel,
    FixedPerShareCommissionModel,
    FixedSlippageModel,
    FractionalCommissionModel,
    VolumeShareSlippageModel,
)
from app.platform.paper_broker import PaperBroker, PaperBrokerConfig
from app.platform.sdk import OrderIntent


def _bar(low="144", high="160", volume=10000):
    return BarEvent(
        timestamp=datetime(2026, 6, 23, 10, 1, tzinfo=timezone.utc),
        source=EventSource.MARKET, symbol="A",
        open=Decimal("150"), high=Decimal(high), low=Decimal(low), close=Decimal("145"), volume=volume,
    )


def test_fixed_slippage_and_fractional_commission_via_fill_model():
    fm = FillModel(slippage_model=FixedSlippageModel(Decimal("0.05")), commission_model=FractionalCommissionModel(Decimal("0.001")))
    broker = PaperBroker(config=PaperBrokerConfig(fill_model=fm))
    broker.submit(OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t"))
    fills = broker.on_bar(_bar())
    assert len(fills) == 1
    assert fills[0].slippage == Decimal("0.05")
    # price = min(150, 145) + 0.05 = 145.05 ; commission = 145.05*10*0.001 = 1.4505
    assert fills[0].price == Decimal("145.05")
    assert fills[0].commission == Decimal("145.05") * Decimal(10) * Decimal("0.001")


def test_volume_share_slippage_grows_with_order_size():
    fm = FillModel(slippage_model=VolumeShareSlippageModel(Decimal("0.1")), commission_model=FixedPerShareCommissionModel(Decimal("0")))
    broker = PaperBroker(config=PaperBrokerConfig(fill_model=fm))
    # qty 1000 vs volume 10000 -> share 0.1 -> slip = 0.1 * 0.1 * base = 0.01*base
    broker.submit(OrderIntent(symbol="A", side="BUY", quantity=1000, order_type="LIMIT", limit_price=Decimal("145"), reason="t"))
    fills = broker.on_bar(_bar(volume=10000))
    assert len(fills) == 1
    base = min(Decimal("150"), Decimal("145"))  # 145
    expected_slip = (Decimal(1000) / Decimal(10000) * Decimal("0.1") * base).quantize(Decimal("0.01"))
    assert fills[0].slippage == expected_slip
    assert fills[0].price == base + expected_slip


def test_backward_compat_without_fill_model_uses_fixed_coeffs():
    broker = PaperBroker(config=PaperBrokerConfig(slippage_ticks=Decimal("0.01"), commission_rate=Decimal("0.0005")))
    broker.submit(OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t"))
    fills = broker.on_bar(_bar())
    assert len(fills) == 1
    assert fills[0].slippage == Decimal("0.01")
    assert fills[0].price == Decimal("145.01")


def test_zero_volume_bar_volume_share_slippage_is_zero():
    fm = FillModel(slippage_model=VolumeShareSlippageModel(Decimal("0.1")), commission_model=FractionalCommissionModel(Decimal("0")))
    broker = PaperBroker(config=PaperBrokerConfig(fill_model=fm))
    broker.submit(OrderIntent(symbol="A", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t"))
    fills = broker.on_bar(_bar(volume=0))
    assert len(fills) == 1
    assert fills[0].slippage == Decimal("0")
