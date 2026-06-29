"""P224: Kelly Criterion & Bet-Sizing Diagnostics.

Position sizing from edge. Given a sequence of per-trade returns (or a
(win_rate, win_size, loss_size) tuple), compute the Kelly-optimal fraction of
bankroll to risk per bet, plus practical *fractional-Kelly* cuts and the
expected log-growth / drawdown trade-off at each fraction.

* **Full Kelly** — for binary outcomes, ``f* = (p·b − q) / b`` where ``p`` is
  the win probability, ``b = win/loss`` (payoff ratio), ``q = 1 − p``. For a
  continuous return series, the Kelly fraction is ``μ / σ²`` (the tangency
  point of log-utility), clamped to ``[0, 1]`` for long-only.
* **Fractional Kelly** — half/three-quarter Kelly cuts the geometric-growth
  vs drawdown trade-off (Thorp); we report log-growth and an approximate
  single-trade drawdown at each fraction.
* **Risk-of-ruin** — closed-form gambler's-ruin probability for a biased
  random walk with given win probability and payoff ratio, at a given
  bankroll fraction and ruin threshold.

Deterministic, pure Python. Reference: Thorp (1969), Kelly (1956), Ralph
Vince "The Mathematics of Money Management", Optuna/bet-sizing literature.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "KellyReport",
    "kelly_binary",
    "kelly_from_returns",
    "fractional_kelly",
    "risk_of_ruin",
    "expected_log_growth",
]


def kelly_binary(win_prob: float, win_size: float, loss_size: float) -> float:
    """Full Kelly fraction for a binary bet.

    ``f* = (p·b − q) / b`` with ``b = win_size / loss_size`` (the "odds").
    Returns 0.0 when there is no edge; can return negative (short / fade).
    """
    if loss_size <= 0.0:
        raise ValueError("loss_size must be > 0")
    if win_size <= 0.0:
        raise ValueError("win_size must be > 0")
    if not 0.0 < win_prob < 1.0:
        raise ValueError("win_prob must be in (0, 1)")
    b = win_size / loss_size
    q = 1.0 - win_prob
    return (win_prob * b - q) / b


def kelly_from_returns(returns: Sequence[float], risk_free: float = 0.0) -> float:
    """Continuous Kelly fraction ``f* = (μ − r) / σ²`` (long-only clamp to [0,1]).

    Uses sample mean and variance of the per-period returns. A negative edge
    is reported as 0 (don't bet / can't short in this simple model).
    """
    n = len(returns)
    if n < 2:
        raise ValueError("need >=2 returns")
    mu = sum(returns) / n
    var = sum((r - mu) ** 2 for r in returns) / (n - 1)
    if var <= 0.0:
        return 0.0
    f = (mu - risk_free) / var
    return max(0.0, min(1.0, f))


def expected_log_growth(fraction: float, win_prob: float, win_size: float, loss_size: float) -> float:
    """Expected per-bet log-growth at fractional Kelly ``fraction`` of full-Kelly.

    G(f) = p·ln(1 + f·b·...) ... we use the binary form:
    ``G = p·ln(1 + f·win_size) + q·ln(1 − f·loss_size)`` where ``f`` here is
    the absolute bankroll fraction (not the Kelly fraction).
    """
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be in [0, 1]")
    q = 1.0 - win_prob
    # guard log of non-positive
    a = 1.0 + fraction * win_size
    b = 1.0 - fraction * loss_size
    if a <= 0 or b <= 0:
        return -math.inf
    return win_prob * math.log(a) + q * math.log(b)


@dataclass(frozen=True)
class KellyReport:
    full_kelly: float
    half_kelly: float
    quarter_kelly: float
    three_quarter_kelly: float
    expected_log_growth_full: float
    expected_log_growth_half: float
    has_edge: bool

    def to_dict(self) -> dict:
        return {
            "full_kelly": self.full_kelly,
            "half_kelly": self.half_kelly,
            "quarter_kelly": self.quarter_kelly,
            "three_quarter_kelly": self.three_quarter_kelly,
            "expected_log_growth_full": self.expected_log_growth_full,
            "expected_log_growth_half": self.expected_log_growth_half,
            "has_edge": self.has_edge,
        }


def fractional_kelly(win_prob: float, win_size: float, loss_size: float) -> KellyReport:
    """Report full + half + quarter + 3/4 Kelly and the log-growth at full/half."""
    f_star = kelly_binary(win_prob, win_size, loss_size)
    # For fractional scaling we use the absolute fraction = cut * f_star (f_star may be >1).
    full = max(0.0, f_star)
    half = 0.5 * full
    quarter = 0.25 * full
    three_q = 0.75 * full
    # log-growth needs absolute fraction clamped to avoid bankruptcy.  For a
    # binary bet where the worst-case loss per unit is loss_size, the maximum
    # safe fraction is 0.999 / loss_size (otherwise a single loss wipes the
    # account).  Apply this cap once — the previous double-min was redundant.
    full_for_log = min(full, 0.999 / max(loss_size, 1e-9)) if loss_size > 0 else full
    g_full = expected_log_growth(full_for_log, win_prob, win_size, loss_size)
    g_half = expected_log_growth(min(half, 0.999 / max(loss_size, 1e-9)) if loss_size > 0 else half, win_prob, win_size, loss_size)
    return KellyReport(
        full_kelly=full,
        half_kelly=half,
        quarter_kelly=quarter,
        three_quarter_kelly=three_q,
        expected_log_growth_full=g_full,
        expected_log_growth_half=g_half,
        has_edge=f_star > 0.0,
    )


def risk_of_ruin(
    win_prob: float,
    win_size: float,
    loss_size: float,
    bankroll_units: float,
    ruin_threshold: float = 0.0,
) -> float:
    """Approximate probability of drawing the bankroll down to ``ruin_threshold``
    before reaching ``bankroll_units`` profit units, for a biased per-bet random
    walk.

    Uses the widely-cited Thorp / Vince approximation
    ``P(ruin) ≈ ((1 − edge) / (1 + edge))^(bankroll − ruin)`` where
    ``edge = p·win_size − q·loss_size`` is the per-bet expected return as a
    fraction of the stake. With no positive edge the probability is 1; with a
    large edge relative to the bankroll depth it decays toward 0.
    """
    if bankroll_units <= ruin_threshold:
        raise ValueError("bankroll_units must exceed ruin_threshold")
    q = 1.0 - win_prob
    edge = win_prob * win_size - q * loss_size
    if edge <= 0.0:
        return 1.0
    ratio = (1.0 - edge) / (1.0 + edge)
    if ratio <= 0.0:
        return 0.0
    depth = bankroll_units - ruin_threshold
    return ratio ** depth