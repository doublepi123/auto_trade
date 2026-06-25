"""P222: Walk-Forward Parameter Stability Diagnostics.

From walk-forward optimizer results (per-window IS/OOS metric pairs per
parameter set), quantify how stably an in-sample optimum generalizes:

* **degradation** — IS-vs-OOS ratio per window (clipped, sign-aware), with the
  average rank-correlation between IS and OOS rankings across param-sets.
* **neighborhood stability** — for each parameter axis, how much the metric
  varies across param-tuples that differ from a neighbor by exactly one grid
  step (Hamming-1 adjacency). Low neighbor variance ⇒ the optimum is robust
  to small parameter perturbations.
* **optimal-param drift** — how much the per-window best parameter set moves
  across windows (numeric: stddev/range; categorical: modal mismatch fraction).

Deterministic, pure Python; consumes the output shape of
:mod:`app.platform.optimizer_service.OptimizerService.walk_forward`.

Reference: Optuna parameter importance / stability across trials; Lean
walk-forward optimizer stability; López de Prado parameter-stability matrix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "analyze_stability",
    "degradation_ratio",
    "neighborhood_stability",
    "optimal_param_drift",
]


@dataclass(frozen=True)
class WindowResult:
    params: dict[str, Any]
    in_sample: float | None
    out_of_sample: float | None
    window_id: int = 0

    @staticmethod
    def from_dict(
        d: dict[str, Any],
        *,
        metric: str,
        is_key: str = "in_sample_sharpe",
        oos_key: str = "out_of_sample_sharpe",
    ) -> WindowResult | None:
        if "params" not in d:
            return None
        is_v = d.get(is_key)
        oos_v = d.get(oos_key)
        return WindowResult(
            params=d["params"],
            in_sample=float(is_v) if is_v is not None else None,
            out_of_sample=float(oos_v) if oos_v is not None else None,
            window_id=int(d.get("window_id", 0)),
        )


def _std(values: list[float], *, eps: float = 1e-12) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1)) + eps


def degradation_ratio(
    in_sample: float,
    out_of_sample: float,
    *,
    higher_is_better: bool = True,
    cap: float = 4.0,
    floor: float = 1e-6,
) -> float:
    """OOS/IS ratio (higher_is_better) clipped to [0, cap]; floor guard on |IS|."""
    if abs(in_sample) < floor:
        return 1.0
    if higher_is_better:
        ratio = out_of_sample / in_sample if in_sample > 0 else 1.0
    else:
        # lower is better (e.g. drawdown): ratio = IS/OOS
        ratio = in_sample / out_of_sample if out_of_sample != 0 else 1.0
    # clip sign-flips / blowups
    if not math.isfinite(ratio):
        return cap
    return max(0.0, min(ratio, cap))


def _spearman(a: list[float], b: list[float]) -> float:
    """Spearman rank correlation between two equal-length lists (1.0 if degenerate)."""
    n = len(a)
    if n < 2:
        return 1.0
    ra = _ranks(a)
    rb = _ranks(b)
    ma = sum(ra) / n
    mb = sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = math.sqrt(sum((r - ma) ** 2 for r in ra))
    db = math.sqrt(sum((r - mb) ** 2 for r in rb))
    if da < 1e-12 or db < 1e-12:
        return 1.0
    return num / (da * db)


def _ranks(values: list[float]) -> list[float]:
    """Average-rank of each value (1-indexed)."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _param_tuple_key(params: dict[str, Any], axes: list[str]) -> tuple:
    return tuple(params.get(a) for a in axes)


def _axis_neighbors(key: tuple, axis_idx: int, sorted_values: list[Any]) -> list[tuple]:
    """Param-tuples differing from ``key`` only on ``axis_idx`` by one grid step."""
    val = key[axis_idx]
    if val not in sorted_values:
        return []
    pos = sorted_values.index(val)
    neighbors: list[tuple] = []
    for delta in (-1, 1):
        np_ = pos + delta
        if 0 <= np_ < len(sorted_values):
            new_key = list(key)
            new_key[axis_idx] = sorted_values[np_]
            neighbors.append(tuple(new_key))
    return neighbors


def neighborhood_stability(
    matrix: dict[tuple, float],
    param_axes: list[str],
    param_order: list[list[Any]],
    *,
    eps: float = 1e-12,
) -> dict[str, float]:
    """Per-axis stability = 1 - mean(neighbor_metric_std) / global_std."""
    if not matrix or not param_axes:
        return {}
    all_metrics = list(matrix.values())
    global_std = _std(all_metrics, eps=eps)
    result: dict[str, float] = {}
    for axis_idx, axis in enumerate(param_axes):
        sorted_values = sorted(param_order[axis_idx])
        per_tuple_stds: list[float] = []
        for key, metric in matrix.items():
            neighbors = _axis_neighbors(key, axis_idx, sorted_values)
            neighbor_metrics = [matrix[n] for n in neighbors if n in matrix]
            if neighbor_metrics:
                per_tuple_stds.append(_std(neighbor_metrics + [metric], eps=eps))
        if not per_tuple_stds:
            result[axis] = 1.0
            continue
        mean_std = sum(per_tuple_stds) / len(per_tuple_stds)
        if global_std <= eps:
            result[axis] = 1.0  # no global variation → perfectly stable
        else:
            result[axis] = max(0.0, min(1.0, 1.0 - mean_std / global_std))
    return result


def optimal_param_drift(
    per_window_best: list[dict[str, Any]],
    param_axes: list[str],
) -> dict[str, Any]:
    """How much the per-window best param-set moves across windows."""
    if not per_window_best or not param_axes:
        return {"per_axis": {}, "global_drift": 0.0, "unique_optima_count": 0}
    per_axis: dict[str, float] = {}
    keys: list[tuple] = []
    for entry in per_window_best:
        keys.append(_param_tuple_key(entry, param_axes))
        for axis in param_axes:
            v = entry.get(axis)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals = [e.get(axis) for e in per_window_best
                        if isinstance(e.get(axis), (int, float)) and not isinstance(e.get(axis), bool)]
                if len(vals) >= 2:
                    rng = max(vals) - min(vals)
                    std = _std([float(x) for x in vals])
                    per_axis[axis] = std / rng if rng > 0 else 0.0
                else:
                    per_axis[axis] = 0.0
            else:
                # categorical: fraction of windows differing from the modal value
                from collections import Counter
                counts = Counter(e.get(axis) for e in per_window_best)
                modal = counts.most_common(1)[0][0]
                mismatch = sum(1 for e in per_window_best if e.get(axis) != modal)
                per_axis[axis] = mismatch / len(per_window_best)
    # global drift: mean of per-axis drifts
    global_drift = sum(per_axis.values()) / len(per_axis) if per_axis else 0.0
    unique_optima = len(set(keys))
    return {"per_axis": per_axis, "global_drift": global_drift,
            "unique_optima_count": unique_optima}


def analyze_stability(
    wf_results: list[dict[str, Any]],
    *,
    metric: str = "sharpe",
    higher_is_better: bool = True,
    ratio_cap: float = 4.0,
    ratio_floor: float = 1e-6,
) -> dict[str, Any]:
    """Aggregate walk-forward stability diagnostics.

    ``wf_results`` is a flat list of per-window param-set rows (each with
    ``params``, ``in_sample_sharpe``, ``out_of_sample_sharpe``). Returns
    ``{windows, degradation, neighborhood_stability, drift, metric,
    higher_is_better}``.
    """
    if not wf_results:
        return {"windows": 0, "degradation": {}, "neighborhood_stability": {},
                "drift": {}, "metric": metric, "higher_is_better": higher_is_better}

    # group by window_id (default 0 if absent → treat as one window)
    windows: dict[int, list[WindowResult]] = {}
    for i, d in enumerate(wf_results):
        wr = WindowResult.from_dict(d, metric=metric)
        if wr is None:
            continue
        windows.setdefault(wr.window_id, []).append(wr)

    # degradation: per-window IS/OOS ratio + IS-vs-OOS rank correlation
    ratios: list[float] = []
    rank_corrs: list[float] = []
    for wid, rows in windows.items():
        is_vals = [r.in_sample for r in rows if r.in_sample is not None]
        oos_vals = [r.out_of_sample for r in rows if r.out_of_sample is not None]
        for r in rows:
            if r.in_sample is not None and r.out_of_sample is not None:
                ratios.append(degradation_ratio(
                    r.in_sample, r.out_of_sample, higher_is_better=higher_is_better,
                    cap=ratio_cap, floor=ratio_floor,
                ))
        # rank correlation between IS and OOS across param-sets in this window
        paired = [(r.in_sample, r.out_of_sample) for r in rows
                  if r.in_sample is not None and r.out_of_sample is not None]
        if len(paired) >= 2:
            is_list = [p[0] for p in paired]
            oos_list = [p[1] for p in paired]
            rank_corrs.append(_spearman(is_list, oos_list))

    mean_ratio = sum(ratios) / len(ratios) if ratios else 0.0
    min_ratio = min(ratios) if ratios else 0.0
    overfit_flag = bool(ratios and mean_ratio < 0.5)
    mean_rank_corr = sum(rank_corrs) / len(rank_corrs) if rank_corrs else 1.0

    # param axes from the first window's first row
    first = wf_results[0].get("params", {}) if wf_results else {}
    param_axes = list(first.keys())
    # for neighborhood stability we need a per-window metric matrix keyed by
    # param-tuple; build it from the first window (most populated).
    param_order: list[list[Any]] = []
    first_window_rows = next(iter(windows.values()), [])
    if first_window_rows:
        for axis in param_axes:
            vals = sorted({r.params.get(axis) for r in first_window_rows})
            param_order.append(vals)
    matrix = {_param_tuple_key(r.params, param_axes): (r.out_of_sample if r.out_of_sample is not None else r.in_sample or 0.0)
              for r in first_window_rows}
    nbhd = neighborhood_stability(matrix, param_axes, param_order)

    # drift: per-window best param-set (by OOS, or IS if OOS missing)
    per_window_best: list[dict[str, Any]] = []
    for wid, rows in windows.items():
        scored = [(r, (r.out_of_sample if r.out_of_sample is not None else r.in_sample or 0.0)) for r in rows]
        if not scored:
            continue
        # tie-break: lexicographically smallest sorted(params.items())
        def sort_key(item):
            r, score = item
            return (score if higher_is_better else -score, tuple(sorted(str(x) for x in r.params.items())))
        best = max(scored, key=sort_key)[0]
        per_window_best.append(best.params)

    drift = optimal_param_drift(per_window_best, param_axes)

    return {
        "windows": len(windows),
        "degradation": {
            "mean_ratio": mean_ratio,
            "min_ratio": min_ratio,
            "ratios": ratios,
            "mean_rank_correlation": mean_rank_corr,
            "overfit_flag": overfit_flag,
        },
        "neighborhood_stability": nbhd,
        "drift": drift,
        "metric": metric,
        "higher_is_better": higher_is_better,
    }