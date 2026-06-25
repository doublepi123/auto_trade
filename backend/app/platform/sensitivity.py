"""P231: Parameter Importance & Sensitivity (fANOVA-style).

From walk-forward (or grid-search) results, quantify how much each parameter
axis contributes to the variability of the objective metric — the
quantitative-trading analog of Optuna's `param_importances` and fANOVA
(Saltelli / Sobol first-order indices computed by a mean-based estimator over
the parameter grid).

* **first_order_sobol** — Saltelli-style first-order sensitivity index per
  parameter axis: ``S_i = Var_{x_i}[ E_{x_{-i}}[Y] ] / Var[Y]``. We estimate it
  by partitioning the grid by the value of axis ``i`` and computing the
  between-group variance of the per-group means, divided by total variance.
  Large ``S_i`` ⇒ that parameter drives most of the result's variance ⇒ it
  is the one to tune carefully and to keep stable.
* **interaction / total-order** — ``S_T_i = 1 − Var_{x_{-i}}[E_{x_i}[Y]] / Var[Y]``
  (the "unconditional on i" complement); the gap ``S_T_i − S_i`` flags
  interactions involving axis ``i``.
* **importance ranking** — axes sorted by total-order index.

Deterministic, pure Python. Reference: Saltelli (2010) variance-based SA,
Optuna fANOVA, Hutter et al. (2014) Bayesian param importance.

Input shape: a list of ``{params: {axis: value}, metric: float}`` records
(same shape as :mod:`app.platform.stability_analysis` consumes).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "SensitivityReport",
    "first_order_sobol",
    "total_order_sobol",
    "parameter_importance",
]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def _group_by_axis(records: Sequence[dict], axis: str) -> dict[Any, list[float]]:
    groups: dict[Any, list[float]] = {}
    for r in records:
        key = r.get("params", {}).get(axis)
        groups.setdefault(key, []).append(float(r.get("metric", 0.0)))
    return groups


def _total_variance(records: Sequence[dict]) -> float:
    vals = [float(r.get("metric", 0.0)) for r in records]
    return _variance(vals)


def first_order_sobol(records: Sequence[dict], axis: str) -> float:
    """First-order sensitivity index for one parameter axis.

    ``S_i = Var_{x_i}[ E_{x_{-i}}[Y] ] / Var[Y]`` estimated via between-group
    variance of per-``x_i`` means (law of total variance). Returns 0 when the
    total variance is degenerate.
    """
    if len(records) < 2:
        return 0.0
    vt = _total_variance(records)
    if vt <= 0:
        return 0.0
    groups = _group_by_axis(records, axis)
    if len(groups) < 2:
        return 0.0
    # weight by group size for population between-group variance
    total_n = sum(len(g) for g in groups.values())
    between = 0.0
    grand = _mean([float(r.get("metric", 0.0)) for r in records])
    for g in groups.values():
        if not g:
            continue
        between += len(g) * (_mean(g) - grand) ** 2
    between /= total_n
    # divide by population variance (use n, not n-1) for a clean [0,1] index
    vals = [float(r.get("metric", 0.0)) for r in records]
    pop_var = sum((v - grand) ** 2 for v in vals) / len(vals)
    if pop_var <= 0:
        return 0.0
    return between / pop_var


def total_order_sobol(records: Sequence[dict], axis: str) -> float:
    """Total-order sensitivity index for one parameter axis.

    ``S_T_i = E_{x_{-i}}[ Var_{x_i}[Y] ] / Var[Y]`` ⇒ ``S_T_i = 1 −
    Var_{x_{-i}}[ E_{x_i}[Y] ] / Var[Y]``. Estimated by grouping on the
    *complement* of ``axis`` (all other axes' joint value) and computing the
    within-group variance of the conditional means on ``axis``. Returns 0 if
    degenerate.
    """
    if len(records) < 2:
        return 0.0
    vt = _total_variance(records)
    if vt <= 0:
        return 0.0
    # group by the joint value of all OTHER axes
    groups: dict[Any, list[float]] = {}
    for r in records:
        params = r.get("params", {})
        key = tuple(sorted((k, v) for k, v in params.items() if k != axis))
        groups.setdefault(key, []).append(float(r.get("metric", 0.0)))
    # within each complement-group, partition by axis value → conditional means
    grand = _mean([float(r.get("metric", 0.0)) for r in records])
    pop_var = sum((v - grand) ** 2 for v in [float(r.get("metric", 0.0)) for r in records]) / len(records)
    if pop_var <= 0:
        return 0.0
    # E_{x_-i}[ Var_{x_i}[Y] ] ≈ mean over complement-groups of the within-group variance,
    # but we want the complement of the *first-order* on axis i computed within each group.
    # Saltelli: S_T_i = 1 - (Var of E_{x_i}[Y | x_-i] over x_-i) / Var[Y].
    # E_{x_i}[Y | x_-i] = mean within the subgroup of the complement-group where axis varies.
    conditional_means: list[float] = []
    cond_weights: list[int] = []
    for key, vals in groups.items():
        if len(vals) < 2:
            # axis doesn't vary within this complement group → conditional mean is degenerate
            conditional_means.append(_mean(vals))
            cond_weights.append(len(vals))
            continue
        # axis varies within → conditional mean over axis = group mean (already marginalized)
        # For the "Var over x_-i of E_{x_i}[Y|x_-i]" we take the per-group mean.
        conditional_means.append(_mean(vals))
        cond_weights.append(len(vals))
    total_w = sum(cond_weights)
    if total_w == 0:
        return 0.0
    weighted_mean = sum(m * w for m, w in zip(conditional_means, cond_weights)) / total_w
    var_of_cond = sum(w * (m - weighted_mean) ** 2 for m, w in zip(conditional_means, cond_weights)) / total_w
    return max(0.0, 1.0 - var_of_cond / pop_var)


@dataclass(frozen=True)
class SensitivityReport:
    axes: list[str]
    first_order: dict[str, float]
    total_order: dict[str, float]
    interaction: dict[str, float]  # S_T_i - S_i
    importance_ranking: list[str]  # axes sorted by total_order desc

    def to_dict(self) -> dict:
        return {
            "axes": self.axes,
            "first_order": self.first_order,
            "total_order": self.total_order,
            "interaction": self.interaction,
            "importance_ranking": self.importance_ranking,
        }


def parameter_importance(records: Sequence[dict]) -> SensitivityReport:
    """Compute first-order + total-order sensitivity for every parameter axis."""
    if not records:
        raise ValueError("records must be non-empty")
    axes: set[str] = set()
    for r in records:
        axes.update(r.get("params", {}).keys())
    axes_list = sorted(axes)
    first = {a: first_order_sobol(records, a) for a in axes_list}
    total = {a: total_order_sobol(records, a) for a in axes_list}
    inter = {a: max(0.0, total[a] - first[a]) for a in axes_list}
    ranking = sorted(axes_list, key=lambda a: total[a], reverse=True)
    return SensitivityReport(
        axes=axes_list,
        first_order=first,
        total_order=total,
        interaction=inter,
        importance_ranking=ranking,
    )