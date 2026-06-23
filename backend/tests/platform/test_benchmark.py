from __future__ import annotations

from app.platform.benchmark import BenchmarkAnalytics


def test_beta_one_for_identical_series():
    eq = [10000 * (1.001 ** i) for i in range(30)]
    result = BenchmarkAnalytics(periods_per_year=252).alpha_beta(eq, eq)
    assert abs(result["beta"] - 1.0) < 1e-9
    assert abs(result["alpha"]) < 1e-9
    assert abs(result["correlation"] - 1.0) < 1e-9


def test_beta_zero_for_uncorrelated_flat_benchmark():
    strategy = [10000 * (1.001 ** i) for i in range(30)]
    benchmark = [10000.0] * 30  # flat -> zero variance
    result = BenchmarkAnalytics(periods_per_year=252).alpha_beta(strategy, benchmark)
    assert result["beta"] == 0.0


def test_capture_ratios():
    # benchmark up 1% each period, strategy up 2% -> up_capture 2.0
    strategy = [10000 * (1.02 ** i) for i in range(10)]
    benchmark = [10000 * (1.01 ** i) for i in range(10)]
    result = BenchmarkAnalytics(periods_per_year=252).capture_ratios(strategy, benchmark)
    assert abs(result["up_capture"] - 2.0) < 1e-9
    assert result["down_capture"] == 0.0


def test_relative_combines_all():
    strategy = [10000 * (1.005 ** i) for i in range(20)]
    benchmark = [10000 * (1.002 ** i) for i in range(20)]
    result = BenchmarkAnalytics(periods_per_year=252).relative(strategy, benchmark)
    assert "alpha" in result and "beta" in result
    assert "strategy_return" in result and "benchmark_return" in result
    assert result["excess_return"] == result["strategy_return"] - result["benchmark_return"]
    assert result["strategy_return"] > result["benchmark_return"]
