"""Tests for P310 volume_profile module."""

from __future__ import annotations

import pytest

from app.platform.volume_profile import volume_profile_report


def test_volume_profile_poc_and_value_area():
    """Known data: prices 1..10, volumes 1..10 → POC = bin with 9,10 (volume=19)."""
    prices = list(range(1, 11))  # 1..10
    volumes = list(range(1, 11))  # 1..10
    result = volume_profile_report(prices, volumes, bins=5)
    d = result.to_dict()

    assert "poc_price" in d
    assert "value_area_low" in d
    assert "value_area_high" in d
    assert "bins" in d
    assert len(d["bins"]) == 5

    # POC should be the midpoint of the bin with highest volume
    # With prices 1..10 and 5 bins: [1,2.8), [2.8,4.6), [4.6,6.4), [6.4,8.2), [8.2,10]
    # Volumes: price 1→1, 2→2, 3→3, 4→4, 5→5, 6→6, 7→7, 8→8, 9→9, 10→10
    # Bin 5 (8.2-10): prices 9,10 → volumes 9+10=19 → highest → POC=9.1
    assert d["poc_price"] == pytest.approx(9.1, abs=0.01)

    # Value area should cover ~70% of total volume
    assert d["value_area_low"] <= d["poc_price"] <= d["value_area_high"]
    # Total volume = 55, 70% = 38.5, should span several bins
    assert d["value_area_low"] < d["value_area_high"]


def test_volume_profile_single_bin():
    prices = [10.0, 12.0, 11.0]
    volumes = [100.0, 200.0, 150.0]
    result = volume_profile_report(prices, volumes, bins=1)
    d = result.to_dict()
    assert len(d["bins"]) == 1
    # One bin covers entire range
    assert d["poc_price"] == 11.0
    assert d["value_area_low"] == 10.0
    assert d["value_area_high"] == 12.0


def test_volume_profile_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        volume_profile_report([1.0, 2.0], [100.0], bins=5)


def test_volume_profile_rejects_empty():
    with pytest.raises(ValueError):
        volume_profile_report([], [], bins=5)


def test_volume_profile_rejects_invalid_bins():
    with pytest.raises(ValueError):
        volume_profile_report([1.0, 2.0], [100.0, 200.0], bins=0)
    with pytest.raises(ValueError):
        volume_profile_report([1.0, 2.0], [100.0, 200.0], bins=-1)
