"""P263: Rolling statistical features for a scalar price / return series.

Pure-Python (standard library only) rolling diagnostics for a uniformly
sampled scalar series. Provides sliding-window mean, *population* std,
z-score of the current point relative to its trailing window, skew,
kurtosis, an exponentially weighted moving average (EWMA), and a rolling
beta versus a benchmark series.

The module is deliberately self-contained — no numpy / scipy / pandas —
to match the conventions of the platform's other analysis modules (see
``spectral_analysis.py``, ``change_point.py``).

Public surface
--------------

* :class:`RollingFeatureResult` — frozen dataclass aggregating all seven
  outputs, plus a ``to_dict`` for JSON serialisation at the API layer.
* :func:`rolling_mean`, :func:`rolling_std`, :func:`rolling_zscore`,
  :func:`rolling_skew`, :func:`rolling_kurtosis` — sliding-window moments.
* :func:`ewma` — recursive exponentially weighted moving average.
* :func:`rolling_beta` — rolling regression beta of ``series`` vs.
  ``benchmark`` over the trailing window.
* :func:`rolling_feature_report` — convenience aggregator producing a
  :class:`RollingFeatureResult`.

Conventions
-----------

* All output lists have the **same length** as the input series.
* The first ``window - 1`` entries of every rolling output are ``None``
  (insufficient data). ``ewma`` is the **only** exception: because EWMA does
  not depend on a fixed rolling window, it has **no warm-up** and is defined
  at every index, using ``y[0] = x[0]`` as the seed (see :func:`ewma`).
* ``std`` is the *population* standard deviation (denominator ``window``).
* ``zscore[i]`` compares the *current* point ``series[i]`` to the trailing
  window ``series[i - window + 1 .. i]``. When the window std is zero the
  z-score is defined to be ``0.0`` (rather than NaN / inf).
* ``skew`` / ``kurtosis`` use population central moments. When the window
  std is zero they return ``0.0``.
* ``beta`` is ``cov(series, benchmark) / var(benchmark)`` over the trailing
  window; when the benchmark variance is zero, beta is defined to be ``0.0``.
* When no ``benchmark`` is supplied to :func:`rolling_feature_report`, the
  ``beta`` field of the result is ``None``.

All functions raise ``ValueError`` for an empty / non-finite series, an
out-of-range window, an out-of-range ``alpha``, or a length-mismatched
benchmark. Following the P263 review, the public surface raises
``ValueError`` **uniformly** for *any* invalid argument — including a
non-integer ``window`` (and ``bool``), a non-numeric / ``bool`` series
entry, and a non-numeric / ``bool`` ``alpha`` — so the callers (the platform
HTTP layer) can map a single exception family to HTTP 422 without
special-casing ``TypeError``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "RollingFeatureResult",
    "ewma",
    "rolling_beta",
    "rolling_feature_report",
    "rolling_kurtosis",
    "rolling_mean",
    "rolling_skew",
    "rolling_std",
    "rolling_zscore",
]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_series(series: Sequence[float]) -> list[float]:
    """Coerce ``series`` to ``list[float]`` and validate each entry.

    Raises ``ValueError`` for an empty series, a non-numeric entry, or a
    non-finite entry. ``bool`` is rejected even though it subclasses ``int``.

    Per the P263 review, the public surface must raise ``ValueError``
    **uniformly** for any invalid argument — so non-iterable inputs
    (``None``, bare scalars) and ``str`` (iterable but semantically not a
    numeric sequence) are converted into ``ValueError`` rather than letting
    a ``TypeError`` leak to the platform HTTP layer.
    """
    # ``str`` / ``bytes`` are iterable (yielding single characters / bytes)
    # but are never a valid numeric series; reject them explicitly so e.g.
    # ``"123"`` is not split into ``['1', '2', '3']`` and only fail later.
    if isinstance(series, (str, bytes)):
        raise ValueError("series must be a sequence of finite numbers")
    # A ``Mapping`` (``dict`` et al.) is iterable — ``list({...})`` yields its
    # *keys* — so without this guard ``{1.0: 'a', 2.0: 'b'}`` would be silently
    # coerced to ``[1.0, 2.0]`` and pass the entry checks. A mapping is
    # semantically not a numeric sequence; reject it explicitly.
    if isinstance(series, Mapping):
        raise ValueError("series must be a sequence of finite numbers")
    # ``None`` and bare scalars (int / float / bool) are not iterable; wrap
    # the conversion in try/except to surface a uniform ``ValueError``
    # instead of letting ``TypeError`` propagate to the caller.
    try:
        raw = list(series)
    except TypeError as exc:
        raise ValueError("series must be a sequence of finite numbers") from exc

    coerced: list[float] = []
    for value in raw:
        # ``bool`` is a subclass of ``int``; reject it explicitly so that
        # ``True`` / ``False`` are not silently coerced to ``1.0`` / ``0.0``.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("series entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("series entries must be finite numbers")
        coerced.append(number)
    if not coerced:
        raise ValueError("series must be non-empty")
    return coerced


def _validate_window(window: int, n: int) -> int:
    """Validate that ``window`` is an int in ``[2, n]``.

    Raises ``ValueError`` for a non-integer ``window`` (including ``bool``)
    or an out-of-range value. ``bool`` is rejected explicitly: although it
    subclasses ``int`` in Python, treating ``True`` as ``window=1`` would
    silently bypass the ``>= 2`` floor.
    """
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError("window must be an int")
    if window < 2:
        raise ValueError("window must be >= 2")
    if window > n:
        raise ValueError("window must be <= len(series)")
    return window


def _validate_alpha(alpha: float) -> float:
    """Validate that ``alpha`` is a finite float in ``(0, 1]``.

    Raises ``ValueError`` for a non-numeric ``alpha`` (including ``bool``),
    a non-finite value, or an out-of-range value.
    """
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise ValueError("alpha must be a finite number")
    number = float(alpha)
    if not math.isfinite(number):
        raise ValueError("alpha must be a finite number")
    if not (0.0 < number <= 1.0):
        raise ValueError("alpha must be in (0, 1]")
    return number


# ---------------------------------------------------------------------------
# Rolling primitives
# ---------------------------------------------------------------------------


def rolling_mean(series: Sequence[float], window: int) -> list[float | None]:
    """Trailing-window arithmetic mean.

    Returns a list of length ``len(series)``; the first ``window - 1``
    entries are ``None``.
    """
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    out: list[float | None] = []
    running_sum = 0.0
    for i, value in enumerate(samples):
        running_sum += value
        if i >= w:
            running_sum -= samples[i - w]
        if i >= w - 1:
            out.append(running_sum / w)
        else:
            out.append(None)
    return out


def rolling_std(series: Sequence[float], window: int) -> list[float | None]:
    """Trailing-window *population* standard deviation (denominator ``window``)."""
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    out: list[float | None] = []
    running_sum = 0.0
    running_sq_sum = 0.0
    for i, value in enumerate(samples):
        running_sum += value
        running_sq_sum += value * value
        if i >= w:
            old = samples[i - w]
            running_sum -= old
            running_sq_sum -= old * old
        if i >= w - 1:
            # population variance = E[X^2] - (E[X])^2
            mean = running_sum / w
            variance = running_sq_sum / w - mean * mean
            # Guard against tiny negative values from floating point error.
            if variance < 0.0:
                variance = 0.0
            out.append(math.sqrt(variance))
        else:
            out.append(None)
    return out


def _rolling_mean_and_std(
    samples: list[float], w: int
) -> tuple[list[float | None], list[float | None]]:
    """Shared mean + population std computation (single pass, used by zscore)."""
    means: list[float | None] = []
    stds: list[float | None] = []
    running_sum = 0.0
    running_sq_sum = 0.0
    for i, value in enumerate(samples):
        running_sum += value
        running_sq_sum += value * value
        if i >= w:
            old = samples[i - w]
            running_sum -= old
            running_sq_sum -= old * old
        if i >= w - 1:
            mean = running_sum / w
            variance = running_sq_sum / w - mean * mean
            if variance < 0.0:
                variance = 0.0
            means.append(mean)
            stds.append(math.sqrt(variance))
        else:
            means.append(None)
            stds.append(None)
    return means, stds


def rolling_zscore(series: Sequence[float], window: int) -> list[float | None]:
    """Z-score of the *current* point relative to its trailing window.

    ``z[i] = (series[i] - mean(series[i-w+1..i])) / std(series[i-w+1..i])``.
    When the window std is zero the z-score is defined to be ``0.0``.
    """
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    _, stds = _rolling_mean_and_std(samples, w)
    out: list[float | None] = []
    for i, value in enumerate(samples):
        std = stds[i]
        if std is None:
            out.append(None)
        elif std == 0.0:
            out.append(0.0)
        else:
            # Recompute the window mean lazily — the z-score only needs it
            # when std > 0. We compute it directly to avoid an extra full
            # mean list and to keep the public surface minimal.
            window_slice = samples[i - w + 1 : i + 1]
            mean = sum(window_slice) / w
            out.append((value - mean) / std)
    return out


def rolling_skew(series: Sequence[float], window: int) -> list[float | None]:
    """Trailing-window skewness (population central third moment, normalised).

    Uses the textbook definition ``m3 / m2^1.5`` where ``mk`` is the k-th
    population central moment. Returns ``0.0`` when the window std is zero.
    """
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    out: list[float | None] = []
    for i in range(len(samples)):
        if i < w - 1:
            out.append(None)
            continue
        window_slice = samples[i - w + 1 : i + 1]
        mean = sum(window_slice) / w
        m2 = sum((x - mean) ** 2 for x in window_slice) / w
        m3 = sum((x - mean) ** 3 for x in window_slice) / w
        if m2 <= 0.0:
            out.append(0.0)
        else:
            out.append(m3 / (m2 ** 1.5))
    return out


def rolling_kurtosis(series: Sequence[float], window: int) -> list[float | None]:
    """Trailing-window kurtosis (population central fourth moment, normalised).

    Uses the textbook definition ``m4 / m2^2`` (the *non-excess* kurtosis;
    a Gaussian has kurtosis 3). Returns ``0.0`` when the window std is zero.
    """
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    out: list[float | None] = []
    for i in range(len(samples)):
        if i < w - 1:
            out.append(None)
            continue
        window_slice = samples[i - w + 1 : i + 1]
        mean = sum(window_slice) / w
        m2 = sum((x - mean) ** 2 for x in window_slice) / w
        m4 = sum((x - mean) ** 4 for x in window_slice) / w
        if m2 <= 0.0:
            out.append(0.0)
        else:
            out.append(m4 / (m2 * m2))
    return out


# ---------------------------------------------------------------------------
# EWMA
# ---------------------------------------------------------------------------


def ewma(series: Sequence[float], alpha: float) -> list[float]:
    """Recursive exponentially weighted moving average.

    ``y[0] = x[0]`` and ``y[i] = alpha * x[i] + (1 - alpha) * y[i-1]``.

    EWMA is the **exception** to the rolling-window warm-up convention: it
    does not depend on a fixed trailing window, so it has **no warm-up**.
    Every output is defined — the first entry equals the first observation
    (``y[0] = x[0]``) and every subsequent index is computed recursively.
    The returned list therefore has the same length as ``series`` and
    contains **no** leading ``None`` entries (contrast with the other
    rolling stats, whose first ``window - 1`` entries are ``None``).

    ``alpha`` must lie in ``(0, 1]``; any invalid value raises
    ``ValueError``.
    """
    samples = _validate_series(series)
    a = _validate_alpha(alpha)
    out: list[float] = []
    prev = samples[0]
    out.append(prev)
    one_minus_a = 1.0 - a
    for value in samples[1:]:
        prev = a * value + one_minus_a * prev
        out.append(prev)
    return out


# ---------------------------------------------------------------------------
# Rolling beta
# ---------------------------------------------------------------------------


def rolling_beta(
    series: Sequence[float], benchmark: Sequence[float], window: int
) -> list[float | None]:
    """Rolling regression beta of ``series`` against ``benchmark``.

    ``beta[i] = cov(series, benchmark) / var(benchmark)`` computed over the
    trailing window ``series[i-w+1..i]`` and the matching benchmark slice.
    When the benchmark variance is zero, beta is defined to be ``0.0``.
    Raises ``ValueError`` for invalid input (including a length mismatch).
    """
    samples = _validate_series(series)
    bench = _validate_series(benchmark)
    if len(samples) != len(bench):
        raise ValueError("benchmark must have the same length as series")
    w = _validate_window(window, len(samples))
    out: list[float | None] = []
    for i in range(len(samples)):
        if i < w - 1:
            out.append(None)
            continue
        x_slice = samples[i - w + 1 : i + 1]
        y_slice = bench[i - w + 1 : i + 1]
        mean_x = sum(x_slice) / w
        mean_y = sum(y_slice) / w
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_slice, y_slice)) / w
        var_y = sum((y - mean_y) ** 2 for y in y_slice) / w
        if var_y <= 0.0:
            out.append(0.0)
        else:
            out.append(cov / var_y)
    return out


# ---------------------------------------------------------------------------
# Result dataclass + aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingFeatureResult:
    """Frozen container for the seven rolling diagnostics.

    The ``mean``, ``std``, ``zscore``, ``skew``, ``kurtosis`` and ``ewma``
    fields are always length-``N`` lists. The rolling stats (``mean`` …
    ``kurtosis``) carry leading ``None`` warm-up entries for the first
    ``window - 1`` indices. ``ewma`` is the exception: it has no warm-up
    (it does not depend on a fixed rolling window) and is defined at every
    index, so it never contains ``None``. ``beta`` is either a length-``N``
    list (when a benchmark was supplied) or ``None`` (when no benchmark was
    supplied to :func:`rolling_feature_report`).
    """

    mean: list[float | None]
    std: list[float | None]
    zscore: list[float | None]
    skew: list[float | None]
    kurtosis: list[float | None]
    ewma: list[float]
    beta: list[float | None] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": self.mean,
            "std": self.std,
            "zscore": self.zscore,
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "ewma": self.ewma,
            "beta": self.beta,
        }


def rolling_feature_report(
    series: Sequence[float],
    window: int = 5,
    alpha: float = 0.2,
    benchmark: Sequence[float] | None = None,
) -> RollingFeatureResult:
    """Aggregate all rolling diagnostics for ``series`` into one result.

    Parameters
    ----------
    series:
        Non-empty list of finite numbers.
    window:
        Trailing-window size (must satisfy ``2 <= window <= len(series)``).
        Defaults to 5.
    alpha:
        EWMA smoothing factor in ``(0, 1]``. Defaults to 0.2.
    benchmark:
        Optional benchmark series of the same length as ``series``. When
        supplied, the ``beta`` field of the result is populated; otherwise
        it is ``None``.

    Returns a :class:`RollingFeatureResult`. Following the P263 review, the
    public surface raises ``ValueError`` **uniformly** for any invalid
    argument — the platform endpoint converts this into an HTTP 422
    response.
    """
    samples = _validate_series(series)
    w = _validate_window(window, len(samples))
    _ = _validate_alpha(alpha)

    if benchmark is not None:
        validated_benchmark = _validate_series(benchmark)
        if len(validated_benchmark) != len(samples):
            raise ValueError("benchmark must have the same length as series")
        beta_field: list[float | None] | None = rolling_beta(
            samples, validated_benchmark, w
        )
    else:
        beta_field = None

    return RollingFeatureResult(
        mean=rolling_mean(samples, w),
        std=rolling_std(samples, w),
        zscore=rolling_zscore(samples, w),
        skew=rolling_skew(samples, w),
        kurtosis=rolling_kurtosis(samples, w),
        ewma=ewma(samples, alpha),
        beta=beta_field,
    )
