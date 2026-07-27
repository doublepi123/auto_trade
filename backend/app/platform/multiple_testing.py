"""Small-sample tests and family-wise p-value adjustment.

The opening-momentum research loop evaluates several challengers against the
same baseline.  A nominally significant result becomes increasingly likely by
chance as that family grows, so promotion decisions need a correction that
counts every attempted challenger.

This module keeps the calculation deterministic and dependency-free:

* ``one_sample_greater_pvalue`` performs a one-sided Student t test for a
  positive mean on paired return differences.
* ``holm_adjusted_pvalues`` applies Holm-Bonferroni family-wise error control
  while retaining ``None`` for hypotheses that do not yet have enough data.
"""

from __future__ import annotations

import math
from statistics import fmean, stdev
from typing import Sequence

from app.platform.stat_utils import t_cdf

__all__ = [
    "holm_adjusted_pvalues",
    "one_sample_greater_pvalue",
]


def one_sample_greater_pvalue(
    values: Sequence[float],
) -> float | None:
    """Return the one-sided p-value for ``mean(values) > 0``.

    ``None`` means fewer than two observations are available.  Constant
    samples are handled explicitly because their standard error is zero.
    """

    data = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in data):
        raise ValueError("values must contain only finite numbers")
    if len(data) < 2:
        return None

    mean_value = fmean(data)
    standard_error = stdev(data) / math.sqrt(len(data))
    if standard_error == 0.0:
        if mean_value > 0.0:
            return 0.0
        if mean_value < 0.0:
            return 1.0
        return 0.5

    t_statistic = mean_value / standard_error
    return min(
        1.0,
        max(0.0, 1.0 - t_cdf(t_statistic, len(data) - 1)),
    )


def holm_adjusted_pvalues(
    pvalues: Sequence[float | None],
) -> list[float | None]:
    """Return Holm-Bonferroni adjusted p-values in input order.

    Missing p-values still count toward the family size and are treated as
    ``1.0`` during adjustment.  Their returned value remains ``None`` so a
    caller cannot mistake missing evidence for a completed test.
    """

    if not pvalues:
        return []
    for pvalue in pvalues:
        if pvalue is None:
            continue
        if not math.isfinite(pvalue) or not 0.0 <= pvalue <= 1.0:
            raise ValueError("pvalues must be finite and in [0, 1]")

    family_size = len(pvalues)
    ordered = sorted(
        enumerate(pvalues),
        key=lambda item: (
            1.0 if item[1] is None else item[1],
            item[0],
        ),
    )
    adjusted = [1.0] * family_size
    running_maximum = 0.0
    for rank, (original_index, pvalue) in enumerate(ordered):
        effective_pvalue = 1.0 if pvalue is None else pvalue
        candidate = min(
            1.0,
            effective_pvalue * (family_size - rank),
        )
        running_maximum = max(running_maximum, candidate)
        adjusted[original_index] = running_maximum

    return [
        None if pvalue is None else adjusted[index]
        for index, pvalue in enumerate(pvalues)
    ]
