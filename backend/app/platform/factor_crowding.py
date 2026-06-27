"""P295: factor crowding diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class FactorCrowdingResult:
    components: dict[str, float]
    crowding_score: float
    label: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def factor_crowding_report(factor: dict[str, float], *, valuations: dict[str, float] | None = None, flows: dict[str, float] | None = None) -> FactorCrowdingResult:
    scores = _validate_map(factor, "factor")
    components = {"signal_concentration": _herfindahl([abs(v) for v in scores.values()])}
    if valuations is not None:
        vals = _validate_optional(valuations, scores, "valuations")
        ordered = sorted(scores, key=lambda name: scores[name])
        half = max(1, len(ordered) // 2)
        low = ordered[:half]
        high = ordered[-half:]
        high_avg = sum(vals[name] for name in high) / len(high)
        low_avg = sum(vals[name] for name in low) / len(low)
        denom = abs(high_avg) + abs(low_avg) + 1e-12
        components["valuation_spread"] = min(1.0, abs(high_avg - low_avg) / denom)
    if flows is not None:
        flow_vals = _validate_optional(flows, scores, "flows")
        components["flow_concentration"] = _herfindahl([abs(v) for v in flow_vals.values()])
    score = sum(components.values()) / len(components)
    label = "crowded" if score > 0.6 else "watch" if score > 0.3 else "normal"
    return FactorCrowdingResult(components, score, label)


def _validate_map(values: dict[str, float], name: str) -> dict[str, float]:
    if not isinstance(values, dict) or len(values) < 2:
        raise ValueError(f"{name} must contain at least two assets")
    return {str(k): _finite(v, name) for k, v in values.items()}


def _validate_optional(values: dict[str, float], base: dict[str, float], name: str) -> dict[str, float]:
    out = _validate_map(values, name)
    if set(out) != set(base):
        raise ValueError(f"{name} keys must match factor keys")
    return out


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} values must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} values must be finite")
    return number


def _herfindahl(values: list[float]) -> float:
    total = sum(values)
    if total == 0:
        return 0.0
    return sum((value / total) ** 2 for value in values)


__all__ = ["FactorCrowdingResult", "factor_crowding_report"]
