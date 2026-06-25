"""Tests for P247 statistical-arbitrage signals."""

from __future__ import annotations

import math

import pytest

from app.platform.stat_arb_signals import (
    distance_method_spread,
    stat_arb_signals,
    zscore_signals,
)


def test_distance_spread_normalisation():
    y = [100.0, 102.0, 104.0]
    x = [50.0, 51.0, 52.0]
    spread, mean, std = distance_method_spread(y, x)
    # Both normalised to start at 1.0; spread[0] == 0.
    assert abs(spread[0]) < 1e-12
    assert abs(spread[1] - (102.0 / 100.0 - 51.0 / 50.0)) < 1e-12


def test_distance_spread_perfectly_correlated_zero_variance():
    y = [100.0, 110.0, 120.0]
    x = [50.0, 55.0, 60.0]
    spread, mean, std = distance_method_spread(y, x)
    # x perfectly tracks y in proportional terms -> spread all zero.
    assert all(abs(s) < 1e-12 for s in spread)


def test_zscore_signals_short_when_wide():
    spread = [0.0, 0.0, 0.0, 5.0]  # huge positive spread
    signals = zscore_signals(spread, mean=0.0, std=1.0, entry=2.0, exit=0.5)
    assert signals[-1] == "SHORT_SPREAD"


def test_zscore_signals_long_when_tight():
    spread = [0.0, 0.0, 0.0, -5.0]
    signals = zscore_signals(spread, mean=0.0, std=1.0, entry=2.0, exit=0.5)
    assert signals[-1] == "LONG_SPREAD"


def test_zscore_signals_flat_near_mean():
    spread = [0.0, 0.0, 0.0]
    signals = zscore_signals(spread, mean=0.0, std=1.0, entry=2.0, exit=0.5)
    assert all(s == "FLAT" for s in signals)


def test_zscore_signals_hysteresis_holds_between_thresholds():
    # z = 1.0 (between exit 0.5 and entry 2.0): should hold previous state.
    spread = [0.0, 1.0]
    signals = zscore_signals(spread, mean=0.0, std=1.0, entry=2.0, exit=0.5)
    # First bar FLAT, second bar z=1 (no trigger) -> holds FLAT.
    assert signals[0] == "FLAT"
    assert signals[1] == "FLAT"


def test_zscore_signals_invalid_thresholds():
    with pytest.raises(ValueError):
        zscore_signals([0.0], 0.0, 1.0, entry=0.3, exit=0.5)
    with pytest.raises(ValueError):
        zscore_signals([0.0], 0.0, 0.0)


def test_stat_arb_signals_full_report():
    # Construct a wide, mean-reverting spread via two diverging series.
    base = [100.0 + i for i in range(60)]
    y = [b + 10.0 * math.sin(i / 5.0) for i, b in enumerate(base)]
    x = [b for b in base]
    res = stat_arb_signals(y, x, entry=1.0, exit=0.3)
    assert len(res.spread) == 60
    assert len(res.signals) == 60
    assert len(res.zscore) == 60
    assert res.n_bars == 60
    # With a 10-unit sinusoidal deviation the z-score must breach entry at some bar.
    assert any(s in ("LONG_SPREAD", "SHORT_SPREAD") for s in res.signals)


def test_stat_arb_signals_to_dict():
    y = [100.0, 102.0, 101.0, 103.0]
    x = [100.0, 101.0, 100.0, 102.0]
    res = stat_arb_signals(y, x)
    d = res.to_dict()
    assert "spread" in d and "signals" in d and "half_life" in d
    assert "half_life_finite" in d


def test_stat_arb_signals_length_mismatch_raises():
    with pytest.raises(ValueError):
        stat_arb_signals([1.0, 2.0], [1.0])


def test_stat_arb_signals_empty_raises():
    with pytest.raises(ValueError):
        stat_arb_signals([], [])


def test_stat_arb_signals_zero_start_raises():
    with pytest.raises(ValueError):
        stat_arb_signals([0.0, 1.0], [1.0, 2.0])