from __future__ import annotations

import pytest

from app.platform.multiple_testing import (
    holm_adjusted_pvalues,
    one_sample_greater_pvalue,
)


def test_one_sample_greater_pvalue_handles_sample_boundaries() -> None:
    assert one_sample_greater_pvalue([]) is None
    assert one_sample_greater_pvalue([1.0]) is None
    assert one_sample_greater_pvalue([3.0] * 20) == 0.0
    assert one_sample_greater_pvalue([0.0] * 20) == 0.5
    assert one_sample_greater_pvalue([-3.0] * 20) == 1.0


def test_one_sample_greater_pvalue_distinguishes_weak_and_strong_edges() -> None:
    weak = one_sample_greater_pvalue(
        [1.0, -1.0] * 9 + [1.0, 1.0],
    )
    strong = one_sample_greater_pvalue(
        [3.0, 2.0, 4.0, 1.0] * 5,
    )

    assert weak is not None
    assert weak > 0.05
    assert strong is not None
    assert strong < 0.001


def test_holm_adjustment_counts_missing_hypotheses_in_family() -> None:
    adjusted = holm_adjusted_pvalues(
        [0.01, 0.02, None, 0.5],
    )

    assert adjusted[0] == pytest.approx(0.04)
    assert adjusted[1] == pytest.approx(0.06)
    assert adjusted[2] is None
    assert adjusted[3] == 1.0


def test_holm_adjustment_is_monotone_and_rejects_invalid_input() -> None:
    assert holm_adjusted_pvalues([]) == []
    assert holm_adjusted_pvalues([0.03, 0.01, 0.02]) == pytest.approx(
        [0.04, 0.03, 0.04],
    )
    with pytest.raises(ValueError, match="finite"):
        holm_adjusted_pvalues([float("nan")])
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        holm_adjusted_pvalues([1.1])
