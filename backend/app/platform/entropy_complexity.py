"""P262: Entropy & complexity diagnostics for a scalar series.

Pure-Python implementations of four complexity / long-memory measures:

* **Shannon entropy** — information content of an equal-width binned histogram,
  optionally normalised to ``[0, 1]``.
* **Sample entropy** — a *normalized* sample-entropy proxy computed via
  ``O(n²)`` template matching. The raw ``-log(a/b)`` statistic is squashed
  monotonically with ``raw/(1+raw)`` so the result lies in ``[0, 1]``; the
  ``a==0, b>0`` long-range-breakdown case saturates at ``1.0``.
* **Permutation entropy** — ordinal-pattern entropy of length-``order`` windows.
* **Hurst exponent** — rescaled-range (R/S) long-memory estimate clamped to
  ``[0, 1]``.

All public functions raise ``ValueError`` (invalid parameter ranges / too-short
input) and ``TypeError`` (non-numeric entries; ``bool`` is rejected explicitly)
so the platform endpoint can translate them into HTTP 422. No numpy / scipy /
pandas dependency.

Public surface
--------------
* :func:`shannon_entropy`
* :func:`sample_entropy`
* :func:`permutation_entropy`
* :func:`hurst_exponent`
* :func:`entropy_complexity_report`
* :class:`EntropyComplexityResult`
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "EntropyComplexityResult",
    "entropy_complexity_report",
    "hurst_exponent",
    "permutation_entropy",
    "sample_entropy",
    "shannon_entropy",
]


_MAX_SERIES = 5000
"""Upper bound on input length, mirroring the platform's other numeric endpoints."""


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_series(series: Sequence[float]) -> list[float]:
    """Coerce ``series`` to a validated ``list[float]``.

    Raises ``ValueError`` for an empty / too-long series or any non-finite
    value. Raises ``TypeError`` for non-numeric entries (``bool`` included) so
    that the HTTP layer can map them to 422.
    """
    if not isinstance(series, list):
        # Accept any sequence (tuple, generator) by materialising a list first;
        # a non-iterable scalar surfaces as ``TypeError`` here.
        series = list(series)  # type: ignore[arg-type]
    if len(series) == 0:
        raise ValueError("series must be non-empty")
    if len(series) > _MAX_SERIES:
        raise ValueError(f"series must contain at most {_MAX_SERIES} values")
    coerced: list[float] = []
    for value in series:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("series entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("series entries must be finite numbers")
        coerced.append(number)
    return coerced


# ---------------------------------------------------------------------------
# Shannon entropy
# ---------------------------------------------------------------------------


def shannon_entropy(
    series: Sequence[float],
    bins: int = 10,
    normalize: bool = True,
) -> float:
    """Shannon entropy of an equal-width histogram of ``series``.

    Parameters
    ----------
    series:
        Non-empty list of finite numbers.
    bins:
        Number of equal-width bins (``>= 2``). The binning edges are derived
        from the series min / max; values outside the range are not possible.
    normalize:
        When ``True`` divide by ``log(bins)`` so the result lies in ``[0, 1]``.

    Returns the entropy (in nats). A constant series yields ``0.0``. Raises
    ``ValueError`` / ``TypeError`` on invalid input.
    """
    samples = _validate_series(series)
    if not isinstance(bins, int) or isinstance(bins, bool):
        raise TypeError("bins must be an int")
    if bins < 2:
        raise ValueError("bins must be >= 2")

    lo = min(samples)
    hi = max(samples)
    if lo == hi:
        # Constant series → degenerate distribution → zero entropy.
        return 0.0

    width = (hi - lo) / bins
    counts = [0] * bins
    for value in samples:
        # Map value to bin index, clamping the maximum to the last bin so the
        # upper edge (== hi) does not overflow.
        idx = int((value - lo) / width)
        if idx >= bins:
            idx = bins - 1
        if idx < 0:
            idx = 0
        counts[idx] += 1

    total = len(samples)
    entropy = 0.0
    for count in counts:
        if count <= 0:
            continue
        p = count / total
        entropy -= p * math.log(p)

    if normalize:
        max_entropy = math.log(bins)
        return entropy / max_entropy if max_entropy > 0.0 else 0.0
    return entropy


# ---------------------------------------------------------------------------
# Sample entropy
# ---------------------------------------------------------------------------


def sample_entropy(
    series: Sequence[float],
    m: int = 2,
    r: float | None = None,
) -> float:
    """Normalized sample-entropy proxy of ``series`` (``O(n²)`` template matching).

    This is a **normalized sample entropy proxy**: the classical sample-entropy
    statistic ``-log(a / b)`` (where ``b`` counts length-``m`` template matches
    and ``a`` counts length-``(m+1)`` template matches under the Chebyshev
    tolerance ``r``) is unbounded on ``[0, +∞)``. To honour this module's
    ``[0, 1]`` normalization contract for the P262 batch, the raw value is
    monotonically squashed via ``raw / (1 + raw)``. The mapping preserves
    ordering (low complexity → near 0, high complexity → approaching 1) so the
    proxy remains a valid relative-complexity signal:

    * raw = 0            → 0.0   (perfectly repetitive / constant series)
    * raw = +∞           → 1.0   (long-range predictability breakdown,
                                  ``a == 0`` with ``b > 0``)

    ``m`` is the template length and ``r`` the similarity tolerance (defaults
    to ``0.2 · std(series)``). For a constant series with the default ``r`` the
    statistic is well-defined and returns ``0.0``.

    ``r`` must be a positive finite number when supplied explicitly. Passing
    ``r <= 0`` raises ``ValueError``; the only tolerated zero-tolerance path is
    the internal fallback (``r is None`` and the series is constant, in which
    case ``0.0`` is returned). Raises ``ValueError`` for ``m < 1`` or
    insufficient samples, and ``TypeError`` for non-numeric input.
    """
    samples = _validate_series(series)
    if not isinstance(m, int) or isinstance(m, bool):
        raise TypeError("m must be an int")
    if m < 1:
        raise ValueError("m must be >= 1")
    # Need at least m+1 points to form a length-(m+1) template.
    if len(samples) < m + 1:
        raise ValueError(f"series must contain at least {m + 1} values")

    n = len(samples)
    std_zero = False
    if r is None:
        # Default tolerance: 0.2 * population standard deviation.
        mean = sum(samples) / n
        var = sum((x - mean) ** 2 for x in samples) / n
        std = math.sqrt(var) if var > 0.0 else 0.0
        if std == 0.0:
            # Constant series (or all-equal) with default r: every equal value
            # pair is a match, so the entropy collapses to 0. Tolerated
            # internally — an explicit ``r <= 0`` from the caller is still
            # rejected (see the ``else`` branch below).
            std_zero = True
            tolerance = 0.0
        else:
            tolerance = 0.2 * std
    else:
        if isinstance(r, bool) or not isinstance(r, (int, float)):
            raise TypeError("r must be a finite number")
        tolerance = float(r)
        if not math.isfinite(tolerance):
            raise ValueError("r must be a finite number")
        if tolerance <= 0.0:
            # Explicit user-supplied non-positive tolerance is invalid: the
            # classical estimator degenerates (no matches except exact-equal
            # pairs) and the result ceases to be a meaningful complexity
            # measure. The internal constant-series fallback above is the only
            # tolerated zero-tolerance path.
            raise ValueError("r must be > 0")

    def _count_matches(template_len: int) -> int:
        matches = 0
        # ``i`` ranges so that ``i + template_len`` is a valid end bound.
        for i in range(n - template_len):
            xi = samples[i : i + template_len]
            for j in range(i + 1, n - template_len + 1):
                xj = samples[j : j + template_len]
                ok = True
                for a, b in zip(xi, xj):
                    if abs(a - b) > tolerance:
                        ok = False
                        break
                if ok:
                    matches += 1
        return matches

    if std_zero:
        # Constant series with default r → fully self-similar → entropy 0.
        return 0.0

    b = _count_matches(m)        # length-m template matches
    a = _count_matches(m + 1)    # length-(m+1) template matches

    if b == 0:
        # No length-m matches at all → cannot estimate a ratio. Treat as the
        # minimally-complex degenerate case (consistent with a constant /
        # perfectly repetitive series that simply failed to match within ``r``).
        return 0.0
    if a == 0:
        # ``a == 0`` with ``b > 0``: every length-m match fails to extend to
        # length-(m+1) — a complete long-range predictability breakdown. The
        # raw statistic is ``-log(0) = +∞``; under ``raw/(1+raw)`` this
        # saturates to the upper bound ``1.0``.
        return 1.0
    raw = -math.log(a / b)

    # Monotonic compression to [0, 1]: raw/(1+raw) maps [0, +∞) → [0, 1).
    return raw / (1.0 + raw)


# ---------------------------------------------------------------------------
# Permutation entropy
# ---------------------------------------------------------------------------


def permutation_entropy(
    series: Sequence[float],
    order: int = 3,
    delay: int = 1,
    normalize: bool = True,
) -> float:
    """Permutation (ordinal-pattern) entropy of ``series``.

    Slides a length-``order`` window separated by ``delay`` samples over the
    series, classifies each window by its rank pattern, and computes the
    Shannon entropy of the resulting pattern histogram. A monotonic series has
    exactly one pattern (entropy 0); a maximally-mixed series has the highest
    entropy.

    Raises ``ValueError`` for ``order < 2``, ``delay < 1`` or a series too
    short to form a single window.
    """
    samples = _validate_series(series)
    if not isinstance(order, int) or isinstance(order, bool):
        raise TypeError("order must be an int")
    if not isinstance(delay, int) or isinstance(delay, bool):
        raise TypeError("delay must be an int")
    if order < 2:
        raise ValueError("order must be >= 2")
    if delay < 1:
        raise ValueError("delay must be >= 1")

    # A window covers indices ``[i, i+delay, i+2*delay, …, i+(order-1)*delay]``;
    # the last start index must therefore leave a full window.
    span = (order - 1) * delay
    if len(samples) <= span:
        raise ValueError(
            f"series must contain more than {span} values for order={order} delay={delay}"
        )

    counts: dict[tuple[int, ...], int] = {}
    num_windows = 0
    for i in range(len(samples) - span):
        window = [samples[i + k * delay] for k in range(order)]
        # The ordinal pattern is the argsort of the window (with stable ties).
        pattern = tuple(sorted(range(order), key=lambda idx: window[idx]))
        counts[pattern] = counts.get(pattern, 0) + 1
        num_windows += 1

    entropy = 0.0
    for count in counts.values():
        p = count / num_windows
        entropy -= p * math.log(p)

    if normalize:
        max_entropy = math.log(math.factorial(order))
        return entropy / max_entropy if max_entropy > 0.0 else 0.0
    return entropy


# ---------------------------------------------------------------------------
# Hurst exponent (R/S analysis)
# ---------------------------------------------------------------------------


def _cumulative_range(values: list[float]) -> float:
    """Rescaled range statistic ``R/S`` of a value sequence."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    cumdev = 0.0
    min_cd = math.inf
    max_cd = -math.inf
    for v in values:
        cumdev += v - mean
        if cumdev < min_cd:
            min_cd = cumdev
        if cumdev > max_cd:
            max_cd = cumdev
    r = max_cd - min_cd
    var = sum((v - mean) ** 2 for v in values) / n
    s = math.sqrt(var)
    if s <= 0.0:
        return 0.0
    return r / s


def hurst_exponent(series: Sequence[float]) -> float:
    """Hurst exponent estimated via classical R/S analysis, clamped to ``[0, 1]``.

    Splits ``series`` into contiguous non-overlapping blocks of length ``k``
    (powers of 2), averages the per-block rescaled range, and fits
    ``log(R/S) ~ H · log(k)``. The slope is reported directly, clamped to
    ``[0, 1]`` to absorb estimator noise on short series.

    Raises ``ValueError`` for series shorter than 4 samples.
    """
    samples = _validate_series(series)
    if len(samples) < 4:
        raise ValueError("series must contain at least 4 values for R/S analysis")

    n = len(samples)
    # Build the set of feasible block sizes: powers of 2 in [2, n//2].
    sizes: list[int] = []
    k = 2
    while k <= n // 2:
        sizes.append(k)
        k *= 2
    if not sizes:
        # Fall back to a single block when n is tiny; the slope is undefined
        # so we return 0.5 (no long-range memory) — clamped below.
        return 0.5

    log_k: list[float] = []
    log_rs: list[float] = []
    for size in sizes:
        num_blocks = n // size
        rs_values: list[float] = []
        for b in range(num_blocks):
            block = samples[b * size : (b + 1) * size]
            rs = _cumulative_range(block)
            if rs > 0.0:
                rs_values.append(rs)
        if not rs_values:
            continue
        mean_rs = sum(rs_values) / len(rs_values)
        log_k.append(math.log(size))
        log_rs.append(math.log(mean_rs))

    if len(log_k) < 2:
        # Not enough distinct block sizes to fit a slope.
        return 0.5

    # Ordinary least squares slope of log(R/S) on log(k).
    m = len(log_k)
    sum_x = sum(log_k)
    sum_y = sum(log_rs)
    sum_xy = sum(x * y for x, y in zip(log_k, log_rs))
    sum_xx = sum(x * x for x in log_k)
    denom = m * sum_xx - sum_x * sum_x
    if denom == 0.0:
        return 0.5
    slope = (m * sum_xy - sum_x * sum_y) / denom

    # Clamp to [0, 1]: the R/S estimator is noisy on short series.
    return max(0.0, min(1.0, slope))


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntropyComplexityResult:
    """Frozen carrier for the four P262 complexity metrics.

    All entropy values are normalized to ``[0, 1]`` — Shannon / permutation via
    ``log(bins)`` / ``log(order!)`` scaling and sample entropy via the
    ``raw/(1+raw)`` proxy squash described in :func:`sample_entropy`.
    ``hurst_exponent`` is clamped to ``[0, 1]``. ``approximation`` describes
    the estimator family used (e.g. ``"rs_estimated"`` for the R/S Hurst,
    ``"exact"`` for the closed-form entropies).
    """

    shannon_entropy: float
    sample_entropy: float
    permutation_entropy: float
    hurst_exponent: float
    n: int
    approximation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shannon_entropy": self.shannon_entropy,
            "sample_entropy": self.sample_entropy,
            "permutation_entropy": self.permutation_entropy,
            "hurst_exponent": self.hurst_exponent,
            "n": self.n,
            "approximation": self.approximation,
        }


def entropy_complexity_report(
    series: Sequence[float],
    bins: int = 10,
    sample_m: int = 2,
    permutation_order: int = 3,
) -> EntropyComplexityResult:
    """Run all four P262 complexity estimators and return a frozen result.

    Parameters
    ----------
    series:
        Non-empty finite-number sequence.
    bins:
        Equal-width bin count for :func:`shannon_entropy` (``>= 2``).
    sample_m:
        Template length for :func:`sample_entropy` (``>= 1``).
    permutation_order:
        Ordinal-pattern order for :func:`permutation_entropy` (``>= 2``).

    The estimator family is recorded as ``approximation`` so downstream callers
    can branch on the algorithm used (currently ``"exact"`` for entropies and
    ``"rs_estimated"`` for the Hurst component).
    """
    samples = _validate_series(series)
    return EntropyComplexityResult(
        shannon_entropy=shannon_entropy(samples, bins=bins, normalize=True),
        sample_entropy=sample_entropy(samples, m=sample_m),
        permutation_entropy=permutation_entropy(samples, order=permutation_order, normalize=True),
        hurst_exponent=hurst_exponent(samples),
        n=len(samples),
        approximation="rs_estimated",
    )
