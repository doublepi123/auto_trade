"""Tests for the P200 macro stress scenario library."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.platform.stress_scenarios import (
    ScenarioLibrary,
    StressInput,
    equity_crash_scenario,
    get_default_library,
)


def _inp() -> StressInput:
    return StressInput(
        positions={"A.US": (100, Decimal("100")), "B.US": (50, Decimal("200"))},
        betas={"A.US": Decimal("1.0"), "B.US": Decimal("1.5")},
        base_nav=Decimal("30000"),  # 100*100 + 50*200 = 20000 positions + 10000 cash
    )


def test_equity_crash_scales_by_beta():
    result = equity_crash_scenario(Decimal("-20"))(_inp())
    # A.US beta 1.0 -> -20% -> 80; B.US beta 1.5 -> -30% -> 140
    assert result.shocked_prices["A.US"] == Decimal("80")
    assert result.shocked_prices["B.US"] == Decimal("140")
    # positions: A 100*(80-100)=-2000; B 50*(140-200)=-3000; total -5000
    assert result.pnl == Decimal("-5000")
    assert result.per_symbol_pnl["A.US"] == Decimal("-2000")
    assert result.per_symbol_pnl["B.US"] == Decimal("-3000")


def test_equity_crash_preserves_cash():
    inp = StressInput(
        positions={"A.US": (100, Decimal("100"))},
        base_nav=Decimal("20000"),  # 10000 position + 10000 cash
    )
    result = equity_crash_scenario(Decimal("-10"))(inp)
    # position drops 10% -> 9000; cash 10000 untouched -> 19000
    assert result.stressed_nav == Decimal("19000")
    assert result.pnl == Decimal("-1000")


def test_pnl_pct_computed_against_baseline():
    result = equity_crash_scenario(Decimal("-20"))(_inp())
    # -5000 / 30000 * 100 = -16.666...
    assert abs(result.pnl_pct - (Decimal("-5000") / Decimal("30000") * Decimal("100"))) < Decimal("1e-9")


def test_volatility_spike_drags_prices():
    lib = get_default_library()
    result = lib.run("volatility_spike", _inp())
    # drag = 50/1000 = 0.05 -> 5% down
    assert result.shocked_prices["A.US"] == Decimal("95")
    assert result.pnl < 0


def test_correlation_breakdown_hurts_low_beta_more():
    lib = get_default_library()
    inp = StressInput(
        positions={"DEF.US": (100, Decimal("100")), "HG.US": (100, Decimal("100"))},
        betas={"DEF.US": Decimal("0.5"), "HG.US": Decimal("2.0")},
        base_nav=Decimal("20000"),
    )
    result = lib.run("correlation_breakdown", inp)
    # DEF (beta 0.5): intensity 0.5, move -15*0.5 = -7.5% -> 92.5
    # HG  (beta 2.0): intensity 0.5 (capped), move -15*0.5 = -7.5% -> 92.5
    assert result.shocked_prices["DEF.US"] == Decimal("92.5")
    assert result.shocked_prices["HG.US"] == Decimal("92.5")


def test_liquidity_discount_uniform_haircut():
    lib = get_default_library()
    result = lib.run("liquidity_discount", _inp())
    # 200 bps = 2% -> *0.98
    assert result.shocked_prices["A.US"] == Decimal("98")
    assert result.shocked_prices["B.US"] == Decimal("196")


def test_library_run_all_returns_every_scenario():
    lib = get_default_library()
    results = lib.run_all(_inp())
    assert set(results.keys()) == {
        "equity_crash", "volatility_spike", "correlation_breakdown", "liquidity_discount"
    }
    for r in results.values():
        assert r.baseline_nav == Decimal("30000")


def test_library_summary_serializes():
    lib = get_default_library()
    summary = lib.summary(_inp())
    assert len(summary) == 4
    for item in summary:
        assert "scenario" in item
        assert "stressed_nav" in item
        assert isinstance(item["pnl"], float)


def test_library_register_duplicate_raises():
    lib = ScenarioLibrary()
    lib.register("x", equity_crash_scenario())
    with pytest.raises(ValueError):
        lib.register("x", equity_crash_scenario())


def test_library_unknown_scenario_raises():
    lib = get_default_library()
    with pytest.raises(KeyError):
        lib.run("nonexistent", _inp())


def test_default_beta_is_one():
    inp = StressInput(positions={"X.US": (10, Decimal("100"))})
    assert inp.beta("X.US") == Decimal("1")
