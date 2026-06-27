from __future__ import annotations

import pytest

from app.platform.ensemble_blending import ensemble_blending_report


def test_ensemble_blending_favors_more_accurate_model():
    body = ensemble_blending_report({"good": [1, 2, 3], "bad": [3, 2, 1]}, [1, 2, 3]).to_dict()
    assert body["weights"]["good"] > body["weights"]["bad"]
    assert body["ensemble_r2"] >= body["model_scores"]["bad"]["r2"]


def test_ensemble_blending_rejects_length_mismatch():
    with pytest.raises(ValueError):
        ensemble_blending_report({"m": [1, 2]}, [1, 2, 3])
    with pytest.raises(ValueError):
        ensemble_blending_report({"m": [1, 2]}, [1, 2], redundancy_threshold="x")  # type: ignore[arg-type]
