"""Tests for volatility targeting and inverse-risk position sizing."""

from __future__ import annotations

import math

import pytest

from app.platform.vol_targeting import (
    VolTargetReport,
    ewma_vol,
    inverse_variance_weights,
    inverse_vol_weights,
    realized_vol,
    vol_target_leverage,
    vol_target_report,
)


def test_realized_vol_matches_annualized_sample_standard_deviation():
    returns = [0.01, 0.02, 0.03]

    result = realized_vol(returns)

    assert result == pytest.approx(0.01 * math.sqrt(252.0))


def test_realized_vol_drops_non_finite_returns():
    returns = [0.01, math.nan, 0.02, math.inf, 0.03]

    result = realized_vol(returns)

    assert result == pytest.approx(0.01 * math.sqrt(252.0))


def test_vol_target_leverage_clips_at_max_leverage():
    result = vol_target_leverage(0.10, target_vol=0.20, max_leverage=1.5)

    assert result == pytest.approx(1.5)


def test_vol_target_leverage_is_zero_below_volatility_floor():
    assert vol_target_leverage(1e-10, target_vol=0.20) == 0.0


def test_inverse_vol_weights_sum_to_one_and_favor_lower_volatility():
    weights = inverse_vol_weights([0.10, 0.20, 0.40])

    assert sum(weights) == pytest.approx(1.0)
    assert weights == pytest.approx([4.0 / 7.0, 2.0 / 7.0, 1.0 / 7.0])


def test_inverse_variance_weights_sum_to_one():
    weights = inverse_variance_weights([0.10, 0.20])

    assert sum(weights) == pytest.approx(1.0)
    assert weights == pytest.approx([0.8, 0.2])


@pytest.mark.parametrize("weight_function", [inverse_vol_weights, inverse_variance_weights])
def test_zero_volatility_assets_are_excluded(weight_function):
    weights = weight_function([0.0, 0.20, 1e-10])

    assert weights == pytest.approx([0.0, 1.0, 0.0])


def test_ewma_vol_is_positive_and_reacts_to_recent_volatility():
    low_volatility = [0.001, -0.001] * 10
    high_volatility = [0.05, -0.05] * 10

    recent_high = ewma_vol(low_volatility + high_volatility)
    recent_low = ewma_vol(high_volatility + low_volatility)

    assert recent_high > 0.0
    assert recent_high > recent_low


@pytest.mark.parametrize(
    "invalid_call",
    [
        lambda: realized_vol([]),
        lambda: realized_vol([0.01]),
        lambda: realized_vol([math.nan, math.inf]),
        lambda: realized_vol([0.01, 0.02], ann_factor=0),
        lambda: realized_vol([0.01, 0.02], ddof=-1),
        lambda: realized_vol([0.01, 0.02], ddof=2),
        lambda: ewma_vol([]),
        lambda: ewma_vol([0.01]),
        lambda: ewma_vol([0.01, 0.02], ann_factor=0),
        lambda: ewma_vol([0.01, 0.02], decay=0.0),
        lambda: ewma_vol([0.01, 0.02], decay=1.0),
        lambda: inverse_vol_weights([]),
        lambda: inverse_vol_weights([0.0, 1e-10]),
        lambda: inverse_variance_weights([]),
        lambda: inverse_variance_weights([0.0, 1e-10]),
        lambda: vol_target_leverage(0.10, target_vol=-0.01),
        lambda: vol_target_leverage(0.10, target_vol=0.20, max_leverage=0.0),
    ],
)
def test_invalid_inputs_raise_value_error(invalid_call):
    with pytest.raises(ValueError):
        invalid_call()


def test_vol_target_report_tracks_finite_periods_and_serializes():
    returns = [0.01, math.nan, -0.01, 0.02, math.inf, -0.02]

    report = vol_target_report(returns, target_vol=0.10, max_leverage=0.75)

    assert report.n_periods == 4
    assert report.realized_vol > 0.0
    assert report.ewma_vol > 0.0
    assert report.leverage <= 0.75

    expected = VolTargetReport(
        realized_vol=report.realized_vol,
        ewma_vol=report.ewma_vol,
        leverage=report.leverage,
        n_periods=4,
    )
    assert report.to_dict() == {
        "realized_vol": expected.realized_vol,
        "ewma_vol": expected.ewma_vol,
        "leverage": expected.leverage,
        "n_periods": expected.n_periods,
    }
