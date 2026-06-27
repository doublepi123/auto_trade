"""P316: Volatility term structure – ATM IV slope and regime.

Computes the volatility term structure from option IV data across expiries.
Returns per-expiry ATM IV, term slope, and contango/backwardation label.

Reference: volatility term structure literature (Contango/Backwardation).
Pure-Python — no scipy, no NumPy, no RNG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, validate_series

__all__ = ["VolTermStructureResult", "vol_term_structure_report"]


@dataclass(frozen=True)
class VolTermStructureResult:
    per_expiry: list[dict[str, float]]
    slope: float
    label: str  # "contango", "backwardation", or "flat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_expiry": self.per_expiry,
            "slope": self.slope,
            "label": self.label,
        }


def _parse_option(row: dict[str, Any]) -> dict[str, float]:
    """Validate a single option dict with expiry and iv fields."""
    if not isinstance(row, dict):
        raise ValueError("options must contain dicts with expiry and iv")
    expiry = row.get("expiry")
    iv = row.get("iv")
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise ValueError("option expiry must be a positive number")
    if isinstance(iv, bool) or not isinstance(iv, (int, float)):
        raise ValueError("option iv must be a positive number")
    e = float(expiry)
    i = float(iv)
    if not math.isfinite(e) or e <= 0:
        raise ValueError("option expiry must be positive")
    if not math.isfinite(i) or i <= 0:
        raise ValueError("option iv must be positive")
    return {"expiry": e, "iv": i}


def vol_term_structure_report(
    options: list[dict[str, Any]],
    *,
    spot: float,
) -> VolTermStructureResult:
    """Compute volatility term structure from option IV data.

    Args:
        options: List of {"expiry": days, "iv": float} dicts.
        spot: Current spot price (must be positive).

    Returns:
        VolTermStructureResult with per_expiry entries, slope, and regime label.
    """
    if not isinstance(spot, (int, float)) or isinstance(spot, bool):
        raise ValueError("spot must be a positive number")
    if not math.isfinite(float(spot)) or float(spot) <= 0:
        raise ValueError("spot must be positive")

    if not isinstance(options, list):
        raise ValueError("options must be a list of dicts with expiry and iv")
    if not options:
        raise ValueError("options must be non-empty")

    parsed = [_parse_option(row) for row in options]

    # Sort by expiry
    parsed.sort(key=lambda r: r["expiry"])

    # Group by expiry (take mean IV per expiry bucket)
    expiry_map: dict[float, list[float]] = {}
    for row in parsed:
        e = row["expiry"]
        if e not in expiry_map:
            expiry_map[e] = []
        expiry_map[e].append(row["iv"])

    per_expiry: list[dict[str, float]] = []
    for expiry in sorted(expiry_map):
        ivs = expiry_map[expiry]
        per_expiry.append({
            "expiry": expiry,
            "iv": mean(ivs),
        })

    # Term slope: (far IV - near IV) / (far expiry - near expiry) * sqrt(30)
    # Normalized to roughly 30-day equivalent slope
    if len(per_expiry) < 2:
        slope = 0.0
    else:
        near = per_expiry[0]
        far = per_expiry[-1]
        dt = far["expiry"] - near["expiry"]
        if dt > 0:
            raw_slope = (far["iv"] - near["iv"]) / dt
            slope = raw_slope * math.sqrt(30.0)  # normalize to 30-day scale
        else:
            slope = 0.0

    # Label
    if slope > 1e-6:
        label = "contango"
    elif slope < -1e-6:
        label = "backwardation"
    else:
        label = "flat"

    return VolTermStructureResult(
        per_expiry=per_expiry,
        slope=slope,
        label=label,
    )
