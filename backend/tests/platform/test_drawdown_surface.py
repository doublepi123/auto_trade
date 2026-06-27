"""Tests for P313 drawdown surface diagnostics."""

from __future__ import annotations

import pytest

from app.platform.drawdown_surface import drawdown_surface_report


def test_empty_equity_curve():
    result = drawdown_surface_report([])
    body = result.to_dict()
    assert body["num_episodes"] == 0
    assert body["joint_matrix"] == []
    assert body["episodes"] == []


def test_monotonic_up_no_drawdowns():
    # Strictly increasing => no drawdown
    equity = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    result = drawdown_surface_report(equity)
    body = result.to_dict()
    assert body["num_episodes"] == 0
    assert body["joint_matrix"] == []
    assert body["episodes"] == []


def test_single_drawdown_extracted():
    # Peak at 100, drop to 80, recover to 95 (drawdown still active at end is OK)
    equity = [100.0, 95.0, 90.0, 85.0, 80.0, 85.0, 95.0]
    result = drawdown_surface_report(equity)
    body = result.to_dict()
    assert body["num_episodes"] >= 1
    assert len(body["episodes"]) >= 1
    # First episode: peak at 100, trough at 80
    ep = body["episodes"][0]
    assert ep["max_depth"] > 0
    assert ep["duration"] >= 4


def test_multiple_drawdowns():
    # Two valleys: 100->90->100 then 100->85->100
    equity = [100.0, 95.0, 90.0, 95.0, 100.0, 97.0, 85.0, 90.0, 95.0, 100.0]
    result = drawdown_surface_report(equity)
    body = result.to_dict()
    assert body["num_episodes"] >= 2


def test_joint_matrix_non_empty():
    equity = [100.0, 90.0, 80.0, 90.0, 100.0, 95.0, 85.0, 95.0, 105.0]
    result = drawdown_surface_report(equity)
    body = result.to_dict()
    # With episodes present, joint_matrix should have rows
    assert isinstance(body["joint_matrix"], list)
    assert len(body["joint_matrix"]) > 0


def test_depth_bins_and_duration_bins():
    equity = [100.0, 90.0, 80.0, 90.0, 100.0, 95.0, 85.0, 95.0, 105.0]
    result = drawdown_surface_report(equity, depth_bins=3, duration_bins=4)
    body = result.to_dict()
    assert body["joint_matrix"]


def test_rejects_non_list():
    with pytest.raises(ValueError):
        drawdown_surface_report("not a list")  # type: ignore[arg-type]


def test_rejects_non_finite():
    with pytest.raises(ValueError):
        drawdown_surface_report([100.0, float("nan"), 105.0])
