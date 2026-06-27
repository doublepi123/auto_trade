from __future__ import annotations

import pytest

from app.platform.event_study import event_study_report


def test_event_study_reports_car_and_significance():
    market = [0.0] * 10
    stock = [0.0, 0.0, 0.0, 0.009, 0.01, 0.01, 0.01, 0.01, 0.0, 0.0]
    body = event_study_report(market, stock, event_indices=[5], window_before=2, window_after=2).to_dict()
    assert body["events"][0]["car"] > 0
    assert body["events"][0]["t_stat"] > 2


def test_event_study_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        event_study_report([0.01], [0.01, 0.02], event_indices=[0])
