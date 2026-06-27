from __future__ import annotations

import pytest

from app.platform.forecast_diagnostics import forecast_diagnostics_report


def test_forecast_diagnostics_perfect_prediction():
    body = forecast_diagnostics_report([0.1, -0.2, 0.3, 0.4], [0.1, -0.2, 0.3, 0.4], n_buckets=2).to_dict()
    assert body["mse"] == pytest.approx(0.0)
    assert body["directional_accuracy"] == pytest.approx(1.0)
    assert body["rank_ic"] == pytest.approx(1.0)
    assert body["top_bottom_spread"] > 0


def test_forecast_diagnostics_rejects_length_mismatch():
    with pytest.raises(ValueError):
        forecast_diagnostics_report([1.0], [1.0, 2.0])
