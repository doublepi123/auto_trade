from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.indicators import ATR, EMA, RSI, SMA, IndicatorService


def _bar(close: str, high: str, low: str, minute: int) -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="A",
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=100,
    )


def test_sma_compute():
    bars = [_bar("10", "11", "9", 0), _bar("20", "21", "19", 1), _bar("30", "31", "29", 2)]
    assert SMA(period=3).compute(bars) == Decimal("20")


def test_sma_returns_none_when_insufficient():
    bars = [_bar("10", "11", "9", 0)]
    assert SMA(period=3).compute(bars) is None


def test_ema_smoothes():
    bars = [_bar("10", "11", "9", 0), _bar("10", "11", "9", 1), _bar("20", "21", "19", 2)]
    ema = EMA(period=2).compute(bars)
    assert ema is not None and ema > Decimal("10")


def test_rsi_all_up_is_high():
    bars = [_bar(str(100 + i), str(101 + i), str(99 + i), i) for i in range(15)]
    rsi = RSI(period=14).compute(bars)
    assert rsi is not None and rsi == Decimal("100")


def test_atr_compute():
    bars = [_bar("100", "105", "95", 0), _bar("100", "108", "98", 1), _bar("100", "110", "96", 2)]
    atr = ATR(period=2).compute(bars)
    assert atr is not None and atr > Decimal("0")


def test_indicator_service_caches_per_symbol():
    svc = IndicatorService([SMA(period=2)])
    svc.on_bar(_bar("10", "11", "9", 0))
    assert svc.value("A", "sma_2") is None  # only 1 bar
    svc.on_bar(_bar("20", "21", "19", 1))
    assert svc.value("A", "sma_2") == Decimal("15")
    assert svc.snapshot("A")["sma_2"] == Decimal("15")
