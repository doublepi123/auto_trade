from __future__ import annotations

import pytest

from app.platform.curve_spread import curve_spread_report


def test_curve_spread_reports_slope_and_roll_down():
    body = curve_spread_report({1: 0.03, 5: 0.04, 10: 0.045}, short_tenor=1, long_tenor=10, history=[0.01, 0.012, 0.015]).to_dict()
    assert body["spread"] == pytest.approx(0.015)
    assert body["roll_down"] != 0
    assert "z_score" in body


def test_curve_spread_rejects_missing_tenor():
    with pytest.raises(ValueError):
        curve_spread_report({1: 0.03, 5: 0.04}, short_tenor=1, long_tenor=10)
