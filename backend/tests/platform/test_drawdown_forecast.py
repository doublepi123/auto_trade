"""Tests for P236 drawdown forecast & recovery-time distribution."""

from __future__ import annotations

import math

import pytest

from app.platform.drawdown_forecast import (
    DrawdownForecastResult,
    DrawdownPeriod,
    ExpectedDrawdownResult,
    RecoveryStats,
    drawdown_forecast_report,
    expected_drawdown,
    recovery_time_distribution,
    underwater_periods,
)


# ---------------------------------------------------------------------------
# underwater_periods
# ---------------------------------------------------------------------------


def test_underwater_periods_monotonic_no_drawdown():
    # strictly increasing equity → no underwater periods
    eq = [100.0, 101.0, 102.0, 103.0]
    assert underwater_periods(eq) == []


def test_underwater_periods_single_recovered_drawdown():
    # peak at 100, trough at 90, recovers at 100
    eq = [100.0, 90.0, 95.0, 100.0]
    periods = underwater_periods(eq)
    assert len(periods) == 1
    p = periods[0]
    assert p.peak_idx == 0
    assert p.trough_idx == 1
    assert p.recovery_idx == 3
    assert abs(p.depth - 0.10) < 1e-9
    assert p.duration_bars == 1
    assert p.recovery_bars == 2


def test_underwater_periods_still_underwater_at_end():
    # peak 100, drops to 90, never recovers
    eq = [100.0, 95.0, 90.0]
    periods = underwater_periods(eq)
    assert len(periods) == 1
    p = periods[0]
    assert p.peak_idx == 0
    assert p.trough_idx == 2
    assert p.recovery_idx is None
    assert p.recovery_bars is None
    assert abs(p.depth - 0.10) < 1e-9


def test_underwater_periods_multiple_runs():
    # two distinct drawdowns with a recovery in between
    eq = [100.0, 90.0, 100.0, 110.0, 99.0, 110.0]
    periods = underwater_periods(eq)
    assert len(periods) == 2
    # first: peak 0 (100), trough 1 (90), recovery 2 (100)
    assert periods[0].peak_idx == 0
    assert periods[0].trough_idx == 1
    assert periods[0].recovery_idx == 2
    assert abs(periods[0].depth - 0.10) < 1e-9
    # second: peak 3 (110), trough 4 (99), recovery 5 (110)
    assert periods[1].peak_idx == 3
    assert periods[1].trough_idx == 4
    assert periods[1].recovery_idx == 5
    assert abs(periods[1].depth - 0.10) < 1e-9


def test_underwater_periods_short_input():
    assert underwater_periods([]) == []
    assert underwater_periods([5.0]) == []


def test_underwater_periods_to_dict():
    eq = [100.0, 90.0, 100.0]
    d = underwater_periods(eq)[0].to_dict()
    assert d["peak_idx"] == 0
    assert d["trough_idx"] == 1
    assert d["recovery_idx"] == 2
    assert d["recovery_bars"] == 1
    assert abs(d["depth"] - 0.10) < 1e-9


# ---------------------------------------------------------------------------
# recovery_time_distribution
# ---------------------------------------------------------------------------


def test_recovery_distribution_stats():
    periods = [
        DrawdownPeriod(0, 1, 3, 0.1, 1, 2),
        DrawdownPeriod(0, 1, 6, 0.1, 1, 5),
        DrawdownPeriod(0, 1, 11, 0.1, 1, 10),
    ]
    rs = recovery_time_distribution(periods)
    assert rs.n_completed == 3
    assert abs(rs.mean - (2 + 5 + 10) / 3) < 1e-9
    assert rs.median == 5.0
    assert rs.max == 10.0
    # P(R > 1) = 3/3 (all > 1), P(R > 5) = 1/3 (only 10 > 5), P(R > 10) = 0/3
    assert abs(rs.survival["1"] - 1.0) < 1e-9
    assert abs(rs.survival["5"] - 1.0 / 3.0) < 1e-9
    assert abs(rs.survival["10"] - 0.0) < 1e-9


def test_recovery_distribution_no_completed_raises():
    periods = [DrawdownPeriod(0, 1, None, 0.1, 1, None)]
    with pytest.raises(ValueError):
        recovery_time_distribution(periods)


def test_recovery_distribution_empty_raises():
    with pytest.raises(ValueError):
        recovery_time_distribution([])


def test_recovery_distribution_to_dict():
    periods = [DrawdownPeriod(0, 1, 3, 0.1, 1, 2)]
    d = recovery_time_distribution(periods).to_dict()
    assert d["n_completed"] == 1
    assert d["mean"] == 2.0
    assert "survival" in d


# ---------------------------------------------------------------------------
# expected_drawdown
# ---------------------------------------------------------------------------


def test_expected_drawdown_closed_form():
    # sigma = 1.0 (standard normal returns), h = 252
    # Build a series with zero mean and unit sample std.
    # Use a deterministic alternating spread scaled to give ddof=1 std ~1.
    # Simpler: directly check the formula via a series with known variance.
    # returns: -1, +1, -1, +1, ... → mean 0, sample std 1 (ddof=1)
    returns = [(-1.0) ** (i % 2) for i in range(100)]
    # sample variance with ddof=1: each value is ±1, mean 0, var = sum(x^2)/(n-1) = n/(n-1)
    n = len(returns)
    sigma = math.sqrt(n / (n - 1))
    h = 252
    conf = 0.95
    res = expected_drawdown(returns, horizon_bars=h, confidence=conf)
    assert res.horizon_bars == h
    assert res.confidence == conf
    assert abs(res.sigma - sigma) < 1e-9
    assert abs(res.expected_max - sigma * math.sqrt(2.0 * h / math.pi)) < 1e-9
    # Acklam z(0.95) ≈ 1.6449
    assert abs(res.percentile - sigma * math.sqrt(h) * 1.6448536) < 1.05


def test_expected_drawdown_scales_with_horizon():
    returns = [(-1.0) ** (i % 2) for i in range(100)]
    short = expected_drawdown(returns, horizon_bars=10, confidence=0.95)
    long_ = expected_drawdown(returns, horizon_bars=1000, confidence=0.95)
    assert long_.expected_max > short.expected_max
    assert long_.percentile > short.percentile
    # expected_max scales like sqrt(h): ratio ≈ sqrt(1000/10) = 10
    assert abs(long_.expected_max / short.expected_max - math.sqrt(100.0)) < 1e-6


def test_expected_drawdown_percentile_increases_with_confidence():
    returns = [(-1.0) ** (i % 2) for i in range(100)]
    low = expected_drawdown(returns, horizon_bars=252, confidence=0.90)
    high = expected_drawdown(returns, horizon_bars=252, confidence=0.99)
    assert high.percentile > low.percentile


def test_expected_drawdown_invalid_inputs():
    with pytest.raises(ValueError):
        expected_drawdown([], horizon_bars=10)
    with pytest.raises(ValueError):
        expected_drawdown([0.01, 0.02], horizon_bars=0)
    with pytest.raises(ValueError):
        expected_drawdown([0.01, 0.02], horizon_bars=10, confidence=1.5)
    with pytest.raises(ValueError):
        expected_drawdown([0.01, 0.02], horizon_bars=10, confidence=0.0)


def test_expected_drawdown_zero_volatility():
    # constant returns → sigma 0 → both forecasts 0
    res = expected_drawdown([0.01, 0.01, 0.01], horizon_bars=100, confidence=0.95)
    assert res.sigma == 0.0
    assert res.expected_max == 0.0
    assert res.percentile == 0.0


def test_expected_drawdown_to_dict():
    res = expected_drawdown([0.01, -0.02, 0.005, -0.01], horizon_bars=63, confidence=0.99)
    d = res.to_dict()
    assert d["horizon_bars"] == 63
    assert d["confidence"] == 0.99
    assert "sigma" in d and "expected_max" in d and "percentile" in d


# ---------------------------------------------------------------------------
# drawdown_forecast_report
# ---------------------------------------------------------------------------


def test_forecast_report_equity_mode():
    # equity curve with a known drawdown + recovery
    eq = [100.0, 90.0, 95.0, 100.0, 105.0, 95.0, 105.0]
    rep = drawdown_forecast_report(eq, horizon_bars=63, confidence=0.95)
    assert isinstance(rep, DrawdownForecastResult)
    assert rep.n_periods == 2
    assert rep.n_open == 0
    assert abs(rep.max_depth - 0.10) < 1e-9
    assert rep.recovery is not None
    assert rep.recovery.n_completed == 2
    assert isinstance(rep.forecast, ExpectedDrawdownResult)
    assert rep.forecast.horizon_bars == 63


def test_forecast_report_returns_mode():
    returns = [0.01, -0.02, 0.005, -0.01, 0.03]
    rep = drawdown_forecast_report(returns, horizon_bars=21, confidence=0.99, input_mode="returns")
    assert rep.n_periods == 0
    assert rep.n_open == 0
    assert rep.max_depth == 0.0
    assert rep.recovery is None
    assert rep.forecast.horizon_bars == 21
    assert rep.forecast.confidence == 0.99


def test_forecast_report_equity_open_drawdown():
    # never recovers → recovery is None (no completed), n_open = 1
    eq = [100.0, 90.0, 85.0]
    rep = drawdown_forecast_report(eq, horizon_bars=10, confidence=0.95)
    assert rep.n_periods == 1
    assert rep.n_open == 1
    assert rep.recovery is None
    assert abs(rep.max_depth - 0.15) < 1e-9


def test_forecast_report_short_equity_raises():
    with pytest.raises(ValueError):
        drawdown_forecast_report([100.0], horizon_bars=10)


def test_forecast_report_invalid_mode():
    with pytest.raises(ValueError):
        drawdown_forecast_report([100.0, 90.0], horizon_bars=10, input_mode="bogus")


def test_forecast_report_to_dict():
    eq = [100.0, 90.0, 100.0]
    rep = drawdown_forecast_report(eq, horizon_bars=12, confidence=0.95)
    d = rep.to_dict()
    assert d["n_periods"] == 1
    assert d["n_open"] == 0
    assert d["recovery"] is not None
    assert d["forecast"]["horizon_bars"] == 12


def test_forecast_report_returns_mode_empty_raises():
    with pytest.raises(ValueError):
        drawdown_forecast_report([], horizon_bars=10, input_mode="returns")


# ---------------------------------------------------------------------------
# reflection-principle known-answer sanity
# ---------------------------------------------------------------------------


def test_reflection_principle_known_answer():
    # E[M_h] = sigma * sqrt(2h/pi). For sigma=1, h=1: E[M_1] = sqrt(2/pi) ≈ 0.7979
    returns = [(-1.0) ** (i % 2) for i in range(100)]
    n = len(returns)
    sigma = math.sqrt(n / (n - 1))  # ~1.0
    res = expected_drawdown(returns, horizon_bars=1, confidence=0.95)
    assert abs(res.expected_max - sigma * math.sqrt(2.0 / math.pi)) < 1e-6
    assert abs(res.expected_max - 0.7978845) < 0.01