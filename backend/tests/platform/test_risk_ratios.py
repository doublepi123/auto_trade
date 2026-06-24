"""Tests for P205 risk-adjusted ratios."""

from __future__ import annotations

import math

from app.platform.risk_ratios import (
    all_ratios,
    information_ratio,
    modigliani_ratio,
    omega_ratio,
    rolling_sharpe,
    sharpe_ratio,
    sortino_ratio,
    treynor_ratio,
)


def test_sharpe_zero_when_constant_returns():
    rets = [0.01] * 100
    assert sharpe_ratio(rets) == 0.0


def test_sharpe_positive_for_upward_trend():
    # Strongly positive drift
    rets = [0.01 + 0.001 * (i % 20) for i in range(100)]
    s = sharpe_ratio(rets)
    assert s > 0


def test_sharpe_known_case_unit_vol():
    # mean=0.01, std ≈ 0 → return 0 (degenerate)
    rets = [0.01, 0.01, 0.01, 0.01]
    assert sharpe_ratio(rets) == 0.0


def test_sharpe_short_series():
    assert sharpe_ratio([0.01]) == 0.0
    assert sharpe_ratio([]) == 0.0


def test_sortino_greater_than_sharpe_for_asymmetric_returns():
    # Big upside, small downside → Sortino > Sharpe
    rets = [0.05, 0.04, 0.06, -0.01, -0.005, 0.03]
    assert sortino_ratio(rets) > sharpe_ratio(rets)


def test_information_ratio_zero_for_identical_series():
    rets = [0.01, -0.02, 0.03]
    assert information_ratio(rets, rets) == 0.0


def test_information_ratio_for_outperforming_strategy():
    # Strategy outperforms benchmark by a non-constant amount → positive IR
    benchmark = [0.01, -0.01, 0.02, -0.005, 0.0, 0.01, -0.02]
    rets = [b + 0.005 for b in benchmark]
    # Constant excess → std = 0, IR = 0; flip a few to add tracking error.
    rets[0] += 0.002
    rets[3] -= 0.003
    ir = information_ratio(rets, benchmark)
    # Excess is mostly 0.5% with a 0.2% and -0.3% wobble → mean positive
    assert ir > 0


def test_treynor_zero_for_zero_beta():
    # Uncorrelated → beta 0 → Treynor = 0
    rets = [0.01, -0.02, 0.03, -0.01, 0.02]
    benchmark = [0.0, 0.0, 0.0, 0.0, 0.0]
    assert treynor_ratio(rets, benchmark) == 0.0


def test_treynor_uses_linear_annualization():
    # Strategy tracks benchmark with 2x beta; rf=0
    benchmark = [0.01, -0.01, 0.02, 0.005, -0.01]
    rets = [b * 2.0 for b in benchmark]
    t = treynor_ratio(rets, benchmark, risk_free=0.0, periods_per_year=252)
    # Excess = mean(rets); beta = 2 → (μ_rets) / 2 per period
    expected_per_period = (sum(rets) / len(rets)) / 2.0
    assert abs(t - expected_per_period * 252) < 1e-6


def test_modigliani_equals_benchmark_annual_return_when_identical():
    # M² of a series equal to its benchmark = annualized benchmark return.
    rets = [0.01, -0.02, 0.03, 0.005, -0.01]
    m2 = modigliani_ratio(rets, rets, risk_free=0.0, periods_per_year=252)
    expected = (sum(rets) / len(rets)) * 252
    assert abs(m2 - expected) < 1e-6


def test_omega_zero_for_all_losses():
    rets = [-0.01, -0.02, -0.03]
    assert omega_ratio(rets) == 0.0


def test_omega_infinite_for_all_gains():
    rets = [0.01, 0.02, 0.03]
    assert omega_ratio(rets) == math.inf


def test_omega_balanced_returns():
    # Equal gain and loss magnitudes → Omega ≈ 1
    rets = [0.05, -0.05, 0.05, -0.05]
    om = omega_ratio(rets)
    assert abs(om - 1.0) < 1e-9


def test_rolling_sharpe_length_and_warmup():
    rets = [0.01 * (1 if i % 2 == 0 else -1) for i in range(50)]
    rs = rolling_sharpe(rets, window=20)
    assert len(rs) == 50
    # First 19 should be 0
    for v in rs[:19]:
        assert v == 0.0


def test_all_ratios_returns_complete_dict():
    rets = [0.01, -0.005, 0.02, -0.01, 0.0, 0.015, -0.005]
    rep = all_ratios(rets)
    for k in ("n", "mean", "std", "sharpe", "sortino", "omega", "information", "treynor", "modigliani"):
        assert k in rep
    # No benchmark passed → benchmark-dependent fields are 0.0
    assert rep["information"] == 0.0
    assert rep["treynor"] == 0.0


def test_all_ratios_with_benchmark_populates_fields():
    rets = [0.01, -0.005, 0.02, -0.01, 0.0, 0.015, -0.005]
    benchmark = [0.005, -0.003, 0.012, -0.008, 0.001, 0.01, -0.004]
    rep = all_ratios(rets, benchmark=benchmark)
    # Now the benchmark-dependent fields have non-trivial values
    assert isinstance(rep["information"], float)
    assert isinstance(rep["treynor"], float)
    assert isinstance(rep["modigliani"], float)
