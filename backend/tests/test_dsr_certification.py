from __future__ import annotations

from statistics import NormalDist

import pytest

from app.platform import overfitting
from app.platform.overfitting import deflated_sharpe_ratio


def test_measured_sweep_winner_below_95_percent_is_not_certified() -> None:
    # Given: measured cross-trial variance and more than one trial.
    # When: the winner clears the expected maximum by about one standard error.
    result = deflated_sharpe_ratio(
        0.26, n_trials=10, sample_size=100, trial_sharpe_variance=0.01,
    )
    # Then: a positive z below the 95th percentile is not evidence of skill.
    assert 0.0 < result["deflated_sharpe"] < 1.6448536269514722
    assert result["psr"] > 0.5
    assert result["trial_variance_assumed"] is False
    assert result["distinguishable_from_luck"] is False


@pytest.mark.parametrize("z", [-3.0, 0.0, 1.0, 1.6448536269514722 - 1e-10,
                              1.6448536269514722, 1.6448536269514722 + 1e-10, 3.0])
def test_probability_and_certification_at_the_95_percent_boundary(z: float) -> None:
    # Given: one trial and raw kurtosis 1 make the standard error exactly 1.
    # When: the observed Sharpe therefore equals the deflated z-score.
    result = deflated_sharpe_ratio(z, n_trials=1, sample_size=2, kurtosis=1.0)
    # Then: disclose the probability separately and apply the inclusive 95% bar.
    assert overfitting.DSR_CONFIDENCE_LEVEL == 0.95
    assert NormalDist().inv_cdf(0.95) == pytest.approx(1.6448536269514722, abs=1e-15)
    assert result["deflated_sharpe"] == z
    assert result["dsr_probability"] == NormalDist().cdf(z)
    assert result["distinguishable_from_luck"] is (z >= 1.6448536269514722)
    assert (result["dsr_probability"] >= 0.95) is (z >= 1.6448536269514722)


@pytest.mark.parametrize("variance", [None, 1.0])
def test_high_probability_preserves_the_assumed_variance_guard(variance: float | None) -> None:
    # Given: the same strong winner with assumed or measured trial variance.
    # When: the DSR is computed across 25 trials.
    result = deflated_sharpe_ratio(
        5.0, n_trials=25, sample_size=252, trial_sharpe_variance=variance,
    )
    # Then: even overwhelming probability cannot replace measured units.
    assert result["dsr_probability"] > 0.99
    assert result["psr"] > 0.5
    assert result["distinguishable_from_luck"] is (variance is not None)


@pytest.mark.parametrize(("trials", "samples"), [(0, 10), (-1, 10), (5, 1), (5, 0)])
def test_degenerate_inputs_report_neutral_probability(trials: int, samples: int) -> None:
    # Given: insufficient trials or observations, despite a large raw Sharpe.
    # When: the fallback is returned.
    result = deflated_sharpe_ratio(10.0, n_trials=trials, sample_size=samples)
    # Then: the legacy raw fallback is unchanged, but is not a usable z-score.
    assert result["deflated_sharpe"] == 10.0
    assert result["dsr_probability"] == result["psr"] == 0.5
    assert result["distinguishable_from_luck"] is False
