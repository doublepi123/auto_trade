"""Tests for P314 tail hedge cost diagnostics."""

from __future__ import annotations

import pytest

from app.platform.tail_hedge_cost import tail_hedge_cost_report


def test_fat_tail_positive_hedge_cost():
    # Fat-tailed returns => hedge_cost > 0
    # Need enough losses so that 95% VaR falls on a negative return
    rets = [0.001] * 40 + [-0.03, -0.04, -0.05, -0.06, -0.07, -0.08, -0.09, -0.10, -0.11, -0.12, 0.03, 0.04] + [0.001] * 48
    result = tail_hedge_cost_report(rets)
    body = result.to_dict()
    assert body["hedge_cost_annual"] > 0
    assert body["tail_index"] > 0
    assert body["var"] > 0
    assert body["cvar"] > 0
    assert body["cvar"] >= body["var"]


def test_normal_returns_low_hedge_cost():
    # Near-normal returns => hedge_cost should be modest
    rets = [0.005, -0.003, 0.002, -0.001, 0.004, -0.002, 0.001, -0.004, 0.003, -0.003,
            0.002, -0.002, 0.001, -0.001, 0.003, -0.002, 0.004, -0.003, 0.002, -0.001]
    result = tail_hedge_cost_report(rets)
    body = result.to_dict()
    assert body["hedge_cost_annual"] >= 0


def test_confidence_parameter():
    rets = [0.001] * 80 + [-0.10, -0.08, -0.05, -0.07] + [0.001] * 16
    result99 = tail_hedge_cost_report(rets, confidence=0.99)
    result95 = tail_hedge_cost_report(rets, confidence=0.95)
    # 99% CVaR >= 95% CVaR
    assert result99.cvar >= result95.cvar


def test_empty_returns():
    with pytest.raises(ValueError):
        tail_hedge_cost_report([])


def test_constant_returns():
    rets = [0.01] * 20
    result = tail_hedge_cost_report(rets)
    body = result.to_dict()
    # No variation => hedge_cost should be 0 or very small
    assert body["hedge_cost_annual"] == 0.0


def test_rejects_non_finite():
    with pytest.raises(ValueError):
        tail_hedge_cost_report([0.01, float("nan")])
