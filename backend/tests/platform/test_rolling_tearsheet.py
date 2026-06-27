from __future__ import annotations

import pytest

from app.platform.rolling_tearsheet import rolling_tearsheet_report


def test_rolling_tearsheet_outputs_leading_none_and_metrics():
    body = rolling_tearsheet_report([0.01, -0.01, 0.02, 0.03], benchmark=[0.0, 0.0, 0.01, 0.01], windows=[3]).to_dict()
    window = body["windows"]["3"]
    assert window["rolling_sharpe"][0] is None
    assert window["rolling_sharpe"][2] is not None
    assert window["rolling_beta"][2] is not None


def test_rolling_tearsheet_rejects_invalid_window():
    with pytest.raises(ValueError):
        rolling_tearsheet_report([0.1, 0.2], windows=[3])
    with pytest.raises(ValueError):
        rolling_tearsheet_report([0.1, 0.2], windows=[2], periods_per_year=0)


def test_rolling_tearsheet_annualizes_by_periods_per_year():
    slow = rolling_tearsheet_report([0.01, 0.02, 0.03], windows=[3], periods_per_year=1).to_dict()
    fast = rolling_tearsheet_report([0.01, 0.02, 0.03], windows=[3], periods_per_year=252).to_dict()
    assert fast["windows"]["3"]["rolling_sharpe"][2] > slow["windows"]["3"]["rolling_sharpe"][2]


def test_rolling_tearsheet_annualizes_alpha_by_periods_per_year():
    slow = rolling_tearsheet_report([0.02, 0.025, 0.04], benchmark=[0.005, 0.01, 0.015], windows=[3], periods_per_year=1).to_dict()
    fast = rolling_tearsheet_report([0.02, 0.025, 0.04], benchmark=[0.005, 0.01, 0.015], windows=[3], periods_per_year=252).to_dict()
    slow_alpha = slow["windows"]["3"]["rolling_alpha"][2]
    fast_alpha = fast["windows"]["3"]["rolling_alpha"][2]
    assert slow_alpha is not None
    assert abs(slow_alpha) > 1e-9
    assert fast_alpha == pytest.approx(slow_alpha * 252)
