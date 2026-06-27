from __future__ import annotations

import pytest

from app.platform.vol_forecast_comparison import vol_forecast_comparison_report


def test_vol_forecast_comparison_ranks_models():
    realized = [0.15, 0.16, 0.14, 0.17, 0.15, 0.16, 0.15, 0.17, 0.16, 0.15]
    forecasts = {
        "ewma": [0.14, 0.15, 0.15, 0.16, 0.15, 0.16, 0.15, 0.16, 0.16, 0.15],
        "bad": [0.30, 0.05, 0.40, 0.02, 0.35, 0.01, 0.38, 0.03, 0.33, 0.02],
    }
    body = vol_forecast_comparison_report(realized, forecasts).to_dict()
    assert body["best_model"] == "ewma"
    assert body["metrics"]["ewma"]["rmse"] < body["metrics"]["bad"]["rmse"]


def test_vol_forecast_comparison_rejects_length_mismatch():
    with pytest.raises(ValueError):
        vol_forecast_comparison_report([0.1, 0.2], {"m": [0.1]})
