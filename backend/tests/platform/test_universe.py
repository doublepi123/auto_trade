from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.events import BarEvent, EventSource
from app.platform.universe import StaticUniverse, TopNByVolumeUniverse


def _bar(symbol: str, volume: int, minute: int) -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=symbol,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=volume,
    )


def test_static_universe_membership():
    u = StaticUniverse(["AAPL.US", "TSLA.US"])
    assert u.contains("AAPL.US") is True
    assert u.contains("MSFT.US") is False


def test_topn_volume_universe_ranks_by_volume():
    u = TopNByVolumeUniverse(n=2, lookback=10)
    u.contains("AAPL.US", _bar("AAPL.US", 1000, 0))
    u.contains("TSLA.US", _bar("TSLA.US", 5000, 0))
    u.contains("MSFT.US", _bar("MSFT.US", 200, 0))
    assert u.contains("TSLA.US") is True  # highest volume
    assert u.contains("AAPL.US") is True  # second
    assert u.contains("MSFT.US") is False  # lowest, excluded from top-2


def test_topn_uses_rolling_lookback():
    u = TopNByVolumeUniverse(n=1, lookback=2)
    # AAPL high early, then low; TSLA steady
    u.contains("AAPL.US", _bar("AAPL.US", 1000, 0))
    u.contains("AAPL.US", _bar("AAPL.US", 10, 1))
    u.contains("TSLA.US", _bar("TSLA.US", 500, 1))
    # rolling window of 2: AAPL sum = 1010, TSLA = 500 -> AAPL still top
    assert u.contains("AAPL.US") is True
    assert u.contains("TSLA.US") is False
