from __future__ import annotations

import pytest

from app.platform.dynamic_factor_exposure import dynamic_factor_exposure_report


def test_dynamic_factor_exposure_tracks_beta():
    strategy = [0.01, 0.02, -0.005, 0.015, -0.01, 0.025, 0.005, -0.008, 0.012, -0.003]
    panel = {"momentum": [0.008, 0.018, -0.004, 0.012, -0.008, 0.022, 0.004, -0.006, 0.01, -0.002]}
    body = dynamic_factor_exposure_report(strategy, panel, window=4).to_dict()
    assert "momentum" in body["betas"]
    assert len(body["betas"]["momentum"]) == len(strategy)


def test_dynamic_factor_exposure_rejects_panel_length_mismatch():
    with pytest.raises(ValueError):
        dynamic_factor_exposure_report([0.01, 0.02], {"f": [0.01]}, window=2)
