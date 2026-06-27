"""Tests for P316 volatility term structure diagnostics."""

from __future__ import annotations

import pytest

from app.platform.vol_term_structure import vol_term_structure_report


def test_contango_slope_positive():
    # Far-dated IV > near-dated IV => contango
    options = [
        {"expiry": 30, "iv": 0.20},
        {"expiry": 60, "iv": 0.22},
        {"expiry": 90, "iv": 0.25},
        {"expiry": 180, "iv": 0.28},
    ]
    result = vol_term_structure_report(options, spot=100.0)
    body = result.to_dict()
    assert body["slope"] > 0
    assert body["label"] == "contango"
    assert len(body["per_expiry"]) == 4


def test_backwardation_slope_negative():
    # Near-dated IV > far-dated IV => backwardation
    options = [
        {"expiry": 30, "iv": 0.30},
        {"expiry": 60, "iv": 0.27},
        {"expiry": 90, "iv": 0.24},
        {"expiry": 180, "iv": 0.20},
    ]
    result = vol_term_structure_report(options, spot=100.0)
    body = result.to_dict()
    assert body["slope"] < 0
    assert body["label"] == "backwardation"


def test_flat_term_structure():
    options = [
        {"expiry": 30, "iv": 0.25},
        {"expiry": 60, "iv": 0.25},
        {"expiry": 90, "iv": 0.25},
    ]
    result = vol_term_structure_report(options, spot=100.0)
    body = result.to_dict()
    assert body["slope"] == 0.0
    assert body["label"] == "flat"


def test_single_option_flat():
    result = vol_term_structure_report([{"expiry": 30, "iv": 0.25}], spot=100.0)
    body = result.to_dict()
    assert body["slope"] == 0.0
    assert body["label"] == "flat"
    assert len(body["per_expiry"]) == 1


def test_options_sorted_by_expiry():
    # Input out of order — module sorts internally
    options = [
        {"expiry": 180, "iv": 0.28},
        {"expiry": 30, "iv": 0.20},
        {"expiry": 90, "iv": 0.25},
    ]
    result = vol_term_structure_report(options, spot=100.0)
    body = result.to_dict()
    expiries = [e["expiry"] for e in body["per_expiry"]]
    assert expiries == sorted(expiries)


def test_empty_options_raises():
    with pytest.raises(ValueError):
        vol_term_structure_report([], spot=100.0)


def test_rejects_non_list():
    with pytest.raises(ValueError):
        vol_term_structure_report("not a list", spot=100.0)  # type: ignore[arg-type]


def test_rejects_invalid_option_entry():
    with pytest.raises(ValueError):
        vol_term_structure_report([{"expiry": 30, "iv": float("nan")}], spot=100.0)


def test_negative_spot_raises():
    with pytest.raises(ValueError):
        vol_term_structure_report([{"expiry": 30, "iv": 0.20}], spot=-100.0)
