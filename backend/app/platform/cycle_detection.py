"""P260: Cycle detection — autocorrelation, Ljung-Box, seasonal strength.

Pure-Python time-domain cycle diagnostics for a uniformly-sampled scalar
series. No numpy/scipy/pandas dependency; autocorrelations are computed
directly with elementary arithmetic.

Public surface
--------------

* **autocorrelation(series, max_lag)** — sample autocorrelation function (ACF)
  for lags ``0 .. max_lag``. ``acf[0]`` is always ``1.0`` (a series is
  perfectly correlated with itself).
* **ljung_box_stat(acf, n)** — the Ljung-Box portmanteau statistic (a
  non-negative scalar that grows when many lags carry significant
  autocorrelation). This is the statistic itself, **not** a p-value.
* **detect_cycles(series, min_period, max_period)** — frozen
  :class:`CycleDetectionResult` aggregating cycle candidates scored by their
  positive autocorrelation peaks, plus a normalised ``seasonal_strength``
  in ``[0, 1]``.

Conventions
-----------

* The series is assumed uniformly sampled; physical period = ``lag`` samples.
* A *positive autocorrelation peak* at lag ``p`` means the series resembles a
  shifted copy of itself ``p`` samples earlier — the hallmark of a repeating
  cycle of period ``p``.
* ``seasonal_strength`` contrasts the strongest positive autocorrelation peak
  against the typical lag-1 autocorrelation of the same series. A purely
  seasonal (repeating) series scores near 1; a pure monotonic trend (whose
  autocorrelation decays smoothly without secondary peaks) scores near 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "CycleCandidate",
    "CycleDetectionResult",
    "autocorrelation",
    "detect_cycles",
    "ljung_box_stat",
]


_MIN_SERIES = 4
"""Lower bound on input length — autocorrelation is meaningless below this."""


def _validate_series(series: Sequence[float]) -> list[float]:
    """Coerce ``series`` to ``list[float]`` after validating each entry.

    Raises ``ValueError`` for any invalid input: a non-iterable scalar, an
    empty / too-short series, or a non-finite / non-numeric entry. (P260
    audit: the public surface raises ``ValueError`` uniformly so callers —
    and the platform endpoint — map every invalid-argument case to HTTP 422
    without distinguishing ``TypeError`` from ``ValueError``.)
    """
    if isinstance(series, list):
        materialised = series
    else:
        # Accept any sequence (e.g. tuple) by materialising a list first; a
        # non-iterable scalar surfaces as a ValueError so callers see a single
        # invalid-argument exception type.
        try:
            materialised = list(series)
        except TypeError as exc:  # pragma: no cover - defensive
            raise ValueError("series must be a sequence of finite numbers") from exc
    if len(materialised) < _MIN_SERIES:
        raise ValueError(f"series must contain at least {_MIN_SERIES} values")
    coerced: list[float] = []
    for value in materialised:
        # ``bool`` is a subclass of ``int``; reject it explicitly so ``True``
        # is not silently coerced to ``1.0``.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("series entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("series entries must be finite numbers")
        coerced.append(number)
    return coerced


def autocorrelation(series: Sequence[float], max_lag: int) -> list[float]:
    """Sample autocorrelation function (ACF) of ``series`` for lags ``0..max_lag``.

    For lag ``k`` the sample autocorrelation is

        ρ(k) = Σ_{t=0}^{N-1-k} (x_t − x̄)(x_{t+k} − x̄) / Σ_{t=0}^{N-1} (x_t − x̄)²

    where ``x̄`` is the full-sample mean. ``acf[0]`` is always ``1.0`` by
    construction (a series is perfectly correlated with itself).

    Parameters
    ----------
    series:
        Non-empty list of finite numbers of length at least ``_MIN_SERIES``.
    max_lag:
        Maximum lag to evaluate; must satisfy ``1 <= max_lag < len(series)``.

    Raises ``ValueError`` on invalid input (P260 audit: the public surface
    raises ``ValueError`` uniformly — including for non-int / bool ``max_lag`` —
    so callers and the platform endpoint map every invalid-argument case to
    HTTP 422 without distinguishing ``TypeError`` from ``ValueError``).
    """
    samples = _validate_series(series)
    n = len(samples)
    # ``bool`` is a subclass of ``int`` (``True == 1``) — reject it explicitly
    # so ``autocorrelation(series, max_lag=True)`` is not silently accepted as
    # lag 1.
    if isinstance(max_lag, bool) or not isinstance(max_lag, int):
        raise ValueError("max_lag must be an int")
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    if max_lag >= n:
        raise ValueError(f"max_lag must be < len(series) ({n})")

    mean = sum(samples) / n
    deviations = [x - mean for x in samples]
    # Denominator is the full-sample variance proxy (sum of squared deviations).
    # Guard against the degenerate constant series where this is zero.
    denom = sum(d * d for d in deviations)
    if denom <= 0.0:
        # Constant series ⇒ perfectly autocorrelated at every lag by definition.
        return [1.0 for _ in range(max_lag + 1)]

    acf: list[float] = [1.0]
    for k in range(1, max_lag + 1):
        cov = 0.0
        for t in range(n - k):
            cov += deviations[t] * deviations[t + k]
        acf.append(cov / denom)
    return acf


def ljung_box_stat(acf: Sequence[float], n: int) -> float:
    """Ljung-Box portmanteau statistic for a sample ACF over ``n`` observations.

    Defined as

        Q = n(n + 2) · Σ_{k=1}^{m} ρ(k)² / (n − k)

    where ``m = len(acf) − 1`` (the number of non-zero-lag autocorrelations).
    ``Q`` is a non-negative scalar that grows when many lags carry
    significant autocorrelation. This returns the **statistic**, not a
    p-value (no chi-square CDF dependency).

    Parameters
    ----------
    acf:
        Sample autocorrelations with ``acf[0] == 1.0`` (lag 0 is ignored).
    n:
        Number of observations used to estimate ``acf`` (must exceed ``m``).

    Raises ``ValueError`` for malformed input (P260 audit: the public surface
    raises ``ValueError`` uniformly — including for non-sequence / non-numeric
    ``acf`` entries and bool / non-int ``n`` — so callers and the platform
    endpoint map every invalid-argument case to HTTP 422 without
    distinguishing ``TypeError`` from ``ValueError``).
    """
    if isinstance(acf, list):
        acf_list = acf
    else:
        try:
            acf_list = list(acf)
        except TypeError as exc:  # pragma: no cover - defensive
            raise ValueError("acf must be a sequence of floats") from exc
    if len(acf_list) < 2:
        raise ValueError("acf must contain lag-0 plus at least one non-zero lag")
    for rho in acf_list:
        # ``bool`` is a subclass of ``int``; reject it (and any other
        # non-numeric type) so callers see a uniform invalid-argument error.
        if isinstance(rho, bool) or not isinstance(rho, (int, float)):
            raise ValueError("acf entries must be finite numbers")
        if not math.isfinite(float(rho)):
            raise ValueError("acf entries must be finite numbers")
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("n must be an int")
    if n < len(acf_list):
        raise ValueError("n must be >= len(acf) (one observation per lag + lag-0)")
    m = len(acf_list) - 1
    total = 0.0
    for k in range(1, m + 1):
        rho = acf_list[k]
        total += (rho * rho) / (n - k)
    return n * (n + 2) * total


@dataclass(frozen=True)
class CycleCandidate:
    """A candidate cycle detected by :func:`detect_cycles`.

    * ``period`` — integer lag (in samples) of the candidate cycle.
    * ``autocorrelation`` — sample ACF value at ``period``.
    * ``score`` — composite score used for ranking (``>= 0``); higher = stronger.
    """

    period: int
    autocorrelation: float
    score: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (``period`` / ``autocorrelation`` / ``score``).

        :meth:`CycleDetectionResult.to_dict` reuses this so the candidate
        shape stays a single source of truth.
        """
        return {
            "period": self.period,
            "autocorrelation": self.autocorrelation,
            "score": self.score,
        }


@dataclass(frozen=True)
class CycleDetectionResult:
    """Aggregated cycle-detection diagnostics for a uniformly-sampled series.

    * ``candidates`` — ``CycleCandidate`` list sorted by descending ``score``;
      empty when no positive autocorrelation peak passes ``min_period``.
    * ``seasonal_strength`` — strength of seasonality in ``[0, 1]``. A purely
      repeating pattern scores near 1; a pure monotonic trend scores near 0.
    * ``ljung_box_stat`` — portmanteau statistic over the candidate lag range.
    * ``n`` — number of input samples.
    * ``max_period`` — maximum candidate period scanned.
    """

    candidates: list[CycleCandidate]
    seasonal_strength: float
    ljung_box_stat: float
    n: int
    max_period: int

    def to_dict(self) -> dict[str, Any]:
        return {
            # Reuse ``CycleCandidate.to_dict`` so the candidate shape has a
            # single source of truth (P260 audit).
            "candidates": [c.to_dict() for c in self.candidates],
            "seasonal_strength": self.seasonal_strength,
            "ljung_box_stat": self.ljung_box_stat,
            "n": self.n,
            "max_period": self.max_period,
        }


def _score_candidate(acf_value: float, period: int, n: int) -> float:
    """Composite score for an autocorrelation peak.

    Rewards high positive autocorrelation and penalises short periods slightly
    (so that among equally-strong peaks, longer periods — which carry more
    statistical evidence per cycle — are not drowned out by aliasing).
    """
    base = max(acf_value, 0.0)
    # Length penalty: very short periods are easily aliased; dampen them so a
    # noisy lag-2 spike cannot outrank the genuine lag-5 cycle. The penalty
    # saturates quickly (``min(1, period/4)``) so it only bites below period 4.
    reliability = min(1.0, period / 4.0)
    # Small finite-sample correction so candidates near the end of the ACF
    # (which have fewer overlapping terms) are not overweighted.
    sample_factor = (n - period) / n
    return base * reliability * sample_factor


def detect_cycles(
    series: Sequence[float],
    min_period: int = 2,
    max_period: int | None = None,
) -> CycleDetectionResult:
    """Detect candidate cycle periods in ``series`` via autocorrelation.

    Parameters
    ----------
    series:
        Non-empty list of finite numbers (length >= ``_MIN_SERIES``, and at
        least ``max_period + 1``).
    min_period:
        Minimum candidate period (>= 2; lag-1 is trend, not seasonality).
    max_period:
        Maximum candidate period; must be ``>= min_period`` and ``< len(series)``.
        Defaults to ``min(len(series) // 2, 24)`` when ``None``.

    Returns a :class:`CycleDetectionResult`. Raises ``ValueError`` on invalid
    input (P260 audit: the public surface raises ``ValueError`` uniformly —
    including for non-int / bool ``min_period`` / ``max_period`` — so the
    platform endpoint maps every invalid-argument case to HTTP 422 without
    distinguishing ``TypeError`` from ``ValueError``).
    """
    samples = _validate_series(series)
    n = len(samples)
    if isinstance(min_period, bool) or not isinstance(min_period, int):
        raise ValueError("min_period must be an int")
    if min_period < 2:
        raise ValueError("min_period must be >= 2")

    if max_period is None:
        max_period = min(n // 2, 24)
    else:
        if isinstance(max_period, bool) or not isinstance(max_period, int):
            raise ValueError("max_period must be an int or None")
    if max_period < min_period:
        raise ValueError("max_period must be >= min_period")
    if max_period >= n:
        raise ValueError(
            f"series too short: need at least {max_period + 1} samples, got {n}"
        )

    acf = autocorrelation(samples, max_lag=max_period)
    candidates: list[CycleCandidate] = []
    for period in range(min_period, max_period + 1):
        acf_value = acf[period]
        if acf_value <= 0.0:
            # Only positive autocorrelation peaks indicate a repeating cycle.
            continue
        score = _score_candidate(acf_value, period, n)
        if score <= 0.0:
            continue
        candidates.append(CycleCandidate(period=period, autocorrelation=acf_value, score=score))

    # Rank by score (descending); ties broken by smaller period (more parsimonious).
    candidates.sort(key=lambda c: (-c.score, c.period))

    # Ljung-Box statistic over the scanned lag range (lags 1..max_period).
    lb_stat = ljung_box_stat(acf, n=n)

    seasonal_strength = _seasonal_strength(acf, min_period, max_period)

    return CycleDetectionResult(
        candidates=candidates,
        seasonal_strength=seasonal_strength,
        ljung_box_stat=lb_stat,
        n=n,
        max_period=max_period,
    )


def _seasonal_strength(
    acf: Sequence[float],
    min_period: int,
    max_period: int,
) -> float:
    """Normalised strength of seasonality in ``[0, 1]``.

    Compares the strongest positive autocorrelation peak inside the candidate
    range ``[min_period, max_period]`` against the lag-1 autocorrelation. A
    pure seasonal series has a sharp peak well above its lag-1 baseline; a
    pure monotonic trend has lag-1 ≈ 1 and smoothly decaying autocorrelations
    with no secondary peak, so the ratio (and hence the strength) is low.
    """
    if len(acf) < 2:
        return 0.0
    lag1 = acf[1]
    # The candidate-range peak — only positive autocorrelation counts as
    # seasonality; negative peaks indicate anti-persistence instead.
    peak = 0.0
    for k in range(min_period, min(max_period + 1, len(acf))):
        if acf[k] > peak:
            peak = acf[k]
    if peak <= 0.0:
        return 0.0
    # Reference scale: the larger of lag-1 autocorrelation and the peak itself,
    # so the ratio is bounded by 1 even when the peak exceeds lag-1 (a clean
    # periodic signal whose lag-1 is near zero).
    scale = max(abs(lag1), peak, 1e-12)
    ratio = peak / scale
    # Clamp into [0, 1] for numerical safety (the ratio is already in range by
    # construction, but rounding can nudge it infinitesimally over 1).
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio
