"""Tests for P214 Combinatorial Purged Cross-Validation (CPCV) splitter."""

from __future__ import annotations

import pytest

from app.platform.cpcv import (
    CpcvConfig,
    cpcv_oos_paths,
    cpcv_pbo,
    cpcv_split,
    cpcv_split_indices,
    cpcv_summary,
)


def test_cpcv_basic_coverage():
    # 5 groups of size 2 over 10 samples, hold out 1 group per split -> 5 splits.
    cfg = CpcvConfig(n_groups=5, k_test=1)
    splits = cpcv_split(10, cfg)
    assert len(splits) == 5
    # union of all test_idx == [0..10) exactly once.
    all_test = sorted(i for s in splits for i in s.test_idx)
    assert all_test == list(range(10))


def test_cpcv_combinatorial_count():
    cfg = CpcvConfig(n_groups=4, k_test=2)
    splits = cpcv_split(12, cfg)
    assert len(splits) == 6  # C(4,2)
    for s in splits:
        assert set(s.train_idx).isdisjoint(set(s.test_idx))


def test_cpcv_purge_strips_boundary():
    # 4 groups of size 2: g0=[0,1] g1=[2,3] g2=[4,5] g3=[6,7]
    cfg = CpcvConfig(n_groups=4, k_test=1, purge=1, embargo=0)
    splits = cpcv_split(8, cfg)
    # find the split where test group is g1 (indices 2,3)
    target = next(s for s in splits if s.test_idx == (2, 3))
    assert 1 not in target.train_idx  # purged before run
    assert 4 not in target.train_idx  # purged after run
    assert set(target.test_idx) == {2, 3}


def test_cpcv_embargo_post_only():
    cfg = CpcvConfig(n_groups=4, k_test=1, purge=0, embargo=2)
    splits = cpcv_split(8, cfg)
    target = next(s for s in splits if s.test_idx == (2, 3))
    # embargo drops 4,5 (post-test); index 1 kept (embargo is post-only).
    assert 1 in target.train_idx
    assert 4 not in target.train_idx
    assert 5 not in target.train_idx


def test_cpcv_purge_plus_embargo():
    # 4 groups over 12: g0=[0,1,2] g1=[3,4,5] g2=[6,7,8] g3=[9,10,11]
    cfg = CpcvConfig(n_groups=4, k_test=1, purge=1, embargo=1)
    splits = cpcv_split(12, cfg)
    target = next(s for s in splits if s.test_idx == (3, 4, 5))
    # purge removes 2 (before) and 6 (after); embargo removes 6 (after) too.
    assert 2 not in target.train_idx
    assert 6 not in target.train_idx
    assert 3 not in target.train_idx  # test index never in train


def test_cpcv_determinism():
    cfg = CpcvConfig(n_groups=5, k_test=2, purge=1, embargo=1)
    a = cpcv_split(12, cfg)
    b = cpcv_split(12, cfg)
    assert a == b


def test_cpcv_oos_paths_disjoint_within_path():
    cfg = CpcvConfig(n_groups=6, k_test=3)
    paths = cpcv_oos_paths(12, cfg)
    assert paths
    for path in paths:
        # no index duplicated within a single path
        assert len(path) == len(set(path))


def test_cpcv_summary_counts():
    summary = cpcv_summary(12, CpcvConfig(n_groups=4, k_test=2, purge=0, embargo=0))
    assert summary["n_splits"] == 6
    assert summary["n_groups"] == 4
    assert summary["k_test"] == 2
    assert summary["coverage_mean"] == pytest.approx(0.5)


def test_cpcv_split_indices_lists():
    cfg = CpcvConfig(n_groups=5, k_test=1)
    idx = cpcv_split_indices(10, cfg)
    assert len(idx) == 5
    assert all(isinstance(t, list) and isinstance(o, list) for t, o in idx)


def test_cpcv_pbo_compose():
    panel = [
        [0.05, 0.05, 0.05, 0.05, -0.05, -0.05, -0.05, -0.05, 0.02, 0.01,
         0.0, 0.0, 0.01, 0.0, -0.01, 0.0, 0.0, 0.01, 0.0, 0.0],
        [-0.05, -0.05, -0.05, -0.05, 0.05, 0.05, 0.05, 0.05, -0.01, 0.0,
         0.01, 0.0, -0.01, 0.0, 0.01, 0.0, 0.0, -0.01, 0.0, 0.0],
    ]
    res = cpcv_pbo(panel, CpcvConfig(n_groups=4, k_test=2, purge=1, embargo=1))
    assert "pbo" in res and "logit_mean" in res and "n_splits" in res
    assert res["n_splits"] > 0
    assert 0.0 <= res["pbo"] <= 1.0


def test_cpcv_invalid_raises():
    with pytest.raises(ValueError):
        cpcv_split(5, CpcvConfig(n_groups=1, k_test=1))
    with pytest.raises(ValueError):
        cpcv_split(5, CpcvConfig(n_groups=4, k_test=0))
    with pytest.raises(ValueError):
        cpcv_split(5, CpcvConfig(n_groups=4, k_test=4))
    with pytest.raises(ValueError):
        cpcv_split(5, CpcvConfig(n_groups=4, k_test=1, purge=-1))


def test_cpcv_zero_samples_empty():
    assert cpcv_split(0, CpcvConfig(n_groups=2, k_test=1)) == []


def test_cpcv_large_purge_can_empty_train():
    # purge large enough to strip all train bars around the single test group.
    cfg = CpcvConfig(n_groups=4, k_test=1, purge=100, embargo=0)
    splits = cpcv_split(8, cfg)
    # at least one split should have an empty (documented) train set.
    assert any(len(s.train_idx) == 0 for s in splits)