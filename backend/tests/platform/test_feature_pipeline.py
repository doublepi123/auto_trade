from __future__ import annotations

import pytest

from app.platform.feature_pipeline import run_feature_pipeline


def test_feature_pipeline_generates_return_and_rank():
    body = run_feature_pipeline({"A": [100, 110, 121], "B": [100, 90, 81]}, [{"name": "ret", "op": "return", "window": 1}, {"name": "rank_ret", "op": "rank", "input": "ret"}]).to_dict()
    assert body["features"]["ret"]["A"][1] == pytest.approx(0.1)
    assert body["features"]["rank_ret"]["A"][1] > body["features"]["rank_ret"]["B"][1]


def test_feature_pipeline_rejects_unknown_op():
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [1, 2]}, [{"name": "x", "op": "eval"}])
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [1, 2]}, ["bad"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [1, 2]}, [{"name": "x", "op": "return", "window": 0}])
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [1, 2]}, [{"name": "x", "op": "return", "window": True}])
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [1, float("nan")]}, [{"name": "x", "op": "return"}])
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": [True, 2]}, [{"name": "x", "op": "return"}])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": 1}, [{"name": "x", "op": "return"}])  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        run_feature_pipeline({"A": []}, [{"name": "x", "op": "return"}])


def test_feature_pipeline_lag_keeps_length_when_window_exceeds_series():
    body = run_feature_pipeline({"A": [1, 2]}, [{"name": "lagged", "op": "lag", "window": 3}]).to_dict()
    assert body["length"] == 2
    assert body["features"]["lagged"]["A"] == [None, None]
