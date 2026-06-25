"""P256: Fixed-income analytics — YTM, duration, convexity, forward rates.

Classical bond mathematics for a vanilla fixed-coupon bond paying coupon ``C``
per period on a face ``F``, maturing at ``T``, priced (dirty) at ``P``. All
functions are pure-Python closed form / Newton-Raphson, no scipy/numpy.

* **yield_to_maturity(price, face, coupon, periods)** — solve the IRR of the
  bond cashflows via Newton-Raphson with bisection fallback.
* **bond_price(ytm, face, coupon, periods)** — discounted-cashflow price.
* **macaulay_duration / modified_duration** — interest-rate sensitivity.
* **convexity** — second-order sensitivity.
* **forward_rate(spot_short, spot_long, short_maturity, long_maturity)** — the
  implied forward rate between two maturities.
* **bond_analytics(...)** — aggregated :class:`BondResult`.

Reference: Fabozzi "Fixed Income Mathematics"; Hull ch. 4. Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "BondResult",
    "bond_price",
    "yield_to_maturity",
    "macaulay_duration",
    "modified_duration",
    "convexity",
    "forward_rate",
    "bond_analytics",
]


def bond_price(ytm: float, face: float, coupon: float, periods: int) -> float:
    """Dirty price of a coupon bond given the per-period yield ``ytm``.

    ``coupon`` is the per-period coupon, ``face`` the redemption, ``periods``
    the number of periods to maturity. Raises ``ValueError`` on bad inputs.
    """
    if periods < 1:
        raise ValueError("periods must be >= 1")
    if face <= 0.0:
        raise ValueError("face must be positive")
    if ytm < -0.999:
        raise ValueError("ytm must be > -100%")
    price = 0.0
    for t in range(1, periods + 1):
        price += coupon / (1.0 + ytm) ** t
    price += face / (1.0 + ytm) ** periods
    return price


def yield_to_maturity(
    price: float,
    face: float,
    coupon: float,
    periods: int,
    *,
    max_iter: int = 200,
    tol: float = 1e-12,
) -> float:
    """Per-period yield to maturity via Newton-Raphson with bisection fallback.

    Raises ``ValueError`` if no IRR is bracketed (price outside
    ``[sum(coupons)+face discounted at 0, tiny]``) or inputs invalid.
    """
    if periods < 1:
        raise ValueError("periods must be >= 1")
    if face <= 0.0:
        raise ValueError("face must be positive")
    if price <= 0.0:
        raise ValueError("price must be positive")

    def f(y: float) -> float:
        return bond_price(y, face, coupon, periods) - price

    # Bracket: a low yield -> high price; high yield -> low price.
    lo, hi = -0.99, 10.0
    flo, fhi = f(lo), f(hi)
    # Expand the high bracket if needed.
    while fhi > 0 and hi < 1e6:
        hi *= 2.0
        fhi = f(hi)
    if flo * fhi > 0:
        raise ValueError("YTM not bracketable: price inconsistent with cashflows")

    # Bisection to get close, then Newton-Raphson for precision.
    y = 0.5 * (lo + hi)
    for _ in range(max_iter):
        diff = f(y)
        # Maintain bracket: f decreasing in y, so diff>0 means y too low.
        if diff > 0:
            lo = y
        else:
            hi = y
        if abs(diff) < tol or hi - lo < tol:
            return y
        # Analytical derivative dP/dy = -Σ t·cf_t/(1+y)^{t+1}.
        disc = 1.0 + y
        deriv = 0.0
        for t in range(1, periods + 1):
            cf = coupon + (face if t == periods else 0.0)
            deriv -= t * cf / disc ** (t + 1)
        y_new = y - diff / deriv if abs(deriv) > 1e-18 else 0.5 * (lo + hi)
        if not (lo < y_new < hi):
            y_new = 0.5 * (lo + hi)
        y = y_new
    return 0.5 * (lo + hi)


def macaulay_duration(ytm: float, face: float, coupon: float, periods: int) -> float:
    """Macaulay duration (in periods) — the PV-weighted average time to cashflow."""
    if periods < 1:
        raise ValueError("periods must be >= 1")
    if ytm <= -1.0:
        raise ValueError("ytm must be > -100%")
    price = bond_price(ytm, face, coupon, periods)
    disc = 1.0 + ytm
    num = 0.0
    for t in range(1, periods + 1):
        cf = coupon + (face if t == periods else 0.0)
        num += t * cf / disc ** t
    return num / price


def modified_duration(ytm: float, face: float, coupon: float, periods: int) -> float:
    """Modified duration = Macaulay / (1 + ytm)."""
    if ytm <= -1.0:
        raise ValueError("ytm must be > -100%")
    return macaulay_duration(ytm, face, coupon, periods) / (1.0 + ytm)


def convexity(ytm: float, face: float, coupon: float, periods: int) -> float:
    """Convexity (in periods²) — second-order price sensitivity to yield."""
    if periods < 1:
        raise ValueError("periods must be >= 1")
    if ytm <= -1.0:
        raise ValueError("ytm must be > -100%")
    price = bond_price(ytm, face, coupon, periods)
    disc = 1.0 + ytm
    num = 0.0
    for t in range(1, periods + 1):
        cf = coupon + (face if t == periods else 0.0)
        num += t * (t + 1) * cf / disc ** (t + 2)
    return num / price


def forward_rate(spot_short: float, spot_long: float, short_maturity: float, long_maturity: float) -> float:
    """Implied forward rate over [short, long] from two spot (zero) rates.

    ``(1 + r_L)^T_L = (1 + r_S)^T_S · (1 + f)^(T_L − T_S)`` ⇒ ``f``.
    """
    if short_maturity <= 0.0 or long_maturity <= short_maturity:
        raise ValueError("long_maturity must exceed short_maturity > 0")
    if spot_short <= -1.0 or spot_long <= -1.0:
        raise ValueError("spot rates must be > -100%")
    growth = (1.0 + spot_long) ** long_maturity / (1.0 + spot_short) ** short_maturity
    return growth ** (1.0 / (long_maturity - short_maturity)) - 1.0


@dataclass(frozen=True)
class BondResult:
    price: float
    ytm: float
    macaulay_duration: float
    modified_duration: float
    convexity: float
    face: float
    coupon: float
    periods: int

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "ytm": self.ytm,
            "macaulay_duration": self.macaulay_duration,
            "modified_duration": self.modified_duration,
            "convexity": self.convexity,
            "face": self.face,
            "coupon": self.coupon,
            "periods": self.periods,
        }


def bond_analytics(price: float, face: float, coupon: float, periods: int) -> BondResult:
    """Full bond analytics from a market price.

    Solves YTM then computes duration & convexity at that yield.
    """
    ytm = yield_to_maturity(price, face, coupon, periods)
    return BondResult(
        price=price,
        ytm=ytm,
        macaulay_duration=macaulay_duration(ytm, face, coupon, periods),
        modified_duration=modified_duration(ytm, face, coupon, periods),
        convexity=convexity(ytm, face, coupon, periods),
        face=face,
        coupon=coupon,
        periods=periods,
    )