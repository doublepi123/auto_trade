from __future__ import annotations

import pytest

from app.platform.tail_dependence import tail_dependence_report


def test_tail_dependence_reports_upper_and_lower():
    x = [-0.05, -0.04, -0.02, 0.0, 0.01, 0.02, 0.04, 0.05, -0.06, 0.07]
    y = [-0.04, -0.03, -0.01, 0.0, 0.02, 0.01, 0.05, 0.06, -0.05, 0.08]
    body = tail_dependence_report(x, y, threshold=0.2).to_dict()
    assert "upper" in body["empirical"]
    assert "lower" in body["empirical"]
    assert 0.0 <= body["empirical"]["upper"] <= 1.0
    assert 0.0 <= body["empirical"]["lower"] <= 1.0


def test_tail_dependence_rejects_length_mismatch():
    with pytest.raises(ValueError):
        tail_dependence_report([0.1, 0.2], [0.1], threshold=0.1)
