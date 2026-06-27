"""P325: capacity frontier tests."""

from __future__ import annotations

import math

import pytest

from app.platform.capacity_frontier import (
    CapacityFrontierResult,
    capacity_frontier_report,
)


def test_capacity_frontier_default_aum_levels():
    """Default aum_levels = [0.01,0.05,0.1,0.2,0.5] × adv."""
    result = capacity_frontier_report(
        base_sharpe=1.0,
        signal_autocorr=0.3,
        adv=1_000_000.0,
        turnover=0.1,
    )
    assert isinstance(result, CapacityFrontierResult)
    d = result.to_dict()
    assert "levels" in d
    assert len(d["levels"]) == 5
    # per-level keys
    for level in d["levels"]:
        assert "aum" in level
        assert "degraded_sharpe" in level
        assert "impact_penalty" in level


def test_capacity_frontier_monotonic_degradation():
    """AUM growth → degraded Sharpe monotonically non-increasing."""
    result = capacity_frontier_report(
        base_sharpe=1.5,
        signal_autocorr=0.2,
        adv=500_000.0,
        turnover=0.2,
    )
    sharpes = [lv["degraded_sharpe"] for lv in result.to_dict()["levels"]]
    for i in range(len(sharpes) - 1):
        assert sharpes[i] >= sharpes[i + 1] - 1e-12, (
            f"Sharpe should be non-increasing, got {sharpes[i]} → {sharpes[i + 1]}"
        )


def test_capacity_frontier_optimal_aum():
    """optimal_aum is the largest AUM where degraded Sharpe >= 0.9 * base_sharpe."""
    result = capacity_frontier_report(
        base_sharpe=2.0,
        signal_autocorr=0.5,
        adv=1_000_000.0,
        turnover=0.05,
    )
    d = result.to_dict()
    opt = d["optimal_aum"]
    assert opt > 0
    # verify: at optimal_aum, Sharpe >= 0.9 * base
    for lv in d["levels"]:
        if lv["aum"] <= opt * 1.0001:
            # the level at or below optimal should have degraded_sharpe >= 0.9 * base
            assert lv["degraded_sharpe"] >= 0.9 * 2.0 - 1e-12


def test_capacity_frontier_custom_aum_levels():
    """Custom aum_levels list overrides default."""
    custom = [10_000.0, 50_000.0, 200_000.0]
    result = capacity_frontier_report(
        base_sharpe=1.0,
        signal_autocorr=0.3,
        adv=1_000_000.0,
        turnover=0.1,
        aum_levels=custom,
    )
    d = result.to_dict()
    assert len(d["levels"]) == 3
    assert [lv["aum"] for lv in d["levels"]] == custom


def test_capacity_frontier_rejects_invalid_inputs():
    """Invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        capacity_frontier_report(
            base_sharpe=float("nan"),
            signal_autocorr=0.3,
            adv=1_000_000.0,
            turnover=0.1,
        )
    with pytest.raises(ValueError):
        capacity_frontier_report(
            base_sharpe=1.0,
            signal_autocorr=0.3,
            adv=-100.0,
            turnover=0.1,
        )
    with pytest.raises(ValueError):
        capacity_frontier_report(
            base_sharpe=1.0,
            signal_autocorr=2.0,
            adv=1_000_000.0,
            turnover=0.1,
        )


def test_capacity_frontier_zero_turnover():
    """Zero turnover → no impact penalty → all sharpes equal to base."""
    result = capacity_frontier_report(
        base_sharpe=1.2,
        signal_autocorr=0.3,
        adv=1_000_000.0,
        turnover=0.0,
    )
    d = result.to_dict()
    for lv in d["levels"]:
        assert lv["degraded_sharpe"] == pytest.approx(1.2, abs=1e-10)
        assert lv["impact_penalty"] == pytest.approx(0.0, abs=1e-10)


def test_capacity_frontier_to_dict_keys():
    """to_dict contains top-level keys."""
    result = capacity_frontier_report(
        base_sharpe=1.0,
        signal_autocorr=0.3,
        adv=1_000_000.0,
        turnover=0.1,
    )
    d = result.to_dict()
    for key in ("base_sharpe", "signal_autocorr", "adv", "turnover", "levels", "optimal_aum"):
        assert key in d, f"missing key {key}"


def test_capacity_frontier_signal_autocorr_affects_penalty():
    high_corr = capacity_frontier_report(base_sharpe=2.0, signal_autocorr=0.8, adv=1000000, turnover=0.2).to_dict()
    low_corr = capacity_frontier_report(base_sharpe=2.0, signal_autocorr=0.0, adv=1000000, turnover=0.2).to_dict()
    assert high_corr["levels"][0]["impact_penalty"] != low_corr["levels"][0]["impact_penalty"], "signal_autocorr must affect penalty"
