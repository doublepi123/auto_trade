"""Page's tabular CUSUM sequential mean-shift detector.

The detector maintains one-sided cumulative sums for upward and downward
departures from a target mean. By default, the target is the series mean, the
allowance (slack) is ``0.5 * population_std``, and the decision threshold is
``5 * population_std``. These are conventional tabular-CUSUM calibration
choices described by Montgomery, *Introduction to Statistical Quality
Control*.

For a constant series, the population standard deviation is zero. This module
uses ``1e-12`` as an effective standard deviation for default calibration,
keeping both default slack and threshold positive while producing no signal
for observations equal to the target.

References: Page (1954), "Continuous Inspection Schemes", *Biometrika* 41;
Montgomery, *Introduction to Statistical Quality Control* (tabular CUSUM).
Pure Python; no numpy, scipy, pandas, I/O, or global mutable state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal, Sequence, TypeAlias

__all__ = ["CusumReport", "cusum"]


_CusumDirection: TypeAlias = Literal["up", "down", "both"]
_ZERO_STD_FALLBACK: Final = 1e-12


def _parse_direction(direction: str) -> _CusumDirection:
    if direction == "up":
        return "up"
    if direction == "down":
        return "down"
    if direction == "both":
        return "both"
    message = "direction must be one of: up, down, both"
    raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CusumReport:
    """Result of Page's tabular CUSUM recurrence over one scalar series."""

    direction: _CusumDirection
    target: float
    slack: float
    threshold: float
    cusum_pos: list[float]
    cusum_neg: list[float]
    signal_indices: list[int]
    n_signals: int

    def to_dict(
        self,
    ) -> dict[str, str | float | int | list[float] | list[int]]:
        """Return a JSON-compatible representation of the report."""
        return {
            "direction": self.direction,
            "target": self.target,
            "slack": self.slack,
            "threshold": self.threshold,
            "cusum_pos": list(self.cusum_pos),
            "cusum_neg": list(self.cusum_neg),
            "signal_indices": list(self.signal_indices),
            "n_signals": self.n_signals,
        }


def cusum(
    values: Sequence[float],
    target: float | None = None,
    slack: float | None = None,
    threshold: float | None = None,
    direction: str = "both",
) -> CusumReport:
    """Detect sequential upward, downward, or two-sided mean shifts.

    The upward recurrence is
    ``S_t = max(0, S_{t-1} + (x_t - target) - slack)`` and the downward
    recurrence is
    ``T_t = max(0, T_{t-1} - (x_t - target) - slack)``. An index signals when
    the selected statistic is strictly greater than ``threshold``. Two-sided
    mode returns the union of upward and downward signal indices.

    Defaults are estimated once from the full input series: its mean for
    ``target``, half its population standard deviation for ``slack``, and five
    population standard deviations for ``threshold``.

    Raises ``ValueError`` for an empty series, an unsupported direction, or an
    explicitly negative slack or threshold.
    """
    data = [float(value) for value in values]
    if not data:
        message = "values must be non-empty"
        raise ValueError(message)
    if slack is not None and slack < 0.0:
        message = "slack must be >= 0"
        raise ValueError(message)
    if threshold is not None and threshold < 0.0:
        message = "threshold must be >= 0"
        raise ValueError(message)

    selected_direction = _parse_direction(direction)

    mean = math.fsum(data) / len(data)
    variance = math.fsum((value - mean) ** 2 for value in data) / len(data)
    population_std = math.sqrt(variance)
    effective_std = (
        population_std if population_std > 0.0 else _ZERO_STD_FALLBACK
    )
    target_value = mean if target is None else float(target)
    slack_value = 0.5 * effective_std if slack is None else float(slack)
    threshold_value = 5.0 * effective_std if threshold is None else float(threshold)

    track_up = selected_direction != "down"
    track_down = selected_direction != "up"
    positive = 0.0
    negative = 0.0
    cusum_pos: list[float] = []
    cusum_neg: list[float] = []
    signal_indices: list[int] = []

    for index, value in enumerate(data):
        deviation = value - target_value
        positive = max(0.0, positive + deviation - slack_value)
        negative = max(0.0, negative - deviation - slack_value)
        cusum_pos.append(positive)
        cusum_neg.append(negative)
        if (track_up and positive > threshold_value) or (
            track_down and negative > threshold_value
        ):
            signal_indices.append(index)

    return CusumReport(
        direction=selected_direction,
        target=target_value,
        slack=slack_value,
        threshold=threshold_value,
        cusum_pos=cusum_pos,
        cusum_neg=cusum_neg,
        signal_indices=signal_indices,
        n_signals=len(signal_indices),
    )
