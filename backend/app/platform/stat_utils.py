"""Shared statistical primitives for pure-computation platform modules.

Pure Python, no scipy/numpy. Quantiles use Hyndman-Fan type 7 linear
interpolation (NumPy's default ``method="linear"``). Student's t probabilities
are evaluated through the regularized incomplete beta function using the
Numerical Recipes ``betacf`` continued fraction with Lentz stabilization.

References: Press et al., *Numerical Recipes*, incomplete-beta ``betacf``;
Hyndman & Fan (1996), sample quantiles; scipy.stats conventions for Fisher
skewness and excess kurtosis; RiskMetrics statistical reporting conventions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Sequence

__all__ = [
    "SummaryReport",
    "betainc",
    "kurtosis",
    "percentile",
    "quantile",
    "skewness",
    "standard_error_of_mean",
    "summary_report",
    "t_cdf",
]

_BETA_EPSILON: Final = 3.0e-14
_BETA_FPMIN: Final = 1.0e-300
_BETA_MAX_ITERATIONS: Final = 200


def _validated_values(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) == 0:
        message = "values must not be empty"
        raise ValueError(message)
    data = tuple(values)
    if not all(math.isfinite(value) for value in data):
        message = "values must contain only finite numbers"
        raise ValueError(message)
    return data


def _linear_quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def quantile(values: Sequence[float], q: float) -> float:
    """Return the type-7 linear-interpolated quantile for ``q`` in ``[0, 1]``."""
    if not math.isfinite(q) or not 0.0 <= q <= 1.0:
        message = "q must be finite and in [0, 1]"
        raise ValueError(message)
    return _linear_quantile(_validated_values(values), q)


def percentile(values: Sequence[float], p: float) -> float:
    """Return the linear-interpolated percentile for ``p`` in ``[0, 100]``."""
    if not math.isfinite(p) or not 0.0 <= p <= 100.0:
        message = "p must be finite and in [0, 100]"
        raise ValueError(message)
    return quantile(values, p / 100.0)


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETA_FPMIN:
        d = _BETA_FPMIN
    d = 1.0 / d
    result = d

    for iteration in range(1, _BETA_MAX_ITERATIONS + 1):
        doubled = 2 * iteration
        coefficient = (
            iteration
            * (b - iteration)
            * x
            / ((qam + doubled) * (a + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + coefficient / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        result *= d * c

        coefficient = -(
            (a + iteration)
            * (qab + iteration)
            * x
            / ((a + doubled) * (qap + doubled))
        )
        d = 1.0 + coefficient * d
        if abs(d) < _BETA_FPMIN:
            d = _BETA_FPMIN
        c = 1.0 + coefficient / c
        if abs(c) < _BETA_FPMIN:
            c = _BETA_FPMIN
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) <= _BETA_EPSILON:
            return result

    message = "incomplete beta continued fraction did not converge"
    raise ArithmeticError(message)


def betainc(a: float, b: float, x: float) -> float:
    """Return the regularized incomplete beta function ``I_x(a, b)``.

    The direct continued fraction is used below its stable switching point;
    above that point the symmetry ``I_x(a,b) = 1 - I_(1-x)(b,a)`` is used.
    """
    if not math.isfinite(a) or a <= 0.0:
        message = "a must be finite and > 0"
        raise ValueError(message)
    if not math.isfinite(b) or b <= 0.0:
        message = "b must be finite and > 0"
        raise ValueError(message)
    if not math.isfinite(x) or not 0.0 <= x <= 1.0:
        message = "x must be finite and in [0, 1]"
        raise ValueError(message)
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0

    log_factor = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    factor = math.exp(log_factor)
    if x < (a + 1.0) / (a + b + 2.0):
        result = factor * _beta_continued_fraction(a, b, x) / a
    else:
        result = 1.0 - (
            factor * _beta_continued_fraction(b, a, 1.0 - x) / b
        )
    return min(1.0, max(0.0, result))


def t_cdf(t: float, df: float) -> float:
    """Return the Student's t cumulative distribution for positive ``df``."""
    if not math.isfinite(t):
        message = "t must be finite"
        raise ValueError(message)
    if not math.isfinite(df) or df <= 0.0:
        message = "df must be finite and > 0"
        raise ValueError(message)
    if t == 0.0:
        return 0.5

    beta_value = betainc(df / 2.0, 0.5, df / (df + t * t))
    if t < 0.0:
        return 0.5 * beta_value
    return 1.0 - 0.5 * beta_value


def _standard_deviation(values: Sequence[float], ddof: int) -> float:
    if len(values) <= ddof:
        return 0.0
    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / (
        len(values) - ddof
    )
    return math.sqrt(variance)


def _standardized_moment(
    values: Sequence[float],
    order: int,
    fisher_offset: float,
) -> float:
    standard_deviation = _standard_deviation(values, 0)
    if standard_deviation == 0.0:
        return 0.0
    mean_value = sum(values) / len(values)
    moment = sum(
        ((value - mean_value) / standard_deviation) ** order for value in values
    ) / len(values)
    return moment - fisher_offset


def skewness(values: Sequence[float]) -> float:
    """Return Fisher-Pearson sample skewness; constant series report ``0.0``."""
    return _standardized_moment(_validated_values(values), 3, 0.0)


def kurtosis(values: Sequence[float]) -> float:
    """Return Fisher excess kurtosis; constant series report ``0.0``."""
    return _standardized_moment(_validated_values(values), 4, 3.0)


def standard_error_of_mean(values: Sequence[float]) -> float:
    """Return sample standard deviation divided by ``sqrt(n)``."""
    data = _validated_values(values)
    return _standard_deviation(data, 1) / math.sqrt(len(data))


@dataclass(frozen=True, slots=True)
class SummaryReport:
    """Immutable descriptive-statistics summary for a numeric series."""

    n: int
    mean: float
    std: float
    skew: float
    kurtosis: float
    sem: float
    median: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "n": self.n,
            "mean": self.mean,
            "std": self.std,
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "sem": self.sem,
            "median": self.median,
        }


def summary_report(values: Sequence[float]) -> SummaryReport:
    """Aggregate count, location, dispersion, shape, and mean uncertainty."""
    data = _validated_values(values)
    count = len(data)
    standard_deviation = _standard_deviation(data, 1)
    return SummaryReport(
        n=count,
        mean=sum(data) / count,
        std=standard_deviation,
        skew=_standardized_moment(data, 3, 0.0),
        kurtosis=_standardized_moment(data, 4, 3.0),
        sem=standard_deviation / math.sqrt(count),
        median=_linear_quantile(data, 0.5),
    )
