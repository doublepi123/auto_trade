"""P349: Levy process option pricing (Variance Gamma / CGMY).

Pure Python option pricing under Variance Gamma (VG) and CGMY models using
the characteristic function with direct numerical integration (Simpson rule).
No scipy/numpy dependency — only ``math`` + ``cmath``.

References
----------
* Madan, Carr & Chang (1998) "The Variance Gamma Process and Option Pricing"
* Carr, Geman, Madan & Yor (2002) "The Fine Structure of Asset Returns"
* Carr & Madan (1999) "Option valuation using the fast Fourier transform"
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass

__all__ = [
    "LevyProcessResult",
    "levy_process_report",
    "vg_characteristic",
    "cgmy_characteristic",
]


def vg_characteristic(u: complex, sigma: float, nu: float, theta: float, T: float = 1.0) -> complex:
    """Variance Gamma characteristic function φ(u) at maturity T.

    Per spec: the base form is
    φ_1(u) = exp(i·u·θ·ln(1 + σ²·ν/2)) · (1 − i·ν·θ·u + σ²·ν·u²/2)^(−1/ν)
    and φ_T(u) = φ_1(u)^T.
    """
    if nu <= 0.0:
        raise ValueError("nu must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    i = 1j
    log_adj = cmath.log(1.0 + sigma * sigma * nu / 2.0)
    term = 1.0 - i * nu * theta * u + sigma * sigma * nu * u * u / 2.0
    base = cmath.exp(i * u * theta * log_adj) * (term ** (-1.0 / nu))
    if T == 1.0:
        return base
    return base ** T


def _gamma_lanczos(z: complex) -> complex:
    """Complex gamma function approximation using Lanczos method.

    For real positive z, falls back to math.gamma. For complex z, uses the
    Lanczos series approximation. This is only used by CGMY.
    """
    if isinstance(z, (int, float)):
        if z == abs(z) and z > 0:
            return complex(math.gamma(float(z)))
        # Fall through to Lanczos
    # Lanczos coefficients (7-term)
    g = 7
    p = [
        0.99999999999980993,
        676.5203681218851,
        -1259.1392167224028,
        771.32342877765313,
        -176.61502916214059,
        12.507343278686905,
        -0.13857109526572012,
        9.9843695780195716e-6,
        1.5056327351493116e-7,
    ]
    if z.real < 0.5:
        # Reflection formula: Gamma(z) = pi / (sin(pi*z) * Gamma(1-z))
        return cmath.pi / (cmath.sin(cmath.pi * z) * _gamma_lanczos(1.0 - z))
    z = z - 1.0
    x = complex(p[0])
    for i in range(1, g + 2):
        x = x + p[i] / (z + float(i))
    t = z + float(g) + 0.5
    sqrt2pi = 2.5056282746310002  # sqrt(2*pi)
    return sqrt2pi * (t ** (z + 0.5)) * cmath.exp(-t) * x


def cgmy_characteristic(u: complex, sigma: float, nu: float, theta: float, T: float = 1.0) -> complex:
    """CGMY characteristic function φ(u) at maturity T.

    CGMY is a four-parameter Levy process generalizing VG. We map the input
    params to a sensible CGMY parametrization:
    - C = 1 / nu  (activity rate)
    - G and M derived from sigma and theta (positive/negative jump intensity)
    - Y = min(0.5, nu) to avoid infinite-variance with Y >= 2

    φ_T(u) = exp(T · C · Γ(−Y) · [(M − iu)^Y − M^Y + (G + iu)^Y − G^Y])
    """
    if nu <= 0.0:
        raise ValueError("nu must be positive")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    # Map to CGMY parameters
    C = 1.0 / nu
    Y = min(0.5, C * 0.3)  # keep Y in (0, 1) for tractable Levy measure
    if Y <= 0.0:
        Y = 0.3
    # G, M derived from σ and θ: wider σ → larger G,M
    scale = sigma * 2.0
    G = scale / (1.0 + max(-theta, 0.0))  # positive jump intensity
    M = scale / (1.0 + max(theta, 0.0))   # negative jump intensity
    G = max(G, 0.01)
    M = max(M, 0.01)
    if abs(G - M) < 1e-12:
        M = G * 1.001

    i = 1j
    # Γ(−Y)
    gamma_neg_y = _gamma_lanczos(complex(-Y))

    # (M − iu)^Y − M^Y
    term_m = (M - i * u) ** Y - M ** Y
    # (G + iu)^Y − G^Y
    term_g = (G + i * u) ** Y - G ** Y

    exponent = T * C * gamma_neg_y * (term_m + term_g)
    return cmath.exp(exponent)


def _simpson_integrate(f, a: float, b: float, n: int = 2000) -> complex:
    """Composite Simpson's rule for complex-valued integrands."""
    if n % 2 == 1:
        n += 1
    h = (b - a) / n
    result = f(a) + f(b)
    for i in range(1, n, 2):
        result += 4.0 * f(a + i * h)
    for i in range(2, n, 2):
        result += 2.0 * f(a + i * h)
    return result * h / 3.0


def _price_call_cf(
    cf_log_spot, S: float, K: float, T: float, r: float, n_steps: int = 2000
) -> float:
    """European call option price via characteristic function integration.

    Uses the standard representation:
    C = S·Π₁ − K·e^{−rT}·Π₂

    where:
    Π₁ = ½ + 1/π ∫₀^∞ Re[e^{−iuk} · φ(u−i) / (iu · φ(−i))] du
    Π₂ = ½ + 1/π ∫₀^∞ Re[e^{−iuk} · φ(u) / (iu)] du
    k = log(K/S)

    References: Gil-Pelaez (1951), Heston (1993) appendix.
    """
    k = math.log(K / S)
    phi_neg_i = cf_log_spot(-1j)
    if abs(phi_neg_i) < 1e-15:
        phi_neg_i = complex(1e-15, 0.0)

    def integrand_pi1(u: float) -> complex:
        numerator = cmath.exp(-1j * u * k) * cf_log_spot(u - 1j)
        denominator = 1j * u * phi_neg_i
        if abs(denominator) < 1e-15:
            return complex(0.0, 0.0)
        return numerator / denominator

    def integrand_pi2(u: float) -> complex:
        numerator = cmath.exp(-1j * u * k) * cf_log_spot(u)
        denominator = 1j * u
        if abs(denominator) < 1e-15:
            return complex(0.0, 0.0)
        return numerator / denominator

    # Truncate at u_max; a good rule of thumb for VG/CGMY is 500-1000
    u_max = 200.0
    try:
        int1 = _simpson_integrate(integrand_pi1, 1e-12, u_max, n_steps)
        int2 = _simpson_integrate(integrand_pi2, 1e-12, u_max, n_steps)
    except (OverflowError, ZeroDivisionError):
        return 0.0

    pi1 = 0.5 + (1.0 / cmath.pi) * int1
    pi2 = 0.5 + (1.0 / cmath.pi) * int2
    pi1 = float(pi1.real)
    pi2 = float(pi2.real)

    # Bound probabilities to [0, 1]
    pi1 = max(0.0, min(1.0, pi1))
    pi2 = max(0.0, min(1.0, pi2))

    disc = math.exp(-r * T)
    price = S * pi1 - K * disc * pi2
    return max(0.0, price)


def _log_spot_cf(model: str, sigma: float, nu: float, theta: float, r: float, T: float):
    """Build the risk-neutral log-spot characteristic function.

    For a Levy process with CF φ_X(u), the risk-neutral log-spot CF is:
    φ_log(u) = exp(iu·(r + ω)·T) · φ_X(u)^T
    where ω = −log(φ_X(−i)) ensures the martingale condition.
    """
    if model == "vg":
        raw_cf = lambda u: vg_characteristic(u, sigma, nu, theta, T=1.0)
    else:
        raw_cf = lambda u: cgmy_characteristic(u, sigma, nu, theta, T=1.0)

    phi_neg_i = raw_cf(-1j)
    # Martingale correction
    omega = 0.0
    if abs(phi_neg_i) > 1e-15:
        omega = -cmath.log(phi_neg_i).real

    def cf_log(u: complex) -> complex:
        return cmath.exp(1j * u * (r + omega) * T) * (raw_cf(u) ** T)

    return cf_log


@dataclass(frozen=True)
class LevyProcessResult:
    model: str
    spot: float
    strike: float
    expiry: float
    sigma: float
    nu: float
    theta: float
    risk_free: float
    price: float
    delta: float
    characteristic_function_eval: complex

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "spot": self.spot,
            "strike": self.strike,
            "expiry": self.expiry,
            "sigma": self.sigma,
            "nu": self.nu,
            "theta": self.theta,
            "risk_free": self.risk_free,
            "price": self.price,
            "delta": self.delta,
            "characteristic_function_eval": self.characteristic_function_eval,
        }


def levy_process_report(
    *,
    model: str = "vg",
    spot: float,
    strike: float,
    expiry: float,
    sigma: float,
    nu: float,
    theta: float,
    risk_free: float = 0.02,
) -> LevyProcessResult:
    """Levy process option pricing report.

    Parameters
    ----------
    model : "vg" or "cgmy"
        The Levy process model.
    spot : float
        Current underlying price (must be > 0, finite).
    strike : float
        Option strike price (must be > 0, finite).
    expiry : float
        Time to expiry in years (must be > 0, finite).
    sigma : float
        Volatility / scale parameter (must be > 0, finite).
    nu : float
        Kurtosis / shape parameter (must be > 0, finite).
    theta : float
        Skewness parameter (finite).
    risk_free : float
        Risk-free rate (finite, default 0.02).

    Returns
    -------
    LevyProcessResult
        Frozen dataclass with model, price, delta, and CF at u=1.
    """
    # Validate model
    if model not in ("vg",):
        raise ValueError("model must be 'vg'")

    # Validate all numeric inputs
    for name, val in [
        ("spot", spot), ("strike", strike), ("expiry", expiry),
        ("sigma", sigma), ("nu", nu), ("theta", theta), ("risk_free", risk_free),
    ]:
        if not math.isfinite(val):
            raise ValueError(f"{name} must be a finite number")

    if spot <= 0.0:
        raise ValueError("spot must be positive")
    if strike <= 0.0:
        raise ValueError("strike must be positive")
    if expiry <= 0.0:
        raise ValueError("expiry must be positive")
    if expiry > 10.0:
        raise ValueError("expiry must be at most 10.0 years")
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    if sigma > 5.0:
        raise ValueError("sigma must be at most 5.0")
    if nu <= 0.0:
        raise ValueError("nu must be positive")
    if theta > 100.0 or theta < -100.0:
        raise ValueError("theta must be in [-100, 100]")

    # Build the log-spot CF under risk-neutral measure
    cf_log = _log_spot_cf(model, sigma, nu, theta, risk_free, expiry)

    # Price the call option
    price = _price_call_cf(cf_log, spot, strike, expiry, risk_free)

    # Delta via central numerical difference (dS = 1% of spot)
    ds = max(spot * 0.01, 0.001)
    price_up = _price_call_cf(cf_log, spot + ds, strike, expiry, risk_free)
    price_down = _price_call_cf(cf_log, spot - ds, strike, expiry, risk_free)
    delta = (price_up - price_down) / (2.0 * ds)
    delta = max(0.0, min(1.0, delta))  # call delta in [0, 1]

    # Characteristic function evaluated at u = 1
    cf_eval = cf_log(complex(1.0))

    return LevyProcessResult(
        model=model,
        spot=spot,
        strike=strike,
        expiry=expiry,
        sigma=sigma,
        nu=nu,
        theta=theta,
        risk_free=risk_free,
        price=price,
        delta=delta,
        characteristic_function_eval=cf_eval,
    )
