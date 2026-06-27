from __future__ import annotations

import pytest

from app.platform.cross_sectional_dispersion import cross_sectional_dispersion_report


def test_cross_sectional_dispersion_reports_spread_and_gini():
    body = cross_sectional_dispersion_report({"A": 0.01, "B": -0.02, "C": 0.04}).to_dict()
    assert body["count"] == 3
    assert body["dispersion"]["range"] == pytest.approx(0.06)
    assert body["dispersion"]["iqr"] > 0
    assert body["dispersion"]["gini"] > 0
    assert body["opportunity_score"] > 0


def test_cross_sectional_dispersion_rejects_bad_inputs():
    with pytest.raises(ValueError):
        cross_sectional_dispersion_report({"A": 0.01})
    with pytest.raises(ValueError):
        cross_sectional_dispersion_report({"A": float("nan"), "B": 0.1})
