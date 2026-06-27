from __future__ import annotations

import pytest

from app.platform.bar_builder import build_bars


def test_bar_builder_builds_dollar_bars_with_ohlc():
    body = build_bars([
        {"timestamp": "t1", "price": 100.0, "volume": 50},
        {"timestamp": "t2", "price": 101.0, "volume": 60},
        {"timestamp": "t3", "price": 99.0, "volume": 10},
    ], mode="dollar", threshold=10000).to_dict()
    assert body["bar_count"] == 2
    assert body["bars"][0]["open"] == 100.0
    assert body["bars"][0]["high"] == 101.0
    assert body["bars"][0]["close"] == 101.0


def test_bar_builder_rejects_non_positive_price():
    with pytest.raises(ValueError):
        build_bars([{"timestamp": "t", "price": 0.0, "volume": 1}], mode="tick", threshold=1)
    with pytest.raises(ValueError):
        build_bars(["bad"], mode="tick", threshold=1)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        build_bars([{"price": float("nan"), "volume": 1}], mode="tick", threshold=1)
    with pytest.raises(ValueError):
        build_bars([{"price": 1, "volume": 1}], mode="tick", threshold=float("inf"))
