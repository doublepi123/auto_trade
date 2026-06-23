"""Tests for P202 multi-period Brinson linking."""

from __future__ import annotations

from app.platform.brinson_multiperiod import (
    brinson_multi_period,
    link_arithmetic,
    link_geometric,
)


def _period(pw, pr, bw, br):  # type: ignore[no-untyped-def]
    return {
        "portfolio_weights": pw,
        "portfolio_returns": pr,
        "benchmark_weights": bw,
        "benchmark_returns": br,
    }


def test_multiperiod_single_period_matches_single_period_brinson():
    period = _period({"tech": 0.6, "energy": 0.4}, {"tech": 0.05, "energy": 0.02},
                     {"tech": 0.5, "energy": 0.5}, {"tech": 0.03, "energy": 0.01})
    result = brinson_multi_period([period])

    assert result["periods"] == 1
    # portfolio return = 0.6*0.05 + 0.4*0.02 = 0.038
    assert abs(result["portfolio_return"] - 0.038) < 1e-9
    assert abs(result["benchmark_return"] - 0.02) < 1e-9


def test_multiperiod_compounds_portfolio_and_benchmark():
    p1 = _period({"a": 1.0}, {"a": 0.10}, {"a": 1.0}, {"a": 0.05})
    p2 = _period({"a": 1.0}, {"a": 0.10}, {"a": 1.0}, {"a": 0.05})
    result = brinson_multi_period([p1, p2])

    # portfolio: 1.1*1.1 - 1 = 0.21
    assert abs(result["portfolio_return"] - 0.21) < 1e-9
    # benchmark: 1.05*1.05 - 1 = 0.1025
    assert abs(result["benchmark_return"] - 0.1025) < 1e-9
    # active = 0.21 - 0.1025
    assert abs(result["active_return"] - (0.21 - 0.1025)) < 1e-9


def test_multiperiod_empty_returns_empty():
    result = brinson_multi_period([])
    assert result["periods"] == 0
    assert result["per_period"] == []


def test_arithmetic_link_sums_per_period_effects():
    p1 = _period({"a": 0.6, "b": 0.4}, {"a": 0.05, "b": 0.02},
                 {"a": 0.5, "b": 0.5}, {"a": 0.03, "b": 0.01})
    p2 = _period({"a": 0.7, "b": 0.3}, {"a": 0.04, "b": 0.03},
                 {"a": 0.5, "b": 0.5}, {"a": 0.02, "b": 0.02})
    result = brinson_multi_period([p1, p2])

    alloc = result["linking"]["arithmetic"]["allocation"]
    per_period_alloc = sum(p["allocation"] for p in result["per_period"])
    assert abs(alloc - per_period_alloc) < 1e-9


def test_geometric_link_equals_arithmetic_when_benchmark_return_zero():
    # When benchmark returns are 0, Frongello prior-growth scaling is 1, so
    # geometric linking == arithmetic linking (both carry the same compounding
    # residual since the scaling factor never deviates from 1).
    p1 = _period({"a": 1.0}, {"a": 0.05}, {"a": 1.0}, {"a": 0.0})
    p2 = _period({"a": 1.0}, {"a": 0.05}, {"a": 1.0}, {"a": 0.0})
    result = brinson_multi_period([p1, p2])

    geo = result["linking"]["geometric"]
    arith = result["linking"]["arithmetic"]
    assert abs(geo["explained"] - arith["explained"]) < 1e-9
    assert abs(geo["residual"] - arith["residual"]) < 1e-9


def test_residual_reported_in_both_modes():
    p1 = _period({"a": 0.6, "b": 0.4}, {"a": 0.10, "b": 0.02},
                 {"a": 0.5, "b": 0.5}, {"a": 0.03, "b": 0.01})
    result = brinson_multi_period([p1, p1])
    for mode in ("arithmetic", "geometric"):
        link = result["linking"][mode]
        # residual = active - explained, should be a defined (finite) number.
        assert "residual" in link
        assert isinstance(link["residual"], float)


def test_link_arithmetic_function_directly():
    effects = [
        {"allocation": 0.01, "selection": 0.02, "interaction": 0.0},
        {"allocation": 0.03, "selection": -0.01, "interaction": 0.005},
    ]
    result = link_arithmetic(effects)
    assert abs(result["allocation"] - 0.04) < 1e-9
    assert abs(result["selection"] - 0.01) < 1e-9
    assert abs(result["interaction"] - 0.005) < 1e-9
    assert abs(result["explained"] - 0.055) < 1e-9


def test_link_geometric_scales_by_prior_growth():
    effects = [
        {"allocation": 0.10, "selection": 0.0, "interaction": 0.0},
        {"allocation": 0.10, "selection": 0.0, "interaction": 0.0},
    ]
    # benchmark grew 10% in period 0 -> period 1 effect scaled by 1.10.
    bench = [0.10, 0.0]
    result = link_geometric(effects, bench)
    assert abs(result["allocation"] - (0.10 + 0.10 * 1.10)) < 1e-9


def test_geometric_residual_smaller_than_arithmetic_for_large_moves():
    # Large moves make arithmetic linking leave a bigger residual; geometric
    # (Frongello) should reconcile better.
    p1 = _period({"a": 0.8, "b": 0.2}, {"a": 0.20, "b": 0.01},
                 {"a": 0.5, "b": 0.5}, {"a": 0.05, "b": 0.02})
    result = brinson_multi_period([p1, p1, p1])
    arith_resid = abs(result["linking"]["arithmetic"]["residual"])
    geom_resid = abs(result["linking"]["geometric"]["residual"])
    assert geom_resid <= arith_resid + 1e-9
