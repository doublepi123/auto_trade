from __future__ import annotations

import pytest

from app.platform.sample_uniqueness import sample_uniqueness_report


def test_sample_uniqueness_drops_when_events_overlap():
    body = sample_uniqueness_report([
        {"id": "a", "start": 0, "end": 2, "return": 0.02},
        {"id": "b", "start": 1, "end": 3, "return": -0.01},
    ]).to_dict()
    assert body["concurrency"] == [1, 2, 2, 1]
    assert body["average_uniqueness"] < 1.0


def test_sample_uniqueness_rejects_invalid_range():
    with pytest.raises(ValueError):
        sample_uniqueness_report([{"id": "x", "start": 3, "end": 1}])
    with pytest.raises(ValueError):
        sample_uniqueness_report(["bad"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        sample_uniqueness_report([{"id": "x", "start": 0, "end": 10001}])
    with pytest.raises(ValueError):
        sample_uniqueness_report([{"id": "x", "start": 0, "end": 1}], time_decay="x")  # type: ignore[arg-type]
