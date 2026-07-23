"""Tests for confidence-shrunk adaptive Kelly sizing."""

from __future__ import annotations

import pytest

from app.platform.adaptive_sizing import adaptive_kelly


def test_large_strong_sample_stays_close_to_full_kelly() -> None:
    # Given
    outcomes = [2.0] * 120 + [-1.0] * 80

    # When
    report = adaptive_kelly(outcomes)

    # Then
    assert report.full_kelly == pytest.approx(0.4)
    assert report.shrink_factor == pytest.approx(200.0 / 230.0)
    assert report.shrunk_kelly == pytest.approx(
        report.full_kelly * report.shrink_factor
    )
    assert 0.8 * report.full_kelly < report.shrunk_kelly <= report.full_kelly
    assert report.confidence == pytest.approx(report.shrink_factor)
    assert report.to_dict()["n_trades"] == 200


def test_tiny_sample_is_shrunk_heavily_relative_to_same_large_sample() -> None:
    # Given
    tiny_outcomes = [2.0, -1.0]
    large_outcomes = tiny_outcomes * 100

    # When
    tiny = adaptive_kelly(tiny_outcomes)
    large = adaptive_kelly(large_outcomes)

    # Then
    assert tiny.full_kelly == pytest.approx(large.full_kelly)
    assert tiny.shrink_factor == pytest.approx(2.0 / 32.0)
    assert tiny.shrunk_kelly < 0.1 * tiny.full_kelly
    assert tiny.shrunk_kelly < large.shrunk_kelly


def test_all_wins_return_zero_sizing_without_inventing_loss_odds() -> None:
    # Given
    outcomes = [1.0, 2.0, 3.0]

    # When
    report = adaptive_kelly(outcomes)

    # Then
    assert report.win_prob == pytest.approx(1.0)
    assert report.avg_win == pytest.approx(2.0)
    assert report.avg_loss == pytest.approx(0.0)
    assert report.edge == pytest.approx(2.0)
    assert report.full_kelly == pytest.approx(0.0)
    assert report.shrunk_kelly == pytest.approx(0.0)
    assert report.shrink_factor == pytest.approx(0.0)
    assert report.confidence == pytest.approx(0.0)


def test_all_losses_return_zero_sizing() -> None:
    # Given
    outcomes = [-1.0, -2.0, -3.0]

    # When
    report = adaptive_kelly(outcomes)

    # Then
    assert report.win_prob == pytest.approx(0.0)
    assert report.avg_win == pytest.approx(0.0)
    assert report.avg_loss == pytest.approx(2.0)
    assert report.edge == pytest.approx(-2.0)
    assert report.full_kelly == pytest.approx(0.0)
    assert report.shrunk_kelly == pytest.approx(0.0)
    assert report.shrink_factor == pytest.approx(0.0)
    assert report.confidence == pytest.approx(0.0)


def test_negative_estimated_edge_is_floored_at_zero() -> None:
    # Given
    outcomes = [1.0, -2.0]

    # When
    report = adaptive_kelly(outcomes)

    # Then
    assert report.edge == pytest.approx(-0.5)
    assert report.full_kelly == pytest.approx(0.0)
    assert report.shrunk_kelly == pytest.approx(0.0)


@pytest.mark.parametrize(
    "outcomes",
    [
        [2.0, -1.0],
        [1.0, -2.0],
        [1.0, 1.0],
        [-1.0, -1.0],
        [2.0, 0.0, -1.0],
    ],
)
def test_shrunk_fraction_and_shrink_factor_remain_bounded(
    outcomes: list[float],
) -> None:
    # When
    report = adaptive_kelly(outcomes)

    # Then
    assert 0.0 <= report.shrink_factor <= 1.0
    assert 0.0 <= report.shrunk_kelly <= report.full_kelly


def test_empty_outcomes_raise_value_error() -> None:
    with pytest.raises(ValueError):
        adaptive_kelly([])


def test_negative_shrinkage_strength_raises_value_error() -> None:
    with pytest.raises(ValueError):
        adaptive_kelly([2.0, -1.0], shrinkage_strength=-1.0)
