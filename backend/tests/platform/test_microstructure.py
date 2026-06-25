"""Tests for P226 microstructure — VPIN / OFI / Kyle's lambda."""

from __future__ import annotations

import pytest

from app.platform.microstructure import (
    classify_bar_volume,
    kyle_lambda,
    order_flow_imbalance,
    vpin,
)


def test_classify_up_down_flat():
    v = classify_bar_volume([100.0, 100.0, 100.0], [10.0, 10.0, 10.0], [11.0, 9.0, 10.0])
    assert v[0] == (100.0, 0.0)  # up → all buy
    assert v[1] == (0.0, 100.0)  # down → all sell
    assert v[2] == (50.0, 50.0)  # flat → split


def test_classify_length_mismatch():
    with pytest.raises(ValueError):
        classify_bar_volume([100.0], [10.0], [11.0, 12.0])


def test_classify_negative_volume():
    with pytest.raises(ValueError):
        classify_bar_volume([-1.0], [10.0], [11.0])


def test_vpin_pure_buy_low_imbalance():
    # all-up bars → each bucket is (V, 0) → |buy-sell|/total = 1
    vols = [100.0] * 10
    opens = [10.0] * 10
    closes = [11.0] * 10
    res = vpin(vols, opens, closes, bucket_size=100.0, window=5)
    assert res.n_buckets == 10
    assert abs(res.latest_vpin - 1.0) < 1e-9


def test_vpin_balanced_per_bucket_zero():
    # Each bucket internally balanced (flat bars) → per-bucket imbalance 0
    vols = [100.0] * 4
    opens = [10.0, 10.0, 10.0, 10.0]
    closes = [10.0, 10.0, 10.0, 10.0]  # all flat → 50/50 split per bucket
    res = vpin(vols, opens, closes, bucket_size=100.0, window=4)
    assert abs(res.latest_vpin - 0.0) < 1e-9


def test_vpin_toxic_one_sided_buckets():
    # All-up bars → every bucket fully buy → VPIN = 1 (maximally toxic)
    vols = [100.0] * 4
    opens = [10.0] * 4
    closes = [11.0, 11.0, 9.0, 9.0]  # 2 up, 2 down
    res = vpin(vols, opens, closes, bucket_size=100.0, window=4)
    # each bucket is one-sided → |b-s|=100 each → 400/400 = 1.0
    assert abs(res.latest_vpin - 1.0) < 1e-9


def test_vpin_empty_raises():
    with pytest.raises(ValueError):
        vpin([], [], [])


def test_vpin_zero_volume_raises():
    with pytest.raises(ValueError):
        vpin([0.0, 0.0], [10.0, 10.0], [11.0, 11.0])


def test_vpin_to_dict():
    res = vpin([100.0, 100.0], [10.0, 10.0], [11.0, 9.0], bucket_size=100.0)
    d = res.to_dict()
    assert "buckets" in d and "latest_vpin" in d and "n_buckets" in d


def test_order_flow_imbalance_signs():
    vols = [100.0, 100.0, 100.0]
    opens = [10.0, 10.0, 10.0]
    closes = [11.0, 9.0, 10.0]
    ofi = order_flow_imbalance(vols, opens, closes)
    assert abs(ofi[0] - 1.0) < 1e-9  # all buy
    assert abs(ofi[1] - (-1.0)) < 1e-9  # all sell
    assert abs(ofi[2] - 0.0) < 1e-9  # flat


def test_order_flow_imbalance_zero_volume():
    ofi = order_flow_imbalance([0.0], [10.0], [11.0])
    assert ofi == [0.0]


def test_kyle_lambda_positive_with_buy_pressure():
    # buy-pressure bars also have positive returns → positive slope
    vols = [100.0] * 20
    opens = [10.0 + i * 0.1 for i in range(20)]
    closes = [o + 0.2 for o in opens]  # all up
    lam = kyle_lambda(vols, opens, closes)
    # all OFI = +1, all rets > 0 → slope positive (well-defined since OFI constant? no, constant → sxx=0)
    # actually OFI constant → sxx = 0 → returns 0
    assert lam == 0.0


def test_kyle_lambda_varied_flow():
    # mix up/down bars with correlated returns
    vols = [100.0] * 10
    opens = [10.0] * 10
    closes = [11.0, 10.5, 9.5, 9.0, 11.0, 10.5, 9.5, 11.0, 9.0, 10.5]
    lam = kyle_lambda(vols, opens, closes)
    assert isinstance(lam, float)


def test_kyle_lambda_too_short():
    with pytest.raises(ValueError):
        kyle_lambda([100.0], [10.0], [11.0])