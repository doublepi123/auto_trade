"""Do the two live entry conditions ever hold at the same minute?

A live entry needs price at or below the band's lower edge AND the regime
gate open, in the same minute. Each condition on its own is easy to measure
and each overstates opportunity: reach rate ignores the gate, gate pass rate
ignores price. On the live deployment the two were anti-correlated — trend
days pushed price through the band while ADX shut the gate; range days opened
the gate while price sat inside the band. Seven sessions, two with any
overlap, and those two would have lost money.

This replays both conditions minute by minute over recorded shadow decisions,
applying the same band-recentering rule and opening warmup the live path
uses, and reports overlap minutes per session. It is a diagnostic for "did a
tradeable window exist", which neither existing surface answers.

Read-only. Never writes, never changes the interval, never submits an order.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import StrategyV2ShadowDecision

logger = logging.getLogger("auto_trade.entry_window_overlap")

VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_NO_OVERLAP = "NO_OVERLAP"
VERDICT_OVERLAP_PRESENT = "OVERLAP_PRESENT"

DEFAULT_MIN_SESSIONS = 10
# Mirrors IntervalRecenterService: the job runs every 30 minutes, so the
# simulated band is re-evaluated every 30 one-minute bars.
_PLACEMENT_STRIDE_BARS = 30


@dataclass(frozen=True)
class SessionOverlap:
    session_date: date
    bars: int
    below_band_minutes: int
    gate_open_minutes: int
    overlap_minutes: int
    recenters: int


@dataclass(frozen=True)
class EntryWindowOverlapReport:
    symbol: str
    lookback_days: int
    warmup_minutes: int
    half_width_pct: float
    min_drift_pct: float
    sessions_total: int
    sessions_with_overlap: int
    overlap_session_share_pct: float
    total_overlap_minutes: int
    total_below_band_minutes: int
    total_gate_open_minutes: int
    verdict: str
    sessions: tuple[SessionOverlap, ...]
    detail: str = ""


@dataclass(frozen=True)
class PoolSymbolRow:
    symbol: str
    verdict: str
    sessions_total: int
    sessions_with_overlap: int
    overlap_session_share_pct: float
    total_overlap_minutes: int


@dataclass(frozen=True)
class PoolDayRow:
    session_date: date
    symbols_with_window: int
    symbols_observed: int


@dataclass(frozen=True)
class EntryWindowPoolReport:
    lookback_days: int
    symbols_total: int
    symbols_with_any_window: int
    symbols: tuple[PoolSymbolRow, ...]
    days: tuple[PoolDayRow, ...]


class EntryWindowOverlapService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def assess_pool(
        self,
        *,
        symbols: list[str],
        lookback_days: int = 30,
        warmup_minutes: int | None = None,
        half_width_pct: float | None = None,
        min_drift_pct: float | None = None,
        now: datetime | None = None,
    ) -> EntryWindowPoolReport:
        """Rank a pool by how often each symbol had a window, and count per day.

        The single-symbol view answers "did TSLA have a window"; this answers
        "did ANYTHING have a window today", which is the question the
        single-primary architecture cannot see. Symbols are ranked by share
        of sessions with a window; per-day rows count symbols with one.
        """
        cleaned = sorted({(s or "").strip().upper() for s in symbols if (s or "").strip()})
        if not cleaned:
            raise ValueError("symbols must not be empty")
        rows: list[PoolSymbolRow] = []
        per_day_window: defaultdict[date, int] = defaultdict(int)
        per_day_seen: defaultdict[date, int] = defaultdict(int)
        for symbol in cleaned:
            report = self.assess(
                symbol=symbol,
                lookback_days=lookback_days,
                warmup_minutes=warmup_minutes,
                half_width_pct=half_width_pct,
                min_drift_pct=min_drift_pct,
                min_sessions=1,
                now=now,
            )
            rows.append(PoolSymbolRow(
                symbol=symbol,
                verdict=report.verdict,
                sessions_total=report.sessions_total,
                sessions_with_overlap=report.sessions_with_overlap,
                overlap_session_share_pct=report.overlap_session_share_pct,
                total_overlap_minutes=report.total_overlap_minutes,
            ))
            for session in report.sessions:
                per_day_seen[session.session_date] += 1
                if session.overlap_minutes > 0:
                    per_day_window[session.session_date] += 1
        rows.sort(
            key=lambda r: (-r.overlap_session_share_pct, -r.total_overlap_minutes, r.symbol)
        )
        days = tuple(
            PoolDayRow(
                session_date=d,
                symbols_with_window=per_day_window.get(d, 0),
                symbols_observed=per_day_seen[d],
            )
            for d in sorted(per_day_seen)
        )
        return EntryWindowPoolReport(
            lookback_days=lookback_days,
            symbols_total=len(rows),
            symbols_with_any_window=sum(1 for r in rows if r.sessions_with_overlap > 0),
            symbols=tuple(rows),
            days=days,
        )

    def assess(
        self,
        *,
        symbol: str,
        lookback_days: int = 30,
        warmup_minutes: int | None = None,
        half_width_pct: float | None = None,
        min_drift_pct: float | None = None,
        min_sessions: int = DEFAULT_MIN_SESSIONS,
        now: datetime | None = None,
    ) -> EntryWindowOverlapReport:
        if lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        warmup = (
            settings.trading_open_warmup_minutes
            if warmup_minutes is None
            else warmup_minutes
        )
        half = (
            settings.llm_interval_volatility_threshold_pct
            if half_width_pct is None
            else half_width_pct
        )
        drift_floor = (
            settings.interval_recenter_min_drift_pct
            if min_drift_pct is None
            else min_drift_pct
        )
        if warmup < 0 or half <= 0 or drift_floor <= 0:
            raise ValueError("warmup must be >= 0; half width and drift floor > 0")

        normalized = (symbol or "").strip().upper()
        sessions = self._load_sessions(normalized, lookback_days, now)
        rows = tuple(
            self._replay_session(day, bars, warmup, half / 100.0, drift_floor)
            for day, bars in sorted(sessions.items())
        )
        total = len(rows)
        with_overlap = sum(1 for r in rows if r.overlap_minutes > 0)
        share = (with_overlap / total * 100.0) if total else 0.0

        if total < min_sessions:
            verdict = VERDICT_INSUFFICIENT_DATA
            detail = f"{total} session(s) recorded; {min_sessions} required"
        elif with_overlap == 0:
            verdict = VERDICT_NO_OVERLAP
            detail = (
                "price reached the band and the gate opened on separate "
                "minutes only; no session had a tradeable window"
            )
        else:
            verdict = VERDICT_OVERLAP_PRESENT
            detail = f"{with_overlap} of {total} sessions had at least one window"

        return EntryWindowOverlapReport(
            symbol=normalized,
            lookback_days=lookback_days,
            warmup_minutes=warmup,
            half_width_pct=half,
            min_drift_pct=drift_floor,
            sessions_total=total,
            sessions_with_overlap=with_overlap,
            overlap_session_share_pct=round(share, 3),
            total_overlap_minutes=sum(r.overlap_minutes for r in rows),
            total_below_band_minutes=sum(r.below_band_minutes for r in rows),
            total_gate_open_minutes=sum(r.gate_open_minutes for r in rows),
            verdict=verdict,
            sessions=rows,
            detail=detail,
        )

    def _load_sessions(
        self,
        symbol: str,
        lookback_days: int,
        now: datetime | None,
    ) -> dict[date, list[tuple[float, bool]]]:
        anchor = (now or datetime.now(timezone.utc)).date()
        earliest = anchor - timedelta(days=lookback_days)
        stmt = (
            select(
                StrategyV2ShadowDecision.session_date,
                StrategyV2ShadowDecision.bar_at,
                StrategyV2ShadowDecision.close_price,
                StrategyV2ShadowDecision.gate_passed,
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
        sessions: dict[date, list[tuple[float, bool]]] = defaultdict(list)
        seen: set[tuple[date, datetime]] = set()
        for session_date, bar_at, close_price, gate_passed in self._db.execute(stmt):
            if session_date is None or close_price is None:
                continue
            # Several config versions record the same bar; one tape per bar.
            key = (session_date, bar_at)
            if key in seen:
                continue
            seen.add(key)
            try:
                price = float(close_price)
            except (TypeError, ValueError):
                continue
            if price > 0:
                sessions[session_date].append((price, bool(gate_passed)))
        return dict(sessions)

    @staticmethod
    def _replay_session(
        session_date: date,
        bars: list[tuple[float, bool]],
        warmup: int,
        half: float,
        drift_floor: float,
    ) -> SessionOverlap:
        buy_low: float | None = None
        sell_high: float | None = None
        recenters = 0
        below = 0
        gate_open = 0
        overlap = 0
        for idx, (price, gate) in enumerate(bars):
            if buy_low is None or sell_high is None or idx % _PLACEMENT_STRIDE_BARS == 0:
                drift = 0.0
                if buy_low is not None and sell_high is not None:
                    if price < buy_low:
                        drift = (buy_low - price) / buy_low * 100.0
                    elif price > sell_high:
                        drift = (price - sell_high) / sell_high * 100.0
                if buy_low is None or drift >= drift_floor:
                    buy_low = price * (1 - half)
                    sell_high = price * (1 + half)
                    recenters += 1
            is_below = price <= buy_low
            if is_below:
                below += 1
            if gate:
                gate_open += 1
            # The live path refuses entries inside the opening warmup, so a
            # coincidence there is not a window the system could have used.
            if is_below and gate and idx >= warmup:
                overlap += 1
        return SessionOverlap(
            session_date=session_date,
            bars=len(bars),
            below_band_minutes=below,
            gate_open_minutes=gate_open,
            overlap_minutes=overlap,
            recenters=recenters,
        )
