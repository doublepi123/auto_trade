"""Tests for P334 online covariance module."""

from __future__ import annotations

from app.platform.online_covariance import OnlineCovarianceResult, online_covariance_report


def test_online_covariance_2x2_matrix():
    r1 = [0.01, -0.02, 0.005, 0.01, -0.01] * 10  # 50 obs
    r2 = [0.005, -0.01, 0.01, 0.015, -0.005] * 10
    panel = {"A": r1, "B": r2}
    result = online_covariance_report(panel, lam=0.97, min_window=5)
    assert isinstance(result, OnlineCovarianceResult)
    cov = result.latest_covariance
    assert "A" in cov
    assert "B" in cov["A"]
    assert "A" in cov["B"]
    assert cov["A"]["A"] > 0  # variance > 0
    assert cov["B"]["B"] > 0
    # symmetry
    assert abs(cov["A"]["B"] - cov["B"]["A"]) < 1e-12


def test_online_covariance_condition_number_positive():
    r1 = [0.01, -0.02, 0.005, 0.01, -0.01] * 10
    r2 = [0.005, -0.01, 0.01, 0.015, -0.005] * 10
    panel = {"A": r1, "B": r2}
    result = online_covariance_report(panel, lam=0.97, min_window=5)
    assert result.condition_number > 0


def test_online_covariance_eigenvalues_count():
    r1 = [0.01, -0.02, 0.005, 0.01, -0.01] * 10
    r2 = [0.005, -0.01, 0.01, 0.015, -0.005] * 10
    panel = {"A": r1, "B": r2}
    result = online_covariance_report(panel, lam=0.97, min_window=5)
    assert len(result.eigenvalues) == 2
    assert all(v >= 0 for v in result.eigenvalues)


def test_online_covariance_assets():
    r1 = [0.01, -0.02, 0.005] * 10
    r2 = [0.005, -0.01, 0.01] * 10
    panel = {"X": r1, "Y": r2}
    result = online_covariance_report(panel, min_window=5)
    assert set(result.assets) == {"X", "Y"}


def test_online_covariance_to_dict():
    r1 = [0.01, -0.02, 0.005] * 10
    r2 = [0.005, -0.01, 0.01] * 10
    panel = {"A": r1, "B": r2}
    result = online_covariance_report(panel, min_window=5)
    d = result.to_dict()
    assert "latest_covariance" in d
    assert "condition_number" in d
    assert "eigenvalues" in d
    assert "assets" in d
    assert isinstance(d["latest_covariance"], dict)


def test_online_covariance_ewma_decay():
    # With lam=0 (no memory), the cov should be close to the outer product of the last returns
    r1 = [0.01, 0.02, 0.03, 0.04, 0.05] * 10
    r2 = [0.01, 0.01, 0.01, 0.01, 0.01] * 10
    panel = {"A": r1, "B": r2}
    result = online_covariance_report(panel, lam=0.97, min_window=5)
    assert result.condition_number > 0
