"""P264: single-period cross-sectional factor IC analysis.

Pure-Python (standard library only) Information Coefficient diagnostics for
a factor vs. its forward returns on the same cross-section. Computes:

* **Pearson IC** — linear correlation of the raw factor values and the
  forward returns.
* **Spearman / rank IC** — Pearson correlation of the *ranks* (ties handled
  via average ranks), which is the conventional rank IC reported by
  equity-quant researchers; ``rank_ic`` is therefore an alias of
  ``spearman_ic``.
* **ICIR** — single-period cross-sectional approximation. The classical
  ICIR is ``mean(IC) / std(IC)`` across many periods, which is undefined
  for a single cross-section; instead we use the Fisher-z-style
  stabilisation ``IC / sqrt(1 - IC^2 + eps)`` (with a tiny ``eps`` to avoid
  division by zero when ``|IC| -> 1``). The approximation family is recorded
  in the docstring below.
* **Quantile spread** — sort the cross-section by the factor and split it
  into ``n_quantiles`` contiguous buckets; the spread is the top-bucket mean
  return minus the bottom-bucket mean return.

All public functions raise ``ValueError`` for every illegal argument:
invalid parameter ranges / length mismatch / non-finite values and
non-numeric / non-sequence entries (``bool`` is rejected explicitly so
``True`` is not silently accepted as ``1.0``; bare scalars and other
non-iterables are also rejected as non-sequence) so the platform endpoint
can translate them into HTTP 422. No numpy / scipy / pandas dependency.

Public surface
--------------
* :func:`pearson_corr`
* :func:`rank_values`
* :func:`spearman_corr`
* :func:`factor_ic_report`
* :class:`QuantileBucket`
* :class:`FactorICResult`
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "FactorICResult",
    "QuantileBucket",
    "factor_ic_report",
    "pearson_corr",
    "rank_values",
    "spearman_corr",
]


_MAX_SERIES = 5000
"""Upper bound on input length, mirroring the platform's other numeric endpoints."""

_EPS = 1e-12
"""Tiny floor under the ICIR denominator to avoid division by zero when |IC| -> 1."""


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_paired(
    factor: Sequence[float],
    forward_returns: Sequence[float],
) -> tuple[list[float], list[float]]:
    """Coerce ``factor`` / ``forward_returns`` to validated ``list[float]``.

    Raises ``ValueError`` for any illegal argument: non-iterable inputs
    (bare scalars), length mismatch, empty input, too-long input,
    non-finite values or non-numeric entries (``bool`` included, so ``True``
    is not silently accepted as ``1.0``).
    """
    # Materialise non-list sequences; a non-iterable scalar surfaces as
    # TypeError from list(...) and is re-raised as ValueError per the spec.
    # Mapping/dict is explicitly rejected up-front so it is NOT silently
    # treated as its keys sequence by list(...).
    if isinstance(factor, Mapping):
        raise ValueError("factor must be a sequence of numbers, not a Mapping/dict")
    if not isinstance(factor, list):
        if isinstance(factor, (str, bytes)):
            raise ValueError("factor must be a sequence of numbers, not str/bytes")
        try:
            factor = list(factor)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("factor must be a sequence of numbers") from exc
    if isinstance(forward_returns, Mapping):
        raise ValueError(
            "forward_returns must be a sequence of numbers, not a Mapping/dict"
        )
    if not isinstance(forward_returns, list):
        if isinstance(forward_returns, (str, bytes)):
            raise ValueError("forward_returns must be a sequence of numbers, not str/bytes")
        try:
            forward_returns = list(forward_returns)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("forward_returns must be a sequence of numbers") from exc

    n = len(factor)
    if n == 0 or len(forward_returns) == 0:
        raise ValueError("factor and forward_returns must be non-empty")
    if len(forward_returns) != n:
        raise ValueError(
            f"factor and forward_returns must have the same length (got {n} vs {len(forward_returns)})"
        )
    if n > _MAX_SERIES:
        raise ValueError(f"factor must contain at most {_MAX_SERIES} values")
    if n < 2:
        raise ValueError("factor and forward_returns must contain at least 2 values")

    f_out: list[float] = []
    r_out: list[float] = []
    for f_val, r_val in zip(factor, forward_returns):
        if isinstance(f_val, bool) or not isinstance(f_val, (int, float)):
            raise ValueError("factor entries must be finite numbers")
        if isinstance(r_val, bool) or not isinstance(r_val, (int, float)):
            raise ValueError("forward_returns entries must be finite numbers")
        f_num = float(f_val)
        r_num = float(r_val)
        if not math.isfinite(f_num) or not math.isfinite(r_num):
            raise ValueError("factor and forward_returns entries must be finite numbers")
        f_out.append(f_num)
        r_out.append(r_num)
    return f_out, r_out


def _validate_single(values: Sequence[float], *, name: str) -> list[float]:
    """Validate a single sequence used by :func:`rank_values` / :func:`pearson_corr`.

    Raises ``ValueError`` for any illegal argument: non-iterable inputs
    (bare scalars), empty input, non-finite values or non-numeric entries
    (``bool`` included).
    """
    # Mapping/dict is explicitly rejected up-front so it is NOT silently
    # treated as its keys sequence by list(...).
    if isinstance(values, Mapping):
        raise ValueError(f"{name} must be a sequence of numbers, not a Mapping/dict")
    if not isinstance(values, list):
        if isinstance(values, (str, bytes)):
            raise ValueError(f"{name} must be a sequence of numbers, not str/bytes")
        try:
            values = list(values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{name} must be a sequence of numbers") from exc
    if len(values) == 0:
        raise ValueError(f"{name} must be non-empty")
    if len(values) > _MAX_SERIES:
        raise ValueError(f"{name} must contain at most {_MAX_SERIES} values")
    out: list[float] = []
    for val in values:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"{name} entries must be finite numbers")
        num = float(val)
        if not math.isfinite(num):
            raise ValueError(f"{name} entries must be finite numbers")
        out.append(num)
    return out


# ---------------------------------------------------------------------------
# pearson_corr
# ---------------------------------------------------------------------------


def pearson_corr(x: Sequence[float], y: Sequence[float]) -> float:
    """Pearson product-moment correlation of two aligned finite-number series.

    Returns ``0.0`` when either series has zero variance (the correlation is
    undefined; reporting a neutral zero keeps downstream aggregations stable).
    Raises ``ValueError`` for length mismatch, empty input, a single sample,
    non-finite values, non-numeric entries (``bool`` rejected explicitly) or
    non-sequence inputs (bare scalars).
    """
    xs = _validate_single(x, name="x")
    ys = _validate_single(y, name="y")
    if len(xs) != len(ys):
        raise ValueError(
            f"x and y must have the same length (got {len(xs)} vs {len(ys)})"
        )
    if len(xs) < 2:
        raise ValueError("x and y must contain at least 2 values")

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = 0.0
    var_x = 0.0
    var_y = 0.0
    for xi, yi in zip(xs, ys):
        dx = xi - mean_x
        dy = yi - mean_y
        cov += dx * dy
        var_x += dx * dx
        var_y += dy * dy
    denom = math.sqrt(var_x * var_y)
    if denom == 0.0:
        # Zero variance in either series → correlation undefined → return 0.
        return 0.0
    return cov / denom


# ---------------------------------------------------------------------------
# rank_values (average ranks for ties)
# ---------------------------------------------------------------------------


def rank_values(values: Sequence[float]) -> list[float]:
    """Return average ranks of ``values`` (ties share the mean of their ranks).

    Ranks are 1-based and ascending (the smallest value gets rank 1). Equal
    values receive the average of the ranks they would have occupied. Raises
    ``ValueError`` for empty input, non-finite values, non-numeric entries
    (``bool`` rejected explicitly) or non-sequence inputs (bare scalars).
    """
    samples = _validate_single(values, name="values")
    n = len(samples)
    # Pair each value with its original index so we can scatter ranks back.
    indexed = sorted(range(n), key=lambda i: samples[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        # Collect all entries tied with samples[indexed[i]].
        j = i
        while j + 1 < n and samples[indexed[j + 1]] == samples[indexed[i]]:
            j += 1
        # Average rank of the tied block: (i+1 + j+1) / 2 → ((i + j) / 2) + 1.
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


# ---------------------------------------------------------------------------
# spearman_corr
# ---------------------------------------------------------------------------


def spearman_corr(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman rank correlation of two aligned finite-number series.

    Computes the Pearson correlation of the average ranks of ``x`` and ``y``
    (ties handled by :func:`rank_values`). Returns ``0.0`` when either series
    has zero variance (constant series). Raises ``ValueError`` for length
    mismatch, empty input, a single sample, non-finite values, non-numeric
    entries or non-sequence inputs (bare scalars).
    """
    # Validate length / content via the paired helper first, then rank.
    rx, ry = _validate_paired(x, y)
    return pearson_corr(rank_values(rx), rank_values(ry))


# ---------------------------------------------------------------------------
# Quantile buckets + aggregate report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuantileBucket:
    """One quantile bucket of the cross-sectional factor sort.

    Attributes
    ----------
    quantile:
        1-based quantile index (1 = bottom / lowest factor values).
    count:
        Number of names in the bucket.
    mean_return:
        Arithmetic mean of the ``forward_returns`` of the names in the bucket.
    """

    quantile: int
    count: int
    mean_return: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "quantile": self.quantile,
            "count": self.count,
            "mean_return": self.mean_return,
        }


@dataclass(frozen=True)
class FactorICResult:
    """Frozen carrier for the P264 factor IC diagnostics.

    Attributes
    ----------
    pearson_ic:
        Pearson correlation of raw factor values and forward returns.
    spearman_ic:
        Spearman (rank) correlation — the conventional rank IC.
    rank_ic:
        Alias of ``spearman_ic`` (kept as a separate field for API clarity).
    icir:
        Single-period cross-sectional ICIR approximation —
        ``pearson_ic / sqrt(1 - pearson_ic^2 + eps)``. The classical
        multi-period ICIR (``mean(IC)/std(IC)``) is undefined for a single
        cross-section, so this Fisher-z-style stabiliser is used instead.
    quantile_spread:
        Top-quantile mean return minus bottom-quantile mean return.
    buckets:
        Per-quantile decomposition (length ``n_quantiles``).
    n:
        Number of names in the cross-section.
    """

    pearson_ic: float
    spearman_ic: float
    rank_ic: float
    icir: float
    quantile_spread: float
    buckets: list[QuantileBucket]
    n: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pearson_ic": self.pearson_ic,
            "spearman_ic": self.spearman_ic,
            "rank_ic": self.rank_ic,
            "icir": self.icir,
            "quantile_spread": self.quantile_spread,
            "buckets": [b.to_dict() for b in self.buckets],
            "n": self.n,
        }


def _icir_approx(ic: float) -> float:
    """Single-period ICIR approximation (Fisher-z stabilisation).

    ``ICIR ≈ IC / sqrt(1 - IC^2 + eps)``. When ``|IC| -> 1`` the denominator
    would collapse to zero; the tiny ``eps`` floor keeps the value finite.
    """
    denom = math.sqrt(max(1.0 - ic * ic + _EPS, _EPS))
    return ic / denom


def _quantile_buckets(
    factor: list[float],
    forward_returns: list[float],
    n_quantiles: int,
) -> list[QuantileBucket]:
    """Sort the cross-section by ``factor`` and slice into ``n_quantiles`` buckets."""
    n = len(factor)
    order = sorted(range(n), key=lambda i: factor[i])
    # Even-ish split: first n % n_quantiles buckets get one extra element so the
    # bucket counts sum to n exactly while each bucket is non-empty.
    base = n // n_quantiles
    rem = n % n_quantiles
    buckets: list[QuantileBucket] = []
    start = 0
    for q in range(n_quantiles):
        size = base + (1 if q < rem else 0)
        slice_idx = order[start : start + size]
        mean_return = sum(forward_returns[i] for i in slice_idx) / size
        buckets.append(
            QuantileBucket(quantile=q + 1, count=size, mean_return=mean_return)
        )
        start += size
    return buckets


def factor_ic_report(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    n_quantiles: int = 5,
) -> FactorICResult:
    """Run all P264 cross-sectional IC diagnostics and return a frozen result.

    Parameters
    ----------
    factor:
        Cross-sectional factor values (one per name).
    forward_returns:
        Forward returns aligned 1-to-1 with ``factor``.
    n_quantiles:
        Number of quantile buckets (``>= 2`` and ``<= len``). Defaults to 5.

    Raises ``ValueError`` for length mismatch, empty / too-short input,
    ``n_quantiles`` out of range, non-finite values, non-numeric /
    non-sequence entries (``bool`` rejected explicitly; bare scalars and
    other non-iterables rejected as non-sequence).
    """
    # n_quantiles must be a strict int (reject bool which subclasses int).
    if isinstance(n_quantiles, bool) or not isinstance(n_quantiles, int):
        raise ValueError("n_quantiles must be an int")
    factor_vals, return_vals = _validate_paired(factor, forward_returns)
    n = len(factor_vals)
    if n_quantiles < 2:
        raise ValueError("n_quantiles must be >= 2")
    if n_quantiles > n:
        raise ValueError("n_quantiles must be <= len(factor)")

    pearson_ic = pearson_corr(factor_vals, return_vals)
    spearman_ic = spearman_corr(factor_vals, return_vals)
    rank_ic = spearman_ic  # alias by definition
    icir = _icir_approx(pearson_ic)
    buckets = _quantile_buckets(factor_vals, return_vals, n_quantiles)
    quantile_spread = buckets[-1].mean_return - buckets[0].mean_return

    return FactorICResult(
        pearson_ic=pearson_ic,
        spearman_ic=spearman_ic,
        rank_ic=rank_ic,
        icir=icir,
        quantile_spread=quantile_spread,
        buckets=buckets,
        n=n,
    )