"""P310: Volume Profile — price-volume distribution analysis.

Compute a volume-at-price histogram with equal-width price bins, identify the
Point of Control (POC — bin with maximum volume) and the Value Area (70% of
total volume centered around the POC).

Deterministic, pure Python. Reference: Steidlmayer (1984) Market Profile.
"""

from __future__ import annotations

from collections.abc import Sequence
import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series, validate_pair

__all__ = [
    "VolumeProfileResult",
    "volume_profile_report",
]


@dataclass(frozen=True)
class VolumeProfileBin:
    low: float
    high: float
    midpoint: float
    volume: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "midpoint": self.midpoint,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class VolumeProfileResult:
    poc_price: float
    value_area_low: float
    value_area_high: float
    bins: list[VolumeProfileBin]

    def to_dict(self) -> dict[str, Any]:
        return {
            "poc_price": self.poc_price,
            "value_area_low": self.value_area_low,
            "value_area_high": self.value_area_high,
            "bins": [b.to_dict() for b in self.bins],
        }


def volume_profile_report(
    prices: Sequence[float],
    volumes: Sequence[float],
    *,
    bins: int = 10,
) -> VolumeProfileResult:
    """Compute volume profile from equal-length *prices* and *volumes*.

    *bins* must be a positive integer. Prices and volumes are validated via
    ``validate_series`` / ``validate_pair``.
    """
    if isinstance(bins, bool) or not isinstance(bins, int):
        raise ValueError("bins must be a positive integer")
    if bins <= 0:
        raise ValueError("bins must be a positive integer")

    ps, vs = validate_pair(prices, volumes, x_name="prices", y_name="volumes")

    p_min = min(ps)
    p_max = max(ps)
    if p_min == p_max:
        # Degenerate case: all prices equal → single bin
        return VolumeProfileResult(
            poc_price=p_min,
            value_area_low=p_min,
            value_area_high=p_min,
            bins=[VolumeProfileBin(low=p_min, high=p_min, midpoint=p_min, volume=sum(vs))],
        )

    bin_width = (p_max - p_min) / bins
    bin_volumes = [0.0] * bins
    bin_lows = [p_min + i * bin_width for i in range(bins)]
    bin_highs = [p_min + (i + 1) * bin_width for i in range(bins)]

    # Adjust last bin to include p_max exactly
    bin_highs[-1] = p_max

    for price, vol in zip(ps, vs):
        idx = int((price - p_min) / bin_width)
        if idx >= bins:
            idx = bins - 1
        bin_volumes[idx] += vol

    # Build bin objects
    bin_objects = [
        VolumeProfileBin(
            low=bin_lows[i],
            high=bin_highs[i],
            midpoint=(bin_lows[i] + bin_highs[i]) / 2.0,
            volume=bin_volumes[i],
        )
        for i in range(bins)
    ]

    # POC: bin with max volume
    poc_idx = max(range(bins), key=lambda i: bin_volumes[i])
    poc_price = (bin_lows[poc_idx] + bin_highs[poc_idx]) / 2.0

    # Value Area: expand outward from POC until 70% of total volume
    total_volume = sum(vs)
    target_volume = total_volume * 0.70

    accumulated = bin_volumes[poc_idx]
    low_idx = poc_idx
    high_idx = poc_idx

    while accumulated < target_volume:
        left_vol = bin_volumes[low_idx - 1] if low_idx > 0 else -1.0
        right_vol = bin_volumes[high_idx + 1] if high_idx < bins - 1 else -1.0

        if left_vol >= right_vol:
            low_idx -= 1
            accumulated += bin_volumes[low_idx]
        else:
            high_idx += 1
            accumulated += bin_volumes[high_idx]

        if low_idx == 0 and high_idx == bins - 1:
            break

    value_area_low = bin_lows[low_idx]
    value_area_high = bin_highs[high_idx]

    return VolumeProfileResult(
        poc_price=poc_price,
        value_area_low=value_area_low,
        value_area_high=value_area_high,
        bins=bin_objects,
    )
