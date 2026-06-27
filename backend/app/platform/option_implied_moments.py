"""P293: option-implied smile and moment diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std


@dataclass(frozen=True)
class OptionImpliedMomentsResult:
    smile: dict[str, float]
    term_structure: dict[str, float]
    moments: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def option_implied_moments_report(options: list[dict[str, Any]], *, spot: float) -> OptionImpliedMomentsResult:
    px = _positive(spot, "spot")
    if not isinstance(options, list):
        raise ValueError("options must be a list")
    rows = [_parse_option(row, px) for row in options]
    if len(rows) < 3:
        raise ValueError("options must contain at least three contracts")
    atm = min(rows, key=lambda row: abs(row["moneyness"] - 1.0))
    put_wing = [row["iv"] for row in rows if row["moneyness"] < 1.0]
    call_wing = [row["iv"] for row in rows if row["moneyness"] > 1.0]
    expiries = sorted(set(row["expiry"] for row in rows))
    near = mean([row["iv"] for row in rows if row["expiry"] == expiries[0]])
    far = mean([row["iv"] for row in rows if row["expiry"] == expiries[-1]])
    ivs = [row["iv"] for row in rows]
    mu = mean(ivs)
    sigma = std(ivs)
    skew = 0.0 if sigma == 0 else mean([((iv - mu) / sigma) ** 3 for iv in ivs])
    kurt = 0.0 if sigma == 0 else mean([((iv - mu) / sigma) ** 4 for iv in ivs])
    return OptionImpliedMomentsResult(
        {"atm_iv": atm["iv"], "skew": (mean(call_wing) if call_wing else atm["iv"]) - (mean(put_wing) if put_wing else atm["iv"]), "curvature": mean(put_wing + call_wing) - atm["iv"] if put_wing or call_wing else 0.0},
        {"near_iv": near, "far_iv": far, "slope": far - near},
        {"variance": mean([iv * iv for iv in ivs]), "skewness": skew, "kurtosis": kurt},
    )


def _parse_option(row: dict[str, Any], spot: float) -> dict[str, float]:
    if not isinstance(row, dict):
        raise ValueError("options must contain dicts")
    strike = _positive(row.get("strike"), "strike")
    iv = _positive(row.get("iv"), "iv")
    expiry = _positive(row.get("expiry"), "expiry")
    return {"strike": strike, "iv": iv, "expiry": expiry, "moneyness": strike / spot}


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be positive")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


__all__ = ["OptionImpliedMomentsResult", "option_implied_moments_report"]
