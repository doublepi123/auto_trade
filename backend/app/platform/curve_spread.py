"""P296: curve spread, carry, and roll-down diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class CurveSpreadResult:
    spread: float
    carry: float
    roll_down: float
    z_score: float | None
    signal: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def curve_spread_report(curve: dict[float, float], *, short_tenor: float, long_tenor: float, history: list[float] | None = None) -> CurveSpreadResult:
    parsed = {_positive(k, "tenor"): _finite(v, "yield") for k, v in curve.items()} if isinstance(curve, dict) else {}
    if len(parsed) < 2:
        raise ValueError("curve must contain at least two tenors")
    short = _positive(short_tenor, "short_tenor")
    long = _positive(long_tenor, "long_tenor")
    if short not in parsed or long not in parsed or short >= long:
        raise ValueError("short_tenor and long_tenor must exist with short < long")
    spread = parsed[long] - parsed[short]
    lower_tenors = [tenor for tenor in parsed if short <= tenor < long]
    roll_down = parsed[max(lower_tenors)] - parsed[long] if lower_tenors else 0.0
    z_score = None
    if history is not None:
        hist = validate_series(history, name="history", min_len=2)
        sigma = std(hist)
        z_score = 0.0 if sigma == 0 else (spread - mean(hist)) / sigma
    signal = "steepener" if z_score is not None and z_score < -1 else "flattener" if z_score is not None and z_score > 1 else "neutral"
    return CurveSpreadResult(spread, spread, roll_down, z_score, signal)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


__all__ = ["CurveSpreadResult", "curve_spread_report"]
