"""P214: Combinatorial Purged Cross-Validation (CPCV) splitter.

López de Prado, *Advances in Financial Machine Learning* (2018), Ch.7
(Cross-Validation for Finance) & Ch.11 (Backtesting Overfitting). CPCV is the
purge+embargo-aware answer to the label-induced leakage that defeats ordinary
k-fold / walk-forward CV on financially time-series.

Given ``n_samples`` observations split into ``n_groups`` contiguous groups, for
every combination of ``k_test`` groups held out as the out-of-sample (OOS) test
path:

* the remaining ``n_groups - k_test`` groups form the in-sample (IS) train set,
* ``purge`` bars on **both** sides of each test group are stripped from the
  train set (labels overlapping the train/test boundary are removed), and
* ``embargo`` bars **after** each test group are held out of train (kills the
  serial-autocorrelation leakage just past the test window).

Enumerating every ``C(n_groups, k_test)`` OOS path gives combinatorial coverage
— every group is OOS in many splits — without the single-split fragility of
walk-forward. Deterministic (no RNG); pure stdlib (``itertools.combinations``).

Composes with the existing :mod:`app.platform.overfitting` PBO diagnostic: a
``cpcv_pbo`` helper maps each split to IS/OOS return slices and reuses
:func:`probability_of_backtest_overfitting` for the aggregate overfitting
probability, so CPCV plugs into the existing diagnostics without duplicating
CSCV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from app.platform.overfitting import probability_of_backtest_overfitting

__all__ = [
    "CpcvConfig",
    "CpcvSplit",
    "cpcv_split",
    "cpcv_split_indices",
    "cpcv_summary",
    "cpcv_oos_paths",
    "cpcv_pbo",
]


@dataclass(frozen=True)
class CpcvConfig:
    """CPCV splitter configuration.

    ``n_groups`` contiguous near-equal groups; ``k_test`` held out per split
    (``1 <= k_test < n_groups``); ``purge`` bars stripped symmetrically around
    each test group; ``embargo`` bars held out after each test group.
    """

    n_groups: int
    k_test: int
    purge: int = 0
    embargo: int = 0


@dataclass(frozen=True)
class CpcvSplit:
    """One IS/OOS split: train indices and test indices."""

    train_idx: tuple[int, ...]
    test_idx: tuple[int, ...]


def _group_spans(n_samples: int, n_groups: int) -> list[tuple[int, int]]:
    """Partition ``[0, n_samples)`` into ``n_groups`` contiguous near-equal spans.

    Returns a list of ``(lo, hi)`` half-open intervals. The first ``R =
    n_samples % n_groups`` groups get one extra element (same rule as
    :mod:`app.platform.overfitting` block construction).
    """
    if n_groups <= 0:
        raise ValueError("n_groups must be >= 1")
    base = n_samples // n_groups
    rem = n_samples % n_groups
    spans: list[tuple[int, int]] = []
    start = 0
    for g in range(n_groups):
        size = base + (1 if g < rem else 0)
        spans.append((start, start + size))
        start += size
    return spans


def _validate(n_samples: int, config: CpcvConfig) -> None:
    if n_samples < 0:
        raise ValueError("n_samples must be >= 0")
    if config.n_groups < 2:
        raise ValueError("n_groups must be >= 2")
    if config.n_groups > n_samples:
        raise ValueError("n_groups must be <= n_samples")
    if config.k_test < 1 or config.k_test >= config.n_groups:
        raise ValueError("k_test must be in [1, n_groups-1]")
    if config.purge < 0 or config.embargo < 0:
        raise ValueError("purge and embargo must be >= 0")


def cpcv_split(n_samples: int, config: CpcvConfig) -> list[CpcvSplit]:
    """Enumerate all ``C(n_groups, k_test)`` IS/OOS splits.

    Each split's ``test_idx`` is the union of the chosen test groups; its
    ``train_idx`` is the remaining groups minus any purge/embargo bars adjacent
    to a test span. Deterministic; pure stdlib. Empty list when ``n_samples``
    is 0.
    """
    if n_samples == 0:
        return []
    _validate(n_samples, config)
    spans = _group_spans(n_samples, config.n_groups)
    group_indices = [list(range(lo, hi)) for lo, hi in spans]

    splits: list[CpcvSplit] = []
    for test_groups in combinations(range(config.n_groups), config.k_test):
        test_set = set(test_groups)
        test_idx = sorted(i for g in test_groups for i in group_indices[g])

        # Contiguous runs within test_idx (a test "group" may span several
        # adjacent selected groups). Purge + embargo apply around each run.
        runs: list[tuple[int, int]] = []
        if test_idx:
            run_start = test_idx[0]
            prev = test_idx[0]
            for x in test_idx[1:]:
                if x == prev + 1:
                    prev = x
                    continue
                runs.append((run_start, prev + 1))
                run_start = x
                prev = x
            runs.append((run_start, prev + 1))

        # Forbidden index set: purge bars (symmetric around each run) + embargo
        # bars (after each run). Indices embargoed/purged from this split's
        # train are still eligible as test indices in other splits.
        forbidden = set()
        for rlo, rhi in runs:
            for i in range(max(rlo - config.purge, 0), min(rhi + config.purge, n_samples)):
                forbidden.add(i)
            for i in range(rhi, min(rhi + config.embargo, n_samples)):
                forbidden.add(i)

        train_idx = sorted(
            i for g in range(config.n_groups)
            if g not in test_set
            for i in group_indices[g]
            if i not in forbidden
        )

        splits.append(CpcvSplit(train_idx=tuple(train_idx), test_idx=tuple(test_idx)))
    return splits


def cpcv_split_indices(
    n_samples: int, config: CpcvConfig
) -> list[tuple[list[int], list[int]]]:
    """Convenience: list of ``(train_idx, test_idx)`` as plain lists."""
    return [
        (list(s.train_idx), list(s.test_idx)) for s in cpcv_split(n_samples, config)
    ]


def cpcv_summary(n_samples: int, config: CpcvConfig) -> dict[str, Any]:
    """Aggregate counts + per-split size min/max/mean for sanity-checking.

    ``n_splits`` is ``math.comb(n_groups, k_test)`` (computed without
    materializing the splits, so callers can preview cost). ``coverage_mean``
    is the average fraction of splits in which a given index is OOS — for
    symmetric CPCV this is ``k_test / n_groups``.
    """
    if n_samples == 0:
        return {
            "n_splits": 0,
            "n_groups": config.n_groups,
            "k_test": config.k_test,
            "purge": config.purge,
            "embargo": config.embargo,
            "train_min": 0,
            "train_max": 0,
            "test_min": 0,
            "test_max": 0,
            "coverage_mean": 0.0,
        }
    _validate(n_samples, config)
    n_splits = math.comb(config.n_groups, config.k_test)
    coverage = config.k_test / config.n_groups
    # cheap train/test size bounds without enumerating (when purge/embargo=0).
    base = n_samples // config.n_groups
    rem = n_samples % config.n_groups
    test_min = config.k_test * base
    test_max = config.k_test * (base + (1 if rem else 0))
    train_min = n_samples - test_max - 2 * config.purge * config.k_test - config.embargo * config.k_test
    train_max = n_samples - test_min
    train_min = max(train_min, 0)
    return {
        "n_splits": n_splits,
        "n_groups": config.n_groups,
        "k_test": config.k_test,
        "purge": config.purge,
        "embargo": config.embargo,
        "train_min": int(train_min),
        "train_max": int(train_max),
        "test_min": int(test_min),
        "test_max": int(test_max),
        "coverage_mean": coverage,
    }


def cpcv_oos_paths(n_samples: int, config: CpcvConfig) -> list[list[int]]:
    """Pack OOS test spans into the minimum number of disjoint backtest paths.

    Greedy interval-graph coloring (deterministic, lexicographic): each path
    is a union of non-overlapping test spans. Approximates López de Prado's
    ``N - k + 1`` OOS paths for the canonical case; greedy is deterministic and
    sufficient for backtest-path construction.
    """
    if n_samples == 0:
        return []
    _validate(n_samples, config)
    spans = _group_spans(n_samples, config.n_groups)
    paths: list[list[tuple[int, int]]] = []
    for test_groups in combinations(range(config.n_groups), config.k_test):
        test_spans = sorted(spans[g] for g in test_groups)
        placed = False
        for path in paths:
            # a span fits in a path if it does not overlap any existing span
            if all(hi <= test_spans[0][0] or test_spans[-1][1] <= lo for lo, hi in path):
                path.extend(test_spans)
                placed = True
                break
        if not placed:
            paths.append(list(test_spans))
    # flatten each path's spans to a sorted index list
    return [
        sorted(i for lo, hi in path for i in range(lo, hi)) for path in paths
    ]


def cpcv_pbo(
    returns_panel: list[list[float]],
    config: CpcvConfig,
    block_size: int = 0,
) -> dict[str, float]:
    """Aggregate PBO over CPCV splits of a per-strategy returns panel.

    Maps each split's IS/OOS index slices to the per-strategy return subseries
    and calls :func:`probability_of_backtest_overfitting` on the assembled
    panel of OOS slices (CSCV over the CPCV OOS paths). Returns the existing
    ``{pbo, logit_mean, n_splits}`` shape.
    """
    if not returns_panel:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    length = min(len(r) for r in returns_panel)
    if length < 4:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    panel = [list(r[:length]) for r in returns_panel]
    splits = cpcv_split(length, config)
    if not splits:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    # Build an OOS-panel per split: each "strategy" column is the concatenated
    # OOS return slice; feed the resulting panel to PBO. The aggregate PBO is
    # the fraction of splits whose IS-winner underperforms the OOS median.
    oos_panels: list[list[list[float]]] = []
    for s in splits:
        if not s.test_idx:
            continue
        oos_col = [[panel[strat][i] for i in s.test_idx] for strat in range(len(panel))]
        oos_panels.append(oos_col)
    if not oos_panels:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    # Average PBO across per-split panels (each computed independently).
    pbo_sum = 0.0
    logit_sum = 0.0
    n = 0
    for oos_col in oos_panels:
        if len(oos_col[0]) < 4:
            continue
        res = probability_of_backtest_overfitting(oos_col, block_size=block_size)
        if res["n_splits"] > 0:
            pbo_sum += res["pbo"]
            logit_sum += res["logit_mean"]
            n += 1
    if n == 0:
        return {"pbo": 0.0, "logit_mean": 0.0, "n_splits": 0}
    return {"pbo": pbo_sum / n, "logit_mean": logit_sum / n, "n_splits": n}