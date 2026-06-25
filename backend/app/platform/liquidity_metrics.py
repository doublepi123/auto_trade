"""P237: Liquidity Metrics — Amihud illiquidity, Roll spread, Pastor-Stambaugh, Corwin-Schultz.

The canonical illiquidity / transaction-cost proxies from market-microstructure
literature. Each captures a different facet of how expensive it is to trade:

* **Amihud (2002)** — ``ILLIQ = (1/D) · Σ |r_t| / DV_t`` where ``DV_t`` is the
  dollar volume on bar ``t``. Average absolute return per dollar of volume is a
  price-impact proxy; illiquid names move a lot on little volume. We skip
  zero-volume bars (the ratio is undefined) and require strictly positive
  volumes elsewhere.
* **Roll (1984)** — the effective bid-ask spread from serial covariance of
  returns. Under a martingale midprice with i.i.d. signed spread shocks, the
  first-order serial covariance ``cov(r_t, r_{t-1}) = −s²/4`` where ``s`` is the
  proportional spread, so ``spread = 2·√(−cov)``. When the covariance is
  non-negative the estimator is undefined and we return 0 (no reliable spread).
* **Pastor-Stambaugh (2003)** — liquidity-beta. Regress returns on market
  returns and their square; the signed coefficient on ``market_r²`` captures
  liquidity-risk exposure (negative ⇒ liquidity-sensitive, the canonical sign
  per Pastor-Stambaugh).
* **Corwin-Schultz (2012)** — the high-low spread estimator. The 1-bar log
  range ``ln(H/L)`` scales with both volatility and the spread, while the
  2-bar range ``ln(max(H)/min(L))`` scales with ``√2``-times the volatility but
  the *same* spread (the spread does not compound over time). Solving the two
  moment equations eliminates volatility and recovers the proportional spread:

    ``spread = (√2 · α₁ − α₂) / (√2 − 1)``

  where ``α₁ = ½(ln(H_i/L_i) + ln(H_{i+1}/L_{i+1}))`` is the mean 1-bar log
  range and ``α₂ = ln(max(H)/min(L))`` is the 2-bar log range. We average across
  consecutive 2-bar windows; per-window estimates that come out negative (no
  spread signal / sampling noise) are clamped to 0.

Deterministic, pure Python (math only). Reference: Amihud (2002) JFE, Roll
(1984) RFS, Pastor-Stambaugh (2003) JFE, Corwin-Schultz (2012) JFE.
Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "RollResult",
    "LiquidityResult",
    "amihud_illiquidity",
    "roll_spread",
    "pastor_stambaugh",
    "corwin_schultz",
    "liquidity_report",
]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _ols_slope(y: Sequence[float], x: Sequence[float]) -> tuple[float, float]:
    """Pure-Python OLS intercept + slope of ``y`` on ``x`` via normal equations.

    Returns ``(alpha, beta)``. Raises ``ValueError`` if ``x`` has zero variance.
    """
    n = len(y)
    if n != len(x) or n < 2:
        raise ValueError("y and x must be equal-length with >=2 points")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx == 0.0:
        raise ValueError("x has zero variance; cannot fit OLS slope")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    beta = sxy / sxx
    alpha = my - beta * mx
    return alpha, beta


def amihud_illiquidity(returns: Sequence[float], volumes: Sequence[float]) -> float:
    """Amihud (2002) illiquidity: average ``|r_t| / dollar_volume_t``.

    ``ILLIQ = (1/D) · Σ_t |r_t| / DV_t`` where ``DV_t`` is interpreted as the
    dollar volume of bar ``t`` (volume × price), the standard Amihud input.
    Zero-volume bars are skipped (the ratio is undefined). Raises ``ValueError``
    on length mismatch, empty inputs, or zero/negative volumes on a bar that
    carries a non-zero return.
    """
    n = len(returns)
    if n == 0:
        raise ValueError("returns must be non-empty")
    if n != len(volumes):
        raise ValueError("returns and volumes must have equal length")
    total = 0.0
    count = 0
    for r, v in zip(returns, volumes):
        if v <= 0.0:
            # zero-volume bar: skip only when return is also 0; a non-zero
            # return on zero volume is an inconsistent input
            if r == 0.0:
                continue
            raise ValueError("volumes must be strictly positive")
        total += abs(r) / v
        count += 1
    if count == 0:
        raise ValueError("no non-zero-volume bars to average")
    return total / count


@dataclass(frozen=True)
class RollResult:
    spread: float  # effective proportional spread = 2*sqrt(-cov)
    serial_cov: float  # cov(r_t, r_{t-1})
    is_positive_autocov: bool  # True => spread undefined, returned 0

    def to_dict(self) -> dict:
        return {
            "spread": self.spread,
            "serial_cov": self.serial_cov,
            "is_positive_autocov": self.is_positive_autocov,
        }


def roll_spread(returns: Sequence[float]) -> RollResult:
    """Roll (1984) effective bid-ask spread from serial covariance.

    ``spread = 2·√(−cov(r_t, r_{t-1}))`` when the serial covariance is negative
    (the sign implied by bid-ask bounce); otherwise the estimator is undefined
    and we return ``spread = 0`` with ``is_positive_autocov = True``. Requires
    ≥2 returns.
    """
    n = len(returns)
    if n < 2:
        raise ValueError("need >=2 returns for Roll spread")
    mu = _mean(returns)
    # cov(r_t, r_{t-1}) with sample mean; n-1 overlapping pairs
    cov = sum((returns[i] - mu) * (returns[i - 1] - mu) for i in range(1, n)) / (n - 1)
    if cov < 0.0:
        spread = 2.0 * math.sqrt(-cov)
        return RollResult(spread=spread, serial_cov=cov, is_positive_autocov=False)
    return RollResult(spread=0.0, serial_cov=cov, is_positive_autocov=True)


def pastor_stambaugh(
    returns: Sequence[float],
    market_returns: Sequence[float],
) -> float:
    """Pastor-Stambaugh (2003) liquidity-beta proxy.

    We isolate the coefficient on ``market_r²`` via a residualized two-step
    OLS procedure (Frisch-Waugh-Lovell):

    1. ``r_t = α + γ·m_t + u_t``  (linear market exposure)
    2. ``m_t² = a + b·m_t + v_t``  (demean the squared market return of its
       level, so the squared term is orthogonal to the linear one)
    3. ``u_t = c + δ·v_t``  ⇒ ``δ`` is the signed liquidity-beta: the part of
       return co-movement with squared market returns not explained by the
       linear term. Negative ``δ`` is the canonical liquidity-sensitive sign.

    Raises ``ValueError`` on length mismatch or <3 points (need ≥3 for a
    meaningful two-step OLS with residual variation).
    """
    n = len(returns)
    if n != len(market_returns):
        raise ValueError("returns and market_returns must have equal length")
    if n < 3:
        raise ValueError("need >=3 points for Pastor-Stambaugh liquidity beta")
    # Step 1: regress returns on market_returns → residuals u_t
    alpha1, gamma = _ols_slope(returns, market_returns)
    u = [returns[i] - (alpha1 + gamma * market_returns[i]) for i in range(n)]
    # Step 2: regress market_returns² on market_returns → residuals v_t
    m2 = [m * m for m in market_returns]
    alpha2, beta2 = _ols_slope(m2, market_returns)
    v = [m2[i] - (alpha2 + beta2 * market_returns[i]) for i in range(n)]
    # Step 3: regress u_t on v_t → δ is the liquidity-beta
    _, delta = _ols_slope(u, v)
    return delta


def corwin_schultz(highs: Sequence[float], lows: Sequence[float]) -> float:
    """Corwin-Schultz (2012) bid-ask spread from 2-bar high-low.

    For each consecutive 2-bar window ``i, i+1``:

        α₁ = ½·(ln(H_i/L_i) + ln(H_{i+1}/L_{i+1}))   (mean 1-bar log range)
        α₂ = ln(max(H_i,H_{i+1}) / min(L_i,L_{i+1})) (2-bar log range)
        spread_i = (√2 · α₁ − α₂) / (√2 − 1)

    The 1-bar range scales with ``σ + S`` (volatility plus spread) and the
    2-bar range with ``√2·σ + S`` (volatility scales with √time, spread does
    not), so eliminating ``σ`` isolates ``S``. Per-window estimates that come
    out negative (no spread signal / sampling noise) are clamped to 0; we
    average across all windows. Raises ``ValueError`` on length mismatch, <2
    bars, or non-positive highs/lows (log undefined).
    """
    n = len(highs)
    if n != len(lows):
        raise ValueError("highs and lows must have equal length")
    if n < 2:
        raise ValueError("need >=2 bars for Corwin-Schultz spread")
    for h, l in zip(highs, lows):
        if h <= 0.0 or l <= 0.0:
            raise ValueError("highs and lows must be strictly positive")
        if h < l:
            raise ValueError("high must be >= low at each bar")
    sqrt2 = math.sqrt(2.0)
    denom = sqrt2 - 1.0
    spreads: list[float] = []
    for i in range(n - 1):
        ln_h1_i = math.log(highs[i] / lows[i])
        ln_h1_j = math.log(highs[i + 1] / lows[i + 1])
        h2 = highs[i] if highs[i] >= highs[i + 1] else highs[i + 1]
        l2 = lows[i] if lows[i] <= lows[i + 1] else lows[i + 1]
        ln_hl_2 = math.log(h2 / l2)
        alpha_1 = 0.5 * (ln_h1_i + ln_h1_j)
        alpha_2 = ln_hl_2
        est = (sqrt2 * alpha_1 - alpha_2) / denom
        # Clamp negative per-window estimates (no spread signal / noise) to 0,
        # matching the Corwin-Schultz (2012) treatment of the estimator.
        if est < 0.0:
            est = 0.0
        spreads.append(est)
    if not spreads:
        raise ValueError("no 2-bar windows to average")
    return sum(spreads) / len(spreads)


@dataclass(frozen=True)
class LiquidityResult:
    amihud: float | None
    roll: RollResult | None
    pastor_stambaugh: float | None
    corwin_schultz: float | None
    n: int

    def to_dict(self) -> dict:
        return {
            "amihud": self.amihud,
            "roll": self.roll.to_dict() if self.roll is not None else None,
            "pastor_stambaugh": self.pastor_stambaugh,
            "corwin_schultz": self.corwin_schultz,
            "n": self.n,
        }


def liquidity_report(
    returns: Sequence[float],
    volumes: Sequence[float] | None = None,
    market_returns: Sequence[float] | None = None,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
) -> LiquidityResult:
    """Aggregate whichever liquidity proxies have the inputs to compute.

    Returns a :class:`LiquidityResult` with ``None`` for any proxy whose inputs
    are missing or invalid (an exception from a single estimator does not abort
    the others). ``n`` is the return-series length.
    """
    n = len(returns)
    amihud: float | None = None
    if volumes is not None:
        try:
            amihud = amihud_illiquidity(returns, volumes)
        except ValueError:
            amihud = None
    roll: RollResult | None = None
    if n >= 2:
        try:
            roll = roll_spread(returns)
        except ValueError:
            roll = None
    ps: float | None = None
    if market_returns is not None:
        try:
            ps = pastor_stambaugh(returns, market_returns)
        except ValueError:
            ps = None
    cs: float | None = None
    if highs is not None and lows is not None:
        try:
            cs = corwin_schultz(highs, lows)
        except ValueError:
            cs = None
    return LiquidityResult(
        amihud=amihud,
        roll=roll,
        pastor_stambaugh=ps,
        corwin_schultz=cs,
        n=n,
    )