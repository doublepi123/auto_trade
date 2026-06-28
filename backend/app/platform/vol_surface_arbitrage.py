"""P348: Volatility surface arbitrage detection.

Detect calendar spread, butterfly, and put-call parity violations on an
option IV surface. Pure Python, no numpy/scipy.

Reference: Gatheral (2006) "The Volatility Surface", Chapters 2 & 4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["VolSurfaceArbitrageResult", "vol_surface_arbitrage_report"]


_VALID_TYPES = {"call", "put"}
_PCP_TOLERANCE = 0.01  # 1% relative difference tolerance for PCP


@dataclass(frozen=True)
class VolSurfaceArbitrageResult:
    calendar_violations: list[dict[str, Any]] = field(default_factory=list)
    butterfly_violations: list[dict[str, Any]] = field(default_factory=list)
    pcp_violations: list[dict[str, Any]] = field(default_factory=list)
    has_arbitrage: bool = False
    violation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calendar_violations": self.calendar_violations,
            "butterfly_violations": self.butterfly_violations,
            "pcp_violations": self.pcp_violations,
            "has_arbitrage": self.has_arbitrage,
            "violation_count": self.violation_count,
        }


def _validate_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate options list and return validated copies."""
    if not options:
        raise ValueError("options must be a non-empty list")
    validated: list[dict[str, Any]] = []
    for i, opt in enumerate(options):
        if not isinstance(opt, dict):
            raise ValueError(f"options[{i}] must be a dict")
        for key in ("strike", "expiry_days", "iv", "type"):
            if key not in opt:
                raise ValueError(f"options[{i}] missing '{key}'")
        opt_type = str(opt["type"])
        if opt_type not in _VALID_TYPES:
            raise ValueError(f"options[{i}] type must be 'call' or 'put', got '{opt_type}'")
        for field in ("strike", "expiry_days", "iv"):
            val = opt[field]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"options[{i}] {field} must be a finite number")
            fv = float(val)
            if not math.isfinite(fv):
                raise ValueError(f"options[{i}] {field} must be finite")
        strike = float(opt["strike"])
        if strike <= 0:
            raise ValueError(f"options[{i}] strike must be positive")
        expiry_days = int(opt["expiry_days"])
        if expiry_days <= 0:
            raise ValueError(f"options[{i}] expiry_days must be positive")
        iv = float(opt["iv"])
        if iv <= 0:
            raise ValueError(f"options[{i}] iv must be positive")
        validated.append({
            "strike": strike,
            "expiry_days": expiry_days,
            "iv": iv,
            "type": opt_type,
        })
    return validated


def _detect_calendar_arbitrage(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect calendar spread arbitrage: same strike, longer expiry has lower IV."""
    violations: list[dict[str, Any]] = []
    # Group by strike
    by_strike: dict[float, list[dict[str, Any]]] = {}
    for opt in options:
        strike = opt["strike"]
        if strike not in by_strike:
            by_strike[strike] = []
        by_strike[strike].append(opt)

    for strike, opts in by_strike.items():
        # For each option type (call/put), check within same strike
        for opt_type in ("call", "put"):
            same_type = [o for o in opts if o["type"] == opt_type]
            if len(same_type) < 2:
                continue
            # Sort by expiry ascending
            same_type.sort(key=lambda o: o["expiry_days"])
            for i in range(len(same_type) - 1):
                short_exp = same_type[i]
                long_exp = same_type[i + 1]
                # Calendar arbitrage: longer expiry IV < shorter expiry IV
                if long_exp["iv"] < short_exp["iv"]:
                    violations.append({
                        "type": "calendar",
                        "strike": strike,
                        "option_type": opt_type,
                        "short_expiry_days": short_exp["expiry_days"],
                        "short_iv": short_exp["iv"],
                        "long_expiry_days": long_exp["expiry_days"],
                        "long_iv": long_exp["iv"],
                        "iv_drop": short_exp["iv"] - long_exp["iv"],
                    })
    return violations


def _detect_butterfly_arbitrage(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect butterfly arbitrage: same expiry, convexity violation.

    For three strikes K1 < K2 < K3 at same expiry, the IV at K2 should not
    exceed the linear interpolation of IV at K1 and K3. Butterfly arbitrage
    exists when the middle IV is too high (convexity violation).
    """
    violations: list[dict[str, Any]] = []
    # Group by (expiry_days, option_type)
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for opt in options:
        key = (opt["expiry_days"], opt["type"])
        if key not in groups:
            groups[key] = []
        groups[key].append(opt)

    for (expiry, opt_type), opts in groups.items():
        if len(opts) < 3:
            continue
        # Sort by strike
        opts.sort(key=lambda o: o["strike"])
        for i in range(len(opts) - 2):
            k1 = opts[i]
            k2 = opts[i + 1]
            k3 = opts[i + 2]
            # Linear interpolation weight: w = (K3 - K2) / (K3 - K1)
            weight = (k3["strike"] - k2["strike"]) / (k3["strike"] - k1["strike"])
            interpolated_iv = weight * k1["iv"] + (1.0 - weight) * k3["iv"]
            # Butterfly arbitrage: middle IV > interpolated IV
            if k2["iv"] > interpolated_iv:
                violations.append({
                    "type": "butterfly",
                    "expiry_days": expiry,
                    "option_type": opt_type,
                    "strike_low": k1["strike"],
                    "iv_low": k1["iv"],
                    "strike_mid": k2["strike"],
                    "iv_mid": k2["iv"],
                    "strike_high": k3["strike"],
                    "iv_high": k3["iv"],
                    "interpolated_iv": interpolated_iv,
                    "iv_excess": k2["iv"] - interpolated_iv,
                })
    return violations


def _detect_pcp_violations(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect put-call parity violations: same strike+expiry, call IV != put IV."""
    violations: list[dict[str, Any]] = []
    # Group by (strike, expiry_days)
    groups: dict[tuple[float, int], dict[str, dict[str, Any]]] = {}
    for opt in options:
        key = (opt["strike"], opt["expiry_days"])
        if key not in groups:
            groups[key] = {}
        groups[key][opt["type"]] = opt

    for (strike, expiry), pair in groups.items():
        if "call" not in pair or "put" not in pair:
            continue
        call_opt = pair["call"]
        put_opt = pair["put"]
        # Relative difference
        avg_iv = (call_opt["iv"] + put_opt["iv"]) / 2.0
        if avg_iv <= 0:
            continue
        rel_diff = abs(call_opt["iv"] - put_opt["iv"]) / avg_iv
        if rel_diff > _PCP_TOLERANCE:
            violations.append({
                "type": "pcp",
                "strike": strike,
                "expiry_days": expiry,
                "call_iv": call_opt["iv"],
                "put_iv": put_opt["iv"],
                "iv_difference": abs(call_opt["iv"] - put_opt["iv"]),
                "relative_difference": rel_diff,
            })
    return violations


def vol_surface_arbitrage_report(
    options: list[dict[str, Any]],
    *,
    spot: float,
) -> VolSurfaceArbitrageResult:
    """Detect arbitrage violations on an option IV surface.

    Args:
        options: List of option dicts with keys ``strike`` (float, positive),
            ``expiry_days`` (int, positive), ``iv`` (float, positive implied
            volatility), and ``type`` (``"call"`` or ``"put"``).
        spot: Current spot price of the underlying. Must be positive.

    Returns:
        VolSurfaceArbitrageResult with calendar_violations, butterfly_violations,
        pcp_violations, has_arbitrage, and violation_count.

    Raises:
        ValueError: On invalid options, non-finite values, negative spot,
            or empty options list.
    """
    if not isinstance(spot, (int, float)) or isinstance(spot, bool):
        raise ValueError("spot must be a finite positive number")
    spot_f = float(spot)
    if not math.isfinite(spot_f) or spot_f <= 0:
        raise ValueError("spot must be a finite positive number")

    validated = _validate_options(options)

    calendar = _detect_calendar_arbitrage(validated)
    butterfly = _detect_butterfly_arbitrage(validated)
    pcp = _detect_pcp_violations(validated)

    total_violations = len(calendar) + len(butterfly) + len(pcp)

    return VolSurfaceArbitrageResult(
        calendar_violations=calendar,
        butterfly_violations=butterfly,
        pcp_violations=pcp,
        has_arbitrage=total_violations > 0,
        violation_count=total_violations,
    )
