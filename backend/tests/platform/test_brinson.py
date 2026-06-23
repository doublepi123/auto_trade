from __future__ import annotations

from app.platform.brinson import brinson_attribution


def test_single_sector_no_active_return_when_matching():
    result = brinson_attribution(
        portfolio_weights={"A": 1.0},
        portfolio_returns={"A": 0.10},
        benchmark_weights={"A": 1.0},
        benchmark_returns={"A": 0.10},
    )
    assert result["active_return"] == 0.0
    assert result["total_allocation"] == 0.0
    assert result["total_selection"] == 0.0


def test_selection_effect_when_outperforming_within_sector():
    # portfolio overweight same sector but outperforms: selection > 0
    result = brinson_attribution(
        portfolio_weights={"A": 0.5, "B": 0.5},
        portfolio_returns={"A": 0.10, "B": 0.04},
        benchmark_weights={"A": 0.5, "B": 0.5},
        benchmark_returns={"A": 0.08, "B": 0.04},
    )
    # only A outperforms (0.10 vs 0.08); selection_A = 0.5 * (0.10 - 0.08) = 0.01
    by_sector = {row["sector"]: row for row in result["per_sector"]}
    assert abs(by_sector["A"]["selection"] - 0.01) < 1e-12
    assert abs(by_sector["B"]["selection"] - 0.0) < 1e-12
    assert abs(result["total_selection"] - 0.01) < 1e-12


def test_allocation_effect_overweight_outperforming_sector():
    # rB_total = 0.5*0.08 + 0.5*0.02 = 0.05
    # overweight A (0.6 vs 0.5), A benchmark return 0.08 > rB_total 0.05
    # allocation_A = (0.6-0.5)*(0.08-0.05) = 0.003
    result = brinson_attribution(
        portfolio_weights={"A": 0.6, "B": 0.4},
        portfolio_returns={"A": 0.08, "B": 0.02},
        benchmark_weights={"A": 0.5, "B": 0.5},
        benchmark_returns={"A": 0.08, "B": 0.02},
    )
    by_sector = {row["sector"]: row for row in result["per_sector"]}
    assert abs(by_sector["A"]["allocation"] - 0.1 * (0.08 - 0.05)) < 1e-12
    assert abs(result["portfolio_return"] - (0.6 * 0.08 + 0.4 * 0.02)) < 1e-12
    assert abs(result["benchmark_return"] - 0.05) < 1e-12


def test_decomposition_reconciles_to_active_return():
    result = brinson_attribution(
        portfolio_weights={"A": 0.6, "B": 0.4},
        portfolio_returns={"A": 0.12, "B": 0.03},
        benchmark_weights={"A": 0.5, "B": 0.5},
        benchmark_returns={"A": 0.08, "B": 0.05},
    )
    explained = result["total_allocation"] + result["total_selection"] + result["total_interaction"]
    assert abs(explained - result["active_return"]) < 1e-12
    assert abs(result["residual"]) < 1e-12


def test_missing_sector_treated_as_zero():
    result = brinson_attribution(
        portfolio_weights={"A": 1.0},
        portfolio_returns={"A": 0.10},
        benchmark_weights={"A": 0.5, "B": 0.5},
        benchmark_returns={"A": 0.10, "B": 0.0},
    )
    sectors = {row["sector"] for row in result["per_sector"]}
    assert sectors == {"A", "B"}
