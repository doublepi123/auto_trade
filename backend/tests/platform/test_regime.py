"""Tests for P213 market regime detection."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.platform.bus import EventBus
from app.platform.events import BarEvent, EventSource, RegimeEvent, event_from_dict
from app.platform.regime import (
    Regime,
    RegimeConfig,
    RegimeModel,
    classify,
    regime_report,
    rolling_regime,
)


def _bars(closes, highs=None, lows=None, start=None):
    start = start or datetime(2024, 1, 1, 9, 30)
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs is not None else c + 1
        lo = lows[i] if lows is not None else c - 1
        out.append(BarEvent(
            timestamp=start + timedelta(minutes=i),
            source=EventSource.MARKET,
            symbol="TEST",
            open=c, high=h, low=lo, close=c, volume=1000,
        ))
    return out


def _ramp(n=101, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def test_classify_bull_trending_up():
    closes = _ramp(101, 100.0, 1.0)
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    snap = classify(closes, highs, lows)
    assert snap.regime == Regime.BULL
    assert snap.slope > 0
    assert snap.sma_short > snap.sma_long


def test_classify_bear_trending_down():
    closes = _ramp(101, 200.0, -1.0)
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    snap = classify(closes, highs, lows)
    assert snap.regime == Regime.BEAR
    assert snap.slope < 0
    assert snap.sma_long > snap.sma_short


def test_classify_sideways_choppy():
    # oscillating, no drift, tight range — full cycles so SMA short ≈ long.
    closes = [100.0 + math.sin(i / 5.0) * 0.5 for i in range(101)]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    cfg = RegimeConfig(trend_threshold=0.02, adx_threshold=40.0)
    snap = classify(closes, highs, lows, cfg)
    assert snap.regime == Regime.SIDEWAYS
    assert abs(snap.slope) < 0.5


def test_classify_constant_price_is_sideways():
    closes = [100.0] * 60
    snap = classify(closes)
    assert snap.regime == Regime.SIDEWAYS
    assert snap.slope == 0.0
    assert snap.realized_vol == 0.0
    d = snap.as_dict()
    assert not any(math.isnan(v) for v in d.values() if isinstance(v, float))


def test_classify_too_short_raises():
    with pytest.raises(ValueError):
        classify([1.0, 2.0, 3.0, 4.0])


def test_classify_empty_raises():
    with pytest.raises(ValueError):
        classify([])


def test_classify_nan_raises():
    with pytest.raises(ValueError):
        classify([1.0, float("nan"), 2.0] + [10.0] * 100)


def test_classify_highs_lows_length_mismatch_raises():
    with pytest.raises(ValueError):
        classify([1.0] * 60, highs=[1.0] * 59)


def test_rolling_regime_length_and_warmup():
    closes = _ramp(101)
    out = rolling_regime(closes)
    assert len(out) == 101
    assert all(x is None for x in out[: RegimeConfig().min_bars - 1])
    assert out[-1] is not None and out[-1].regime == Regime.BULL


def test_rolling_regime_detects_transition():
    closes = [100.0] * 60 + list(range(100, 160))
    out = rolling_regime(closes)
    regimes = [o.regime for o in out if o is not None]
    assert regimes[0] == Regime.SIDEWAYS
    assert regimes[-1] == Regime.BULL


def test_regime_model_snapshot_none_before_warmup():
    model = RegimeModel()
    bars = _bars(_ramp(10))
    for b in bars:
        model.on_bar(b)
    assert model.snapshot() is None
    for b in _bars(_ramp(60))[10:]:
        model.on_bar(b)
    assert model.snapshot() is not None


def test_regime_event_published_on_change():
    bus = EventBus()
    collected = []
    bus.subscribe("regime", collected.append)
    model = RegimeModel(bus=bus)
    # feed flat then trending up
    flat = [100.0] * 60
    trending = list(range(100, 130))
    for bar in _bars(flat + trending):
        model.on_bar(bar)
    # exactly one SIDEWAYS->BULL transition (first regime assignment + one change)
    regime_events = [e for e in collected if isinstance(e, RegimeEvent)]
    assert len(regime_events) >= 1
    bull_events = [e for e in regime_events if e.regime == "bull"]
    assert len(bull_events) >= 1
    assert all(isinstance(e, RegimeEvent) for e in regime_events)


def test_regime_event_round_trips_through_registry():
    ev = RegimeEvent(
        timestamp=datetime(2024, 1, 1, 9, 30),
        source=EventSource.SYSTEM,
        symbol="AAPL",
        regime="bull",
        slope=0.1,
        realized_vol=0.2,
        adx=30.0,
        sma_short=105.0,
        sma_long=100.0,
        confidence=0.5,
    )
    d = ev.to_dict()
    assert d["event_type"] == "regime"
    restored = event_from_dict(d)
    assert isinstance(restored, RegimeEvent)
    assert restored.regime == "bull"
    assert restored.adx == 30.0


def test_regime_report_empty():
    r = regime_report([])
    assert r["n"] == 0
    assert r["regime"] is None
    assert r["bull"] == 0


def test_regime_report_counts():
    closes = _ramp(101)
    r = regime_report(closes)
    assert r["regime"] == Regime.BULL.value
    assert r["bull"] > 0
    assert r["bear"] == 0


def test_classify_determinism():
    closes = _ramp(101)
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    a = classify(closes, highs, lows)
    b = classify(closes, highs, lows)
    assert a == b


def test_classify_no_nan_in_slope():
    closes = [100.0] * 60
    snap = classify(closes)
    assert not math.isnan(snap.slope)