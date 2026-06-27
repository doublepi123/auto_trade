from __future__ import annotations

import pytest

from app.platform.factor_neutralization import neutralize_factor


def test_factor_neutralization_group_demean_zeroes_group_means():
    body = neutralize_factor({"A": 2.0, "B": 4.0, "C": 10.0, "D": 14.0}, method="group_demean", groups={"A": "x", "B": "x", "C": "y", "D": "y"}).to_dict()
    assert body["neutralized"]["A"] == pytest.approx(-1.0)
    assert body["group_means_after"]["x"] == pytest.approx(0.0)
    assert body["group_means_after"]["y"] == pytest.approx(0.0)


def test_factor_neutralization_rejects_key_mismatch():
    with pytest.raises(ValueError):
        neutralize_factor({"A": 1.0}, method="group_demean", groups={"B": "x"})
    with pytest.raises(ValueError):
        neutralize_factor({"A": 1.0, "B": 2.0}, method="residualize", exposures={"A": 1.0, "B": {"x": 2.0}})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        neutralize_factor({"A": 1.0, "B": 2.0}, method="residualize", exposures={"A": {"x": 1.0}, "B": {"y": 2.0}})
    with pytest.raises(ValueError):
        neutralize_factor({"A": 1.0, "B": 2.0}, method="residualize", exposures="AB")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        neutralize_factor({"A": 1.0, "B": 2.0}, method="residualize", exposures={"A": {"x": float("nan")}, "B": {"x": 2.0}})


def test_factor_neutralization_residualize_removes_linear_exposure():
    body = neutralize_factor({"A": 1.0, "B": 2.0, "C": 3.0}, method="residualize", exposures={"A": {"x": 1.0}, "B": {"x": 2.0}, "C": {"x": 3.0}}).to_dict()
    assert body["neutralized"]["A"] == pytest.approx(0.0, abs=1e-9)
    assert body["neutralized"]["B"] == pytest.approx(0.0, abs=1e-9)
    assert body["neutralized"]["C"] == pytest.approx(0.0, abs=1e-9)
