"""P354: Seasonality analysis — day-of-week and month effects.

Computes mean return, standard deviation, t-statistic, and sample count
for returns grouped by day-of-week and/or calendar month. Flags effects
with |t-stat| > 2 as significant.

Public surface
--------------
* **seasonality_report(returns, day_of_week, months)** — frozen
  :class:`SeasonalityResult` with per-group statistics and significant
  effect flags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SeasonalityResult",
    "seasonality_report",
]


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _validate_returns(returns: Sequence[float]) -> list[float]:
    """Validate the returns series."""
    if isinstance(returns, list):
        materialised = returns
    else:
        try:
            materialised = list(returns)
        except TypeError as exc:
            raise ValueError("returns must be a sequence of finite numbers") from exc
    if len(materialised) == 0:
        raise ValueError("returns must be non-empty")
    coerced: list[float] = []
    for value in materialised:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("returns entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("returns entries must be finite numbers")
        coerced.append(number)
    return coerced


def _validate_labels(
    labels: Sequence[int],
    valid_set: set[int],
    name: str,
    n_returns: int,
) -> list[int]:
    """Validate a list of integer labels matching returns length."""
    if isinstance(labels, list):
        materialised = labels
    else:
        try:
            materialised = list(labels)
        except TypeError as exc:
            raise ValueError(f"{name} must be a list of ints") from exc
    if len(materialised) != n_returns:
        raise ValueError(f"{name} length must match returns length ({n_returns})")
    result: list[int] = []
    for value in materialised:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} entries must be ints")
        if value not in valid_set:
            raise ValueError(f"{name} value {value} is invalid")
        result.append(value)
    return result


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def _group_stats(
    returns: list[float], labels: list[int]
) -> dict[int, dict[str, float]]:
    """Compute mean, std, t_stat, n for each label group."""
    groups: dict[int, list[float]] = {}
    for r, label in zip(returns, labels):
        groups.setdefault(label, []).append(r)

    result: dict[int, dict[str, float]] = {}
    for label, values in groups.items():
        n = len(values)
        mean = sum(values) / n
        if n >= 2:
            var_sum = sum((v - mean) ** 2 for v in values)
            std = math.sqrt(var_sum / n)  # population std
        else:
            std = 0.0
        # t-stat = mean / (std / sqrt(n))  (two-sided test against zero)
        if std > 0 and n >= 2:
            t_stat = mean / (std / math.sqrt(n))
        else:
            t_stat = 0.0
        result[label] = {
            "mean": mean,
            "std": std,
            "t_stat": t_stat,
            "n": n,
        }
    return result


# ---------------------------------------------------------------------------
# dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonalityResult:
    """Result of :func:`seasonality_report`."""

    day_of_week_effects: dict[int, dict[str, float]]
    month_effects: dict[int, dict[str, float]]
    significant_effects: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_of_week_effects": self.day_of_week_effects,
            "month_effects": self.month_effects,
            "significant_effects": self.significant_effects,
        }


# ---------------------------------------------------------------------------
# public function
# ---------------------------------------------------------------------------


def seasonality_report(
    returns: Sequence[float],
    *,
    day_of_week: list[int] | None = None,
    months: list[int] | None = None,
) -> SeasonalityResult:
    """Analyze seasonal patterns in a return series.

    Parameters
    ----------
    returns:
        Period returns (e.g. daily).
    day_of_week:
        Day-of-week labels (0=Mon ... 6=Sun). Defaults to a cycle of
        [0,1,2,3,4] repeating to match ``returns`` length.
    months:
        Month labels (1=Jan ... 12=Dec). Defaults to a cycle of 1..12
        repeating to match ``returns`` length.

    Returns
    -------
    SeasonalityResult
        Per-group statistics and list of significant effects (|t-stat| > 2).

    Raises
    ------
    ValueError
        On any invalid input.
    """
    data = _validate_returns(returns)
    n = len(data)

    # Resolve defaults
    if day_of_week is None:
        # Default: Mon-Fri repeating
        base_dow = [0, 1, 2, 3, 4]
        day_of_week = [base_dow[i % 5] for i in range(n)]
    else:
        day_of_week = _validate_labels(day_of_week, set(range(7)), "day_of_week", n)

    if months is None:
        # Default: Jan-Dec repeating
        month_labels = [((i % 12) + 1) for i in range(n)]
    else:
        month_labels = _validate_labels(months, set(range(1, 13)), "months", n)

    # Compute stats
    dow_effects = _group_stats(data, day_of_week)
    month_effects = _group_stats(data, month_labels)

    # Collect significant effects (|t_stat| > 2)
    significant: list[dict[str, Any]] = []
    for key, stats in dow_effects.items():
        if abs(stats["t_stat"]) > 2:
            significant.append({
                "type": "day_of_week",
                "key": key,
                "t_stat": stats["t_stat"],
                "mean": stats["mean"],
                "n": stats["n"],
            })
    for key, stats in month_effects.items():
        if abs(stats["t_stat"]) > 2:
            significant.append({
                "type": "month",
                "key": key,
                "t_stat": stats["t_stat"],
                "mean": stats["mean"],
                "n": stats["n"],
            })

    return SeasonalityResult(
        day_of_week_effects=dow_effects,
        month_effects=month_effects,
        significant_effects=significant,
    )
