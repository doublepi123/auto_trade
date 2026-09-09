"""Is the configured interval width ever actually touched — and does it pay?

A range strategy has two ways to earn nothing, and from the outside they look
the same (zero fills, healthy everything):

* **Stranded** — the band sits where price no longer goes. The live deployment
  ran 34 days like this. ``IntervalRecenterService`` fixes that one.
* **Untouchable** — the band is centred correctly but so wide that price never
  reaches its edges. Recentering cannot fix this, and it is invisible in the
  decision funnel, which shows the same zeros for both.

The second failure is dangerous precisely because its obvious remedy —
narrow the band — increases fill count. On a signal with no edge, that does
not produce profit, it produces losses sooner. Measured on this deployment:
half-width 1.0% is touched 24% of the time, 0.5% is touched 42%, 0.3% is
touched 56%; yet a replay of the same tape returns -37.9 / -23.8 / -34.3 net
bps respectively, and pooled across symbols -10.1 bps with a 95% CI upper
bound still below zero. Reach improved, money did not.

So this service refuses to report reach on its own. Every width carries its
round-trip net return, day-clustered, with a CI bound — and a width is only
ever recommended when that lower bound clears zero. ``INSUFFICIENT_DATA`` is
kept distinct from ``NEGATIVE_EDGE``: thin evidence is not a finding about
the width.

Read-only. It replays recorded shadow closes; it never writes a row, changes
the interval, or submits an order.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StrategyV2ShadowDecision

logger = logging.getLogger("auto_trade.interval_width_fitness")

VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_UNREACHABLE = "UNREACHABLE"
VERDICT_NEGATIVE_EDGE = "NEGATIVE_EDGE"
VERDICT_POSITIVE_EDGE = "POSITIVE_EDGE"

# Half-widths to probe, in percent. 1.0 is the deployed value
# (``llm_interval_volatility_threshold_pct``); the rest bracket it so the
# report shows the whole reach/return trade-off rather than one point on it.
DEFAULT_HALF_WIDTHS_PCT: tuple[float, ...] = (0.3, 0.5, 0.75, 1.0, 1.5, 2.0)

# Round-trip cost floor in bps: 2x5bps US fee + 4bps assumed slippage. Held as
# a default rather than read from config so a config edit cannot silently make
# a losing width look profitable in this report.
DEFAULT_COST_BPS = 14.0

# Evidence floors. Below either, the verdict is INSUFFICIENT_DATA — never a
# judgement about the width.
DEFAULT_MIN_TRADES = 20
DEFAULT_MIN_DAYS = 10

# Bars between successive simulated band placements. The recentering job runs
# every 30 minutes, so sampling every 30 one-minute bars mirrors how often a
# band would actually be (re)established intraday.
_PLACEMENT_STRIDE_BARS = 30
# A placement needs some tape left for the round trip to be able to resolve.
_MIN_REMAINING_BARS = 10


@dataclass(frozen=True)
class WidthRow:
    half_width_pct: float
    placements: int
    trades: int
    distinct_days: int
    reach_rate_pct: float
    win_rate_pct: float
    net_mean_bps: float
    net_ci_lower_bps: float
    clustered_t: float


@dataclass(frozen=True)
class IntervalWidthFitnessReport:
    symbol: str
    lookback_days: int
    cost_bps: float
    distinct_days: int
    bars: int
    min_trades: int
    min_days: int
    verdict: str
    best_width: float | None
    widths: tuple[WidthRow, ...]
    detail: str = ""


class IntervalWidthFitnessService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def assess(
        self,
        *,
        symbol: str,
        lookback_days: int = 30,
        cost_bps: float = DEFAULT_COST_BPS,
        half_widths_pct: tuple[float, ...] = DEFAULT_HALF_WIDTHS_PCT,
        min_trades: int = DEFAULT_MIN_TRADES,
        min_days: int = DEFAULT_MIN_DAYS,
        now: datetime | None = None,
    ) -> IntervalWidthFitnessReport:
        if lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        if cost_bps < 0:
            raise ValueError("cost_bps must not be negative")
        if not half_widths_pct or any(w <= 0 for w in half_widths_pct):
            raise ValueError("half_widths_pct must all be positive")

        normalized = (symbol or "").strip().upper()
        sessions = self._load_sessions(normalized, lookback_days, now)
        bars = sum(len(v) for v in sessions.values())

        rows: list[WidthRow] = []
        for half_pct in sorted(half_widths_pct):
            rows.append(self._evaluate_width(sessions, half_pct, cost_bps))

        verdict, best, detail = self._verdict(
            rows,
            distinct_days=len(sessions),
            min_trades=min_trades,
            min_days=min_days,
        )
        return IntervalWidthFitnessReport(
            symbol=normalized,
            lookback_days=lookback_days,
            cost_bps=cost_bps,
            distinct_days=len(sessions),
            bars=bars,
            min_trades=min_trades,
            min_days=min_days,
            verdict=verdict,
            best_width=best,
            widths=tuple(rows),
            detail=detail,
        )

    # --- internals -------------------------------------------------------

    def _load_sessions(
        self,
        symbol: str,
        lookback_days: int,
        now: datetime | None,
    ) -> dict[date, list[float]]:
        anchor = (now or datetime.now(timezone.utc)).date()
        earliest = anchor - timedelta(days=lookback_days)
        stmt = (
            select(
                StrategyV2ShadowDecision.session_date,
                StrategyV2ShadowDecision.bar_at,
                StrategyV2ShadowDecision.close_price,
            )
            .where(
                StrategyV2ShadowDecision.symbol == symbol,
                StrategyV2ShadowDecision.session_date >= earliest,
                StrategyV2ShadowDecision.close_price.is_not(None),
            )
            .order_by(
                StrategyV2ShadowDecision.session_date,
                StrategyV2ShadowDecision.bar_at,
            )
        )
        sessions: dict[date, list[float]] = defaultdict(list)
        seen: set[tuple[date, datetime]] = set()
        for session_date, bar_at, close_price in self._db.execute(stmt):
            if session_date is None or close_price is None:
                continue
            # One tape per bar: several config versions record the same bar,
            # and counting each would inflate both reach and trade counts.
            key = (session_date, bar_at)
            if key in seen:
                continue
            seen.add(key)
            try:
                price = float(close_price)
            except (TypeError, ValueError):
                continue
            if price > 0 and math.isfinite(price):
                sessions[session_date].append(price)
        return dict(sessions)

    @staticmethod
    def _round_trip_bps(entry: float, exit_price: float, *, cost_bps: float) -> float:
        """Net bps for one long round trip, costs already deducted."""
        if entry <= 0:
            return 0.0
        return (exit_price - entry) / entry * 10_000 - cost_bps

    def _evaluate_width(
        self,
        sessions: dict[date, list[float]],
        half_pct: float,
        cost_bps: float,
    ) -> WidthRow:
        half = half_pct / 100.0
        placements = 0
        per_day: dict[date, list[float]] = defaultdict(list)

        for session_date, prices in sessions.items():
            n = len(prices)
            entered = False
            for i in range(0, max(0, n - _MIN_REMAINING_BARS), _PLACEMENT_STRIDE_BARS):
                # One entry per session, matching the live
                # live_max_entries_per_symbol_per_day=1 cap. Without it the
                # replay would report fills the live path could never take.
                if entered:
                    break
                placements += 1
                reference = prices[i]
                buy_low = reference * (1 - half)
                sell_high = reference * (1 + half)
                entry_idx = self._first_at_or_below(prices, i + 1, buy_low)
                if entry_idx is None:
                    continue
                exit_idx = self._first_at_or_above(prices, entry_idx + 1, sell_high)
                # Unresolved by the close is flattened at the last print, which
                # is what the live flatten-before-close rule does.
                exit_price = prices[exit_idx] if exit_idx is not None else prices[-1]
                per_day[session_date].append(
                    self._round_trip_bps(buy_low, exit_price, cost_bps=cost_bps)
                )
                entered = True

        nets = [x for values in per_day.values() for x in values]
        trades = len(nets)
        distinct_days = len(per_day)
        reach = (trades / placements * 100.0) if placements else 0.0
        if trades == 0:
            return WidthRow(
                half_width_pct=half_pct,
                placements=placements,
                trades=0,
                distinct_days=0,
                reach_rate_pct=round(reach, 3),
                win_rate_pct=0.0,
                net_mean_bps=0.0,
                net_ci_lower_bps=0.0,
                clustered_t=0.0,
            )

        # Cluster by day: round trips on one session share a tape, so treating
        # them as independent overstates significance.
        day_means = [statistics.mean(v) for v in per_day.values()]
        k = len(day_means)
        mean = statistics.mean(day_means)
        if k > 1:
            se = statistics.stdev(day_means) / math.sqrt(k)
        else:
            se = 0.0
        t_stat = mean / se if se > 0 else 0.0
        # Two-sided 95% t critical, approximated conservatively for small k.
        t_crit = 2.05 if k >= 25 else 2.2 if k >= 12 else 2.6
        ci_lower = mean - t_crit * se if se > 0 else mean
        wins = sum(1 for x in nets if x > 0)
        return WidthRow(
            half_width_pct=half_pct,
            placements=placements,
            trades=trades,
            distinct_days=distinct_days,
            reach_rate_pct=round(reach, 3),
            win_rate_pct=round(wins / trades * 100.0, 3),
            net_mean_bps=round(mean, 3),
            net_ci_lower_bps=round(ci_lower, 3),
            clustered_t=round(t_stat, 3),
        )

    @staticmethod
    def _first_at_or_below(prices: list[float], start: int, level: float) -> int | None:
        for idx in range(start, len(prices)):
            if prices[idx] <= level:
                return idx
        return None

    @staticmethod
    def _first_at_or_above(prices: list[float], start: int, level: float) -> int | None:
        for idx in range(start, len(prices)):
            if prices[idx] >= level:
                return idx
        return None

    @staticmethod
    def _verdict(
        rows: list[WidthRow],
        *,
        distinct_days: int,
        min_trades: int,
        min_days: int,
    ) -> tuple[str, float | None, str]:
        # Absence of tape is checked FIRST and separately. Without enough
        # sessions there is no basis to call a width unreachable: an untouched
        # band and an unobserved one produce the same zero, and conflating them
        # would report "too wide" for a symbol that simply has no history.
        if distinct_days < min_days:
            return (
                VERDICT_INSUFFICIENT_DATA,
                None,
                (
                    f"only {distinct_days} session(s) of recorded prices; "
                    f"{min_days} required before judging the width"
                ),
            )
        if all(row.trades == 0 for row in rows):
            return (
                VERDICT_UNREACHABLE,
                None,
                "no probed half-width was ever touched",
            )
        eligible = [
            row
            for row in rows
            if row.trades >= min_trades and row.distinct_days >= min_days
        ]
        if not eligible:
            return (
                VERDICT_INSUFFICIENT_DATA,
                None,
                (
                    f"no half-width cleared the evidence floor "
                    f"({min_trades} trades, {min_days} days)"
                ),
            )
        # Recommend ONLY on the net lower bound. Ranking by reach is exactly
        # the mistake this service exists to prevent.
        best = max(eligible, key=lambda row: row.net_ci_lower_bps)
        if best.net_ci_lower_bps > 0:
            return (
                VERDICT_POSITIVE_EDGE,
                best.half_width_pct,
                (
                    f"half-width {best.half_width_pct}% nets "
                    f"{best.net_mean_bps} bps with CI lower "
                    f"{best.net_ci_lower_bps} bps"
                ),
            )
        return (
            VERDICT_NEGATIVE_EDGE,
            None,
            (
                "every reachable half-width has a net CI lower bound at or "
                "below zero; narrowing the band would only trade more often, "
                "not more profitably"
            ),
        )
