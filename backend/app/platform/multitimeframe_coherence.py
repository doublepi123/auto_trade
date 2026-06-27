"""P318: Multi-timeframe signal coherence.

Pure-Python coherence estimator: given equal-length signal vectors across
multiple timeframes (e.g. "1d", "1w", "1m"), computes a per-bar coherence
score as the weighted sum of signs and an agreement_ratio as the mean
absolute coherence normalised by total weight.

Public surface
--------------

* **multitimeframe_coherence_report(signals, weights)** → frozen
  :class:`MultitimeframeCoherenceResult` with ``coherence_scores`` (per-bar)
  and ``agreement_ratio`` (scalar in ``[0, 1]``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["MultitimeframeCoherenceResult", "multitimeframe_coherence_report"]


@dataclass(frozen=True)
class MultitimeframeCoherenceResult:
    coherence_scores: list[float]
    agreement_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {"coherence_scores": list(self.coherence_scores), "agreement_ratio": self.agreement_ratio}


def multitimeframe_coherence_report(
    signals: dict[str, list[float]],
    *,
    weights: dict[str, float] | None = None,
) -> MultitimeframeCoherenceResult:
    if not isinstance(signals, dict) or not signals:
        raise ValueError("signals must be a non-empty dict of signal series")

    # Validate each signal series and enforce equal length.
    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in signals.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"signals['{name}'] must be a list of finite numbers")
        vec = validate_series(series, name=f"signals['{name}']", min_len=1)
        if length is None:
            length = len(vec)
        elif len(vec) != length:
            raise ValueError("all signal series must have equal length")
        validated[str(name)] = vec

    # Resolve weights: uniform if not provided.
    if weights is None:
        w = {name: 1.0 for name in validated}
    else:
        if not isinstance(weights, dict):
            raise ValueError("weights must be a dict")
        w: dict[str, float] = {}
        for name in validated:
            if name not in weights:
                raise ValueError(f"weight missing for timeframe '{name}'")
            raw = weights[name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"weights['{name}'] must be a finite number")
            val = float(raw)
            if not math.isfinite(val):
                raise ValueError(f"weights['{name}'] must be a finite number")
            w[name] = val

    total_weight = sum(w.values())
    if total_weight == 0.0:
        raise ValueError("sum of weights must be positive")

    assert length is not None
    n = length
    coherence_scores: list[float] = []
    for i in range(n):
        score = sum(w[name] * (1.0 if validated[name][i] > 0 else (-1.0 if validated[name][i] < 0 else 0.0)) for name in validated) / total_weight
        coherence_scores.append(score)

    agreement = sum(abs(s) for s in coherence_scores) / n if n > 0 else 0.0
    return MultitimeframeCoherenceResult(coherence_scores=coherence_scores, agreement_ratio=agreement)
