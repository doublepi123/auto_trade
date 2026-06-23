"""Tests for P201 backtest overfitting diagnostics (PBO + Deflated Sharpe)."""

from __future__ import annotations

from app.platform.overfitting import (
    _norm_cdf,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)


def test_norm_cdf_known_values():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-6
    assert _norm_cdf(3.0) > 0.998
    assert _norm_cdf(-3.0) < 0.002


def test_pbo_no_overfit_when_one_strategy_consistently_dominates():
    # Strategy 0 has higher (varied) returns in every block; others lower.
    # Use varied returns so Sharpe is well-defined (std > 0).
    winner = [0.02, 0.01, 0.015, 0.025, 0.02, 0.018, 0.022, 0.015]
    mid = [0.001, -0.001, 0.0, 0.002, -0.001, 0.001, 0.0, 0.002]
    loser = [-0.02, -0.01, -0.015, -0.025, -0.02, -0.018, -0.022, -0.015]
    panel = [winner, mid, loser]
    result = probability_of_backtest_overfitting(panel)
    # IS-best is always strategy 0 and it stays best OOS -> PBO should be low.
    assert result["pbo"] <= 0.5
    assert result["n_splits"] > 0


def test_pbo_high_when_is_winner_does_not_generalize():
    # Construct returns where IS-half and OOS-half are anti-correlated per strategy,
    # so the IS winner is systematically the OOS loser.
    # strategy 0: strong first half, weak second half
    # strategy 1: weak first half, strong second half
    panel = [
        [0.05, 0.05, 0.05, 0.05, -0.05, -0.05, -0.05, -0.05],
        [-0.05, -0.05, -0.05, -0.05, 0.05, 0.05, 0.05, 0.05],
    ]
    result = probability_of_backtest_overfitting(panel)
    # For the single 50/50 split, IS-winner is the OOS-loser -> PBO == 1.0.
    assert result["pbo"] >= 0.5


def test_pbo_logit_mean_negative_when_overfit():
    panel = [
        [0.05, 0.05, 0.05, 0.05, -0.05, -0.05, -0.05, -0.05],
        [-0.05, -0.05, -0.05, -0.05, 0.05, 0.05, 0.05, 0.05],
    ]
    result = probability_of_backtest_overfitting(panel)
    assert result["logit_mean"] < 0


def test_pbo_empty_panel_returns_zero():
    result = probability_of_backtest_overfitting([])
    assert result["pbo"] == 0.0
    assert result["n_splits"] == 0


def test_pbo_too_short_returns_zero():
    result = probability_of_backtest_overfitting([[0.01], [0.02]])
    assert result["n_splits"] == 0


def test_dsr_single_trial_keeps_observed_sharpe_relative():
    # With one trial, expected_max_null is 0, so deflated is observed / std.
    result = deflated_sharpe_ratio(
        observed_sharpe=2.0, n_trials=1, sample_size=252, skewness=0.0, kurtosis=3.0
    )
    assert result["observed_sharpe"] == 2.0
    assert result["expected_max_null_sharpe"] == 0.0
    assert result["deflated_sharpe"] > 0
    assert 0.9 < result["psr"] <= 1.0  # strong SR, normal returns -> high PSR


def test_dsr_many_trials_reduces_deflated_sharpe():
    single = deflated_sharpe_ratio(2.0, n_trials=1, sample_size=252)
    many = deflated_sharpe_ratio(2.0, n_trials=100, sample_size=252)
    # More trials -> higher expected max null -> lower deflated Sharpe.
    assert many["expected_max_null_sharpe"] > single["expected_max_null_sharpe"]
    assert many["deflated_sharpe"] < single["deflated_sharpe"]


def test_dsr_negative_skew_lowers_psr():
    # Use a modest Sharpe + small sample so PSR is not saturated at 1.0.
    normal = deflated_sharpe_ratio(0.5, n_trials=10, sample_size=30, skewness=0.0)
    neg_skew = deflated_sharpe_ratio(0.5, n_trials=10, sample_size=30, skewness=-1.5)
    assert neg_skew["psr"] < normal["psr"]
    assert normal["psr"] < 1.0  # sanity: not saturated


def test_dsr_invalid_inputs_fallback():
    result = deflated_sharpe_ratio(2.0, n_trials=0, sample_size=10)
    assert result["deflated_sharpe"] == 2.0
    assert result["psr"] == 0.5
