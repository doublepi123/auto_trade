"""P308: tail dependence diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import ranks, validate_pair


@dataclass(frozen=True)
class TailDependenceResult:
    empirical: dict[str, float]
    parametric: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"empirical": self.empirical, "parametric": self.parametric}


def tail_dependence_report(x: list[float], y: list[float], *, threshold: float = 0.1) -> TailDependenceResult:
    xs, ys = validate_pair(x, y, x_name="x", y_name="y")
    if threshold <= 0 or threshold > 0.5:
        raise ValueError("threshold must be in (0, 0.5]")
    n = len(xs)
    rx = ranks(xs)
    ry = ranks(ys)
    norm_rx = [(r - 1) / (n - 1) if n > 1 else 0.0 for r in rx]
    norm_ry = [(r - 1) / (n - 1) if n > 1 else 0.0 for r in ry]
    upper_cutoff = 1.0 - threshold
    upper_x = sum(1 for r in norm_rx if r >= upper_cutoff)
    upper_y = sum(1 for r in norm_ry if r >= upper_cutoff)
    upper_both = sum(1 for a, b in zip(norm_rx, norm_ry) if a >= upper_cutoff and b >= upper_cutoff)
    lower_cutoff = threshold
    lower_x = sum(1 for r in norm_rx if r <= lower_cutoff)
    lower_y = sum(1 for r in norm_ry if r <= lower_cutoff)
    lower_both = sum(1 for a, b in zip(norm_rx, norm_ry) if a <= lower_cutoff and b <= lower_cutoff)
    emp_upper = upper_both / min(upper_x, upper_y) if min(upper_x, upper_y) > 0 else 0.0
    emp_lower = lower_both / min(lower_x, lower_y) if min(lower_x, lower_y) > 0 else 0.0
    tau = _kendall_tau(norm_rx, norm_ry)
    param_upper = _gumbel_upper(tau)
    param_lower = _clayton_lower(tau)
    return TailDependenceResult({"upper": emp_upper, "lower": emp_lower}, {"upper": param_upper, "lower": param_lower})


def _kendall_tau(rx: list[float], ry: list[float]) -> float:
    n = len(rx)
    if n < 2:
        return 0.0
    concordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if (rx[i] - rx[j]) * (ry[i] - ry[j]) > 0:
                concordant += 1
    total = n * (n - 1) / 2
    return concordant / total if total > 0 else 0.0


def _gumbel_upper(tau: float) -> float:
    if tau <= 0:
        return 0.0
    theta = 1.0 / (1.0 - tau) if tau < 1 else 100.0
    return 2.0 - math.pow(2.0, 1.0 / theta)


def _clayton_lower(tau: float) -> float:
    if tau <= 0:
        return 0.0
    theta = 2.0 * tau / (1.0 - tau) if tau < 1 else 100.0
    return math.pow(2.0, -1.0 / theta)


__all__ = ["TailDependenceResult", "tail_dependence_report"]
