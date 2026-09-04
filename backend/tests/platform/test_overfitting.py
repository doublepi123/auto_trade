"""Tests for P201 backtest overfitting diagnostics (PBO + Deflated Sharpe)."""

from __future__ import annotations

import math

import pytest

from app.platform.overfitting import (
    _norm_cdf,
    _norm_inv_cdf,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)

# Euler-Mascheroni constant, as it appears in Bailey & Lopez de Prado (2014),
# "The Deflated Sharpe Ratio", Journal of Portfolio Management 40(5) 94-107,
# equations (2)/(6). Spelled out here rather than imported from the module under
# test so the reference values below are derived independently of it.
_EULER_MASCHERONI = 0.5772156649015329


def _published_expected_max(n_trials: int, trial_sharpe_variance: float = 1.0) -> float:
    """E[max SR] per Bailey & Lopez de Prado (2014) eq. (2)/(6).

    ``sqrt(V[SR_n]) * ((1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N*e)))``

    Computed here from gamma, the inverse normal CDF and Euler's number so the
    assertions below never restate the implementation's own arithmetic.
    """
    g = _EULER_MASCHERONI
    bracket = (1.0 - g) * _norm_inv_cdf(1.0 - 1.0 / n_trials) + g * _norm_inv_cdf(
        1.0 - 1.0 / (n_trials * math.e)
    )
    return math.sqrt(trial_sharpe_variance) * bracket


def _superseded_expected_max(n_trials: int) -> float:
    """The single-term benchmark this module used before the correction.

    Kept only so the anti-conservative regression test can pin the observed
    Sharpe strictly between the old (too low) bar and the published one. It is
    not a target: it understates the luck bar by up to 35% at small N.
    """
    return (1.0 - 0.5 / (1.0 + n_trials)) * _norm_inv_cdf(1.0 - 1.0 / n_trials)


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


# ---------------------------------------------------------------------------
# The published multiple-testing benchmark.
#
# The luck bar a swept winner has to clear is E[max SR] over N zero-edge trials.
# Understating it certifies lucky search results, so these pin it to the paper
# rather than to whatever this module happens to compute.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_trials", [5, 10, 25, 100, 2000])
def test_dsr_expected_max_matches_the_published_two_term_formula(n_trials: int):
    """Bailey & Lopez de Prado (2014) eq. (2)/(6), reference values.

    The benchmark is a two-term expression weighted by the Euler-Mascheroni
    constant, not a single ``Phi^-1(1 - 1/N)`` term scaled by ``1 - 0.5/(1+N)``.
    The expected value is rebuilt in the test from gamma, ``Phi^-1`` and e.
    """
    result = deflated_sharpe_ratio(
        observed_sharpe=1.0,
        n_trials=n_trials,
        sample_size=252,
    )
    assert result["expected_max_null_sharpe"] == pytest.approx(
        _published_expected_max(n_trials), abs=1e-12
    )


def test_dsr_expected_max_scales_with_the_square_root_of_trial_variance():
    """``sqrt(V[SR_n])`` is a multiplicative factor, so the benchmark carries the
    same units as the trial Sharpes it is compared against."""
    unit = deflated_sharpe_ratio(
        1.0, n_trials=25, sample_size=252, trial_sharpe_variance=1.0
    )["expected_max_null_sharpe"]
    quarter = deflated_sharpe_ratio(
        1.0, n_trials=25, sample_size=252, trial_sharpe_variance=0.25
    )["expected_max_null_sharpe"]
    assert quarter == pytest.approx(0.5 * unit, rel=1e-12)


@pytest.mark.parametrize("n_trials", [5, 10, 25, 100, 2000])
def test_dsr_published_bar_is_higher_than_the_superseded_single_term_one(
    n_trials: int,
):
    """Direction check on the defect: the old approximation sat BELOW the paper's
    benchmark at every N (65% of it at N=5), so it let lucky winners through."""
    assert _superseded_expected_max(n_trials) < _published_expected_max(n_trials)


def test_dsr_sharpe_between_the_old_and_published_bar_is_no_longer_significant():
    """The anti-conservative regression, stated as a verdict flip.

    At N=10 the superseded bar was 1.2233 and the published one is 1.5746. A
    Sharpe of 1.40 sits between them: it cleared the old bar and does not clear
    the real one. Under the old code this sweep winner was reported as
    distinguishable from luck. It is not.
    """
    n_trials = 10
    observed = 1.40
    assert _superseded_expected_max(n_trials) < observed < _published_expected_max(
        n_trials
    )

    result = deflated_sharpe_ratio(
        observed_sharpe=observed,
        n_trials=n_trials,
        sample_size=252,
    )
    assert result["deflated_sharpe"] < 0.0
    assert result["distinguishable_from_luck"] is False


# ---------------------------------------------------------------------------
# One kurtosis convention, stated once.
# ---------------------------------------------------------------------------


def test_dsr_variance_term_uses_the_papers_raw_kurtosis_convention():
    """``Var(SR) = (1 - g3*SR + (g4 - 1)/4 * SR^2) / (T - 1)`` with RAW g4.

    With raw kurtosis 3 and zero skew and SR=1 the bracket is 1.5, not the 1.25
    a ``(K - 2)/4`` term produces.
    """
    sample_size = 252
    result = deflated_sharpe_ratio(
        observed_sharpe=1.0,
        n_trials=1,
        sample_size=sample_size,
        skewness=0.0,
        kurtosis=3.0,
    )
    expected_bracket = 1.0 - 0.0 * 1.0 + (3.0 - 1.0) / 4.0 * 1.0**2
    assert expected_bracket == 1.5
    assert result["sharpe_std"] == pytest.approx(
        math.sqrt(expected_bracket / (sample_size - 1)), rel=1e-12
    )


@pytest.mark.parametrize(
    ("skewness", "kurtosis"),
    [(0.0, 3.0), (-1.5, 3.0), (0.0, 9.0), (0.8, 4.5)],
)
def test_dsr_deflated_and_psr_share_one_variance_convention(
    skewness: float, kurtosis: float
):
    """``psr`` must be built from the same estimator standard deviation that
    scales ``deflated_sharpe``. Two different kurtosis conventions in one
    function is how a gate ends up disagreeing with itself.
    """
    observed = 0.5
    result = deflated_sharpe_ratio(
        observed_sharpe=observed,
        n_trials=10,
        sample_size=30,
        skewness=skewness,
        kurtosis=kurtosis,
    )
    assert result["psr"] == pytest.approx(
        _norm_cdf(observed / float(result["sharpe_std"])), rel=1e-12
    )


# ---------------------------------------------------------------------------
# Units contract.
# ---------------------------------------------------------------------------


def test_dsr_declares_when_the_trial_variance_was_assumed_rather_than_measured():
    assumed = deflated_sharpe_ratio(1.0, n_trials=25, sample_size=252)
    assert assumed["trial_variance_assumed"] is True
    assert assumed["trial_sharpe_variance"] == 1.0


def test_dsr_declares_a_supplied_trial_variance_as_measured():
    measured = deflated_sharpe_ratio(
        1.0, n_trials=25, sample_size=252, trial_sharpe_variance=0.04
    )
    assert measured["trial_variance_assumed"] is False
    assert measured["trial_sharpe_variance"] == 0.04


def test_dsr_refuses_to_certify_a_multi_trial_winner_on_an_assumed_benchmark():
    """Fail-closed: with N>1 the benchmark is non-zero, so an unmeasured
    ``V[SR_n]`` makes it unit-mismatched against the observed Sharpe. A gate
    cannot certify on a benchmark whose units it does not know."""
    assumed = deflated_sharpe_ratio(5.0, n_trials=25, sample_size=252)
    assert assumed["deflated_sharpe"] > 0.0
    assert assumed["psr"] > 0.5
    assert assumed["trial_variance_assumed"] is True
    assert assumed["distinguishable_from_luck"] is False

    measured = deflated_sharpe_ratio(
        5.0, n_trials=25, sample_size=252, trial_sharpe_variance=1.0
    )
    assert measured["distinguishable_from_luck"] is True


def test_dsr_single_trial_needs_no_measured_variance_to_certify():
    """At N=1 the benchmark is exactly zero in every unit system, so nothing is
    being assumed and the fail-closed rule must not fire."""
    result = deflated_sharpe_ratio(2.0, n_trials=1, sample_size=252)
    assert result["expected_max_null_sharpe"] == 0.0
    assert result["distinguishable_from_luck"] is True


# ---------------------------------------------------------------------------
# Monotonicity.
# ---------------------------------------------------------------------------


def test_dsr_expected_max_strictly_increases_in_n_trials():
    bars = [
        float(
            deflated_sharpe_ratio(1.0, n_trials=n, sample_size=252)[
                "expected_max_null_sharpe"
            ]
        )
        for n in (2, 3, 5, 10, 25, 100, 500, 2000)
    ]
    assert all(later > earlier for earlier, later in zip(bars, bars[1:]))


def test_dsr_strictly_decreases_as_trials_grow_with_observed_sharpe_fixed():
    deflated = [
        float(
            deflated_sharpe_ratio(2.0, n_trials=n, sample_size=252)[
                "deflated_sharpe"
            ]
        )
        for n in (2, 3, 5, 10, 25, 100, 500, 2000)
    ]
    assert all(later < earlier for earlier, later in zip(deflated, deflated[1:]))


# ---------------------------------------------------------------------------
# Degenerate guards keep behaving as before.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n_trials", "sample_size"), [(0, 10), (-1, 10), (5, 1), (5, 0)]
)
def test_dsr_degenerate_guards_return_the_neutral_fallback(
    n_trials: int, sample_size: int
):
    result = deflated_sharpe_ratio(2.0, n_trials=n_trials, sample_size=sample_size)
    assert result["observed_sharpe"] == 2.0
    assert result["expected_max_null_sharpe"] == 0.0
    assert result["sharpe_std"] == 0.0
    assert result["deflated_sharpe"] == 2.0
    assert result["psr"] == 0.5
    assert result["distinguishable_from_luck"] is False
