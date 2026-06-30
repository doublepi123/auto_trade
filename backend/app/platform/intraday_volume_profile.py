"""P381: Intraday Volume Profile — time-bucket volume distribution analysis.

Computes the average volume and percentage share for each intraday time bucket,
derives a U-shape score (open-30min + close-30min / total), and identifies the
top-3 peak times and bottom-3 lull times.

Pure Python, deterministic. Frozen dataclass result with to_dict().
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "IntradayVolumeProfileResult",
    "intraday_volume_profile_report",
]

_MAX_BUCKETS = 50
_MIN_VALUES = 1


@dataclass(frozen=True)
class IntradayVolumeProfileResult:
    per_bucket: dict[str, dict[str, float]]
    u_shape_score: float
    peak_times: list[str]
    lull_times: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_bucket": self.per_bucket,
            "u_shape_score": self.u_shape_score,
            "peak_times": self.peak_times,
            "lull_times": self.lull_times,
        }


def _validate_time_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("time bucket keys must be non-empty strings")
    return key


def _validate_volumes(values: Any, bucket: str) -> list[float]:
    if not isinstance(values, list):
        raise ValueError(f"volumes for '{bucket}' must be a non-empty list of finite numbers")
    if not values:
        raise ValueError(f"volumes for '{bucket}' must be a non-empty list of finite numbers")
    if len(values) > 5000:
        raise ValueError(f"volumes for '{bucket}' must contain at most 5000 values")
    out: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"volumes for '{bucket}' entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"volumes for '{bucket}' entries must be finite non-negative numbers")
        out.append(number)
    return out


def intraday_volume_profile_report(
    volumes_by_time: Mapping[str, Sequence[float]],
) -> IntradayVolumeProfileResult:
    """Analyze intraday volume distribution across time buckets.

    Args:
        volumes_by_time: Mapping from time string (e.g. "09:30") to list of
            volume observations (one per day).

    Returns:
        IntradayVolumeProfileResult with per-bucket stats, U-shape score,
        peak times (top 3 by avg volume), and lull times (bottom 3).

    Raises:
        ValueError: If input is invalid (non-dict, >50 buckets, invalid values).
    """
    if not isinstance(volumes_by_time, Mapping):
        raise ValueError("volumes_by_time must be a dict")
    if not volumes_by_time:
        raise ValueError("volumes_by_time must be non-empty")
    if len(volumes_by_time) > _MAX_BUCKETS:
        raise ValueError(f"volumes_by_time must contain at most {_MAX_BUCKETS} buckets")

    # Validate all buckets
    validated: dict[str, list[float]] = {}
    for raw_key, raw_volumes in volumes_by_time.items():
        key = _validate_time_key(raw_key)
        validated[key] = _validate_volumes(raw_volumes, key)

    # Compute per-bucket stats
    per_bucket: dict[str, dict[str, float]] = {}
    for bucket, vols in validated.items():
        avg = sum(vols) / len(vols)
        per_bucket[bucket] = {"avg_volume": avg}

    # Total average volume across all buckets
    total_avg = sum(v["avg_volume"] for v in per_bucket.values())
    if total_avg == 0:
        for v in per_bucket.values():
            v["pct"] = 0.0
        return IntradayVolumeProfileResult(
            per_bucket=per_bucket,
            u_shape_score=0.0,
            peak_times=[],
            lull_times=[],
        )

    # Compute percentages
    for bucket in per_bucket:
        per_bucket[bucket]["pct"] = per_bucket[bucket]["avg_volume"] / total_avg

    # Sorted by avg_volume
    sorted_buckets = sorted(validated.keys(), key=lambda b: per_bucket[b]["avg_volume"], reverse=True)

    # Top 3 and bottom 3
    peak_times = sorted_buckets[:3]
    lull_times = sorted(sorted_buckets[-3:])

    # U-shape score: (open_30min_pct + close_30min_pct) / total
    # Use first bucket as open_30min, last bucket as close_30min proxy
    time_order = sorted(validated.keys())
    open_pct = per_bucket[time_order[0]]["pct"]
    close_pct = per_bucket[time_order[-1]]["pct"]
    u_shape_score = (open_pct + close_pct) / 1.0  # pct is already a ratio

    return IntradayVolumeProfileResult(
        per_bucket=per_bucket,
        u_shape_score=u_shape_score,
        peak_times=peak_times,
        lull_times=lull_times,
    )
