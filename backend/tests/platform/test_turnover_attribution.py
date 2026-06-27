from __future__ import annotations

import pytest

from app.platform.turnover_attribution import turnover_attribution_report


def test_turnover_attribution_splits_drift_and_rebalance():
    body = turnover_attribution_report({"A": 0.5, "B": 0.5}, {"A": 0.7, "B": 0.3}, drifted_weights={"A": 0.6, "B": 0.4}).to_dict()
    assert body["total_turnover"] == pytest.approx(0.2)
    assert body["components"]["drift_turnover"] == pytest.approx(0.1)
    assert body["components"]["rebalance_turnover"] == pytest.approx(0.1)


def test_turnover_attribution_rejects_empty_current():
    with pytest.raises(ValueError):
        turnover_attribution_report({"A": 1.0}, {})
