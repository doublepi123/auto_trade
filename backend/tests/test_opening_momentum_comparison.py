from __future__ import annotations

import pytest

from app.domain.opening_momentum_comparison import (
    compare_opening_momentum_variants,
)


def test_comparison_collects_until_early_evidence_threshold() -> None:
    result = compare_opening_momentum_variants(
        [0.0, 10.0, -5.0, 0.0],
        [5.0, 12.0, -1.0, 3.0],
    )

    assert result.resolved_sessions == 4
    assert result.cumulative_delta_bps == 14.0
    assert result.mean_delta_bps == 3.5
    assert result.outperformance_rate == 1.0
    assert result.recommendation == "COLLECTING"
    assert result.promotion_ready is False


def test_comparison_marks_positive_small_sample_as_early_leader() -> None:
    result = compare_opening_momentum_variants(
        [0.0] * 5,
        [4.0] * 5,
    )

    assert result.confidence_lower_bps == 4.0
    assert result.confidence_upper_bps == 4.0
    assert result.recommendation == "EARLY_LEADER"
    assert result.promotion_ready is False


def test_comparison_requires_sample_confidence_and_drawdown_guard() -> None:
    incumbent = [0.0] * 20
    challenger = [5.0] * 20

    ready = compare_opening_momentum_variants(
        incumbent,
        challenger,
    )
    excessive_drawdown = compare_opening_momentum_variants(
        incumbent,
        [10.0] * 19 + [-30.0],
    )

    assert ready.risk_guard_passed is True
    assert ready.promotion_ready is True
    assert ready.recommendation == "PROMOTION_CANDIDATE"
    assert excessive_drawdown.risk_guard_passed is False
    assert excessive_drawdown.promotion_ready is False
    assert excessive_drawdown.recommendation == "INCONCLUSIVE"


def test_comparison_marks_confident_negative_delta_underperforming() -> None:
    result = compare_opening_momentum_variants(
        [0.0] * 20,
        [-3.0] * 20,
    )

    assert result.confidence_upper_bps == -3.0
    assert result.recommendation == "UNDERPERFORMING"
    assert result.promotion_ready is False


def test_comparison_rejects_unpaired_or_non_finite_inputs() -> None:
    with pytest.raises(ValueError, match="equal length"):
        compare_opening_momentum_variants(
            [0.0],
            [],
        )
    with pytest.raises(ValueError, match="finite"):
        compare_opening_momentum_variants(
            [float("nan")],
            [0.0],
        )
