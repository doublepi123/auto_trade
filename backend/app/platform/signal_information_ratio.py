"""P298: signal information-ratio diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, pearson, std, validate_pair


@dataclass(frozen=True)
class SignalInformationRatioResult:
    information_ratio: float
    signal_to_noise: float
    stability: float | None
    bucket_quality: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def signal_information_ratio_report(signals: list[float], forward_returns: list[float], *, periods_per_year: int = 252, n_buckets: int = 5) -> SignalInformationRatioResult:
    sig, rets = validate_pair(signals, forward_returns, x_name="signals", y_name="forward_returns")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if isinstance(n_buckets, bool) or not isinstance(n_buckets, int) or n_buckets < 2 or n_buckets > len(sig):
        raise ValueError("n_buckets must be in [2, len(signals)]")
    weighted = [s * r for s, r in zip(sig, rets)]
    sigma = std(weighted, sample=True)
    ir = 0.0 if sigma == 0 else mean(weighted) / sigma * math.sqrt(periods_per_year)
    sig_std = std(sig)
    snr = 0.0 if sig_std == 0 else abs(mean(sig)) / sig_std
    stability = None
    half = len(sig) // 2
    if half >= 2 and len(sig) - half >= 2:
        stability = pearson(sig[:half], sig[-half:])
    bucket_quality = _bucket_quality(sig, rets, n_buckets)
    return SignalInformationRatioResult(ir, snr, stability, bucket_quality)


def _bucket_quality(signals: list[float], returns: list[float], n_buckets: int) -> dict[str, float]:
    ordered = sorted(zip(signals, returns), key=lambda item: item[0])
    buckets: list[float] = []
    for bucket in range(n_buckets):
        start = bucket * len(ordered) // n_buckets
        end = (bucket + 1) * len(ordered) // n_buckets
        buckets.append(mean([ret for _, ret in ordered[start:end]]))
    return {"bottom_bucket": buckets[0], "top_bucket": buckets[-1], "top_bottom_spread": buckets[-1] - buckets[0]}


__all__ = ["SignalInformationRatioResult", "signal_information_ratio_report"]
