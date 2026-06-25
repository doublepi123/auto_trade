"""Tests for P227 Almgren-Chriss optimal execution."""

from __future__ import annotations

import math

import pytest

from app.platform.execution_cost import (
    almgren_chriss,
    almgren_chriss_trajectory,
    efficient_frontier,
    execution_cost,
)


def test_execution_cost_linear():
    # x = [1000, 500, 0], eta=0.1 → v = [500, 500], cost = 0.1*(500^2+500^2)=50000
    ec, var = execution_cost([1000.0, 500.0, 0.0], eta=0.1, sigma=0.3)
    assert abs(ec - 0.1 * (500 ** 2 + 500 ** 2)) < 1e-6
    # var = sigma^2 * (1000^2 + 500^2)
    assert abs(var - 0.09 * (1000 ** 2 + 500 ** 2)) < 1e-6


def test_execution_cost_too_short():
    with pytest.raises(ValueError):
        execution_cost([100.0], eta=0.1, sigma=0.3)


def test_execution_cost_negative_eta():
    with pytest.raises(ValueError):
        execution_cost([100.0, 0.0], eta=-0.1, sigma=0.3)


def test_trajectory_risk_neutral_linear():
    # lambda=0 → straight line
    traj = almgren_chriss_trajectory(1000.0, 5, risk_aversion=0.0)
    assert len(traj) == 6
    assert abs(traj[0] - 1000.0) < 1e-9
    assert abs(traj[-1] - 0.0) < 1e-9
    # linear: step = 200
    for i in range(6):
        assert abs(traj[i] - 1000.0 * (5 - i) / 5) < 1e-9


def test_trajectory_risk_averse_front_loaded():
    # Larger risk aversion → more inventory liquidated early
    traj_neutral = almgren_chriss_trajectory(1000.0, 10, risk_aversion=0.0)
    traj_averse = almgren_chriss_trajectory(1000.0, 10, eta=0.1, sigma=0.5, risk_aversion=10.0)
    # at mid-point, averse should have liquidated more (smaller remaining)
    assert traj_averse[5] <= traj_neutral[5] + 1e-6


def test_trajectory_invalid_inputs():
    with pytest.raises(ValueError):
        almgren_chriss_trajectory(-1.0, 5)
    with pytest.raises(ValueError):
        almgren_chriss_trajectory(100.0, 0)
    with pytest.raises(ValueError):
        almgren_chriss_trajectory(100.0, 5, eta=-0.1)


def test_almgren_chriss_result():
    res = almgren_chriss(1000.0, 5, eta=0.1, sigma=0.3, risk_aversion=0.0)
    assert res.expected_cost > 0
    assert res.risk >= 0
    d = res.to_dict()
    assert d["n_slices"] == 5
    assert d["total_shares"] == 1000.0


def test_efficient_frontier_monotone_cost_with_risk_aversion():
    pts = efficient_frontier(1000.0, 10, eta=0.1, sigma=0.3)
    assert len(pts) >= 2
    # Higher risk aversion → higher expected cost (faster liquidation)
    costs = [p["expected_cost"] for p in pts]
    assert costs[-1] >= costs[0]


def test_efficient_frontier_risk_decreases():
    pts = efficient_frontier(1000.0, 10, eta=0.1, sigma=0.3)
    # Higher risk aversion → lower timing risk
    risks = [p["risk"] for p in pts]
    assert risks[-1] <= risks[0] + 1e-6