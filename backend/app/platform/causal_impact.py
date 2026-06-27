"""P321: Causal Impact — counterfactual prediction via OLS.

Estimates the causal effect of an intervention on a target time-series by
fitting an OLS regression of ``target ~ control`` on the pre-intervention
period and comparing post-intervention actuals against the predicted
counterfactual.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


__all__ = ["CausalImpactResult", "causal_impact_report"]


@dataclass(frozen=True)
class CausalImpactResult:
    """Frozen aggregate result of :func:`causal_impact_report`.

    * ``causal_effect`` — mean(actual_post − predicted_post).
    * ``standard_error`` — standard error of the causal effect.
    * ``p_value`` — approximate two-tailed p-value (t-distribution with
      ``n_pre − 2`` degrees of freedom).
    * ``n_pre`` / ``n_post`` — observation counts per period.
    """

    causal_effect: float
    standard_error: float
    p_value: float
    n_pre: int
    n_post: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "causal_effect": self.causal_effect,
            "standard_error": self.standard_error,
            "p_value": self.p_value,
            "n_pre": self.n_pre,
            "n_post": self.n_post,
        }


def _validate_inputs(
    target: list[float], control: list[float], intervention_index: int
) -> tuple[list[float], list[float], int]:
    """Validate and return cleaned inputs, raising ValueError on any issue."""
    if len(target) != len(control):
        raise ValueError("target and control must have equal length")
    n = len(target)
    if n < 4:
        raise ValueError("need at least 4 observations")
    if not isinstance(intervention_index, int) or isinstance(intervention_index, bool):
        raise ValueError("intervention_index must be an int")
    if intervention_index < 2 or intervention_index > n - 2:
        raise ValueError(
            f"intervention_index must be in [2, {n - 2}] "
            f"(at least 2 pre and 2 post observations)"
        )
    for i, (t, c) in enumerate(zip(target, control)):
        if not math.isfinite(t) or not math.isfinite(c):
            raise ValueError(f"target and control must be finite at index {i}")
    return target, control, intervention_index


def causal_impact_report(
    target: list[float], control: list[float], intervention_index: int
) -> CausalImpactResult:
    """Estimate causal impact via OLS counterfactual.

    Parameters
    ----------
    target : list[float]
        The outcome time-series.
    control : list[float]
        A control time-series used to predict the target.
    intervention_index : int
        Index of the intervention. ``target[:intervention_index]`` is the
        pre-period, ``target[intervention_index:]`` is the post-period.

    Returns
    -------
    CausalImpactResult
    """
    target, control, idx = _validate_inputs(target, control, intervention_index)

    pre_t = target[:idx]
    pre_c = control[:idx]
    n_pre = len(pre_t)

    # OLS: target = alpha + beta * control + epsilon
    mean_t = sum(pre_t) / n_pre
    mean_c = sum(pre_c) / n_pre

    numerator = 0.0
    denominator = 0.0
    for t, c in zip(pre_t, pre_c):
        dt = t - mean_t
        dc = c - mean_c
        numerator += dt * dc
        denominator += dc * dc

    if denominator < 1e-15:
        # control is constant → beta = 0, alpha = mean_t
        beta = 0.0
        alpha = mean_t
    else:
        beta = numerator / denominator
        alpha = mean_t - beta * mean_c

    # Compute residuals in pre-period for standard error
    residuals = [t - (alpha + beta * c) for t, c in zip(pre_t, pre_c)]
    rss = sum(r * r for r in residuals)
    # Residual standard error (unbiased: divide by n_pre - 2)
    if n_pre > 2:
        sigma = math.sqrt(rss / (n_pre - 2))
    else:
        sigma = 0.0

    # Predict counterfactual in post-period
    post_t = target[idx:]
    post_c = control[idx:]
    n_post = len(post_t)

    predicted = [alpha + beta * c for c in post_c]
    diffs = [t - p for t, p in zip(post_t, predicted)]
    causal_effect = sum(diffs) / n_post

    # Standard error of the causal effect (pointwise prediction SE)
    # SE = sigma * sqrt(1/n_post + 1/n_pre + (mean_post_c - mean_pre_c)^2 / SSX)
    mean_post_c = sum(post_c) / n_post
    ssx = denominator  # sum of squared deviations in pre control

    if sigma > 0 and ssx > 1e-15:
        se = sigma * math.sqrt(
            1.0 / n_post + 1.0 / n_pre + (mean_post_c - mean_c) ** 2 / ssx
        )
    elif sigma > 0:
        se = sigma * math.sqrt(1.0 / n_post + 1.0 / n_pre)
    else:
        se = 0.0

    # Approximate p-value via t-distribution
    if se > 1e-15 and n_pre > 2:
        t_stat = causal_effect / se
        # Use normal approximation for t(n_pre-2)
        from app.platform._math_utils import norm_cdf

        p_value = 2.0 * (1.0 - norm_cdf(abs(t_stat)))
    else:
        p_value = 1.0

    return CausalImpactResult(
        causal_effect=causal_effect,
        standard_error=se,
        p_value=max(0.0, min(1.0, p_value)),
        n_pre=n_pre,
        n_post=n_post,
    )
