from __future__ import annotations

import pytest

from app.platform.strategy_diversification import strategy_diversification_report


def test_strategy_diversification_flags_redundant_pairs():
    report = strategy_diversification_report(
        {
            "A": [0.01, 0.02, -0.01, 0.03],
            "B": [0.01, 0.02, -0.01, 0.03],
            "C": [-0.01, -0.02, 0.01, -0.03],
        },
        redundancy_threshold=0.95,
    )
    body = report.to_dict()
    assert ["A", "B"] in body["redundant_pairs"]
    assert 0.0 <= body["diversification_score"] <= 1.0
    assert body["average_pairwise_correlation"] < 1.0


def test_strategy_diversification_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        strategy_diversification_report({"A": [1, 2], "B": [1]})


def test_strategy_diversification_rejects_constant_series():
    with pytest.raises(ValueError):
        strategy_diversification_report({"A": [1, 1, 1], "B": [1, 1, 1]})
    with pytest.raises(ValueError):
        strategy_diversification_report({"A": [1, 2, 3], "B": [3, 2, 1]}, redundancy_threshold=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        strategy_diversification_report({"A": [1, 2, 3], "B": [3, 2, 1]}, redundancy_threshold="0.9")  # type: ignore[arg-type]
