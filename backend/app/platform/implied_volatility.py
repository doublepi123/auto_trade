"""P244: Implied volatility and the Gatheral SVI volatility surface.

Two capabilities:

* **implied_volatility** — invert Black-Scholes for σ given a market price,
  via Newton-Raphson with a Brenner-Subrahmanyam (1988) initial guess and a
  bisection fallback when the Newton step leaves the bracket. Converges to
  ~1e-10 in a handful of iterations for realistic quotes; no scipy.

* **svi_fit** — fit Gatheral's (2004) **raw SVI** parameterization

      w(k) = a + b · ( ρ · (k − m) + √((k − m)² + σ²) )

  where ``k = log(K/F)`` is log-moneyness, ``w = σ²`` is total variance, ``a``
  the asymptotic left variance, ``b`` the wing slope, ``ρ`` the left/right
  asymmetry (∈ [−1, 1]), ``m`` the horizontal shift and ``σ>0`` the smoothness
  (at-the-money curvature). Fitting is non-linear least squares via
  Gauss-Newton with Levenberg-Marquardt damping; the smoothness parameter
  ``σ`` is kept strictly positive via a soft reparametrisation and the
  constraints ``a ≥ 0``, ``b ≥ 0``, ``|ρ| ≤ 1`` are enforced by projection at
  each step. No scipy/numpy.

Reference: Gatheral (2004) "Arbitrage-free SVI volatility surfaces";
Brenner-Subrahmanyam (1988) for the IV initial guess; Jaeckel (2010)
"By Implication". Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.platform._math_utils import norm_pdf
from app.platform.options_pricing import OptionType, black_scholes

__all__ = [
    "ImpliedVolResult",
    "SviFit",
    "implied_volatility",
    "svi_total_variance",
    "svi_fit",
]


def _bs_vega(spot: float, strike: float, t: float, r: float, sigma: float, q: float) -> float:
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    return spot * math.exp(-q * t) * norm_pdf(d1) * sqrt_t


def implied_volatility(
    option_type: OptionType,
    price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    dividend_yield: float = 0.0,
    *,
    max_iter: int = 100,
    tol: float = 1e-10,
) -> float:
    """Invert Black-Scholes for σ via Newton-Raphson with bisection fallback.

    Brenner-Subrahmanyam initial guess σ₀ ≈ sqrt(2π/T) · (C/S). Raises
    ``ValueError`` if the price is outside the no-arbitrage bounds
    (intrinsic < price < asset-forward for calls, etc.).
    """
    if price <= 0.0:
        raise ValueError("price must be positive")
    if time_to_expiry <= 0.0:
        raise ValueError("time_to_expiry must be positive")
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError("spot and strike must be positive")
    disc_r = math.exp(-risk_free * time_to_expiry)
    disc_q = math.exp(-dividend_yield * time_to_expiry)
    if option_type == "call":
        intrinsic = max(spot * disc_q - strike * disc_r, 0.0)
        upper = spot * disc_q
    elif option_type == "put":
        intrinsic = max(strike * disc_r - spot * disc_q, 0.0)
        upper = strike * disc_r
    else:
        raise ValueError("option_type must be 'call' or 'put'")
    if not (intrinsic - 1e-12 < price < upper + 1e-12):
        raise ValueError("price outside no-arbitrage bounds")

    # Brenner-Subrahmanyam initial guess (for ATM-ish call), guarded.
    guess = math.sqrt(2.0 * math.pi / time_to_expiry) * price / spot
    if guess <= 0.0:
        guess = 0.2
    sigma = max(min(guess, 5.0), 1e-4)

    # Newton-Raphson with bisection bracket fallback.
    lo, hi = 1e-6, 5.0
    for _ in range(max_iter):
        bs_price = black_scholes(option_type, spot, strike, time_to_expiry, risk_free, sigma, dividend_yield)
        diff = bs_price - price
        if abs(diff) < tol:
            return sigma
        v = _bs_vega(spot, strike, time_to_expiry, risk_free, sigma, dividend_yield)
        if v < 1e-12:
            break
        step = diff / v
        new_sigma = sigma - step
        # Keep inside the bracket; if Newton overshoots, shrink via bisection.
        if new_sigma <= lo or new_sigma >= hi:
            mid = 0.5 * (lo + hi)
            # Decide which half keeps the root.
            p_mid = black_scholes(option_type, spot, strike, time_to_expiry, risk_free, mid, dividend_yield)
            if (p_mid - price) * diff < 0:
                if diff > 0:
                    hi = mid
                else:
                    lo = mid
            else:
                if diff > 0:
                    lo = mid
                else:
                    hi = mid
            sigma = 0.5 * (lo + hi)
        else:
            # Update bracket to the side that still brackets.
            if diff > 0:
                hi = sigma
            else:
                lo = sigma
            sigma = new_sigma
        if hi - lo < tol:
            return sigma
    return sigma


@dataclass(frozen=True)
class ImpliedVolResult:
    option_type: str
    price: float
    implied_vol: float
    spot: float
    strike: float
    time_to_expiry: float
    risk_free: float
    dividend_yield: float

    def to_dict(self) -> dict:
        return {
            "option_type": self.option_type,
            "price": self.price,
            "implied_vol": self.implied_vol,
            "spot": self.spot,
            "strike": self.strike,
            "time_to_expiry": self.time_to_expiry,
            "risk_free": self.risk_free,
            "dividend_yield": self.dividend_yield,
        }


# ---------------------------------------------------------------------------
# SVI raw parameterisation
# ---------------------------------------------------------------------------


def svi_total_variance(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> float:
    """Gatheral raw-SVI total variance w(k) = a + b·(ρ(k−m) + √((k−m)²+σ²))."""
    if sigma <= 0.0:
        raise ValueError("SVI sigma must be positive")
    km = k - m
    return a + b * (rho * km + math.sqrt(km * km + sigma * sigma))


def _svi_residuals(
    log_moneyness: Sequence[float],
    total_var: Sequence[float],
    a: float,
    b: float,
    rho: float,
    m: float,
    sigma: float,
) -> list[float]:
    return [
        svi_total_variance(k, a, b, rho, m, sigma) - w
        for k, w in zip(log_moneyness, total_var)
    ]


def _svi_jacobian(k: float, a: float, b: float, rho: float, m: float, sigma: float) -> list[float]:
    """Jacobian row ∂w/∂(a,b,rho,m,sigma) at a single k."""
    km = k - m
    s = math.sqrt(km * km + sigma * sigma)
    # dw/da = 1
    # dw/db = rho*km + s
    # dw/drho = b*km
    # dw/dm = b * (-rho - km/s)  [since d(km)/dm = -1, and d/dm sqrt(km^2+sig^2) = -km/s]
    # dw/dsigma = b * sigma / s
    dw_da = 1.0
    dw_db = rho * km + s
    dw_drho = b * km
    dw_dm = b * (-rho - km / s)
    dw_dsigma = b * sigma / s
    return [dw_da, dw_db, dw_drho, dw_dm, dw_dsigma]


def _project_params(a: float, b: float, rho: float, m: float, sigma: float) -> tuple[float, float, float, float, float]:
    """Project parameters into the admissible set: a≥0, b≥0, |ρ|≤1, σ>0."""
    a = max(a, 0.0)
    b = max(b, 0.0)
    rho = max(-1.0, min(1.0, rho))
    sigma = max(sigma, 1e-6)
    return a, b, rho, m, sigma


def svi_fit(
    log_moneyness: Sequence[float],
    implied_vols: Sequence[float],
    time_to_expiry: float,
    *,
    init: tuple[float, float, float, float, float] | None = None,
    max_iter: int = 200,
    tol: float = 1e-10,
    lam: float = 1e-3,
) -> SviFit:
    """Fit raw-SVI parameters to a slice of implied vols via Gauss-Newton + LM.

    Inputs are log-moneyness ``k = ln(K/F)`` and the corresponding Black-Scholes
    implied volatilities; total variances ``w = σ_imp²`` are fit. Raises
    ``ValueError`` on length mismatch / empty / non-positive T.
    """
    n = len(log_moneyness)
    if n != len(implied_vols):
        raise ValueError("log_moneyness and implied_vols must have equal length")
    if n < 5:
        raise ValueError("at least 5 points required to fit 5 SVI parameters")
    if time_to_expiry <= 0.0:
        raise ValueError("time_to_expiry must be positive")
    ks = [float(k) for k in log_moneyness]
    ws = [float(v) * float(v) * time_to_expiry for v in implied_vols]
    if any(w <= 0.0 for w in ws):
        raise ValueError("implied vols must be positive")

    # Initial guess: ATM variance ~ mean(w); small slope, symmetric, no shift.
    if init is None:
        w0 = sum(ws) / n
        a0 = 0.5 * w0
        b0 = 0.1
        rho0 = 0.0
        m0 = 0.0
        sigma0 = 0.1 * w0 if w0 > 0 else 0.1
        a, b, rho, m, sigma = a0, b0, rho0, m0, max(sigma0, 1e-3)
    else:
        a, b, rho, m, sigma = init
    a, b, rho, m, sigma = _project_params(a, b, rho, m, sigma)

    def sse(a: float, b: float, rho: float, m: float, sigma: float) -> float:
        r = _svi_residuals(ks, ws, a, b, rho, m, sigma)
        return sum(x * x for x in r)

    cur_sse = sse(a, b, rho, m, sigma)
    cur_lam = lam
    for _ in range(max_iter):
        r = _svi_residuals(ks, ws, a, b, rho, m, sigma)
        # Normal equations: (JᵀJ + λI) Δ = -Jᵀ r
        jtj = [[0.0] * 5 for _ in range(5)]
        jtr = [0.0] * 5
        for k, ri in zip(ks, r):
            jac = _svi_jacobian(k, a, b, rho, m, sigma)
            for i in range(5):
                jtr[i] += jac[i] * ri
                for j in range(5):
                    jtj[i][j] += jac[i] * jac[j]
        # LM damping on the diagonal.
        for i in range(5):
            jtj[i][i] += cur_lam * (jtj[i][i] + 1e-12)
        delta = _solve5(jtj, [-x for x in jtr])
        if delta is None:
            cur_lam *= 10.0
            if cur_lam > 1e12:
                break
            continue
        na, nb, nr, nm, ns = _project_params(
            a + delta[0], b + delta[1], rho + delta[2], m + delta[3], sigma + delta[4]
        )
        new_sse = sse(na, nb, nr, nm, ns)
        if new_sse < cur_sse:
            a, b, rho, m, sigma = na, nb, nr, nm, ns
            if abs(cur_sse - new_sse) < tol * max(cur_sse, 1e-12):
                cur_sse = new_sse
                break
            cur_sse = new_sse
            cur_lam = max(cur_lam * 0.5, 1e-12)
        else:
            cur_lam *= 2.0
            if cur_lam > 1e12:
                break
    rms = math.sqrt(cur_sse / n)
    return SviFit(
        a=a, b=b, rho=rho, m=m, sigma=sigma,
        time_to_expiry=time_to_expiry,
        rms=rms,
        n_points=n,
    )


def _solve5(mat: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve a 5×5 linear system via Gaussian elimination with partial pivoting."""
    n = 5
    a = [row[:] + [rhs[i]] for i, row in enumerate(mat)]
    for col in range(n):
        # pivot
        pivot = col
        best = abs(a[col][col])
        for r in range(col + 1, n):
            if abs(a[r][col]) > best:
                best = abs(a[r][col])
                pivot = r
        if best < 1e-18:
            return None
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
        piv = a[col][col]
        for r in range(col + 1, n):
            factor = a[r][col] / piv
            if factor != 0.0:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    # back-substitute
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = a[i][n]
        for c in range(i + 1, n):
            s -= a[i][c] * x[c]
        x[i] = s / a[i][i]
    return x


@dataclass(frozen=True)
class SviFit:
    a: float
    b: float
    rho: float
    m: float
    sigma: float
    time_to_expiry: float
    rms: float
    n_points: int

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "rho": self.rho,
            "m": self.m,
            "sigma": self.sigma,
            "time_to_expiry": self.time_to_expiry,
            "rms": self.rms,
            "n_points": self.n_points,
        }

def implied_volatility_result(
    option_type: OptionType,
    price: float,
    spot: float,
    strike: float,
    time_to_expiry: float,
    risk_free: float,
    dividend_yield: float = 0.0,
) -> ImpliedVolResult:
    """Wrap :func:`implied_volatility` in an :class:`ImpliedVolResult`."""
    iv = implied_volatility(option_type, price, spot, strike, time_to_expiry, risk_free, dividend_yield)
    return ImpliedVolResult(
        option_type=option_type, price=price, implied_vol=iv,
        spot=spot, strike=strike, time_to_expiry=time_to_expiry,
        risk_free=risk_free, dividend_yield=dividend_yield,
    )
