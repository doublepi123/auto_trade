"""Tests for P315 correlation risk premium diagnostics."""

from __future__ import annotations

import pytest

from app.platform.correlation_risk_premium import correlation_risk_premium_report


def test_implied_above_realized_positive_crp():
    realized = [0.3, 0.32, 0.28, 0.31, 0.29, 0.33, 0.30, 0.31, 0.29, 0.32]
    implied = [0.5, 0.52, 0.48, 0.51, 0.49, 0.53, 0.50, 0.51, 0.49, 0.52]
    result = correlation_risk_premium_report(realized, implied)
    body = result.to_dict()
    assert body["crp"] > 0
    assert body["z_score"] > 0
    assert body["regime"] in {"rich", "normal", "cheap"}


def test_implied_below_realized_negative_crp():
    realized = [0.5, 0.52, 0.48, 0.51, 0.49, 0.53, 0.50, 0.51, 0.49, 0.52]
    implied = [0.3, 0.32, 0.28, 0.31, 0.29, 0.33, 0.30, 0.31, 0.29, 0.32]
    result = correlation_risk_premium_report(realized, implied)
    body = result.to_dict()
    assert body["crp"] < 0


def test_equal_series_crp_near_zero():
    data = [0.4] * 10
    result = correlation_risk_premium_report(data, data)
    body = result.to_dict()
    assert abs(body["crp"]) < 1e-9


def test_single_element():
    with pytest.raises(ValueError):
        correlation_risk_premium_report([0.3], [0.5])


def test_unequal_length_raises():
    with pytest.raises(ValueError):
        correlation_risk_premium_report([0.3, 0.4], [0.5])


def test_empty_series_raises():
    with pytest.raises(ValueError):
        correlation_risk_premium_report([], [0.5])


def test_rejects_non_finite():
    with pytest.raises(ValueError):
        correlation_risk_premium_report([0.3, float("nan")], [0.5, 0.6])
