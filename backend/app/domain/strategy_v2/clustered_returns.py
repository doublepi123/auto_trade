from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

DEFAULT_T_CRITICAL = 2.0

# Two-sided 95% (p=.975), df=D-1; PREREGISTRATION.md section 3.2.
# Fixed like quant-v6: no SciPy/runtime-version dependency or runtime solver.
# Generated and cross-validated by tests/test_student_t_table.py.
DAY_CLUSTER_T95_BY_DF: Mapping[int, Decimal] = MappingProxyType({
    1: Decimal("12.706204736175"),
    2: Decimal("4.302652729749"),
    3: Decimal("3.182446305284"),
    4: Decimal("2.776445105198"),
    5: Decimal("2.570581835636"),
    6: Decimal("2.446911851145"),
    7: Decimal("2.364624251593"),
    8: Decimal("2.306004135204"),
    9: Decimal("2.262157162798"),
    10: Decimal("2.228138851986"),
    11: Decimal("2.200985160092"),
    12: Decimal("2.178812829667"),
    13: Decimal("2.160368656463"),
    14: Decimal("2.144786687918"),
    15: Decimal("2.131449545560"),
    16: Decimal("2.119905299221"),
    17: Decimal("2.109815577833"),
    18: Decimal("2.100922040241"),
    19: Decimal("2.093024054408"),
    20: Decimal("2.085963447266"),
    21: Decimal("2.079613844728"),
    22: Decimal("2.073873067904"),
    23: Decimal("2.068657610419"),
    24: Decimal("2.063898561628"),
    25: Decimal("2.059538552753"),
    26: Decimal("2.055529438643"),
    27: Decimal("2.051830516480"),
    28: Decimal("2.048407141795"),
    29: Decimal("2.045229642133"),
    30: Decimal("2.042272456301"),
    31: Decimal("2.039513446396"),
    32: Decimal("2.036933343460"),
    33: Decimal("2.034515297449"),
    34: Decimal("2.032244509318"),
    35: Decimal("2.030107928250"),
    36: Decimal("2.028094000980"),
    37: Decimal("2.026192463029"),
    38: Decimal("2.024394163912"),
    39: Decimal("2.022690920037"),
    40: Decimal("2.021075390306"),
    41: Decimal("2.019540970441"),
    42: Decimal("2.018081702818"),
    43: Decimal("2.016692199228"),
    44: Decimal("2.015367574444"),
    45: Decimal("2.014103388881"),
    46: Decimal("2.012895598919"),
    47: Decimal("2.011740513730"),
    48: Decimal("2.010634757624"),
    49: Decimal("2.009575237129"),
    50: Decimal("2.008559112101"),
    51: Decimal("2.007583770316"),
    52: Decimal("2.006646805062"),
    53: Decimal("2.005745995318"),
    54: Decimal("2.004879288188"),
    55: Decimal("2.004044783289"),
    56: Decimal("2.003240718848"),
    57: Decimal("2.002465459291"),
    58: Decimal("2.001717484145"),
    59: Decimal("2.000995378088"),
    60: Decimal("2.000297822014"),
    61: Decimal("1.999623584995"),
    62: Decimal("1.998971517033"),
    63: Decimal("1.998340542521"),
    64: Decimal("1.997729654318"),
    65: Decimal("1.997137908392"),
    66: Decimal("1.996564418952"),
    67: Decimal("1.996008354025"),
    68: Decimal("1.995468931430"),
    69: Decimal("1.994945415107"),
    70: Decimal("1.994437111771"),
    71: Decimal("1.993943367846"),
    72: Decimal("1.993463566662"),
    73: Decimal("1.992997125890"),
    74: Decimal("1.992543495181"),
    75: Decimal("1.992102154002"),
    76: Decimal("1.991672609645"),
    77: Decimal("1.991254395388"),
    78: Decimal("1.990847068812"),
    79: Decimal("1.990450210230"),
    80: Decimal("1.990063421254"),
    81: Decimal("1.989686323457"),
    82: Decimal("1.989318557137"),
    83: Decimal("1.988959780175"),
    84: Decimal("1.988609666976"),
    85: Decimal("1.988267907477"),
    86: Decimal("1.987934206239"),
    87: Decimal("1.987608281589"),
    88: Decimal("1.987289864831"),
    89: Decimal("1.986978699506"),
    90: Decimal("1.986674540704"),
    91: Decimal("1.986377154419"),
    92: Decimal("1.986086316951"),
    93: Decimal("1.985801814346"),
    94: Decimal("1.985523441867"),
    95: Decimal("1.985251003505"),
    96: Decimal("1.984984311522"),
    97: Decimal("1.984723186014"),
    98: Decimal("1.984467454508"),
    99: Decimal("1.984216951586"),
    100: Decimal("1.983971518524"),
    101: Decimal("1.983731002956"),
    102: Decimal("1.983495258563"),
    103: Decimal("1.983264144773"),
    104: Decimal("1.983037526484"),
    105: Decimal("1.982815273795"),
    106: Decimal("1.982597261765"),
    107: Decimal("1.982383370176"),
    108: Decimal("1.982173483308"),
    109: Decimal("1.981967489736"),
    110: Decimal("1.981765282132"),
    111: Decimal("1.981566757075"),
    112: Decimal("1.981371814876"),
    113: Decimal("1.981180359415"),
    114: Decimal("1.980992297976"),
    115: Decimal("1.980807541104"),
    116: Decimal("1.980626002459"),
    117: Decimal("1.980447598683"),
    118: Decimal("1.980272249273"),
    119: Decimal("1.980099876457"),
    120: Decimal("1.979930405082"),
})


def day_cluster_t_critical(count: int) -> float:
    """Look up the preregistered two-sided 95% Student-t bar for D clusters."""
    critical = DAY_CLUSTER_T95_BY_DF.get(count - 1)
    if critical is None:
        message = (
            "day cluster count exceeds the fixed t table (supported: 2..121)"
            if count > 121 else "day cluster count is below the fixed t table (supported: 2..121)"
        )
        raise ValueError(message)
    return float(critical)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1))


def _t_statistic(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    standard_deviation = _stdev(values)
    if standard_deviation <= 0:
        return None
    return _mean(values) / (standard_deviation / math.sqrt(len(values)))


@dataclass(frozen=True)
class ClusteredTTestResult:
    observations: int
    distinct_days: int
    naive_t: float | None
    clustered_t: float | None
    naive_mean: float | None
    day_mean: float | None
    clustered_standard_error: float | None
    ci_lower: float | None
    ci_upper: float | None
    inflation_factor: float | None
    significant: bool
    t_critical: float | None = None
    degrees_of_freedom: int | None = None


def clustered_t_test(
    observations: Sequence[tuple[date, float]],
    *,
    t_critical: float | None = None,
) -> ClusteredTTestResult:
    """Test the per-trade mean with a day-clustered robust standard error.

    For the intercept-only per-trade estimand, each cluster contributes the sum
    of its trade residuals. The CR1 ``G / (G - 1)`` correction keeps cluster
    influence proportional to trade count instead of estimating an average day.
    """
    if t_critical is not None and t_critical <= 0:
        raise ValueError("t_critical must be positive")

    values = [value for _, value in observations]
    if not values:
        return ClusteredTTestResult(
            observations=0,
            distinct_days=0,
            naive_t=None,
            clustered_t=None,
            naive_mean=None,
            day_mean=None,
            clustered_standard_error=None,
            ci_lower=None,
            ci_upper=None,
            inflation_factor=None,
            significant=False,
            t_critical=t_critical,
        )

    by_day: dict[date, list[float]] = {}
    for day, value in observations:
        by_day.setdefault(day, []).append(value)
    day_means = [_mean(day_values) for day_values in by_day.values()]

    trade_mean = _mean(values)
    naive_t = _t_statistic(values)
    distinct_days = len(by_day)
    critical = t_critical
    if critical is None and distinct_days >= 2:
        critical = day_cluster_t_critical(distinct_days)
    clustered_standard_error: float | None = None
    if distinct_days >= 2:
        cluster_score_squares = sum(
            sum(value - trade_mean for value in day_values) ** 2
            for day_values in by_day.values()
        )
        variance = (
            distinct_days
            / (distinct_days - 1)
            * cluster_score_squares
            / (len(values) ** 2)
        )
        if variance > 0:
            clustered_standard_error = math.sqrt(variance)
    clustered_t = (
        trade_mean / clustered_standard_error
        if clustered_standard_error is not None
        else None
    )
    ci_lower = (
        trade_mean - critical * clustered_standard_error
        if clustered_standard_error is not None and critical is not None
        else None
    )
    ci_upper = (
        trade_mean + critical * clustered_standard_error
        if clustered_standard_error is not None and critical is not None
        else None
    )
    return ClusteredTTestResult(
        observations=len(values),
        distinct_days=distinct_days,
        naive_t=naive_t,
        clustered_t=clustered_t,
        naive_mean=trade_mean,
        day_mean=_mean(day_means),
        clustered_standard_error=clustered_standard_error,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        inflation_factor=math.sqrt(len(values) / distinct_days),
        significant=clustered_t is not None and critical is not None and clustered_t > critical,
        t_critical=critical,
        degrees_of_freedom=distinct_days - 1 if distinct_days >= 2 else None,
    )


__all__ = [
    "ClusteredTTestResult", "DEFAULT_T_CRITICAL", "DAY_CLUSTER_T95_BY_DF",
    "clustered_t_test", "day_cluster_t_critical",
]
