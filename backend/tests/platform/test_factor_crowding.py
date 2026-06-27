from __future__ import annotations

import pytest

from app.platform.factor_crowding import factor_crowding_report


def test_factor_crowding_reports_high_signal_concentration():
    body = factor_crowding_report({"A": 10, "B": 1, "C": 0.5}, valuations={"A": 50, "B": 10, "C": 8}, flows={"A": 5, "B": 1, "C": 0}).to_dict()
    assert body["crowding_score"] > 0
    assert body["components"]["signal_concentration"] > 0


def test_factor_crowding_rejects_mismatched_optional_maps():
    with pytest.raises(ValueError):
        factor_crowding_report({"A": 1, "B": 2}, valuations={"A": 10})


def test_factor_crowding_normalizes_large_valuation_scale():
    body = factor_crowding_report({"A": 1, "B": 2, "C": 3}, valuations={"A": 10, "B": 1000, "C": 5000}).to_dict()
    assert 0.0 <= body["components"]["valuation_spread"] <= 1.0
    assert 0.0 <= body["crowding_score"] <= 1.0
