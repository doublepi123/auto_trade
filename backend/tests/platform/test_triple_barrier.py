from __future__ import annotations

import pytest

from app.platform.triple_barrier import triple_barrier_report


def test_triple_barrier_labels_long_profit_and_timeout():
    body = triple_barrier_report([100, 103, 101, 100], [{"index": 0, "side": "long"}, {"index": 2, "side": "long"}], profit_take_pct=0.02, stop_loss_pct=0.02, max_holding_bars=1).to_dict()
    assert body["labels"][0]["label"] == 1
    assert body["labels"][0]["hit"] == "profit_take"
    assert body["labels"][1]["hit"] == "timeout"


def test_triple_barrier_labels_short_profit_and_rejects_bad_params():
    body = triple_barrier_report([100, 98, 99], [{"index": 0, "side": "short"}], profit_take_pct=0.01, stop_loss_pct=0.03, max_holding_bars=2).to_dict()
    assert body["labels"][0]["label"] == 1
    with pytest.raises(ValueError):
        triple_barrier_report([100], [{"index": 0}], profit_take_pct=0.0)
    with pytest.raises(ValueError):
        triple_barrier_report([0, 1], [{"index": 0}])
    with pytest.raises(ValueError):
        triple_barrier_report([100, 101], [{"index": 0}], profit_take_pct=float("nan"))
