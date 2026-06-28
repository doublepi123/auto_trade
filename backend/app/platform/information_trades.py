"""P360: information-based trade classification.

Identify informed trading activity using volume self-information (surprise
relative to rolling empirical distribution), directional conditional entropy,
and a normalized informed-trade probability proxy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["InformationTradesResult", "information_trades_report"]


@dataclass(frozen=True)
class InformationTradesResult:
    self_information: list[float]
    information_asymmetry: float
    informed_trade_prob: list[float]
    entropy_decomposition: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "self_information": self.self_information,
            "information_asymmetry": self.information_asymmetry,
            "informed_trade_prob": self.informed_trade_prob,
            "entropy_decomposition": dict(self.entropy_decomposition),
        }


def information_trades_report(
    volumes: list[float],
    *,
    direction: list[float] | None = None,
    window: int = 20,
) -> InformationTradesResult:
    """Compute information-based trade classification.

    Args:
        volumes: Non-empty list of finite trading volumes.
        direction: Optional list of direction signs (positive = buy, negative = sell),
            same length as volumes.
        window: Rolling window size for empirical distribution estimation.

    Returns:
        InformationTradesResult with self_information (per-bar surprise),
        information_asymmetry (directional entropy difference), informed_trade_prob
        (normalized proxy in [0,1]), and entropy_decomposition dictionary.
    """
    validated_volumes = validate_series(volumes, name="volumes", min_len=2)
    n = len(validated_volumes)

    if direction is not None:
        validated_dir = validate_series(direction, name="direction", min_len=2)
        if len(validated_dir) != n:
            raise ValueError("direction must have the same length as volumes")
    else:
        validated_dir = None

    # Volume self-information: for each bar, estimate empirical probability
    # from the rolling window and compute -log(p+1e-12).
    self_information: list[float] = []
    for i in range(n):
        start = max(0, i - window + 1)
        segment = validated_volumes[start : i + 1]
        # Count how many values in segment are >= current value (upper tail prob).
        current = validated_volumes[i]
        count_ge = sum(1 for v in segment if v >= current)
        p = count_ge / len(segment)
        surprise = -math.log(max(p, 1e-12))
        self_information.append(surprise)

    # Entropy decomposition (only if direction is available).
    if validated_dir is not None:
        info_asym, ent_decomp = _entropy_decomposition(
            validated_volumes, validated_dir, window
        )
    else:
        info_asym = 0.0
        ent_decomp = {
            "h_direction_given_high_vol": 0.0,
            "h_direction_given_low_vol": 0.0,
            "unconditional_h_direction": 0.0,
        }

    # Informed trade probability proxy.
    # directional_surprise: how surprising is the volume *given* the direction sign.
    if validated_dir is not None:
        directional_surprise: list[float] = []
        for i in range(n):
            start = max(0, i - window + 1)
            seg_dir = validated_dir[start : i + 1]
            current_dir = validated_dir[i]
            # Probability of observing this direction sign in the rolling window.
            same_sign = sum(1 for d in seg_dir if math.copysign(1.0, d) == math.copysign(1.0, current_dir))
            p_dir = same_sign / len(seg_dir)
            dir_surprise = -math.log(max(p_dir, 1e-12))
            directional_surprise.append(dir_surprise)

        # Raw proxy: directional_surprise * volume_surprise.
        raw_proxy = [
            directional_surprise[i] * self_information[i] for i in range(n)
        ]
        # Normalize to [0, 1].
        max_proxy = max(raw_proxy) if raw_proxy else 1.0
        if max_proxy <= 0.0:
            informed_trade_prob = [0.0] * n
        else:
            informed_trade_prob = [min(v / max_proxy, 1.0) for v in raw_proxy]
    else:
        # Without direction, use only volume surprise normalized.
        max_si = max(self_information) if self_information else 1.0
        if max_si <= 0.0:
            informed_trade_prob = [0.0] * n
        else:
            informed_trade_prob = [min(v / max_si, 1.0) for v in self_information]

    return InformationTradesResult(
        self_information=self_information,
        information_asymmetry=info_asym,
        informed_trade_prob=informed_trade_prob,
        entropy_decomposition=ent_decomp,
    )


def _entropy_decomposition(
    volumes: Sequence[float],
    direction: Sequence[float],
    window: int,
) -> tuple[float, dict[str, float]]:
    """Compute directional conditional entropy for high-vol vs low-vol regimes."""
    n = len(volumes)

    # Split into high-vol and low-vol based on median volume.
    vol_median = _median(volumes)
    high_idx = [i for i in range(n) if volumes[i] >= vol_median]
    low_idx = [i for i in range(n) if volumes[i] < vol_median]

    # Helper: entropy of a discrete binary sequence.
    def _binary_entropy(indices: list[int]) -> float:
        if not indices:
            return 0.0
        pos = sum(1 for i in indices if direction[i] > 0)
        neg = len(indices) - pos
        p_pos = pos / len(indices)
        p_neg = neg / len(indices)
        ent = 0.0
        if p_pos > 0:
            ent -= p_pos * math.log2(p_pos)
        if p_neg > 0:
            ent -= p_neg * math.log2(p_neg)
        return ent

    h_high = _binary_entropy(high_idx)
    h_low = _binary_entropy(low_idx)
    h_uncond = _binary_entropy(list(range(n)))

    # Information asymmetry = |H(direction|low_vol) - H(direction|high_vol)|
    info_asym = abs(h_low - h_high)

    return info_asym, {
        "h_direction_given_high_vol": h_high,
        "h_direction_given_low_vol": h_low,
        "unconditional_h_direction": h_uncond,
    }


def _median(values: Sequence[float]) -> float:
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return sorted_vals[n // 2]
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0
