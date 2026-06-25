"""Tests for P221 scenario stress report aggregator."""

from __future__ import annotations

from decimal import Decimal

from app.platform.stress_report import StressReportBuilder, build_stress_report


def _positions():
    return {"A.US": (100, Decimal("100")), "B.US": (200, Decimal("50"))}


def test_build_stress_report_has_all_scenarios():
    report = build_stress_report(_positions())
    names = [s["scenario"] for s in report["scenarios"]]
    assert "equity_crash" in names
    assert "volatility_spike" in names
    assert "correlation_breakdown" in names
    assert "liquidity_discount" in names


def test_worst_scenario_is_equity_crash():
    report = build_stress_report(_positions())
    # equity_crash (-20% beta-scaled) should be the worst for a long book
    assert report["worst_scenario"] == "equity_crash"
    assert report["worst_pnl"] < 0


def test_worst_pnl_pct_negative():
    report = build_stress_report(_positions())
    assert report["worst_pnl_pct"] < 0


def test_capital_adequacy_ratio():
    # small buffer vs large loss → ratio < 1 (inadequate)
    report = build_stress_report(_positions(), capital_buffer=Decimal("100"))
    assert report["capital_adequacy_ratio"] < 1.0
    # large buffer → adequate
    report2 = build_stress_report(_positions(), capital_buffer=Decimal("100000"))
    assert report2["capital_adequacy_ratio"] > 1.0


def test_scenario_var_present():
    report = build_stress_report(_positions())
    assert "scenario_var" in report
    # VaR report has confidence_levels keys
    assert isinstance(report["scenario_var"], dict)


def test_baseline_nav_from_positions():
    report = build_stress_report(_positions())
    # positions value = 100*100 + 200*50 = 20000
    assert abs(report["baseline_nav"] - 20000.0) < 1e-6


def test_per_symbol_pnl_present():
    report = build_stress_report(_positions())
    for s in report["scenarios"]:
        assert "per_symbol_pnl" in s
        assert "A.US" in s["per_symbol_pnl"]


def test_builder_class():
    builder = StressReportBuilder()
    report = builder.build(_positions())
    assert report["worst_scenario"] is not None


def test_betas_affect_equity_crash():
    # high-beta positions lose more in equity_crash
    high_beta = {"A.US": (100, Decimal("100"))}
    report_low = build_stress_report(high_beta, betas={"A.US": Decimal("0.5")})
    report_high = build_stress_report(high_beta, betas={"A.US": Decimal("2.0")})
    # high beta → larger loss
    assert report_high["worst_pnl"] < report_low["worst_pnl"]