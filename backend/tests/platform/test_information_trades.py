"""P360: information trades tests."""

from __future__ import annotations

import math

import pytest

from app.platform.information_trades import information_trades_report


def test_volume_spike_with_direction_produces_informed_proxy():
    """Volume spike + consistent direction should yield non-empty informed_trade_prob
    and information_asymmetry >= 0."""
    n = 60
    volumes: list[float] = []
    direction: list[float] = []
    for i in range(n):
        # baseline
        volumes.append(1000.0 + 100.0 * math.sin(i * 0.3))
        direction.append(math.copysign(1.0, math.sin(i * 0.5)))
    # spike at the end
    for i in range(n, n + 20):
        volumes.append(5000.0 + 200.0 * math.sin(i * 0.8))
        direction.append(math.copysign(1.0, math.sin(i * 1.2)))

    result = information_trades_report(volumes, direction=direction, window=20)
    assert len(result.self_information) > 0
    assert result.information_asymmetry >= 0.0
    assert len(result.informed_trade_prob) > 0


def test_self_information_nonnegative():
    """Self-information should be non-negative (surprise >= 0)."""
    volumes = [1000.0 + 50.0 * math.sin(i * 0.3) for i in range(40)]
    result = information_trades_report(volumes, window=10)
    for si in result.self_information:
        assert si >= -1e-9  # allow tiny float noise


def test_informed_trade_prob_in_01():
    """Informed trade probability should be in [0, 1]."""
    volumes = [500.0 + 200.0 * abs(math.sin(i * 0.5)) for i in range(50)]
    direction = [math.copysign(1.0, math.sin(i * 0.7)) for i in range(50)]
    result = information_trades_report(volumes, direction=direction, window=10)
    for p in result.informed_trade_prob:
        assert 0.0 <= p <= 1.0 + 1e-9


def test_entropy_decomposition_has_keys():
    """entropy_decomposition should contain expected keys."""
    volumes = [1000.0 + 50.0 * math.sin(i * 0.3) for i in range(50)]
    direction = [math.copysign(1.0, math.sin(i * 0.7)) for i in range(50)]
    result = information_trades_report(volumes, direction=direction, window=10)
    d = result.entropy_decomposition
    assert isinstance(d, dict)
    assert "h_direction_given_high_vol" in d
    assert "h_direction_given_low_vol" in d
    assert "unconditional_h_direction" in d


def test_to_dict_roundtrips():
    """to_dict() returns a plain dict with all expected keys."""
    volumes = [1000.0 + 50.0 * math.sin(i * 0.3) for i in range(40)]
    result = information_trades_report(volumes, window=10)
    d = result.to_dict()
    assert "self_information" in d
    assert "information_asymmetry" in d
    assert "informed_trade_prob" in d
    assert "entropy_decomposition" in d


def test_empty_volumes_raises():
    with pytest.raises(ValueError):
        information_trades_report([], window=10)


def test_infinite_volume_raises():
    with pytest.raises(ValueError):
        information_trades_report([1000.0, float("inf")], window=10)


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        information_trades_report([1000.0, 1100.0, 1200.0], direction=[1.0, -1.0], window=5)


def test_no_direction_works():
    """When direction is not provided, still produce result."""
    volumes = [1000.0 + 50.0 * math.sin(i * 0.3) for i in range(40)]
    result = information_trades_report(volumes, window=10)
    assert result.self_information is not None
    assert result.information_asymmetry == 0.0  # no direction info
    assert result.informed_trade_prob is not None
