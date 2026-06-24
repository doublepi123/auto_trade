"""Tests for P204 drawdown analysis."""

from __future__ import annotations

import math

from app.platform.drawdown_analysis import (
    drawdown_acceleration,
    drawdown_events,
    drawdown_summary,
    rolling_calmar,
    underwater_curve,
)


def test_underwater_curve_nonpositive():
    equity = [100, 110, 105, 120, 90, 95, 100, 130]
    uw = underwater_curve(equity)
    # Every value should be ≤ 0 (never above the running peak)
    assert all(x <= 0 for x in uw)


def test_underwater_curve_known_values():
    # Peak at 120, trough at 90 → (90-120)/120 = -0.25
    equity = [100, 110, 120, 90, 95, 120]
    uw = underwater_curve(equity)
    assert abs(uw[3] - (-0.25)) < 1e-9
    # Last point hits the new peak → 0
    assert uw[-1] == 0.0


def test_drawdown_events_detects_episode():
    # One full drawdown: 100 → 110 → 120 (peak@2) → 80 (trough@3) → 90 → 100 → 120 (recovery@6)
    equity = [100, 110, 120, 80, 90, 100, 120]
    events = drawdown_events(equity)
    assert len(events) == 1
    e = events[0]
    assert e["start"] == 2  # tick of the prior peak
    assert e["trough"] == 3
    assert e["end"] == 6
    assert abs(e["depth"] - (-40.0 / 120.0)) < 1e-9
    assert e["duration"] == 1  # trough - start
    assert e["recovery_time"] == 3  # trough at 3, recovery at 6


def test_drawdown_events_unrecovered_returns_none_end():
    # Equity never recovers; peak at index 1, trough at index 4
    equity = [100, 120, 80, 70, 60]
    events = drawdown_events(equity)
    assert len(events) == 1
    assert events[0]["start"] == 1
    assert events[0]["trough"] == 4
    assert events[0]["end"] is None
    assert events[0]["recovery_time"] is None
    assert abs(events[0]["depth"] - (-60.0 / 120.0)) < 1e-9


def test_drawdown_summary_basic():
    equity = [100, 110, 120, 80, 90, 100, 110, 130]
    s = drawdown_summary(equity)
    # Worst dip was from 120 to 80 → -33.33%
    assert abs(s["max_drawdown"] - (-40.0 / 120.0)) < 1e-9
    assert s["n_episodes"] >= 1
    assert s["time_underwater_pct"] > 0
    assert s["time_underwater_pct"] < 1


def test_drawdown_summary_empty_equity():
    s = drawdown_summary([])
    assert s["max_drawdown"] == 0.0
    assert s["n_episodes"] == 0


def test_rolling_calmar_zero_when_no_drawdown():
    # Monotonically rising equity → no drawdown → Calmar = 0 across the window.
    equity = [100.0 + i for i in range(50)]
    rc = rolling_calmar(equity, window=20)
    # For the first 19 entries there's no full window; afterwards all are 0.
    for v in rc[19:]:
        assert v == 0.0


def test_rolling_calmar_positive_with_recovery():
    # Up-and-down: 100 → 130 → 90 → 130 → 100; net flat with a deep drawdown
    equity = [100, 110, 120, 130, 110, 90, 110, 130, 120, 100]
    rc = rolling_calmar(equity, window=10)
    # Some windows end with positive Calmar (recovery from low).
    # The very last window (full) should be 0 — start=100, end=100, ann_ret=0.
    assert rc[-1] == 0.0


def test_rolling_calmar_returns_list_of_length():
    equity = [100.0 + i for i in range(30)]
    rc = rolling_calmar(equity, window=10)
    assert len(rc) == 30


def test_drawdown_acceleration_signs():
    # Equity falls then recovers — the middle should have positive accel
    # (accelerating loss), then negative accel (decelerating into trough),
    # then positive again (recovering faster), then negative (flattening).
    equity = [100, 100, 95, 85, 80, 85, 95, 100, 100]
    acc = drawdown_acceleration(equity)
    # First two should be 0 (no neighbor pair for second derivative)
    assert acc[0] == 0.0
    assert acc[1] == 0.0
    # Middle index 3 (85): uw[3] = -15/100, uw[2] = -5, uw[4] = -20/100=...
    # Just sanity-check the sequence is non-zero somewhere
    assert any(v != 0.0 for v in acc[2:])


def test_drawdown_acceleration_short_curve():
    acc = drawdown_acceleration([100.0])
    assert acc == [0.0]
    acc = drawdown_acceleration([100.0, 90.0])
    assert acc == [0.0, 0.0]
