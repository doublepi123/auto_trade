"""P359: volatility regime detection tests."""

from __future__ import annotations

import math

import pytest

from app.platform.volatility_regime import volatility_regime_report


def test_basic_low_to_high_regime_switch():
    """Construct a low-volatility segment followed by a high-volatility segment.

    The low segment (flat line) produces near-zero realized vol; the high
    segment (large oscillations) produces high vol. After the breakpoint the
    rolling-window vol should jump enough to trigger a regime switch.
    """
    n = 80
    returns: list[float] = []
    # 40 low-vol bars: very small noise
    for i in range(40):
        returns.append(0.0001 * math.sin(i * 0.5))
    # 40 high-vol bars: amplified noise
    for i in range(40):
        returns.append(0.05 * math.sin(i * 3.0))

    result = volatility_regime_report(returns, window=20, n_quantiles=3)
    lbl = result.regime_labels
    switches = result.switch_points

    # Must contain at least "high" in the second half.
    assert "high" in lbl
    assert len(switches) > 0
    assert "low" in lbl


def test_persistence_computed():
    """Persistence (lag-1 autocorrelation of volatility series) must be finite
    and in [-1, 1]."""
    returns = [0.01 * math.sin(i * 0.3) for i in range(60)]
    result = volatility_regime_report(returns, window=10, n_quantiles=3)
    p = result.persistence
    assert math.isfinite(p)
    assert -1.0 <= p <= 1.0


def test_regime_stats_consistent():
    """Each regime in regime_stats has mean_vol, avg_duration, count."""
    returns = [0.005 * math.sin(i * 0.2) for i in range(100)]
    result = volatility_regime_report(returns, window=10, n_quantiles=3)
    stats = result.regime_stats
    assert isinstance(stats, dict)
    for label, info in stats.items():
        assert "mean_vol" in info
        assert "avg_duration" in info
        assert "count" in info
        assert info["count"] >= 1
        assert info["mean_vol"] >= 0
        assert info["avg_duration"] >= 1


def test_to_dict_roundtrips():
    """to_dict() returns a plain dict with all expected keys."""
    returns = [0.01 * math.sin(i * 0.5) for i in range(50)]
    result = volatility_regime_report(returns, window=10, n_quantiles=3)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "regime_labels" in d
    assert "switch_points" in d
    assert "regime_stats" in d
    assert "persistence" in d


def test_empty_returns_raises():
    with pytest.raises(ValueError):
        volatility_regime_report([], window=10, n_quantiles=3)


def test_too_short_returns_raises():
    with pytest.raises(ValueError):
        volatility_regime_report([0.01, 0.02], window=10, n_quantiles=3)


def test_infinite_values_raise():
    with pytest.raises(ValueError):
        volatility_regime_report([0.01, float("inf")], window=10, n_quantiles=3)


def test_regime_labels_length_matches_returns():
    """regime_labels should have same length as input returns."""
    returns = [0.01 * math.sin(i * 0.3) for i in range(50)]
    result = volatility_regime_report(returns, window=15, n_quantiles=3)
    assert len(result.regime_labels) == len(returns)
