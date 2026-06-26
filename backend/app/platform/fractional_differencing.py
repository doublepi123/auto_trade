"""P258: Fractional differencing — stationarity-preserving differencing.

López de Prado's (2018) fractional-differencing technique widens the standard
integer-difference operator (1−B)^d to non-integer ``d ∈ (0, 1)``. With
``d < 1`` the differenced series retains a controlled amount of long memory
while still achieving (near-)stationarity — useful for features that lose too
much signal under a full first-difference.

The binomial expansion of ``(1 − B)^d`` gives weights

    w₀ = 1,   wₖ = w_{k−1} · (k − 1 − d) / k   for k ≥ 1.

* **fractional_weights(d, threshold)** — the weight vector truncated once
  ``|wₖ| < threshold`` (default 1e-2, a practical short-series window).
* **fractional_difference(series, d, threshold)** — the standard (expanding-
  window) fractional difference; each point uses the available history so there
  is no warm-up gap.
* **fractional_difference_ffd(series, d, threshold)** — the **fixed-window**
  (FFD) variant that applies a constant-length weight window to every point,
  giving a single stationary series with no warm-up loss after the window.

Pure Python, no scipy/numpy. Reference: López de Prado (2018) "Advances in
Financial Machine Learning" §5.4. Deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "FractionalDiffResult",
    "fractional_weights",
    "fractional_difference",
    "fractional_difference_ffd",
    "fractional_adf_stat",
    "fractional_diff_report",
]


_MAX_WEIGHTS = 10000


def fractional_weights(d: float, threshold: float = 1e-2) -> list[float]:
    """Binomial weights of (1−B)^d truncated when |wₖ| < threshold.

    For ``d ∈ (0,1)`` the weights decay in magnitude past ``k > d``.
    Raises ``ValueError`` if ``d`` is not in (0, 1) or threshold invalid.
    """
    if not math.isfinite(d) or not 0.0 < d < 1.0:
        raise ValueError("d must be in (0, 1) for fractional differencing")
    if not math.isfinite(threshold) or not 0.0 < threshold < 1.0:
        raise ValueError("threshold must be in (0, 1)")
    weights = [1.0]
    k = 1
    while True:
        w = weights[-1] * (k - 1 - d) / k
        weights.append(w)
        if abs(w) < threshold:
            break
        k += 1
        if k > _MAX_WEIGHTS:
            raise ValueError("threshold produces too many fractional weights")
    return weights


def fractional_difference(series: Sequence[float], d: float, threshold: float = 1e-2) -> list[float | None]:
    """Expanding-window fractional difference ``(1−B)^d series``.

    Returns a list the same length as ``series``. At index ``t`` it uses the
    available history ``x_t, x_{t-1}, …, x_0`` and the first ``t+1`` weights,
    so there is no warm-up gap. Raises ``ValueError`` on empty series / invalid d.
    """
    if not series:
        raise ValueError("series must be non-empty")
    weights = fractional_weights(d, threshold)
    n = len(series)
    out: list[float | None] = []
    for t in range(n):
        val = 0.0
        width = min(t + 1, len(weights))
        for k in range(width):
            val += weights[k] * series[t - k]
        out.append(val)
    return out


def fractional_difference_ffd(series: Sequence[float], d: float, threshold: float = 1e-2) -> list[float | None]:
    """Fixed-window (FFD) fractional difference.

    Applies the same constant-length weight window at every point (no
    expanding history), producing a single stationary output after the window.
    The first ``width−1`` entries are ``None``.
    """
    if not series:
        raise ValueError("series must be non-empty")
    weights = fractional_weights(d, threshold)
    width = len(weights)
    n = len(series)
    out: list[float | None] = []
    for t in range(n):
        if t < width - 1:
            out.append(None)
            continue
        val = 0.0
        for k in range(width):
            val += weights[k] * series[t - k]
        out.append(val)
    return out


def fractional_adf_stat(series: Sequence[float]) -> float:
    """A crude augmented-Dickey-Fuller-style t-statistic proxy.

    Regresses Δx on x_{t-1}: t = slope / se(slope). Used only to *rank*
    candidate ``d`` values for stationarity (higher magnitude ⇒ more
    stationary). Not a calibrated ADF — documented as a heuristic.
    """
    xs = [float(v) for v in series if v is not None]
    n = len(xs)
    if n < 3:
        return 0.0
    lhs = [xs[i] - xs[i - 1] for i in range(1, n)]
    rhs = [xs[i - 1] for i in range(1, n)]
    m = sum(rhs) / len(rhs)
    sxx = sum((r - m) ** 2 for r in rhs)
    if sxx == 0.0:
        return 0.0
    sxy = sum(r * l for r, l in zip(rhs, lhs))
    slope = sxy / sxx
    resid = [l - slope * r for l, r in zip(lhs, rhs)]
    ssr = sum(e * e for e in resid)
    se = math.sqrt(ssr / (len(lhs) - 1) / sxx) if sxx > 0 else float("inf")
    return slope / se if se > 0 else 0.0


@dataclass(frozen=True)
class FractionalDiffResult:
    d: float
    n_weights: int
    n_output: int
    adf_stat: float
    output: list[float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "d": self.d,
            "n_weights": self.n_weights,
            "n_output": self.n_output,
            "adf_stat": self.adf_stat,
            "output": self.output,
        }


def fractional_diff_report(series: Sequence[float], d: float = 0.4, threshold: float = 1e-2) -> FractionalDiffResult:
    """Fractional-difference a series (FFD) and report an ADF-style statistic."""
    out = fractional_difference_ffd(series, d, threshold)
    present = [v for v in out if v is not None]
    return FractionalDiffResult(
        d=d,
        n_weights=len(fractional_weights(d, threshold)),
        n_output=len(present),
        adf_stat=fractional_adf_stat(present),
        output=out,
    )
