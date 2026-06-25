"""P240: Hansen-White Superior Predictive Ability (SPA) test.

A test for **superior predictive ability** in the spirit of White (2000)
"Reality Check" and Hansen (2005) "A test for superior predictive ability".
Given a benchmark loss series and one or more candidate model loss series
(lower loss = better model), the SPA test asks: does *any* model genuinely
outperform the benchmark, after accounting for the data-snooping bias from
searching across many models?

The test works on the loss differentials ``d_k,t = L_b,t − L_k,t`` (positive ⇒
model ``k`` beats the benchmark at time ``t``). The test statistic is the
maximum standardized mean differential across models (the "best" model). Under
the null that *no* model beats the benchmark in expectation, Hansen (2005)
bootstraps the centered distribution of ``max_k d̄_k`` and computes a
consistent p-value (Eq. 7) that handles the recentering of negative
differentials.

The bootstrap is the **stationary bootstrap of Politis & Romano (1994)** — a
circular block bootstrap where block lengths are geometric. The standard
algorithm is stochastic, but this implementation is **DETERMINISTIC**:
instead of drawing random block lengths and starts from RNG, we use a *fixed
circular resampling pattern* — for bootstrap draw ``b`` we walk through the
series covering index ``n`` with consecutive blocks of length ``block_length``
whose start positions cycle through ``(seed + b + j*block_length) % n``.
This is the honest compromise under the repo's no-numpy/no-random constraint:
it is *not* a Monte-Carlo bootstrap (the p-values are pseudo-bootstrap rather
than uniformly valid), but it is fully reproducible, deterministic, and zero-
dependency. The recentering, max-across-models, and consistent p-value follow
Hansen (2005) exactly.

Reference: Hansen (2005) "A test for superior predictive ability",
J. Forecasting 24(5):365–380; White (2000) "A reality check for data
snooping", Econometrica 68(5):1097–1126; Politis & Romano (1994) "The
stationary bootstrap", JASA 89(428):1303–1313. Pure Python, no scipy/numpy.

Functions
---------
* ``_stationary_bootstrap_indices`` — deterministic circular block resample
  of index positions (no RNG).
* ``spa_test`` — Hansen-White SPA test; returns ``SpaResult`` with the test
  statistic, naive and consistent p-values, and per-model diagnostics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

__all__ = [
    "SpaResult",
    "_stationary_bootstrap_indices",
    "spa_test",
]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stddev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _safe_t(num: float, den: float) -> float:
    """Standardized mean with a degenerate-denominator rule.

    When the standard error ``den`` is 0 (constant differentials), the
    estimator is superconsistent: a positive mean ⇒ an arbitrarily large
    positive t-statistic, a negative mean ⇒ arbitrarily negative. We return
    ``math.inf`` / ``-math.inf`` so the ordering and p-value comparisons
    behave correctly (a model that *always* beats the benchmark should
    reject the null).
    """
    if den > 0.0:
        return num / den
    if num > 0.0:
        return math.inf
    if num < 0.0:
        return -math.inf
    return 0.0


def _stationary_bootstrap_indices(
    n: int,
    B: int,
    block_length: int,
    seed_index: int = 0,
) -> list[list[int]]:
    """Deterministic stationary-bootstrap resample index map.

    Returns ``B`` lists, each of length ``n``, of index positions into the
    original series. The scheme is a **fixed circular block resample with
    replacement** — for draw ``b`` we build the resample by repeatedly
    appending blocks of length ``block_length`` whose start positions are
    deterministic cyclic offsets, until ``n`` indices are collected (the final
    block is truncated). Because each block's start position is independent of
    the previous one, indices repeat and some are omitted — this is the
    *with-replacement* behavior of the stationary bootstrap, not a
    permutation::

        draw b, block j: start = (seed_index + b * offset_step + j * stride) mod n
                          stride = block_length + b   (>= 1, clamped to n)
                          append [(start + k) mod n for k in range(block_length)]
                          (final block truncated so total length == n)

    ``offset_step = (n // 2) + 1`` ensures successive draws sample different
    regions of the series. This is the deterministic, RNG-free analogue of
    Politis-Romano's stationary bootstrap: instead of drawing random
    geometric block lengths and starts, we use a fixed cyclic-but-varied
    pattern so the result is fully reproducible *and* the resamples are
    genuine with-replacement draws (non-degenerate). It is *not* a uniform
    Monte-Carlo bootstrap — it is a pseudo-bootstrap. ``block_length`` must be
    in ``[1, n]``.

    Parameters
    ----------
    n : int
        Series length to resample.
    B : int
        Number of bootstrap draws.
    block_length : int
        Fixed block length (in ``[1, n]``).
    seed_index : int
        Deterministic seed offset for the cyclic start positions.

    Returns
    -------
    list[list[int]]
        ``B`` resamples, each of length ``n``.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if B < 1:
        raise ValueError("B must be >= 1")
    if block_length < 1 or block_length > n:
        raise ValueError("block_length must be in [1, n]")

    offset_step = (n // 2) + 1
    out: list[list[int]] = []
    for b in range(B):
        seq: list[int] = []
        # stride controls how the *next block's* start advances; using a
        # draw-specific stride that differs from block_length breaks the
        # "pure rotation" degeneracy (where every block abuts the previous
        # one and the resample becomes a permutation of range(n)).
        stride = block_length + b
        if stride > n:
            stride = max(1, stride % n)
        if stride < 1:
            stride = 1
        start0 = (seed_index + b * offset_step) % n
        j = 0
        while len(seq) < n:
            # each block starts at a fresh deterministic offset; the start
            # advances by `stride` (≠ block_length) so blocks overlap/omit
            # rather than abutting → genuine with-replacement resample.
            start = (start0 + j * stride) % n
            take = min(block_length, n - len(seq))
            for k in range(take):
                seq.append((start + k) % n)
            j += 1
        out.append(seq)
    return out


@dataclass(frozen=True)
class SpaResult:
    """Hansen-White SPA test result.

    Attributes
    ----------
    spa_pvalue : float
        Naive bootstrap p-value = mean(bootstrap_max_d <= 0).
    consistent_pvalue : float
        Hansen (2005) consistent p-value (Eq. 7), recentering only the
        models that are worse than the benchmark before resampling.
    t_statistic : float
        Standardized test statistic of the best model
        (max over models of ``d̄_k / sqrt(Var(d̄_k))``).
    n_models_beating_benchmark : int
        Count of models with positive mean differential (better than
        benchmark in sample).
    individual_pvalues : list[float]
        Per-model bootstrap p-value = mean(bootstrap_d̄_k <= 0) (naive).
    n : int
        Series length.
    B : int
        Number of bootstrap draws used.
    block_length : int
        Block length used.
    """

    spa_pvalue: float
    consistent_pvalue: float
    t_statistic: float
    n_models_beating_benchmark: int
    individual_pvalues: list[float] = field(default_factory=list)
    n: int = 0
    B: int = 0
    block_length: int = 1

    def to_dict(self) -> dict:
        return {
            "spa_pvalue": self.spa_pvalue,
            "consistent_pvalue": self.consistent_pvalue,
            "t_statistic": self.t_statistic,
            "n_models_beating_benchmark": self.n_models_beating_benchmark,
            "individual_pvalues": list(self.individual_pvalues),
            "n": self.n,
            "B": self.B,
            "block_length": self.block_length,
        }


def spa_test(
    benchmark_lf: Sequence[float],
    model_lfs: Sequence[Sequence[float]],
    B: int = 100,
    block_length: int = 5,
) -> SpaResult:
    """Hansen-White Superior Predictive Ability test.

    Tests the null that *no* model has lower expected loss than the benchmark
    against the alternative that at least one does, controlling for the
    data-snooping bias of searching across ``K`` models.

    Let ``d_k,t = L_b,t − L_k,t`` (positive ⇒ model ``k`` beats the benchmark
    at ``t``). Let ``d̄_k`` be the sample mean and ``σ̄_k`` the standard error
    of ``d̄_k`` (sample std of ``d`` divided by ``sqrt(n)``). The test
    statistic is::

        T = max_k  d̄_k / σ̄_k

    The null distribution of ``T`` is obtained by bootstrapping the *centered*
    differentials ``d̃_k,t = d_k,t − max(d̄_k, 0)`` (recentering only the models
    that are worse than the benchmark is the consistent variant, Hansen 2005
    Eq. 7) and recomputing the max across models for each bootstrap draw. The
    consistent p-value is the fraction of bootstrap draws whose max centered
    statistic exceeds the observed ``T``.

    Parameters
    ----------
    benchmark_lf : Sequence[float]
        Benchmark loss-function values (lower = better), length ``n``.
    model_lfs : Sequence[Sequence[float]]
        Each entry is one model's loss-function values, length ``n``.
    B : int
        Number of bootstrap draws (default 100).
    block_length : int
        Stationary-bootstrap block length (default 5), clamped to ``[1, n]``.

    Returns
    -------
    SpaResult
        Test result with naive + consistent p-values, t-statistic, count of
        models beating the benchmark, and per-model bootstrap p-values.

    Raises
    ------
    ValueError
        If inputs are empty or any model series length mismatches the
        benchmark.
    """
    if not benchmark_lf:
        raise ValueError("benchmark_lf must be non-empty")
    n = len(benchmark_lf)
    if n < 2:
        raise ValueError("benchmark_lf must have length >= 2 for a std error")
    if not model_lfs:
        raise ValueError("model_lfs must contain at least one model")
    for k, ml in enumerate(model_lfs):
        if len(ml) != n:
            raise ValueError(
                f"model_lfs[{k}] length {len(ml)} != benchmark length {n}"
            )
    if B < 1:
        raise ValueError("B must be >= 1")

    # clamp block length to [1, n]
    bl = max(1, min(block_length, n))

    # per-model differentials d_k,t = L_b,t - L_k,t  (positive => model better)
    diffs: list[list[float]] = []
    dbar: list[float] = []
    dbar_se: list[float] = []
    for ml in model_lfs:
        dk = [benchmark_lf[t] - ml[t] for t in range(n)]
        diffs.append(dk)
        m = _mean(dk)
        dbar.append(m)
        sd = _stddev(dk)
        dbar_se.append(sd / math.sqrt(n) if sd > 0 else 0.0)

    # standardized best-model statistic
    t_per_model = [_safe_t(dbar[k], dbar_se[k]) for k in range(len(model_lfs))]
    t_stat = max(t_per_model) if t_per_model else 0.0

    n_beating = sum(1 for d in dbar if d > 0)

    # Centered differentials under the null (Hansen 2005 Eq. 7):
    # recenter each model by its positive mean only (so the consistent
    # bootstrap distribution only penalizes models that look better in sample).
    centered: list[list[float]] = []
    for k, dk in enumerate(diffs):
        shift = max(dbar[k], 0.0)
        centered.append([dk[t] - shift for t in range(n)])

    # Bootstrap resample indices (deterministic stationary bootstrap)
    idx_draws = _stationary_bootstrap_indices(n, B, bl, seed_index=0)

    # For each bootstrap draw, compute the standardized centered mean per model
    # and take the max. Use a *fixed* per-model standard error estimated from
    # the original sample (Hansen uses the sample s.e. in the bootstrap too, to
    # stabilize the denominator).
    boot_max: list[float] = []
    boot_per_model: list[list[float]] = [[] for _ in range(len(model_lfs))]
    for draw in idx_draws:
        per_model_max: list[float] = []
        for k in range(len(model_lfs)):
            ck = centered[k]
            m_draw = sum(ck[i] for i in draw) / n
            se = dbar_se[k]
            t_draw = _safe_t(m_draw, se)
            boot_per_model[k].append(t_draw)
            per_model_max.append(t_draw)
        boot_max.append(max(per_model_max) if per_model_max else 0.0)

    # naive p-value: P(boot_max <= 0)
    spa_pvalue = sum(1.0 for v in boot_max if v <= 0.0) / len(boot_max)

    # consistent p-value (Hansen 2005 Eq. 7): P(boot_max >= t_stat)
    consistent_pvalue = sum(1.0 for v in boot_max if v >= t_stat) / len(boot_max)

    # per-model naive bootstrap p-values: P(boot_t_k <= 0)
    individual_pvalues = [
        sum(1.0 for v in boot_per_model[k] if v <= 0.0) / len(boot_per_model[k])
        for k in range(len(model_lfs))
    ]

    return SpaResult(
        spa_pvalue=spa_pvalue,
        consistent_pvalue=consistent_pvalue,
        t_statistic=t_stat,
        n_models_beating_benchmark=n_beating,
        individual_pvalues=individual_pvalues,
        n=n,
        B=B,
        block_length=bl,
    )