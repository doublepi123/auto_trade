from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base
from app.platform.data_catalog import DataCatalog, resample_bars
from app.platform.events import BarEvent, EventSource
from app.platform.store import EventStore


def _bar(minute: int, opn: str, high: str, low: str, close: str, volume: int = 100, symbol: str = "A") -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=symbol,
        open=Decimal(opn),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def test_resample_buckets_five_minutes():
    bars = [
        _bar(0, "10", "12", "9", "11"),
        _bar(1, "11", "13", "10", "12"),
        _bar(2, "12", "14", "11", "13"),
        _bar(3, "13", "15", "12", "14"),
        _bar(4, "14", "16", "13", "15"),
        _bar(5, "15", "17", "14", "16"),  # new bucket
    ]
    out = resample_bars(bars, target_minutes=5)
    assert len(out) == 2
    first = out[0]
    assert first.open == Decimal("10")
    assert first.high == Decimal("16")
    assert first.low == Decimal("9")
    assert first.close == Decimal("15")
    assert first.volume == 500
    assert out[1].open == Decimal("15")


def test_resample_one_minute_returns_copy():
    bars = [_bar(0, "10", "11", "9", "10")]
    out = resample_bars(bars, target_minutes=1)
    assert out == bars
    assert out is not bars  # copy, not same object


def test_data_catalog_load_and_resample():
    Base.metadata.create_all(bind=engine)
    store = EventStore()
    store.clear()
    for m in range(6):
        store.append(_bar(m, "10", "11", "9", "10"))
    catalog = DataCatalog(store)
    bars_1m = catalog.load_bars("A", limit=100, resolution_minutes=1)
    assert len(bars_1m) == 6
    bars_5m = catalog.load_bars("A", limit=100, resolution_minutes=5)
    assert len(bars_5m) == 2
