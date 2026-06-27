"""P274: signal persistence and decay diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, pearson, validate_series


@dataclass(frozen=True)
class SignalPersistenceResult:
    autocorrelation: dict[str, float]
    half_life_lag: int | None
    turnover_proxy: float
    decay_score: float

    def to_dict(self) -> dict[str, Any]:
        return {"autocorrelation": self.autocorrelation, "half_life_lag": self.half_life_lag, "turnover_proxy": self.turnover_proxy, "decay_score": self.decay_score}


def signal_persistence_report(signal: list[float], *, max_lag: int = 5) -> SignalPersistenceResult:
    values = validate_series(signal, name="signal", min_len=3)
    if len(set(values)) < 2:
        raise ValueError("signal must not be constant")
    if isinstance(max_lag, bool) or not isinstance(max_lag, int) or max_lag < 1 or max_lag >= len(values):
        raise ValueError("max_lag must be in [1, len(signal)-1]")
    autocorr: dict[str, float] = {}
    half_life = None
    for lag in range(1, max_lag + 1):
        corr = pearson(values[:-lag], values[lag:])
        autocorr[str(lag)] = corr
        if half_life is None and corr <= 0.5:
            half_life = lag
    deltas = [abs(b - a) for a, b in zip(values, values[1:])]
    return SignalPersistenceResult(autocorr, half_life, mean(deltas), max(0.0, autocorr.get("1", 0.0)))


__all__ = ["SignalPersistenceResult", "signal_persistence_report"]
