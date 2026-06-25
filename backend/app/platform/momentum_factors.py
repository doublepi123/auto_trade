"""P238: Momentum / Reversal Factor Library (Jegadeesh-Titman, De Bondt-Thaler, Carhart).

Cross-sectional **and** time-series momentum / reversal factor construction
used by equity style factors and long-short portfolios:

* **time_series_momentum** — Moskowitz-Ooi-Pedersen (2012) TSMOM: the average
  signed forward return ``E_t[ r_{t→t+h} ]`` conditional on the past lookback
  return being signed.  Computed as the mean over ``t`` of
  ``sign(p_t / p_{t-L} − 1) · (p_{t+h} / p_t − 1)``; we skip the latest
  ``holding`` bars so every evaluation window is fully realised (no lookahead).
  Deterministic closed form, no RNG.
* **cross_sectional_momentum** — per-asset past-``lookback`` return
  ``p_t / p_{t-L} − 1``; the input used for cross-sectional long-short ranking.
* **momentum_factor** — Jegadeesh-Titman (1993) classic: rank assets by past
  ``lookback`` return, long the top ``n_long`` and short the bottom ``n_short``,
  equal-weighted long-short return over the ``holding`` period.
* **reversal_factor** — De Bondt-Thaler (1985) long-term reversal: the
  sign-flipped momentum leg (long past-LOSERS, short past-WINNERS).
* **carhart_momentum** — the Carhart (1997) single-factor momentum leg, an
  alias of :func:`momentum_factor` with equal long/short halves.

Reference: Jegadeesh & Titman (1993) JF; De Bondt & Thaler (1985); Carhart
(1997); Moskowitz, Ooi & Pedersen (2012).  Pure Python, no scipy / numpy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "MomentumFactorResult",
    "time_series_momentum",
    "cross_sectional_momentum",
    "momentum_factor",
    "reversal_factor",
    "carhart_momentum",
]


def _past_return(prices: Sequence[float], lookback: int) -> float:
    """Return ``p_t / p_{t-L} − 1`` using the last ``lookback+1`` prices."""
    if lookback <= 0:
        raise ValueError("lookback must be a positive int")
    if len(prices) < lookback + 1:
        raise ValueError("not enough prices for the requested lookback")
    base = prices[-lookback - 1]
    if base == 0:
        return 0.0
    return prices[-1] / base - 1.0


def time_series_momentum(
    prices: Sequence[float],
    lookback: int,
    holding: int = 1,
) -> float:
    """Time-series momentum (Moskowitz-Ooi-Pedersen 2012).

    Average signed forward return:

        TSMOM = (1/T) · Σ_t  sign(p_t / p_{t−L} − 1) · (p_{t+h} / p_t − 1)

    where the sum runs over every ``t`` for which both the lookback window
    ``[t−L, t]`` and the holding window ``[t, t+h]`` are fully populated. The
    latest ``holding`` bars are skipped so every evaluation is realised (no
    lookahead). ``holding`` defaults to 1.

    Parameters
    ----------
    prices : sequence of float
        Ordered price history (oldest first).
    lookback : int
        Formation-window length ``L`` (must be ≥ 1).
    holding : int, optional
        Forward-horizon length ``h`` (must be ≥ 1, default 1).

    Returns
    -------
    float
        The average signed forward return.

    Raises
    ------
    ValueError
        If inputs are empty or insufficient to form even one evaluation.
    """
    n = len(prices)
    if n == 0:
        raise ValueError("prices must be non-empty")
    if lookback <= 0:
        raise ValueError("lookback must be a positive int")
    if holding <= 0:
        raise ValueError("holding must be a positive int")
    # need at least lookback + holding + 1 prices for one evaluation
    if n < lookback + holding + 1:
        raise ValueError(
            f"need at least lookback+holding+1={lookback + holding + 1} prices, got {n}"
        )
    total = 0.0
    count = 0
    # t indexes the *formation* point; valid range [lookback, n-holding-1]
    last_t = n - holding - 1
    for t in range(lookback, last_t + 1):
        p_past = prices[t - lookback]
        p_now = prices[t]
        if p_past == 0:
            continue
        raw = p_now / p_past - 1.0
        sign = 1.0 if raw > 0 else (-1.0 if raw < 0 else 0.0)
        p_fwd = prices[t + holding]
        if p_now == 0:
            continue
        fwd = p_fwd / p_now - 1.0
        total += sign * fwd
        count += 1
    if count == 0:
        return 0.0
    return total / count


def cross_sectional_momentum(
    price_panel: Mapping[str, Sequence[float]],
    lookback: int,
) -> dict[str, float]:
    """Cross-sectional momentum scores (per-asset past return).

    For each asset ``i`` the score is the past-``lookback`` return:

        score_i = p^{(i)}_t / p^{(i)}_{t−L} − 1

    These scores are the cross-sectional ranking input used by long-short
    momentum strategies (Jegadeesh-Titman 1993).

    Parameters
    ----------
    price_panel : Mapping[str, Sequence[float]]
        Mapping ``asset → ordered price history`` (oldest first). Every asset
        must have at least ``lookback + 1`` prices.
    lookback : int
        Formation-window length ``L`` (must be ≥ 1).

    Returns
    -------
    dict[str, float]
        Per-asset momentum score.

    Raises
    ------
    ValueError
        If the panel is empty or any asset has insufficient history.
    """
    if not price_panel:
        raise ValueError("price_panel must be non-empty")
    if lookback <= 0:
        raise ValueError("lookback must be a positive int")
    out: dict[str, float] = {}
    for asset, prices in price_panel.items():
        out[asset] = _past_return(prices, lookback)
    return out


def _long_short_return(
    price_panel: Mapping[str, Sequence[float]],
    lookback: int,
    holding: int,
    n_long: int,
    n_short: int,
    flip: bool,
) -> "MomentumFactorResult":
    if not price_panel:
        raise ValueError("price_panel must be non-empty")
    if lookback <= 0:
        raise ValueError("lookback must be a positive int")
    if holding <= 0:
        raise ValueError("holding must be a positive int")
    n_assets = len(price_panel)
    if n_long <= 0 or n_short <= 0:
        raise ValueError("n_long and n_short must be positive ints")
    if n_long + n_short > n_assets:
        raise ValueError(
            f"need at least n_long+n_short={n_long + n_short} assets, got {n_assets}"
        )
    # per-asset past return (formation leg) — needs lookback+1 prices
    scores: list[tuple[str, float]] = []
    for asset, prices in price_panel.items():
        scores.append((asset, _past_return(prices, lookback)))
    # rank ascending by past return
    scores.sort(key=lambda kv: kv[1])
    if not flip:
        winners = [a for a, _ in scores[-n_long:]]  # past winners → long leg
        losers = [a for a, _ in scores[:n_short]]    # past losers → short leg
        long_assets = winners
        short_assets = losers
    else:
        # reversal: long past-LOSERS, short past-WINNERS (sign-flipped momentum)
        long_assets = [a for a, _ in scores[:n_long]]
        short_assets = [a for a, _ in scores[-n_short:]]

    def _leg_return(assets: list[str]) -> float:
        rets: list[float] = []
        for a in assets:
            prices = price_panel[a]
            if len(prices) < lookback + holding + 1:
                raise ValueError(
                    f"asset {a!r} has insufficient history for lookback+holding"
                )
            p_form = prices[-holding - 1]
            p_end = prices[-1]
            if p_form == 0:
                rets.append(0.0)
            else:
                rets.append(p_end / p_form - 1.0)
        if not rets:
            return 0.0
        return sum(rets) / len(rets)

    long_leg = _leg_return(long_assets)
    short_leg = _leg_return(short_assets)
    ls_return = long_leg - short_leg
    return MomentumFactorResult(
        long_leg_return=long_leg,
        short_leg_return=short_leg,
        ls_return=ls_return,
        winners=winners if not flip else short_assets,
        losers=losers if not flip else long_assets,
    )


@dataclass(frozen=True)
class MomentumFactorResult:
    """Long-short momentum / reversal factor leg report."""

    long_leg_return: float
    short_leg_return: float
    ls_return: float
    winners: list[str]
    losers: list[str]

    def to_dict(self) -> dict:
        return {
            "long_leg_return": self.long_leg_return,
            "short_leg_return": self.short_leg_return,
            "ls_return": self.ls_return,
            "winners": list(self.winners),
            "losers": list(self.losers),
        }


def momentum_factor(
    price_panel: Mapping[str, Sequence[float]],
    lookback: int = 12,
    holding: int = 1,
    n_long: int = 3,
    n_short: int = 3,
) -> MomentumFactorResult:
    """Jegadeesh-Titman (1993) cross-sectional momentum factor.

    Rank assets by past-``lookback`` return, **long the top ``n_long``** (past
    winners) and **short the bottom ``n_short``** (past losers), equal-weighted
    long-short return over the ``holding`` period:

        LS = mean(R_top) − mean(R_bottom)

    where ``R_top`` / ``R_bottom`` are the forward ``holding``-period returns of
    the winner / loser baskets.

    Parameters
    ----------
    price_panel : Mapping[str, Sequence[float]]
        ``asset → ordered price history`` (oldest first); each asset needs at
        least ``lookback + holding + 1`` prices.
    lookback : int, optional
        Formation window (default 12, classic 12-1 momentum).
    holding : int, optional
        Forward-horizon length (default 1).
    n_long : int, optional
        Number of past winners to long (default 3).
    n_short : int, optional
        Number of past losers to short (default 3).

    Returns
    -------
    MomentumFactorResult
        Long / short / long-short returns and basket memberships.

    Raises
    ------
    ValueError
        If fewer than ``n_long + n_short`` assets, or any asset has
        insufficient history.
    """
    return _long_short_return(
        price_panel, lookback, holding, n_long, n_short, flip=False
    )


def reversal_factor(
    price_panel: Mapping[str, Sequence[float]],
    lookback: int = 60,
    holding: int = 1,
    n_long: int = 3,
    n_short: int = 3,
) -> MomentumFactorResult:
    """De Bondt-Thaler (1985) long-term reversal factor.

    Sign-flipped momentum: **long past-LOSERS, short past-WINNERS**. The
    hypothesis is that over long horizons (the classic 3-year / 60-month
    formation) extreme losers tend to outperform extreme winners (mean
    reversion). Mathematically identical to :func:`momentum_factor` with the
    baskets swapped:

        LS_reversal = mean(R_bottom) − mean(R_top)

    Parameters
    ----------
    price_panel : Mapping[str, Sequence[float]]
        ``asset → ordered price history``; each asset needs at least
        ``lookback + holding + 1`` prices.
    lookback : int, optional
        Formation window (default 60, the long-horizon De Bondt-Thaler choice).
    holding : int, optional
        Forward-horizon length (default 1).
    n_long : int, optional
        Number of past losers to long (default 3).
    n_short : int, optional
        Number of past winners to short (default 3).

    Returns
    -------
    MomentumFactorResult
        Long / short / long-short reversal returns and basket memberships
        (``winners`` = the past winners that are shorted, ``losers`` = the past
        losers that are longed).

    Raises
    ------
    ValueError
        If fewer than ``n_long + n_short`` assets, or any asset has
        insufficient history.
    """
    return _long_short_return(
        price_panel, lookback, holding, n_long, n_short, flip=True
    )


def carhart_momentum(
    price_panel: Mapping[str, Sequence[float]],
    lookback: int = 12,
    holding: int = 1,
) -> MomentumFactorResult:
    """Carhart (1997) single-factor momentum leg.

    The Carhart four-factor model adds a momentum factor (``WML`` — winners
    minus losers) on top of the Fama-French three. Here it is the equal
    long/short halves alias of :func:`momentum_factor`: rank assets by past
    ``lookback`` return and split into two equal baskets, long the winners and
    short the losers.

    Parameters
    ----------
    price_panel : Mapping[str, Sequence[float]]
        ``asset → ordered price history``; each asset needs at least
        ``lookback + holding + 1`` prices.
    lookback : int, optional
        Formation window (default 12).
    holding : int, optional
        Forward-horizon length (default 1).

    Returns
    -------
    MomentumFactorResult
        Long / short / long-short momentum returns and basket memberships.

    Raises
    ------
    ValueError
        If fewer than 2 assets, or any asset has insufficient history.
    """
    n = len(price_panel)
    if n < 2:
        raise ValueError(f"need at least 2 assets, got {n}")
    half = n // 2
    # equal long/short halves: long top half, short bottom half
    n_long = half
    n_short = n - half
    if n_long == 0 or n_short == 0:
        raise ValueError("need at least 2 assets to form two non-empty halves")
    return _long_short_return(
        price_panel, lookback, holding, n_long, n_short, flip=False
    )