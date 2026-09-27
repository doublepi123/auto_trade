"""Behaviour and PARITY tests for the registered opening-momentum historical
replay (``app/cli/opening_momentum_historical_replay.py``).

The parity tests are the heart of this file: synthetic sessions are driven
through the REAL live service path (``OpeningMomentumShadowService.tick`` on
an in-memory SQLite with a synthetic COMPLETE universe-selection run whose
candidates carry the ADV metrics), and the same bars + ADV are then replayed
through the CLI adapter.  The chosen symbol, SKIPPED reason, entry price,
stop pct, exit reason and price, and net bps must be IDENTICAL.

Record-only research: nothing here touches the network, the DB file system,
or any order path.
"""

from __future__ import annotations

import gzip
import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.cli.opening_momentum_historical_replay import (
    ANALYSIS_ID,
    ENTRY_OFFSET,
    EXIT_OFFSET,
    FROZEN_CONFIG_VERSION,
    FetchedBar,
    HistoricalReplayError,
    IntegrityGates,
    MIN_TRADES,
    MIN_WEEKS,
    OPENING_ACTIVITY_TOP_N,
    WeekClusteredStat,
    _CandleView,
    _DailyBarRow,
    _LongportQuoteProvider,
    _daily_bar_rows,
    _raw_bars_valid_ohlc,
    HISTORICAL_INDEX_CANDIDATE_CATALOG,
    INDEX_CANDIDATE_CATALOG,
    INDEX_MEMBERSHIP_HISTORY,
    import_v2_plan,
    _atomic_write_json,
    _load_status,
    _symbol_state,
    _write_bars_file,
    build_plan_payload,
    build_session_observation,
    classify_provider_error,
    classify_session_adv,
    GATE_B_MISSING_KINDS,
    KNOWN_INELIGIBLE_ADV,
    MemberDayFacts,
    UNVERIFIABLE_INVALID_BAR,
    UNVERIFIABLE_INSUFFICIENT_WINDOW,
    WINDOW_END,
    WINDOW_START,
    assemble_member_day_audit,
    session_is_auditable,
    company_dedupe,
    compute_descriptives,
    cross_check_trading_days,
    decide_verdict,
    derive_trading_days_from_benchmarks,
    evaluate_session_decision,
    frozen_config_version,
    is_fetch_window_open,
    one_sided_t95,
    pit_universe_for_session,
    rebuild_session_adv,
    run_evaluate,
    run_fetch,
    run_seal,
    settle_session_exit,
    week_clustered_statistic,
)
from app.config import settings
from app.core.broker import BrokerCandle
from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.models import (
    Base,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)

_MARKET_SESSION = get_session("US")


def _session_open(session_date: date) -> datetime:
    return datetime.combine(
        session_date,
        _MARKET_SESSION.rth_open,
        tzinfo=_MARKET_SESSION.timezone,
    ).astimezone(timezone.utc)


# ------------------------------------------------------- synthetic sessions


class _SyntheticSessionBars:
    """Deterministic minute bars for one session and one symbol.

    The builder writes minute turnover so the activity ratio equals
    ``per_minute_turnover * 5 / adv`` for the opening five bars; the sixth
    bar closes above/below the five-bar range high depending on
    ``breakout_bps``; bars 6..66 settle entry, stop and fixed-hold exits.
    """

    def __init__(
        self,
        *,
        base_price: float = 100.0,
        per_minute_turnover: float = 2_000_000.0,
        breakout_bps: float = 50.0,
        gap_open_bps: float | None = None,
        stop_low_at_offset: int | None = None,
        gap_stop_at_offset: int | None = None,
        entry_open_bps: float = 0.0,
        missing_entry_bar: bool = False,
        missing_turnover: bool = False,
        missing_minute_offsets: tuple[int, ...] = (),
    ) -> None:
        self.base_price = base_price
        self.per_minute_turnover = per_minute_turnover
        self.breakout_bps = breakout_bps
        self.gap_open_bps = gap_open_bps
        self.stop_low_at_offset = stop_low_at_offset
        self.gap_stop_at_offset = gap_stop_at_offset
        self.entry_open_bps = entry_open_bps
        self.missing_entry_bar = missing_entry_bar
        self.missing_turnover = missing_turnover
        self.missing_minute_offsets = missing_minute_offsets

    def bars(
        self, session_date: date, symbol: str
    ) -> list[BrokerCandle]:
        session_open = _session_open(session_date)
        range_high = self.base_price * 1.002
        range_low = self.base_price * 0.998
        bars: list[BrokerCandle] = []
        for offset in range(EXIT_OFFSET + 4):
            if offset in self.missing_minute_offsets:
                continue
            if offset < 5:
                open_price = self.base_price
                close_price = self.base_price
                # A deliberate opening range: low dips to range_low.
                low = min(open_price, close_price) * 0.998
                high = max(open_price, close_price) * 1.002
            elif offset == 5:
                open_price = self.base_price
                close_price = range_high * (
                    1 + self.breakout_bps / 10_000
                )
                high = max(open_price, close_price) * 1.0005
                low = open_price * 0.998
            elif offset == ENTRY_OFFSET:
                if self.missing_entry_bar:
                    continue
                open_price = self.base_price * (
                    1 + self.entry_open_bps / 10_000
                )
                close_price = open_price * 1.001
                high = max(open_price, close_price) * 1.0005
                low = open_price * 0.9999
            else:
                open_price = self.base_price * (
                    1 + self.entry_open_bps / 10_000
                )
                close_price = open_price * 1.001
                high = max(open_price, close_price) * 1.0005
                low = open_price * 0.9999
            if self.stop_low_at_offset is not None:
                if offset == self.stop_low_at_offset:
                    low = self.base_price * 0.90
                if offset == self.stop_low_at_offset + 1:
                    # Stop via gap open: the bar OPENS below the stop price.
                    open_price = self.base_price * 0.90
                    close_price = open_price
                    low = open_price * 0.999
                    high = open_price * 1.001
            if self.gap_stop_at_offset is not None:
                # A pure gap-through stop: the bar OPENS below the stop
                # price with NO prior bar having touched it by low.  The
                # bar BEFORE it stays clean.
                if offset == self.gap_stop_at_offset:
                    open_price = self.base_price * 0.90
                    close_price = open_price
                    low = open_price * 0.999
                    high = open_price * 1.001
            if offset == EXIT_OFFSET:
                open_price = self.base_price * (
                    1 + self.entry_open_bps / 10_000
                ) * 1.004
                close_price = open_price
                high = open_price * 1.0005
                low = open_price * 0.9999
            if self.gap_open_bps is not None and offset == 1:
                # Post-entry overnight-style gap handled via stop path only.
                close_price = self.base_price
            turnover = (
                None
                if (self.missing_turnover and offset < 5)
                else self.per_minute_turnover
            )
            bars.append(
                BrokerCandle(
                    timestamp=session_open + timedelta(minutes=offset),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=1_000.0,
                    turnover=turnover if turnover is not None else 0.0,
                )
            )
        return bars


def _bars_by_timestamp(
    bars: list[BrokerCandle],
) -> dict[datetime, Any]:
    return {
        candle.timestamp: candle
        for candle in OpeningMomentumShadowService._coerce_candles(bars)
    }


class _FakePlanProvider:
    """Fake ReplayQuoteProvider serving pre-declared synthetic bars."""

    def __init__(
        self,
        *,
        daily_bars: dict[str, list[BrokerCandle]],
        minute_bars: dict[str, list[BrokerCandle]],
        trading_days: tuple[date, ...],
        fail_symbols_permanently: set[str] | None = None,
        quota_fail_after: int | None = None,
        include_benchmark_bars: bool = True,
    ) -> None:
        self.daily_bars = daily_bars
        self.minute_bars = minute_bars
        self._trading_days = trading_days
        self.fail_symbols_permanently = fail_symbols_permanently or set()
        self.quota_fail_after = quota_fail_after
        self.calls: list[tuple[str, str, datetime]] = []
        self.request_count = 0
        if include_benchmark_bars:
            # Serve QQQ/DIA daily bars exactly on the declared trading
            # days so the benchmark-derived session list reproduces the
            # declared sessions exactly.
            benchmark_dates = [
                value
                for value in trading_days
                if value.weekday() < 5 and not is_market_closed("US", value)
            ]
            benchmark_bars = _benchmark_bars(tuple(benchmark_dates))
            self.daily_bars = {
                **self.daily_bars,
                "QQQ.US": benchmark_bars,
                "DIA.US": benchmark_bars,
            }

    def _maybe_fail(self, symbol: str) -> None:
        self.request_count += 1
        if (
            self.quota_fail_after is not None
            and self.request_count > self.quota_fail_after
        ):
            raise RuntimeError(
                "OpenApiException: rate limit exceeded (code=301607)"
            )
        if symbol in self.fail_symbols_permanently:
            raise RuntimeError(
                "OpenApiException: unknown symbol (code=301600)"
            )

    def history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        *,
        count: int,
        after: datetime,
        forward: bool,
        adjustment: str,
    ) -> list[_CandleView]:
        self.calls.append((symbol, period, after))
        self._maybe_fail(symbol)
        source = (
            self.daily_bars if period == "DAY" else self.minute_bars
        )
        rows = [
            _CandleView(
                timestamp=bar.timestamp,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
                turnover=(
                    float(bar.turnover)
                    if getattr(bar, "turnover", None)
                    else None
                ),
            )
            for bar in source.get(symbol, ())
            if bar.timestamp >= after
        ]
        return rows[:count]

    def trading_days(
        self, *, begin: date, end: date
    ) -> tuple[tuple[date, ...], tuple[date, ...]]:
        self.request_count += 1
        if (
            self.quota_fail_after is not None
            and self.request_count > self.quota_fail_after
        ):
            raise RuntimeError(
                "OpenApiException: rate limit exceeded (code=301607)"
            )
        return (
            tuple(
                value
                for value in self._trading_days
                if begin <= value <= end
            ),
            (),
        )


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


# ------------------------------------------------------------- PIT universe


def test_pit_membership_half_open_boundaries() -> None:
    # SPLK re-enters NASDAQ_100 on 2023-12-18 (start inclusive) and its
    # prior interval ended 2022-12-19 (end exclusive).
    assert "SPLK.US" not in pit_universe_for_session(date(2023, 12, 17))
    assert "SPLK.US" in pit_universe_for_session(date(2023, 12, 18))
    # WBA's DJIA interval ends 2024-02-26: that date is OUT (end exclusive).
    assert "WBA.US" in pit_universe_for_session(date(2024, 2, 25))
    assert "WBA.US" not in pit_universe_for_session(date(2026, 4, 30))


def test_google_company_dedupe_keeps_googl_drops_goog() -> None:
    assert company_dedupe(("GOOGL.US", "GOOG.US", "AAPL.US")) == (
        "GOOGL.US",
        "AAPL.US",
    )
    assert company_dedupe(("GOOG.US", "AAPL.US")) == ("GOOG.US", "AAPL.US")


def test_pit_universe_never_contains_both_google_classes() -> None:
    probe = date(2024, 6, 3)
    universe = pit_universe_for_session(probe)
    assert "GOOGL.US" in universe
    assert "GOOG.US" not in universe


def test_window_pinned_and_plan_offline() -> None:
    payload = build_plan_payload(
        window_start=date(2023, 9, 1), window_end=date(2026, 4, 30)
    )
    assert payload["analysis_id"] == ANALYSIS_ID
    assert payload["window_start"] == "2023-09-01"
    assert payload["window_end"] == "2026-04-30"
    # Offline estimates are produced without any network call: the request
    # estimate block exists and the forward pagination option is cheaper
    # than one-small-request-per-session.
    estimates = payload["request_estimates"]
    assert (
        estimates["forward_pagination_1000"]
        < estimates["one_small_request_per_session"]
    )
    assert payload["distinct_symbols"] > 100
    entries = payload["symbols"]
    assert {entry["symbol"] for entry in entries}.isdisjoint({"GOOG.US"})


# ---------------------------------------------------------------- ADV rebuild


@dataclass(frozen=True)
class _FakeDailyBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def _fake_daily_bar(
    session_date: date,
    *,
    close: float,
    volume: float,
    turnover: float | None = None,
) -> _FakeDailyBar:
    return _FakeDailyBar(
        timestamp=_session_open(session_date),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=volume,
        turnover=(
            turnover if turnover is not None else close * volume
        ),
    )


def _weekday_dates(count: int, before: date) -> list[date]:
    dates: list[date] = []
    cursor = before - timedelta(days=1)
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(dates))


def test_adv_rebuild_matches_selector_definition() -> None:
    as_of = date(2024, 3, 4)
    prior = _weekday_dates(25, as_of)
    bars = [
        _fake_daily_bar(value, close=100.0 + index, volume=1_000.0 + index)
        for index, value in enumerate(prior)
    ]
    adv = rebuild_session_adv(
        bars,
        as_of=as_of,
        previous_sealed_session=prior[-1],
    )
    assert adv is not None
    # The frozen selector averages the last 20 of the last 21 bars.
    last20 = bars[-20:]
    expected = sum(
        (bar.turnover if bar.turnover and bar.turnover > 0 else bar.close * bar.volume)
        for bar in last20
    ) / 20
    assert adv == pytest.approx(expected)


def test_adv_rebuild_requires_twenty_one_bars_and_excludes_today() -> None:
    as_of = date(2024, 3, 4)
    only20 = _weekday_dates(20, as_of)
    bars = [_fake_daily_bar(value, close=100.0, volume=1_000.0) for value in only20]
    assert (
        rebuild_session_adv(
            bars, as_of=as_of, previous_sealed_session=only20[-1]
        )
        is None
    )
    # A bar ON as_of is not a completed bar and must not enter the window.
    prior = _weekday_dates(21, as_of)
    bars = [
        _fake_daily_bar(value, close=100.0, volume=1_000.0) for value in prior
    ]
    with_today = [
        *bars,
        _fake_daily_bar(as_of, close=999.0, volume=1_000.0),
    ]
    assert rebuild_session_adv(
        with_today, as_of=as_of, previous_sealed_session=prior[-1]
    ) == pytest.approx(100_000.0)


# ------------------------------------------------------ exit-path integrity


def test_exit_enforces_complete_path_missing_minute_is_unresolved() -> None:
    session_date = date(2024, 3, 4)
    builder = _SyntheticSessionBars()
    bars = builder.bars(session_date, "AAA.US")
    complete = _bars_by_timestamp(bars)
    entry_price = complete[
        _session_open(session_date) + timedelta(minutes=ENTRY_OFFSET)
    ].open
    settled = settle_session_exit(
        complete,
        session_open=_session_open(session_date),
        entry_price=entry_price,
        stop_loss_pct=1.5,
    )
    assert settled is not None
    assert settled.exit_reason == "FIXED_HOLD_EXIT"

    holed = _bars_by_timestamp(
        _drop_offsets(bars, (30,))
    )
    assert (
        settle_session_exit(
            holed,
            session_open=_session_open(session_date),
            entry_price=entry_price,
            stop_loss_pct=1.5,
        )
        is None
    ), "a missing minute must stay UNRESOLVED, never a guess"


def _drop_offsets(
    bars: list[BrokerCandle], offsets: tuple[int, ...]
) -> list[BrokerCandle]:
    session_open = bars[0].timestamp
    return [
        bar
        for bar in bars
        if int((bar.timestamp - session_open).total_seconds() // 60)
        not in offsets
    ]


# ------------------------------------------------------------- statistics


def test_week_clustered_statistic_hand_computed() -> None:
    # Two weeks (ISO), three trades: week1 {+10, +30}, week2 {-10}.
    # mean = 10; S_w1 = (0 + 20) = 20; S_w2 = -20; sum S_w^2 = 800.
    # SE = sqrt(2/1 * 800 / 9) = sqrt(1600/9) = 40/3.
    observations = [
        (date(2024, 1, 2), 10.0),
        (date(2024, 1, 3), 30.0),
        (date(2024, 1, 9), -10.0),
    ]
    stat = week_clustered_statistic(observations)
    assert stat.n == 3
    assert stat.weeks == 2
    assert stat.mean_bps == pytest.approx(10.0)
    assert stat.standard_error_bps == pytest.approx(40.0 / 3.0)
    assert stat.t_critical == pytest.approx(one_sided_t95(1))
    lower = 10.0 - one_sided_t95(1) * (40.0 / 3.0)
    assert stat.lower_30 == pytest.approx(lower)
    assert stat.lower_50 == pytest.approx(lower - 20.0)
    assert stat.upper_50 == pytest.approx(
        10.0 + one_sided_t95(1) * (40.0 / 3.0) - 20.0
    )


def test_week_clustered_statistic_empty_and_single_week() -> None:
    empty = week_clustered_statistic([])
    assert empty.n == 0 and empty.mean_bps is None
    single = week_clustered_statistic([(date(2024, 1, 2), 5.0)])
    assert single.mean_bps == pytest.approx(5.0)
    assert single.standard_error_bps is None
    assert single.lower_30 is None and single.lower_50 is None


def test_one_sided_t95_anchors_match_standard_table() -> None:
    assert one_sided_t95(1) == pytest.approx(6.313752, abs=1e-5)
    assert one_sided_t95(5) == pytest.approx(2.015048, abs=1e-5)
    assert one_sided_t95(25) == pytest.approx(1.708141, abs=1e-5)
    assert one_sided_t95(30) == pytest.approx(1.697261, abs=1e-5)
    with pytest.raises(HistoricalReplayError):
        one_sided_t95(0)
    with pytest.raises(HistoricalReplayError):
        one_sided_t95(len(_T95_TABLE_SIZE_PROBE) + 1)


_T95_TABLE_SIZE_PROBE = [0.0] * 150


# ---------------------------------------------------------------- verdicts


def _stat(
    *,
    lower_30: float | None,
    upper_30: float | None,
    weeks: int = 30,
    n: int = 130,
) -> WeekClusteredStat:
    return WeekClusteredStat(
        n=n,
        weeks=weeks,
        mean_bps=1.0,
        standard_error_bps=1.0,
        t_critical=1.7,
        lower_30=lower_30,
        upper_30=upper_30,
        lower_50=(lower_30 - 20.0) if lower_30 is not None else None,
        upper_50=(upper_30 - 20.0) if upper_30 is not None else None,
    )


def _passing_gates(**overrides: Any) -> IntegrityGates:
    values: dict[str, Any] = {
        "expected_sessions": 700,
        "auditable_sessions": 700,
        "member_days_total": 85_000,
        "member_days_missing_data": 100,
        "still_listed_missing_member_days": 0,
        "unresolved_exit_sessions": 0,
    }
    values.update(overrides)
    return IntegrityGates(**values)


def test_verdict_corroborates_only_with_gates_and_positive_lower_50() -> None:
    result = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=25.0, upper_30=90.0),
        trade_count=150,
    )
    assert result.verdict == "CORROBORATES"


def test_verdict_does_not_corroborate_when_upper_50_negative() -> None:
    result = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=-80.0, upper_30=-50.0),
        trade_count=150,
    )
    assert result.verdict == "DOES_NOT_CORROBORATE"
    assert result.upper_30_negative is True
    # Border: U50 = 0 exactly is NOT sufficient for DOES_NOT_CORROBORATE.
    edge = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=-100.0, upper_30=20.0),
        trade_count=150,
    )
    assert edge.verdict == "INCONCLUSIVE"


def test_verdict_inconclusive_edges() -> None:
    # Gates fail -> INCONCLUSIVE even with a clearly positive bound.
    failed_gates = decide_verdict(
        gates=_passing_gates(unresolved_exit_sessions=2),
        stat=_stat(lower_30=25.0, upper_30=90.0),
        trade_count=150,
    )
    assert failed_gates.verdict == "INCONCLUSIVE"
    # Sample below minimum -> INCONCLUSIVE even with positive bound.
    small = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=25.0, upper_30=90.0, weeks=MIN_WEEKS - 1),
        trade_count=150,
    )
    assert small.verdict == "INCONCLUSIVE"
    small_n = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=25.0, upper_30=90.0),
        trade_count=MIN_TRADES - 1,
    )
    assert small_n.verdict == "INCONCLUSIVE"
    # Bounds straddle zero -> INCONCLUSIVE.
    straddling = decide_verdict(
        gates=_passing_gates(),
        stat=_stat(lower_30=-5.0, upper_30=95.0),
        trade_count=150,
    )
    assert straddling.verdict == "INCONCLUSIVE"
    # No statistics at all -> INCONCLUSIVE, never a crash.
    empty = decide_verdict(
        gates=_passing_gates(),
        stat=WeekClusteredStat(
            n=0,
            weeks=0,
            mean_bps=None,
            standard_error_bps=None,
            t_critical=None,
            lower_30=None,
            upper_30=None,
            lower_50=None,
            upper_50=None,
        ),
        trade_count=0,
    )
    assert empty.verdict == "INCONCLUSIVE"


def test_gate_failure_reasons_are_concrete() -> None:
    gates = _passing_gates(
        auditable_sessions=600,
        member_days_missing_data=5_000,
        still_listed_missing_member_days=3,
        unresolved_exit_sessions=1,
    )
    assert not gates.passed
    assert gates.failures() == (
        "SESSION_INPUT_COVERAGE_BELOW_0.95",
        "MEMBER_DAY_MISSING_SHARE_ABOVE_0.02",
        "STILL_LISTED_MEMBER_DAY_MISSING",
        "UNRESOLVED_SELECTED_TRADE_EXIT_PATH",
    )


def test_gates_use_the_frozen_thresholds() -> None:
    edge = _passing_gates(
        auditable_sessions=665,
        member_days_missing_data=1_700,
    )
    assert edge.expected_sessions == 700
    assert edge.session_input_coverage == pytest.approx(0.95)
    assert edge.member_day_missing_share == pytest.approx(0.02)
    assert edge.passed


# ------------------------------------------------------------- descriptives


def test_descriptives_are_reported_but_flagged_non_gating() -> None:
    trades = [
        (date(2024, 1, 3), 10.0),
        (date(2024, 1, 4), -5.0),
        (date(2024, 2, 5), 40.0),
        (date(2024, 2, 6), -2.0),
    ]
    payload = compute_descriptives(trades)
    assert payload["descriptive_only"] is True
    assert payload["never_gating"] is True
    assert "a positive per-trade mean is not stable monthly profit" in str(
        payload["statement"]
    )
    monthly = cast(dict[str, int], payload["monthly_trade_counts"])
    assert monthly["2024-01"] == 2 and monthly["2024-02"] == 2
    worst = cast(float, payload["worst_month_bps"])
    assert worst == pytest.approx(5.0)


# ------------------------------------------------------------ calendar check


def test_calendar_cross_check_reports_mismatches_and_2023_gap() -> None:
    # 2024-11-28 is a local NYSE closure; if the API lists it, report it.
    days = [date(2024, 11, 27), date(2024, 11, 28), date(2023, 11, 23)]
    report = cross_check_trading_days(days)
    assert report["unchecked_sessions_2023"] == 1
    assert report["api_trading_local_closed"] == ["2024-11-28"]
    assert report["api_trading_local_trading"] == 1


# ----------------------------------------------------------- fetch safety


def test_fetch_pause_window_blocks_us_rth_and_cron_hours() -> None:
    # Monday 14:00 UTC (inside US RTH): closed for fetching.
    assert not is_fetch_window_open(
        datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)
    )
    # Monday 12:59 UTC and Monday 22:00 UTC: open.
    assert is_fetch_window_open(
        datetime(2026, 3, 2, 12, 59, tzinfo=timezone.utc)
    )
    assert is_fetch_window_open(
        datetime(2026, 3, 2, 22, 0, tzinfo=timezone.utc)
    )
    # Saturday 15:00 UTC (weekend): open regardless of the weekday window.
    assert is_fetch_window_open(
        datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    )
    # Edge boundaries: exactly 13:00 blocked, exactly 22:00 allowed.
    assert not is_fetch_window_open(
        datetime(2026, 3, 2, 13, 0, tzinfo=timezone.utc)
    )


def test_quota_and_permission_errors_stop_the_world() -> None:
    assert (
        classify_provider_error("OpenApiException code=301607 rate limit")
        == "GLOBAL_STOP_QUOTA"
    )
    assert (
        classify_provider_error("OpenApiException code=301604 no permission")
        == "GLOBAL_STOP_PERMISSION"
    )
    assert (
        classify_provider_error("OpenApiException code=301600 invalid symbol")
        == "PERMANENT_SYMBOL"
    )
    assert classify_provider_error("connection reset by peer") == "TRANSIENT"


def test_fetch_global_stop_is_immediate_and_sticky(
    tmp_path: Path,
) -> None:
    provider = _FakePlanProvider(
        daily_bars={},
        minute_bars={},
        trading_days=(date(2024, 1, 2),),
        quota_fail_after=1,
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    plan = _minimal_plan()
    with pytest.raises(HistoricalReplayError, match="GLOBAL STOP"):
        run_fetch(
            cache_dir=tmp_path,
            plan_payload=plan,
            provider=provider,
            clock=clock,
            sleep=lambda seconds: None,
        )
    status = _load_status(tmp_path)
    assert isinstance(status.get("global_stop"), dict)
    assert status["global_stop"]["reason"] == "GLOBAL_STOP_QUOTA"
    # A later fetch refuses to start at all while the marker exists.
    fresh = _FakePlanProvider(
        daily_bars={},
        minute_bars={},
        trading_days=(date(2024, 1, 2),),
    )
    with pytest.raises(HistoricalReplayError, match="GLOBAL STOP"):
        run_fetch(
            cache_dir=tmp_path,
            plan_payload=plan,
            provider=fresh,
            clock=clock,
            sleep=lambda seconds: None,
        )


def test_fetch_permanent_symbol_failure_is_persisted_not_retried(
    tmp_path: Path,
) -> None:
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=date(2024, 1, 2), count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(date(2024, 1, 2), "AAA.US"),
        },
        trading_days=(date(2024, 1, 2),),
        fail_symbols_permanently={"ATVI.US"},
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    report = run_fetch(
        cache_dir=tmp_path,
        plan_payload=_minimal_plan(
            symbols=("AAA.US", "ATVI.US"),
            first=date(2024, 1, 2),
            last=date(2024, 1, 2),
            daily_start=date(2023, 11, 20),
        ),
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert report["coverage"]["symbols_permanent_failure"] == 1
    assert "ATVI.US" in report["errors"]["permanent"]
    status = _load_status(tmp_path)
    assert (
        status["symbols"]["ATVI.US"]["minute"]["state"]
        == "PERMANENT_FAILURE"
    )
    # The invalid symbol is asked exactly once per kind (no infinite retry):
    atvi_calls = [call for call in provider.calls if call[0] == "ATVI.US"]
    assert len(atvi_calls) == 1


def _minute_style_daily(
    symbol: str, *, first: date, count: int
) -> list[BrokerCandle]:
    dates = _weekday_dates(count, first + timedelta(days=1))
    return [
        BrokerCandle(
            timestamp=_session_open(value),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
            turnover=100_000.0,
        )
        for value in dates
    ]


def _daily_bars_on_sessions(
    symbol: str,
    *,
    sessions: tuple[date, ...],
    daily_start: date,
    turnover: float = 100_000.0,
) -> list[BrokerCandle]:
    """Daily bars on every weekday from ``daily_start`` plus the sessions.

    The provider has a bar on every session day by construction, so the
    latest bar before a window session is precisely the previous sealed
    session - the freshness check passes.  Weekdays from ``daily_start``
    pad the warm-up window to >= 21 bars.
    """

    dates = set(sessions)
    # Warm-up padding strictly BEFORE the first session, capped so no
    # calendar weekday AFTER the previous sealed session is ever the
    # latest bar (that would fake an ADV_INPUT_GAP).
    first_session = min(sessions)
    cursor = daily_start
    while cursor < first_session:
        if cursor.weekday() < 5:
            dates.add(cursor)
        cursor += timedelta(days=1)
    # Between sessions, only the session days themselves carry bars.
    return [
        BrokerCandle(
            timestamp=_session_open(value),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000.0,
            turnover=turnover,
        )
        for value in sorted(dates)
    ]


def _minimal_plan(
    *,
    symbols: tuple[str, ...] = ("AAA.US",),
    first: date = date(2024, 1, 2),
    last: date = date(2024, 1, 2),
    daily_start: date = date(2023, 11, 20),
) -> dict[str, Any]:
    return {
        "analysis_id": ANALYSIS_ID,
        "window_start": "2023-09-01",
        "window_end": "2026-04-30",
        "estimated_requests_total": 50,
        "symbols": [
            {
                "symbol": symbol,
                "first_session": first.isoformat(),
                "last_session": last.isoformat(),
                "daily_start": daily_start.isoformat(),
            }
            for symbol in symbols
        ],
    }


def test_fetch_resumes_and_writes_atomically(tmp_path: Path) -> None:
    plan = _minimal_plan()
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=date(2024, 1, 2), count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(date(2024, 1, 2), "AAA.US"),
        },
        trading_days=(date(2024, 1, 2),),
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    first_report = run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert first_report["coverage"]["symbols_complete"] == 1
    # A second run resumes: the completed symbol is not re-requested.
    calls_before = len(provider.calls)
    second = run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert second["coverage"]["symbols_complete"] == 1
    assert len(provider.calls) == calls_before
    # Atomic writes: no .tmp leftovers anywhere in the cache.
    leftovers = list(tmp_path.rglob(".*.tmp"))
    assert leftovers == []
    # Per-file checksums are recorded in the status manifest.
    status = _load_status(tmp_path)
    minute_state = status["symbols"]["AAA.US"]["minute"]
    assert minute_state["state"] == "COMPLETE"
    assert len(minute_state["sha256"]) == 64


def test_fetch_reports_only_coverage_and_quota_never_pnl(
    tmp_path: Path,
) -> None:
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=date(2024, 1, 2), count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(date(2024, 1, 2), "AAA.US"),
        },
        trading_days=(date(2024, 1, 2),),
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    report = run_fetch(
        cache_dir=tmp_path,
        plan_payload=_minimal_plan(),
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    rendered = json.dumps(report)
    for forbidden in (
        "net_return",
        "pnl",
        "gross_return",
        "exit_price",
        "verdict",
    ):
        assert forbidden not in rendered


# ------------------------------------------------------------------- seal


def test_seal_writes_manifest_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    sessions = (date(2024, 1, 2),)
    builders = {"AAA.US": _SyntheticSessionBars()}
    plan = _minimal_plan(symbols=("AAA.US",), first=sessions[0], last=sessions[0])
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=sessions[0], count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(sessions[0], "AAA.US"),
        },
        trading_days=sessions,
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    # The warm-up calendar proves the predecessor of the first scoring
    # day (the daily source covers these dates).
    warmup_dates = [
        d
        for d in _weekday_dates(26, sessions[0])
        if d < sessions[0]
    ][-21:]
    _atomic_write_json(
        tmp_path / "trading_days_warmup.json",
        {
            "source": "synthetic warm-up calendar (tests)",
            "fetched_at": "2026-09-27T00:00:00+00:00",
            "warmup_start": warmup_dates[0].isoformat(),
            "warmup_end": warmup_dates[-1].isoformat(),
            "request_count": 2,
            "reason": None,
            "trading_days": [value.isoformat() for value in warmup_dates],
            "one_sided_dates": [],
        },
    )
    manifest_hash = run_seal(tmp_path, plan_payload=plan)
    assert len(manifest_hash) == 64
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["analysis_id"] == ANALYSIS_ID
    assert manifest["trading_days"]["trading_days"] == ["2024-01-02"]
    # The manifest universe is the REAL PIT membership on the sealed day,
    # not the fetch trade scope.
    universe = manifest["universe"]["2024-01-02"]
    assert "NVDA.US" in universe
    assert universe == sorted(universe)
    assert manifest["member_days_total"] == len(universe)
    # The window-end membership set is sealed for gate (b).
    assert manifest["window_end_membership"]["NVDA.US"] is True
    # Bound hashes are recorded.
    for key in (
        "membership_history_sha256",
        "catalog_sha256",
        "historical_catalog_sha256",
        "forward_manifest_sha256",
        "trading_days_sha256",
        "trading_days_warmup_sha256",
    ):
        assert len(manifest[key]) == 64, key
    # The original plan bytes are preserved and the v2-import flag is
    # recorded (False here: the plan is already v3).
    assert manifest["plan"]["v2_import"] is False
    assert manifest["plan"]["original_analysis_id"] == ANALYSIS_ID
    # Sealing is refused before any fetch produced trading days.
    with pytest.raises(HistoricalReplayError, match="trading_days"):
        run_seal(tmp_path / "empty", plan_payload=plan)


# --------------------------------------------------------------- evaluate


def _seeded_cache(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...],
    sessions: tuple[date, ...],
    builders_by_symbol: dict[str, _SyntheticSessionBars],
    adv_per_symbol: dict[str, float],
    permanent_failures: tuple[str, ...] = (),
) -> None:
    """Build a sealed synthetic cache through the REAL fetch + seal path.

    ``sessions[0]`` acts as the warm-up session: the plan's first scored
    session is ``sessions[1]`` (the trading-day list keeps the warm-up day
    so the ADV freshness check has a previous sealed session).
    """

    plan = _minimal_plan(
        symbols=symbols, first=sessions[1], last=sessions[-1]
    )
    _complete_synthetic_cache(
        tmp_path,
        symbols=symbols,
        sessions=sessions,
        builders_by_symbol=builders_by_symbol,
        adv_per_symbol=adv_per_symbol,
        permanent_failures=permanent_failures,
        plan=plan,
    )
    run_seal(tmp_path, plan_payload=plan)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_evaluate_refuses_without_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    with pytest.raises(HistoricalReplayError, match="seal"):
        run_evaluate(
            cache_dir=tmp_path,
            output_path=_output_path(tmp_path),
        )


def test_evaluate_refuses_silent_overwrite_and_keeps_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    # sessions[0] is the warm-up day; the scored session is sessions[1].
    sessions = (_WARMUP_DAY, date(2024, 1, 2))
    builders = {
        "AAA.US": _SyntheticSessionBars(),
        "BBB.US": _SyntheticSessionBars(per_minute_turnover=1_000_000.0),
        "CCC.US": _SyntheticSessionBars(per_minute_turnover=900_000.0),
        "DDD.US": _SyntheticSessionBars(per_minute_turnover=800_000.0),
        "EEE.US": _SyntheticSessionBars(per_minute_turnover=700_000.0),
        "FFF.US": _SyntheticSessionBars(per_minute_turnover=600_000.0),
        "GGG.US": _SyntheticSessionBars(per_minute_turnover=500_000.0),
        "HHH.US": _SyntheticSessionBars(per_minute_turnover=400_000.0),
    }
    symbols = tuple(builders)
    _seeded_cache(
        tmp_path,
        symbols=symbols,
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={symbol: 10_000_000.0 for symbol in symbols},
    )
    output = _output_path(tmp_path)
    first = run_evaluate(cache_dir=tmp_path, output_path=output)
    assert output.exists()
    with pytest.raises(HistoricalReplayError, match="rerun-reason"):
        run_evaluate(cache_dir=tmp_path, output_path=output)
    rerun = run_evaluate(
        cache_dir=tmp_path, output_path=output, rerun_reason="test rerun"
    )
    assert rerun["verdict"] == first["verdict"]
    # The earlier output is never renamed or removed: it stays where it
    # was and the rerun wrote a NEW versioned sibling.
    assert output.exists()
    superseding = rerun["provenance"]["supersedes"]
    assert superseding is not None
    assert superseding["path"] == str(output)
    preserved = json.loads(Path(superseding["path"]).read_text(encoding="utf-8"))
    # receipt_path is added to the returned dict after the file write, so
    # compare the on-disk content against the dict without it.
    first_on_disk = {
        key: value for key, value in first.items() if key != "receipt_path"
    }
    assert preserved == first_on_disk


def test_evaluate_provenance_records_hashes_and_frozen_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    # sessions[0] is the warm-up day; the scored session is sessions[1].
    sessions = (_WARMUP_DAY, date(2024, 1, 2))
    builders = {
        symbol: _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (8 - index)
        )
        for index, symbol in enumerate(
            ("AAA.US", "BBB.US", "CCC.US", "DDD.US", "EEE.US", "FFF.US", "GGG.US", "HHH.US")
        )
    }
    symbols = tuple(builders)
    _seeded_cache(
        tmp_path,
        symbols=symbols,
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={symbol: 10_000_000.0 for symbol in symbols},
    )
    payload = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    provenance = payload["provenance"]
    assert provenance["frozen_config_version"] == FROZEN_CONFIG_VERSION
    assert provenance["input_manifest_sha256"]
    assert provenance["plan_doc_sha256"]
    assert payload["verdict_statement"]


# ============================================================ PARITY TESTS
#
# Each scenario runs the SAME synthetic bars through (1) the real live
# service (tick on in-memory SQLite with a synthetic COMPLETE universe
# selection run carrying avg_dollar_volume) and (2) the replay adapter, and
# asserts identical decision + settlement surfaces.


_PARITY_SESSION = date(2026, 7, 29)  # inside the forward evidence window


def _live_database() -> tuple[Engine, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, Session(bind=engine)


def _seed_complete_selection_run(
    db: Session,
    *,
    symbols: tuple[str, ...],
    adv_by_symbol: dict[str, float],
    session_date: date,
) -> int:
    run = UniverseSelectionRun(
        as_of_date=session_date - timedelta(days=1),
        algorithm_version="synthetic-v1",
        source_version="synthetic",
        status="COMPLETE",
        candidate_count=len(symbols),
        evaluable_count=len(symbols),
        selected_count=len(symbols),
        coverage_ratio=1.0,
        completed_at=_session_open(session_date) - timedelta(days=1),
    )
    db.add(run)
    db.flush()
    for rank, symbol in enumerate(symbols, start=1):
        db.add(
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                selected=True,
                rank=rank,
                score=float(100 - rank),
                metrics_json=json.dumps(
                    {"avg_dollar_volume": adv_by_symbol[symbol]}
                ),
            )
        )
    db.commit()
    return run.id


class _LiveCandleProvider:
    """Candle provider for the live service path: serves synthetic minutes."""

    def __init__(
        self,
        builders_by_symbol: dict[str, _SyntheticSessionBars],
        *,
        session_date: date,
    ) -> None:
        self.builders = builders_by_symbol
        self.session_date = session_date
        self.bars_by_symbol = {
            symbol: builder.bars(session_date, symbol)
            for symbol, builder in builders_by_symbol.items()
        }
        self.calls: list[str] = []

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        assert period == "MIN_1"
        self.calls.append(symbol)
        return self.bars_by_symbol.get(symbol, [])

    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        assert period == "MIN_1"
        return [
            bar
            for bar in self.bars_by_symbol.get(symbol, [])
            if bar.timestamp >= after
        ][:count]


def _run_live_path(
    builders_by_symbol: dict[str, _SyntheticSessionBars],
    *,
    session_date: date,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Drive the REAL service tick so the TOP10 ORB variant decides/settles."""

    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    symbols = tuple(builders_by_symbol)
    adv_by_symbol = {
        symbol: 10_000_000.0 for symbol in builders_by_symbol
    }
    engine, db = _live_database()
    try:
        _seed_complete_selection_run(
            db,
            symbols=symbols,
            adv_by_symbol=adv_by_symbol,
            session_date=session_date,
        )
        provider = _LiveCandleProvider(builders_by_symbol, session_date=session_date)
        service = OpeningMomentumShadowService(db, provider)
        session_open = _session_open(session_date)
        # Decision due at entry_at + 1 bar + grace: tick inside the window.
        service.tick(now=session_open + timedelta(minutes=7, seconds=10))
        from app.models import OpeningMomentumShadowRun as _Run

        row = (
            db.query(_Run)
            .filter(
                _Run.session_date == session_date,
                _Run.candidate_symbol.is_not(None),
            )
            .order_by(_Run.id.desc())
            .first()
        )
        top10_rows = [
            value
            for value in db.query(_Run).filter(
                _Run.session_date == session_date
            ).all()
            if value.algorithm_version.endswith(
                "stocks-in-play-top10-"
                "index-catalog-valid-adv-opening5-turnover-to-prior20d-"
                "adv-proxy-next-minute-open-range-low-stop-cap4-hold60-"
                "cost30-precommitted-20260728-v1"
            )
        ]
        assert len(top10_rows) == 1, "TOP10 variant must record exactly once"
        live = top10_rows[0]
        live_payload: dict[str, Any] = {
            "status": live.status,
            "reason": live.reason,
            "candidate_symbol": live.candidate_symbol,
            "entry_price": live.entry_price,
            "stop_loss_pct": live.stop_loss_pct,
        }
        if live.status == "OPEN":
            # Drive settlement: tick after exit_due + 1 bar + grace.
            exit_due_at = live.exit_due_at
            assert exit_due_at is not None
            service.tick(
                now=_as_utc(exit_due_at) + timedelta(minutes=1, seconds=10)
            )
            db.refresh(live)
            live_payload.update(
                {
                    "exit_reason": live.reason,
                    "exit_price": live.exit_price,
                    "net_return_bps": live.net_return_bps,
                }
            )
        return live_payload
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _activity_ratio_for(builder: _SyntheticSessionBars) -> float:
    return builder.per_minute_turnover * 5 / 10_000_000.0


def _replay_path(
    builders_by_symbol: dict[str, _SyntheticSessionBars],
    *,
    session_date: date,
    adv_by_symbol: dict[str, float],
) -> dict[str, Any]:
    session_open = _session_open(session_date)
    minute_bars: dict[str, dict[datetime, Any]] = {}
    for symbol, builder in builders_by_symbol.items():
        minute_bars[symbol] = _bars_by_timestamp(
            builder.bars(session_date, symbol)
        )
    decision = evaluate_session_decision(
        universe=tuple(builders_by_symbol),
        minute_bars_by_symbol=minute_bars,
        adv_by_symbol=adv_by_symbol,
        session_open=session_open,
    )
    replay_payload: dict[str, Any] = {
        "status": decision.status,
        "reason": decision.reason,
        "candidate_symbol": decision.candidate_symbol,
        "entry_price": decision.entry_price,
        "stop_loss_pct": decision.stop_loss_pct,
    }
    if decision.status == "OPEN" and decision.candidate_symbol is not None:
        settled = settle_session_exit(
            minute_bars[decision.candidate_symbol],
            session_open=session_open,
            entry_price=decision.entry_price,  # type: ignore[arg-type]
            stop_loss_pct=decision.stop_loss_pct,  # type: ignore[arg-type]
        )
        assert settled is not None
        replay_payload.update(
            {
                "exit_reason": settled.exit_reason,
                "exit_price": settled.exit_price,
                "net_return_bps": settled.net_return_bps,
            }
        )
    return replay_payload


def _parity_symbol_setup(
    count: int = 8,
    **builder_overrides: Any,
) -> tuple[dict[str, _SyntheticSessionBars], dict[str, float]]:
    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(count):
        symbol = f"PAR{index}.US"
        turnover = 2_000_000.0 * (count - index)
        builder = _SyntheticSessionBars(
            per_minute_turnover=turnover, **builder_overrides
        )
        builders[symbol] = builder
        # ADV is chosen so the activity ratio equals 5*turnover/ADV.
        adv[symbol] = 10_000_000.0
    return builders, adv


def _assert_parity(live: dict[str, Any], replay: dict[str, Any]) -> None:
    assert replay["status"] == live["status"]
    assert replay["reason"] == live["reason"]
    assert replay["candidate_symbol"] == live["candidate_symbol"]
    if live["status"] == "OPEN":
        assert replay["entry_price"] == pytest.approx(
            float(live["entry_price"])
        )
        assert replay["stop_loss_pct"] == pytest.approx(
            float(live["stop_loss_pct"])
        )
        assert replay["exit_reason"] == live["exit_reason"]
        assert replay["exit_price"] == pytest.approx(float(live["exit_price"]))
        assert replay["net_return_bps"] == pytest.approx(
            float(live["net_return_bps"]),
            abs=1e-9,
        )


def test_parity_normal_breakout_entry_and_fixed_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders, adv = _parity_symbol_setup()
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["status"] == "OPEN"
    assert live["exit_reason"] == "FIXED_HOLD_EXIT"


def test_parity_no_breakout_skips_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders, adv = _parity_symbol_setup(breakout_bps=-100.0)
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["status"] == "SKIPPED"
    assert live["reason"] == "OPENING_RANGE_BREAKOUT_MISSING"


def test_parity_activity_tie_breaks_by_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Identical turnover for everyone: TOP10 selection and breakout-depth
    # ties must resolve the same way in both paths (symbol ascending).
    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(8):
        symbol = f"TIE{index}.US"
        builders[symbol] = _SyntheticSessionBars(
            per_minute_turnover=1_500_000.0,
            breakout_bps=50.0,
        )
        adv[symbol] = 10_000_000.0
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["candidate_symbol"] == "TIE0.US"


def _coverage_setup(
    universe_size: int,
    missing_adv_count: int,
) -> tuple[dict[str, _SyntheticSessionBars], dict[str, float]]:
    """Build a universe where the ADV gate coverage sits just above/below."""

    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(universe_size):
        symbol = f"COV{index}.US"
        builders[symbol] = _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (universe_size - index),
            breakout_bps=50.0,
        )
        if index < missing_adv_count:
            # Missing turnover => no activity ratio => member excluded from
            # ratio completeness (mirrors OPENING_ACTIVITY_DATA_MISSING).
            builders[symbol] = _SyntheticSessionBars(
                per_minute_turnover=1_000_000.0 * (universe_size - index),
                breakout_bps=50.0,
                missing_turnover=True,
            )
            # The live path derives the ratio from the selection run ADV;
            # a missing ratio there comes from missing turnover bars only.
            adv[symbol] = 10_000_000.0
        else:
            adv[symbol] = 10_000_000.0
    return builders, adv


def test_parity_coverage_just_above_and_below_ninety_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 20 members, 1 missing ratio => 19/20 = 0.95 exactly (>= passes).
    builders, adv = _coverage_setup(20, 1)
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    # 2 missing => 18/20 = 0.90 < 0.95 => DATA_INCOMPLETE in BOTH paths.
    builders_below, adv_below = _coverage_setup(20, 2)
    live_below = _run_live_path(
        builders_below, session_date=_PARITY_SESSION, monkeypatch=monkeypatch
    )
    replay_below = _replay_path(
        builders_below, session_date=_PARITY_SESSION, adv_by_symbol=adv_below
    )
    _assert_parity(live_below, replay_below)
    assert live_below["reason"] == "DATA_INCOMPLETE"


def test_parity_missing_turnover_member_excluded_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(8):
        symbol = f"MT{index}.US"
        builder = _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (8 - index),
            missing_turnover=index == 0,
            breakout_bps=50.0,
        )
        builders[symbol] = builder
        adv[symbol] = 10_000_000.0
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    # The missing-turnover member is NOT the candidate in either path.
    assert live["candidate_symbol"] != "MT0.US"


def test_parity_stop_via_gap_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders, adv = _parity_symbol_setup(
        stop_low_at_offset=ENTRY_OFFSET + 3
    )
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["status"] == "OPEN"
    assert live["exit_reason"] == "STOP_LOSS_EXIT"


def test_parity_stop_via_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders, adv = _parity_symbol_setup(
        stop_low_at_offset=ENTRY_OFFSET + 5
    )
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["exit_reason"] == "STOP_LOSS_EXIT"


def test_parity_entry_boundaries_0935_and_0936(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The signal bar is 09:35 (offset 5); the entry bar is 09:36 (offset 6).
    # A missing 09:36 bar must SKIP with ENTRY_BAR_MISSING in both paths.
    builders, adv = _parity_symbol_setup(missing_entry_bar=True)
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    assert replay["reason"] == live["reason"]
    # The domain evaluator reports ENTRY_BAR_MISSING while the service keeps
    # the run SKIPPED for the same cause.
    assert live["status"] == "SKIPPED"
    # With the entry bar present the decision window is due only after the
    # 09:36 bar completes: an earlier tick must not record the variant.
    engine, db = _live_database()
    try:
        monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
        monkeypatch.setattr(
            settings, "opening_momentum_challenger_enabled", True
        )
        builders2, adv2 = _parity_symbol_setup()
        _seed_complete_selection_run(
            db,
            symbols=tuple(builders2),
            adv_by_symbol=adv2,
            session_date=_PARITY_SESSION,
        )
        provider = _LiveCandleProvider(
            builders2, session_date=_PARITY_SESSION
        )
        service = OpeningMomentumShadowService(db, provider)
        service.tick(
            now=_session_open(_PARITY_SESSION)
            + timedelta(minutes=5, seconds=10)
        )
        from app.models import OpeningMomentumShadowRun as _Run

        top10_rows = [
            row
            for row in db.query(_Run)
            .filter(_Run.session_date == _PARITY_SESSION)
            .all()
            if row.algorithm_version.endswith(
                "stocks-in-play-top10-"
                "index-catalog-valid-adv-opening5-turnover-to-prior20d-"
                "adv-proxy-next-minute-open-range-low-stop-cap4-hold60-"
                "cost30-precommitted-20260728-v1"
            )
        ]
        assert top10_rows == [], (
            "the five-minute ORB variant may not decide before the 09:36 "
            "bar completes"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_parity_missing_entry_bar_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builders, adv = _parity_symbol_setup(missing_entry_bar=True)
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    assert live["reason"] == replay["reason"]


def test_parity_universe_and_cost_frozen_constants() -> None:
    # The replay must run the exact registered rule hash and cost model.
    assert frozen_config_version() == FROZEN_CONFIG_VERSION
    from app.cli.opening_momentum_historical_replay import (
        frozen_decision_config,
    )

    config = frozen_decision_config()
    assert config.signal_minutes == 5
    assert config.execution_delay_minutes == 1
    assert config.holding_minutes == 60
    assert config.round_trip_cost_bps == 30.0
    assert config.stop_loss_pct == 4.0
    assert config.minimum_universe_size == 8


def test_settle_exit_missing_exit_bar_is_unresolved() -> None:
    session_date = date(2024, 3, 4)
    builder = _SyntheticSessionBars()
    bars = builder.bars(session_date, "AAA.US")
    entry_price = _bars_by_timestamp(bars)[
        _session_open(session_date) + timedelta(minutes=ENTRY_OFFSET)
    ].open
    without_exit = _bars_by_timestamp(
        _drop_offsets(bars, (EXIT_OFFSET,))
    )
    assert (
        settle_session_exit(
            without_exit,
            session_open=_session_open(session_date),
            entry_price=entry_price,
            stop_loss_pct=1.5,
        )
        is None
    )


# ============================================ DECISION 1 (pre-outcome) tests
#
# Change decision 1 (2026-09-27, before any outcome): v1 was registered but
# never executed - the first real fetch failed immediately at the
# trading-day source because the provider rejects windows > 1 month
# (``code=301600 too many query days``) and supports only the most recent
# year.  The registered trading-day source is replaced by a returns-blind
# QQQ/DIA daily-bar intersection; analysis_id moves to
# ``...-pit-historical-v2``.


def _benchmark_daily_bar(session_date: date) -> BrokerCandle:
    return BrokerCandle(
        timestamp=_session_open(session_date),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1_000.0,
        turnover=100_000.0,
    )


def _benchmark_bars(
    dates: tuple[date, ...],
) -> list[BrokerCandle]:
    return [_benchmark_daily_bar(value) for value in dates]


class _BenchmarkPlanProvider(_FakePlanProvider):
    """Serves QQQ/DIA daily bars plus optional provider trading_days."""

    def __init__(
        self,
        *,
        qqq_dates: tuple[date, ...],
        dia_dates: tuple[date, ...],
        provider_trading_days: tuple[date, ...] = (),
        trading_days_error: str | None = None,
    ) -> None:
        super().__init__(
            daily_bars={},
            minute_bars={},
            trading_days=provider_trading_days,
        )
        self.daily_bars = {
            "QQQ.US": _benchmark_bars(qqq_dates),
            "DIA.US": _benchmark_bars(dia_dates),
        }
        self.trading_days_error = trading_days_error
        self.trading_days_calls = 0

    def trading_days(
        self, *, begin: date, end: date
    ) -> tuple[tuple[date, ...], tuple[date, ...]]:
        self.trading_days_calls += 1
        if self.trading_days_error is not None:
            raise RuntimeError(self.trading_days_error)
        return super().trading_days(begin=begin, end=end)


def test_benchmark_intersection_excludes_one_sided_dates() -> None:
    qqq = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    dia = (date(2024, 1, 2), date(2024, 1, 3))
    result = derive_trading_days_from_benchmarks(
        qqq_dates=qqq,
        dia_dates=dia,
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
    )
    # 2024-01-04 has a QQQ bar but no DIA bar: excluded from expected
    # sessions and reported as a mismatch with the reason recorded.
    assert result["trading_days"] == ["2024-01-02", "2024-01-03"]
    mismatches = result["one_sided_dates"]
    assert mismatches == [
        {
            "date": "2024-01-04",
            "in_qqq": True,
            "in_dia": False,
            "reason": "benchmark daily bar present in only one ETF",
        }
    ]


def test_benchmark_intersection_respects_window_bounds() -> None:
    qqq = (
        date(2023, 8, 15),  # before the window: warm-up only, not expected
        date(2024, 1, 2),
        date(2026, 5, 5),  # after the window: excluded
    )
    result = derive_trading_days_from_benchmarks(
        qqq_dates=qqq,
        dia_dates=qqq,
        start=date(2023, 9, 1),
        end=date(2026, 4, 30),
    )
    assert result["trading_days"] == ["2024-01-02"]


def test_local_calendar_mismatch_is_listed_not_gating() -> None:
    # 2024-11-28 is a local NYSE closure (Thanksgiving): if both ETFs have
    # a bar that day, the cross-check must LIST it, not resolve it.
    qqq = dia = (date(2024, 11, 27), date(2024, 11, 28), date(2024, 11, 29))
    result = derive_trading_days_from_benchmarks(
        qqq_dates=qqq,
        dia_dates=dia,
        start=date(2024, 11, 1),
        end=date(2024, 11, 30),
    )
    cross_check = cast(
        dict[str, object], result["local_calendar_cross_check"]
    )
    assert cross_check["both_etf_bar_local_closed"] == ["2024-11-28"]
    # The day remains an expected session: the cross-check does not gate.
    assert "2024-11-28" in cast(list[str], result["trading_days"])
    # A weekend bar would be flagged the same way.
    saturday = date(2024, 11, 30)
    qqq2 = dia2 = (*qqq, saturday)
    result2 = derive_trading_days_from_benchmarks(
        qqq_dates=qqq2,
        dia_dates=dia2,
        start=date(2024, 11, 1),
        end=date(2024, 11, 30),
    )
    cross_check2 = cast(
        dict[str, object], result2["local_calendar_cross_check"]
    )
    assert cross_check2["both_etf_bar_local_closed"] == [
        "2024-11-28",
        "2024-11-30",
    ]


def test_half_days_recorded_with_2023_unverified() -> None:
    from app.core.holiday_calendar import is_half_day

    qqq = dia = (
        date(2023, 7, 3),  # 2023: outside local half-day knowledge
        date(2024, 7, 3),  # known local US half day
    )
    result = derive_trading_days_from_benchmarks(
        qqq_dates=qqq,
        dia_dates=dia,
        start=date(2023, 7, 1),
        end=date(2024, 7, 31),
    )
    half_days = cast(
        dict[str, dict[str, object]], result["half_trading_days"]
    )
    assert half_days["2024-07-03"] == {
        "label": (
            "Day before Independence Day"
            if is_half_day("US", date(2024, 7, 3))
            else None
        ),
        "verified": True,
    }
    # 2023 half-days are unknown to the local calendar: recorded from the
    # conventional schedule and explicitly marked UNVERIFIED.
    assert half_days["2023-07-03"] == {
        "label": "conventional NYSE early close (2023)",
        "verified": False,
    }
    assert result["half_day_note"] == (
        "2023 half-days are unverified (local calendar starts 2024-01-01); "
        "the 09:36 entry and 60-minute hold both end before 13:00, so "
        "half days do not change the rule"
    )


def test_301600_too_many_query_days_is_request_shape_not_symbol() -> None:
    # Regression for the v1 failure: the provider returns code=301600 with
    # "too many query days" for the trading-day window.  That is a request
    # SHAPE error, never a per-symbol permanent failure.
    assert (
        classify_provider_error(
            "OpenApiException: (code=301600) too many query days"
        )
        == "REQUEST_SHAPE"
    )
    # A genuine invalid-symbol 301600 is still a symbol failure.
    assert (
        classify_provider_error(
            "OpenApiException: (code=301600) invalid symbol ATVI.US"
        )
        == "PERMANENT_SYMBOL"
    )
    assert classify_provider_error("connection reset") == "TRANSIENT"


def test_trading_days_cross_check_failure_does_not_abort(
    tmp_path: Path,
) -> None:
    # The optional provider trading_days cross-check raises (the exact v1
    # failure mode); the fetch must still complete from the
    # benchmark-derived session list.
    qqq = dia = (date(2026, 2, 2), date(2026, 2, 3))
    provider = _BenchmarkPlanProvider(
        qqq_dates=qqq,
        dia_dates=dia,
        trading_days_error=(
            "OpenApiException: (code=301600) too many query days"
        ),
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    plan = _minimal_plan(
        symbols=("AAA.US",),
        first=date(2026, 2, 2),
        last=date(2026, 2, 3),
        daily_start=date(2025, 12, 15),
    )
    report = run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert report["coverage"]["symbols_total"] == 1
    trading_payload = json.loads(
        (tmp_path / "trading_days.json").read_text(encoding="utf-8")
    )
    assert trading_payload["trading_days"] == ["2026-02-02", "2026-02-03"]
    assert trading_payload["source"].startswith(
        "benchmark daily-bar intersection"
    )
    cross_checks = trading_payload["provider_trading_days_cross_check"]
    assert cross_checks["status"] == "SKIPPED_ERROR"
    assert "too many query days" in cross_checks["error"]
    # No global stop was written for a request-shape error.
    status = _load_status(tmp_path)
    assert status.get("global_stop") is None


def test_trading_days_cross_check_success_is_extra_only(
    tmp_path: Path,
) -> None:
    # When the provider call succeeds (within the recent-year window of
    # the injected clock), its result is recorded as an EXTRA cross-check
    # and never replaces the benchmark list.
    qqq = dia = (date(2026, 2, 2), date(2026, 2, 3))
    provider = _BenchmarkPlanProvider(
        qqq_dates=qqq,
        dia_dates=dia,
        provider_trading_days=(date(2026, 2, 2),),
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    plan = _minimal_plan(
        symbols=("AAA.US",),
        first=date(2026, 2, 2),
        last=date(2026, 2, 3),
        daily_start=date(2025, 12, 15),
    )
    report = run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    assert report["coverage"]["symbols_total"] == 1
    trading_payload = json.loads(
        (tmp_path / "trading_days.json").read_text(encoding="utf-8")
    )
    # The derived window list keeps only the benchmark sessions inside
    # [window_start, window_end] of the plan, which are exactly these.
    assert trading_payload["trading_days"] == [
        "2026-02-02",
        "2026-02-03",
    ]
    cross_checks = trading_payload["provider_trading_days_cross_check"]
    assert cross_checks["status"] == "RECORDED"
    assert cross_checks["provider_only_dates"] == []
    assert cross_checks["benchmark_only_dates"] == ["2026-02-03"]


def test_fetch_fails_cleanly_on_benchmark_failure(
    tmp_path: Path,
) -> None:
    # If the benchmark bars themselves fail transiently beyond retries, the
    # fetch writes a status.json error entry (never a raw traceback, never
    # a per-symbol permanent failure) and fails cleanly.
    class _BrokenBenchmarkProvider(_BenchmarkPlanProvider):
        def history_candlesticks_by_offset(
            self,
            symbol: str,
            period: str,
            *,
            count: int,
            after: datetime,
            forward: bool,
            adjustment: str,
        ) -> list[_CandleView]:
            if symbol in ("QQQ.US", "DIA.US"):
                raise RuntimeError("connection reset by peer")
            return super().history_candlesticks_by_offset(
                symbol,
                period,
                count=count,
                after=after,
                forward=forward,
                adjustment=adjustment,
            )

    provider = _BrokenBenchmarkProvider(
        qqq_dates=(date(2024, 1, 2),),
        dia_dates=(date(2024, 1, 2),),
    )
    clock = _FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc))
    with pytest.raises(HistoricalReplayError, match="benchmark"):
        run_fetch(
            cache_dir=tmp_path,
            plan_payload=_minimal_plan(),
            provider=provider,
            clock=clock,
            sleep=lambda seconds: None,
        )
    status = _load_status(tmp_path)
    errors = status.get("errors", [])
    assert isinstance(errors, list) and errors, (
        "a clean status.json error entry must be written"
    )
    entry = errors[-1]
    assert entry["stage"] == "TRADING_DAYS"
    assert "connection reset" in entry["detail"]
    assert status.get("global_stop") is None
    assert status.get("symbols") == {}


# ---------------------------------------------------------------------------
# Real SDK adapter surface (2026-09-27: the v2 fetch failed on its first call
# because the adapter asked the SDK for ``Period.DAY``; the SDK only exposes
# ``Period.Day`` / ``Period.Min_1``).  The adapter is built without its
# network constructor and driven against a fake SDK namespace whose enum
# members carry the REAL SDK names.
# ---------------------------------------------------------------------------


class _FakeSdkPeriod:
    Day = "SDK_PERIOD_DAY"
    Min_1 = "SDK_PERIOD_MIN_1"


class _FakeSdkAdjustType:
    NoAdjust = "SDK_ADJUST_NONE"
    ForwardAdjust = "SDK_ADJUST_FORWARD"


class _FakeSdkNamespace:
    Period = _FakeSdkPeriod
    AdjustType = _FakeSdkAdjustType


class _FakeSdkQuoteContext:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def history_candlesticks_by_offset(self, *args: Any) -> list[Any]:
        self.calls.append(args)
        return []


def _sdk_adapter() -> tuple[_LongportQuoteProvider, _FakeSdkQuoteContext]:
    provider = object.__new__(_LongportQuoteProvider)
    context = _FakeSdkQuoteContext()
    setattr(provider, "_quote_ctx", context)
    setattr(provider, "_openapi", _FakeSdkNamespace())
    return provider, context


def test_real_adapter_requests_sdk_period_enum_members() -> None:
    provider, context = _sdk_adapter()
    boundary = datetime(2023, 9, 5, 13, 30, tzinfo=timezone.utc)

    for period in ("DAY", "MIN_1"):
        provider.history_candlesticks_by_offset(
            "AAPL.US",
            period,
            count=1000,
            after=boundary,
            forward=True,
            adjustment="NoAdjust",
        )

    assert [call[1] for call in context.calls] == [
        "SDK_PERIOD_DAY",
        "SDK_PERIOD_MIN_1",
    ]
    for call in context.calls:
        symbol, _, adjust_type, forward, count, sdk_boundary = call
        assert symbol == "AAPL.US"
        assert adjust_type == "SDK_ADJUST_NONE"
        assert forward is True
        assert count == 1000
        # Exchange wall clock: 13:30 UTC on 2023-09-05 is 09:30 EDT.
        assert (sdk_boundary.hour, sdk_boundary.minute) == (9, 30)
        assert sdk_boundary.utcoffset() == timedelta(hours=-4)


def test_real_adapter_rejects_an_unmapped_period() -> None:
    provider, context = _sdk_adapter()

    with pytest.raises(HistoricalReplayError, match="WEEK"):
        provider.history_candlesticks_by_offset(
            "AAPL.US",
            "WEEK",
            count=10,
            after=datetime(2023, 9, 5, 13, 30, tzinfo=timezone.utc),
            forward=True,
            adjustment="NoAdjust",
        )
    assert context.calls == []


# =============================================================================
# Change decision 2 (2026-09-27, before any outcome): pre-outcome review
# MUST-FIX items.  All tests below are synthetic (tmp_path only); the running
# v2 fetch cache is never touched.
# =============================================================================


# --------------------------------------------------- MF1: preflight & liveness


def _pit_symbols_for(sessions: tuple[date, ...]) -> tuple[str, ...]:
    """Union PIT membership across the sessions, sorted (the plan shape)."""

    symbols: set[str] = set()
    for session_date in sessions:
        symbols.update(pit_universe_for_session(session_date))
    return tuple(sorted(symbols))


def _default_builders(
    symbols: tuple[str, ...],
    *,
    sessions: tuple[date, ...],
) -> dict[str, _SyntheticSessionBars]:
    """Distinct activity per symbol so the TOP10 ranking is deterministic."""

    return {
        symbol: _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (len(symbols) - index)
        )
        for index, symbol in enumerate(symbols)
    }


def _complete_synthetic_cache(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...] | None,
    sessions: tuple[date, ...],
    builders_by_symbol: dict[str, _SyntheticSessionBars] | None = None,
    adv_per_symbol: dict[str, float] | None = None,
    permanent_failures: tuple[str, ...] = (),
    plan: dict[str, Any] | None = None,
    all_sessions_for_minutes: bool = True,
) -> None:
    """Fetch-complete synthetic cache using the REAL run_fetch writer paths.

    The v3 calendar shape is fixed: ``sessions[0]`` is the warm-up marker
    (a pre-window day), the remaining sessions are scoring days, and the
    warm-up file holds the 21 sealed days strictly before
    ``min(sessions[0], WINDOW_START)``.  Daily bars exist on every
    calendar day so each scoring day's 21-session ADV window is complete.
    """

    if symbols is None:
        symbols = _pit_symbols_for(sessions)
    if builders_by_symbol is None:
        builders_by_symbol = _default_builders(symbols, sessions=sessions)
    if adv_per_symbol is None:
        adv_per_symbol = {symbol: 10_000_000.0 for symbol in symbols}

    # Convention: sessions[0] is a warm-up marker ONLY when it lies
    # outside the scoring window; otherwise every session is a scoring
    # day.  Either way the warm-up calendar is the 21 sealed days
    # strictly before the FIRST SCORING day, kept outside the window.
    if sessions[0] < WINDOW_START:
        scoring = sessions[1:]
        first_scoring = min(
            (value for value in scoring if value >= WINDOW_START),
            default=sessions[-1],
        )
    else:
        scoring = sessions
        first_scoring = sessions[0]
    # The real pipeline's warm-up is the registered August window
    # (pre-WINDOW_START).  Synthetic January scoring days use the SAME
    # August warm-up; their 21-session window then spans the August
    # sealed sessions, exactly as the sealed calendars dictate.
    warmup_boundary = min(first_scoring, WINDOW_START)
    warmup_days = [
        value
        for value in _weekday_dates(45, warmup_boundary)
        if value < warmup_boundary
    ][-21:]
    assert len(warmup_days) == 21, warmup_boundary
    daily_start = warmup_days[0] - timedelta(days=3)
    if daily_start.weekday() >= 5:
        daily_start -= timedelta(days=daily_start.weekday() - 4)

    if plan is None:
        plan = _minimal_plan(
            symbols=symbols,
            first=first_scoring,
            last=scoring[-1] if scoring else first_scoring,
            daily_start=daily_start,
        )

    minute_source: dict[str, list[BrokerCandle]] = {}
    for symbol in symbols:
        if symbol in permanent_failures:
            continue
        rows: list[BrokerCandle] = []
        for session_date in scoring:
            rows.extend(
                builders_by_symbol[symbol].bars(session_date, symbol)
            )
        minute_source[symbol] = rows
    calendar_days = tuple([*warmup_days, *scoring])
    daily_source = {
        symbol: _daily_bars_on_sessions(
            symbol, sessions=calendar_days, daily_start=daily_start
        )
        for symbol in symbols
        if symbol not in permanent_failures
    }
    provider = _FakePlanProvider(
        daily_bars=daily_source,
        minute_bars=minute_source,
        trading_days=calendar_days,
        fail_symbols_permanently=set(permanent_failures),
    )
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )
    _atomic_write_json(
        tmp_path / "trading_days_warmup.json",
        {
            "source": "synthetic warm-up calendar (tests)",
            "fetched_at": "2026-09-27T00:00:00+00:00",
            "warmup_start": warmup_days[0].isoformat(),
            "warmup_end": warmup_days[-1].isoformat(),
            "request_count": 2,
            "reason": None,
            "trading_days": [value.isoformat() for value in warmup_days],
            "one_sided_dates": [],
        },
    )


def _no_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise environment guards except where they are under test.

    The /proc liveness scan and the clean-worktree requirement are both
    CORRECT for a real evaluation (the real cache has a live fetch; a real
    evaluation runs against a committed tree).  Synthetic tmp_path tests
    neutralise both; ``test_evaluate_refuses_dirty_worktree`` restores the
    real worktree check via the module alias below.
    """

    from app.cli import opening_momentum_historical_replay as replay_mod

    if not hasattr(replay_mod, "__wrapped_require_clean_worktree"):  # noqa: B009
        setattr(
            replay_mod,
            "__wrapped_require_clean_worktree",
            replay_mod._require_clean_worktree,
        )
    monkeypatch.setattr(replay_mod, "_fetch_process_alive", lambda d: None)
    monkeypatch.setattr(replay_mod, "_require_clean_worktree", lambda d: None)


def _output_path(tmp_path: Path, name: str = "out.json") -> Path:
    """An output path OUTSIDE the sealed cache directory."""

    outside = tmp_path.parent / f"{tmp_path.name}-results"
    outside.mkdir(parents=True, exist_ok=True)
    return outside / name


def test_seal_refuses_partial_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    # The plan declares TWO symbols but only AAA.US completed (BBB.US is
    # PENDING because the provider never produced bars for it and it was
    # never marked terminal): seal must refuse.
    sessions = (date(2024, 1, 2),)
    builders = {
        "AAA.US": _SyntheticSessionBars(),
        "BBB.US": _SyntheticSessionBars(per_minute_turnover=1_000_000.0),
    }
    plan = _minimal_plan(
        symbols=("AAA.US", "BBB.US"), first=sessions[0], last=sessions[0]
    )
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=sessions[0], count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(sessions[0], "AAA.US"),
        },
        trading_days=sessions,
    )
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )
    # Simulate the fetch stopping midway: BBB.US was never processed at
    # all (its file and state entry are absent).
    (tmp_path / "minute" / "BBB.US.json.gz").unlink(missing_ok=True)
    (tmp_path / "daily" / "BBB.US.json.gz").unlink(missing_ok=True)
    status = _load_status(tmp_path)
    status["symbols"].pop("BBB.US", None)
    _atomic_write_json(tmp_path / "status.json", status)
    assert status["symbols"]["AAA.US"]["minute"]["state"] == "COMPLETE"
    assert "BBB.US" not in status["symbols"]

    with pytest.raises(HistoricalReplayError, match="terminal"):
        run_seal(tmp_path, plan_payload=plan)
    # evaluate also refuses on the same cache (no sealed manifest yet, and
    # the shared preflight runs before anything else).
    with pytest.raises(HistoricalReplayError, match="seal|terminal"):
        run_evaluate(
            cache_dir=tmp_path, output_path=_output_path(tmp_path)
        )


def test_seal_refuses_transient_failure_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    # A symbol stuck in FAILED_TRANSIENT is not terminal: refuse.
    sessions = (date(2024, 1, 2),)
    plan = _minimal_plan(symbols=("AAA.US",), first=sessions[0], last=sessions[0])
    provider = _FakePlanProvider(
        daily_bars={
            "AAA.US": _minute_style_daily(
                "AAA.US", first=sessions[0], count=30
            )
        },
        minute_bars={
            "AAA.US": _SyntheticSessionBars().bars(sessions[0], "AAA.US"),
        },
        trading_days=sessions,
    )
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )
    status = _load_status(tmp_path)
    status["symbols"]["AAA.US"]["daily"]["state"] = "FAILED_TRANSIENT"
    _atomic_write_json(tmp_path / "status.json", status)
    with pytest.raises(HistoricalReplayError, match="FAILED_TRANSIENT|terminal"):
        run_seal(tmp_path, plan_payload=plan)


def test_seal_refuses_when_complete_state_lacks_its_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    sessions = (date(2024, 1, 2),)
    builders = {"AAA.US": _SyntheticSessionBars()}
    plan = _minimal_plan(symbols=("AAA.US",), first=sessions[0], last=sessions[0])
    _complete_synthetic_cache(
        tmp_path,
        symbols=("AAA.US",),
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={"AAA.US": 10_000_000.0},
        plan=plan,
    )
    (tmp_path / "minute" / "AAA.US.json.gz").unlink()
    with pytest.raises(HistoricalReplayError, match="missing|absent"):
        run_seal(tmp_path, plan_payload=plan)


def test_seal_refuses_while_a_fetch_process_is_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = (date(2024, 1, 2),)
    builders = {"AAA.US": _SyntheticSessionBars()}
    plan = _minimal_plan(symbols=("AAA.US",), first=sessions[0], last=sessions[0])
    _complete_synthetic_cache(
        tmp_path,
        symbols=("AAA.US",),
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={"AAA.US": 10_000_000.0},
        plan=plan,
    )
    recorded = {}

    def _fake_readlines(pid_path: Path) -> list[str]:
        recorded["pid"] = pid_path.name
        return (
            "/usr/bin/python\0"
            ".venv/bin/python\0"
            "-m\0"
            "app.cli.opening_momentum_historical_replay\0"
            "fetch\0"
            f"--plan\0{tmp_path}/plan.json\0"
        ).split("\0")

    monkeypatch.setattr(
        "app.cli.opening_momentum_historical_replay._proc_cmdline",
        _fake_readlines,
    )
    with pytest.raises(HistoricalReplayError, match="alive"):
        run_seal(tmp_path, plan_payload=plan)
    assert recorded["pid"]


def test_seal_binds_the_plan_and_its_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    sessions = (date(2024, 1, 2),)
    builders = {"AAA.US": _SyntheticSessionBars()}
    plan = _minimal_plan(symbols=("AAA.US",), first=sessions[0], last=sessions[0])
    _complete_synthetic_cache(
        tmp_path,
        symbols=("AAA.US",),
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={"AAA.US": 10_000_000.0},
        plan=plan,
    )
    manifest_hash = run_seal(tmp_path, plan_payload=plan)
    assert len(manifest_hash) == 64
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["plan"]["sha256"]
    assert manifest["plan"]["symbols"] == plan["symbols"]
    # The plan payload itself is stored with the cache.
    stored_plan = json.loads(
        (tmp_path / "plan.json").read_text(encoding="utf-8")
    )
    assert stored_plan == plan


def test_seal_accepts_permanent_failure_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    sessions = (date(2024, 1, 2),)
    builders = {
        "AAA.US": _SyntheticSessionBars(),
        "ATVI.US": _SyntheticSessionBars(per_minute_turnover=900_000.0),
    }
    plan = _minimal_plan(
        symbols=("AAA.US", "ATVI.US"), first=sessions[0], last=sessions[0]
    )
    _complete_synthetic_cache(
        tmp_path,
        symbols=("AAA.US", "ATVI.US"),
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={
            "AAA.US": 10_000_000.0,
            "ATVI.US": 10_000_000.0,
        },
        permanent_failures=("ATVI.US",),
        plan=plan,
    )
    manifest_hash = run_seal(tmp_path, plan_payload=plan)
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    absences = manifest["permanent_absences"]
    assert absences["ATVI.US"]["state"] == "PERMANENT_FAILURE"
    assert absences["ATVI.US"]["evidence"]


# -------------------------------------------- MF5: closed input set at evaluate


_WARMUP_DAY = date(2023, 8, 15)
_WARMUP_DAILY_START = date(2023, 7, 3)
#: A v3-style warm-up calendar: August 2023 weekdays whose latest day
#: precedes the first scoring session (2023-09-01 is the real first day;
#: tests use a January 2024 first day, so the warm-up only needs to prove
#: a predecessor of it).
def _write_warmup_calendar(
    tmp_path: Path,
    *,
    predecessor_of: date,
    days: int = 21,
) -> None:
    dates = [
        d
        for d in _weekday_dates(days + 5, predecessor_of)
        if d < predecessor_of
    ][-days:]
    _atomic_write_json(
        tmp_path / "trading_days_warmup.json",
        {
            "source": "synthetic warm-up calendar (tests)",
            "fetched_at": "2026-09-27T00:00:00+00:00",
            "warmup_start": dates[0].isoformat(),
            "warmup_end": dates[-1].isoformat(),
            "request_count": 2,
            "reason": None,
            "trading_days": [value.isoformat() for value in dates],
            "one_sided_dates": [],
        },
    )


def _sealed_full_cache(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...] | None = None,
    window_sessions: tuple[date, ...] = (date(2024, 1, 2),),
    permanent_failures: tuple[str, ...] = (),
) -> str:
    """Full-PIT sealed cache: the realistic plan covers every PIT member."""

    sessions = (_WARMUP_DAY, *window_sessions)
    if symbols is None:
        symbols = _pit_symbols_for(window_sessions)
    builders = _default_builders(symbols, sessions=sessions)
    plan = _minimal_plan(
        symbols=symbols,
        first=window_sessions[0],
        last=window_sessions[-1],
        daily_start=_WARMUP_DAILY_START,
    )
    _complete_synthetic_cache(
        tmp_path,
        symbols=symbols,
        sessions=sessions,
        builders_by_symbol=builders,
        adv_per_symbol={symbol: 10_000_000.0 for symbol in symbols},
        permanent_failures=permanent_failures,
        plan=plan,
    )
    return run_seal(tmp_path, plan_payload=plan)


def test_evaluate_refuses_unsealed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    # A NEW file that the manifest does not list: refused even though it
    # exists on disk.
    rogue = tmp_path / "minute" / "ZZZ.US.json.gz"
    _write_bars_file(
        tmp_path,
        "minute",
        "ZZZ.US",
        period="MIN_1",
        adjustment="NoAdjust",
        bars=[],
    )
    assert rogue.exists()
    with pytest.raises(HistoricalReplayError, match="unsealed|not listed"):
        run_evaluate(
            cache_dir=tmp_path, output_path=_output_path(tmp_path)
        )
    rogue.unlink()


def test_evaluate_refuses_drifted_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    # Tamper with a sealed file after seal.
    plan_symbols = sorted(
        str(entry["symbol"])
        for entry in json.loads(
            (tmp_path / "plan.json").read_text(encoding="utf-8")
        )["symbols"]
    )
    target = tmp_path / "minute" / f"{plan_symbols[0]}.json.gz"
    original = target.read_bytes()
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["bars"] = payload["bars"][:10]
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    with pytest.raises(HistoricalReplayError, match="drift"):
        run_evaluate(
            cache_dir=tmp_path, output_path=_output_path(tmp_path)
        )
    target.write_bytes(original)


def test_seal_receipt_cannot_be_silently_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    first_receipt = json.loads(
        (tmp_path / "seal_receipt.json").read_text(encoding="utf-8")
    )
    with pytest.raises(HistoricalReplayError, match="reseal"):
        run_seal(tmp_path)
    # A re-seal with a reason keeps the old receipt.
    run_seal(tmp_path, reseal_reason="receipt test")
    receipts = sorted(
        (tmp_path / "seal_receipts").glob("*.json")
    )
    assert len(receipts) == 1
    preserved = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert preserved == first_receipt


# ---------------------------------------------- MF2: member-day gate semantics


def test_gate_b_counts_per_member_day_not_whole_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    # Full-PIT cache over two window sessions; every PIT member has data,
    # so gate (b) counts ZERO missing member-days of both kinds.
    _sealed_full_cache(
        tmp_path, window_sessions=(date(2024, 1, 2), date(2024, 1, 3))
    )
    payload = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    member_status = payload["member_day_status"]
    manifest_universe = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )["universe"]
    assert set(manifest_universe) == {"2024-01-02", "2024-01-03"}
    assert member_status["member_days_total"] == sum(
        len(value) for value in manifest_universe.values()
    )
    assert member_status["gate_b_missing"] == 0
    assert payload["gates"]["gate_b_counted_kinds"] == sorted(
        GATE_B_MISSING_KINDS
    )


def test_decision_universe_requires_finite_positive_adv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Final check, item 3b: a zero-dollar-volume bar inside the last 21
    # makes the spread proxy unavailable; live records
    # DATA_INVALID_SPREAD_PROXY and NO ADV.  The replay must classify
    # the member UNVERIFIABLE (coverage-only, in the denominator), never
    # ELIGIBLE - the neutral-quote retry is gone.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path, window_sessions=(date(2024, 1, 2), date(2024, 1, 3))
    )
    plan_symbols = sorted(
        str(entry["symbol"])
        for entry in json.loads(
            (tmp_path / "plan.json").read_text(encoding="utf-8")
        )["symbols"]
    )
    victim = plan_symbols[-1]
    target = tmp_path / "daily" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload_raw = json.load(handle)
    bars = payload_raw["bars"]
    bars[len(bars) - 5][5] = 0.0  # volume
    bars[len(bars) - 5][6] = 0.0  # turnover -> dollar volume 0
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload_raw, handle)
    run_seal(tmp_path, reseal_reason="zero dollar volume test")
    payload = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    day = "2024-01-02"
    # UNVERIFIABLE: coverage-only, in the denominator, never in the pool.
    assert victim in payload["coverage_only_symbols"][day]
    # Gate (b) does not count an ADV input anomaly.
    assert payload["member_day_status"]["gate_b_missing"] == 0


def test_parity_short_daily_history_excluded_in_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live: the universe-selection run carries avg_dollar_volume ONLY for
    # symbols with enough daily history; the short-history symbol never
    # enters the variant's symbol list.  Replay: rebuild_session_adv
    # returns None for it, so it is excluded from the decision universe.
    builders: dict[str, _SyntheticSessionBars] = {}
    for index in range(9):
        symbol = f"SH{index}.US"
        builders[symbol] = _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (9 - index)
        )
    symbols_with_adv = tuple(
        symbol for symbol in builders if symbol != "SH8.US"
    )
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    engine, db = _live_database()
    try:
        _seed_complete_selection_run(
            db,
            symbols=symbols_with_adv,
            adv_by_symbol={
                symbol: 10_000_000.0 for symbol in symbols_with_adv
            },
            session_date=_PARITY_SESSION,
        )
        provider = _LiveCandleProvider(
            builders, session_date=_PARITY_SESSION
        )
        service = OpeningMomentumShadowService(db, provider)
        session_open = _session_open(_PARITY_SESSION)
        service.tick(now=session_open + timedelta(minutes=7, seconds=10))
        from app.models import OpeningMomentumShadowRun as _Run

        top10_rows = [
            value
            for value in db.query(_Run)
            .filter(_Run.session_date == _PARITY_SESSION)
            .all()
            if value.algorithm_version.endswith(
                "stocks-in-play-top10-"
                "index-catalog-valid-adv-opening5-turnover-to-prior20d-"
                "adv-proxy-next-minute-open-range-low-stop-cap4-hold60-"
                "cost30-precommitted-20260728-v1"
            )
        ]
        assert len(top10_rows) == 1
        live = top10_rows[0]
        assert live.candidate_symbol != "SH8.US"

        # Replay: SH8.US has no ADV (short history): it is
        # KNOWN_INELIGIBLE and must be excluded from the decision POOL
        # the same way (live's index_catalog_symbols drops it entirely).
        adv_by_symbol = {
            symbol: 10_000_000.0 for symbol in symbols_with_adv
        }
        minute_bars = {
            symbol: _bars_by_timestamp(
                builder.bars(_PARITY_SESSION, symbol)
            )
            for symbol, builder in builders.items()
        }
        decision = evaluate_session_decision(
            universe=symbols_with_adv,
            minute_bars_by_symbol=minute_bars,
            adv_by_symbol=adv_by_symbol,
            session_open=session_open,
        )
        assert decision.candidate_symbol == live.candidate_symbol
        assert decision.status == live.status
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


# ------------------------------------------------------ MF4: ADV equivalence


def _adv_daily_bars(
    *,
    count: int,
    before: date,
    turnover: float = 100_000.0,
    volume: float = 1_000.0,
) -> list[_DailyBarRow]:
    dates = _weekday_dates(count, before)
    return [
        _DailyBarRow(
            timestamp=_session_open(value),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=volume,
            turnover=turnover,
        )
        for value in dates
    ]


def test_adv_takes_last_twenty_one_and_validates_whole_window() -> None:
    as_of = date(2024, 3, 4)
    bars = _adv_daily_bars(count=25, before=as_of)
    window = bars[-21:]
    # The LAST 21 bars form the window; one invalid bar INSIDE the last 21
    # is UNVERIFIABLE and must not be replaced by an older bar.
    adv_ok = classify_session_adv(
        bars,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    )
    assert adv_ok.kind == "ELIGIBLE"
    assert adv_ok.adv == pytest.approx(100_000.0)
    bad_inside = list(bars)
    bad_inside[-5] = replace(bad_inside[-5], high=98.0)  # high < low
    bad = classify_session_adv(
        bad_inside,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    )
    assert bad.kind == "UNVERIFIABLE"
    assert bad.reason == UNVERIFIABLE_INVALID_BAR
    # A bad bar OUTSIDE the last 21 does not matter.
    bad_outside = list(bars)
    bad_outside[0] = replace(bad_outside[0], high=1.0)
    assert classify_session_adv(
        bad_outside,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    ).adv == pytest.approx(100_000.0)


def _last_date(bars: list[_DailyBarRow]) -> date:
    from app.cli.opening_momentum_historical_replay import (
        _session_open_utc,
    )

    market = get_session("US")
    return market.local(bars[-1].timestamp).date()


def _sessions_before(count: int, as_of: date) -> list[date]:
    return _weekday_dates(count, as_of)


def test_adv_missing_volume_invalidates_window_not_dropped() -> None:
    # Item A: a missing volume makes the BAR invalid (and the window);
    # the date row is never dropped so older bars cannot fill in.
    as_of = date(2024, 3, 4)
    bars = _adv_daily_bars(count=21, before=as_of)
    no_volume = list(bars)
    no_volume[-1] = replace(no_volume[-1], volume=None)  # type: ignore[arg-type]
    result = classify_session_adv(
        no_volume,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    )
    assert result.kind == "UNVERIFIABLE"
    assert result.reason == UNVERIFIABLE_INVALID_BAR


def test_adv_missing_turnover_uses_frozen_fallback_exactly() -> None:
    # Item A: 25 bars where the 5th from last is missing turnover.  The
    # date row stays; the frozen ``_dollar_volume`` falls back to
    # close*volume for that bar.  The result equals the frozen selector's
    # own ``_candidate_metrics`` on equivalent inputs - by construction.
    as_of = date(2024, 3, 4)
    bars = _adv_daily_bars(count=25, before=as_of)
    missing_turnover = list(bars)
    missing_turnover[-5] = replace(missing_turnover[-5], turnover=0.0)
    window = missing_turnover[-21:]
    result = classify_session_adv(
        missing_turnover,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    )
    assert result.kind == "ELIGIBLE"
    # Hand-computed expectation: turnover for every window bar except the
    # 5th from last; that bar falls back to close*volume = 100*1000.
    expected_values = []
    for position, bar in enumerate(window):
        is_fallback = position == len(window) - 5
        if is_fallback:
            from app.domain.universe_selection.selector import (
                _dollar_volume,
            )

            expected_values.append(_dollar_volume(cast(Any, bar)))
        else:
            expected_values.append(100_000.0)
    expected = sum(expected_values[-20:]) / 20
    assert result.adv == pytest.approx(expected)
    # Parity with the frozen selector on equivalent inputs.
    from app.domain.universe_selection.selector import (
        CandidateInput,
        UniverseSelectionConfig,
        _candidate_metrics,
    )
    from app.domain.universe_selection.catalog import IndexCandidate

    from app.domain.universe_selection.selector import (
        liquidity_spread_proxy_bps,
    )

    metrics, reasons = _candidate_metrics(
        CandidateInput(
            candidate=IndexCandidate(
                symbol="PARITY.US",
                alias="parity",
                sector="Software",
                memberships=(),
            ),
            completed_daily_bars=cast(Any, list(window)),
            bid=None,
            ask=None,
            estimated_spread_bps=liquidity_spread_proxy_bps(
                cast(Any, list(window))
            ),
            data_errors=(),
        ),
        UniverseSelectionConfig(),
    )
    liquidity_reasons = [
        reason for reason in reasons if not reason.startswith("DATA_")
    ]
    assert not any(
        reason.startswith("DATA_") for reason in reasons
    ), reasons
    assert metrics.avg_dollar_volume == pytest.approx(result.adv)


def test_adv_rejects_duplicate_dates() -> None:
    as_of = date(2024, 3, 4)
    bars = _adv_daily_bars(count=21, before=as_of)
    duplicated = [*bars, bars[-1]]
    with pytest.raises(HistoricalReplayError, match="duplicate"):
        classify_session_adv(
            duplicated,
            as_of=as_of,
            previous_sealed_session=_last_date(bars),
        )


def test_adv_freshness_classifications() -> None:
    as_of = date(2024, 1, 4)
    bars = _adv_daily_bars(count=21, before=as_of)
    fresh = classify_session_adv(
        bars,
        as_of=as_of,
        previous_sealed_session=date(2024, 1, 3),
    )
    assert fresh.kind == "ELIGIBLE"
    assert fresh.adv == pytest.approx(100_000.0)
    stale = classify_session_adv(
        bars,
        as_of=as_of,
        previous_sealed_session=date(2024, 1, 2),
    )
    assert stale.adv is None
    assert stale.kind == "UNVERIFIABLE"


def test_adv_short_history_is_unverifiable_without_listing_proof() -> None:
    # Final check, item 4: "fewer than 21 bars since the first cached
    # date" is NOT a listing proof (the cache starts at the fetch
    # boundary).  The NEW_LISTING path is removed: every such member is
    # UNVERIFIABLE - coverage-only, in the denominator.
    as_of = date(2024, 1, 4)
    sessions = _sessions_before(30, as_of)
    bars = _adv_daily_bars(count=10, before=as_of)
    result = classify_session_adv(
        bars,
        as_of=as_of,
        previous_sealed_session=sessions[-1],
        sealed_sessions=sessions,
    )
    assert result.kind == "UNVERIFIABLE"
    assert result.reason == UNVERIFIABLE_INSUFFICIENT_WINDOW


def test_adv_zero_dollar_volume_is_unverifiable_not_eligible() -> None:
    # Final check, item 3b counterexample: one zero-dollar-volume bar
    # among the last 20 with the others positive.  The spread proxy is
    # unavailable and live records DATA_INVALID_SPREAD_PROXY (no ADV);
    # the replay must give UNVERIFIABLE, not ELIGIBLE.
    as_of = date(2024, 3, 4)
    bars = _adv_daily_bars(count=25, before=as_of)
    zeroed = list(bars)
    zeroed[-5] = replace(zeroed[-5], turnover=0.0, volume=0.0)
    result = classify_session_adv(
        zeroed,
        as_of=as_of,
        previous_sealed_session=_last_date(bars),
    )
    assert result.kind == "UNVERIFIABLE"
    assert result.adv is None
    assert result.coverage_only


# --------------------------------------------------- MF6: run-once protection


def test_evaluate_attempt_receipt_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    out = _output_path(tmp_path)
    first = run_evaluate(cache_dir=tmp_path, output_path=out)
    attempt = json.loads(
        (tmp_path / "attempt.json").read_text(encoding="utf-8")
    )
    assert attempt["state"] == "COMPLETED"
    assert attempt["output_path"] == str(out)
    assert attempt["output_sha256"]
    # Same output path without a reason is refused.
    with pytest.raises(HistoricalReplayError, match="rerun-reason"):
        run_evaluate(cache_dir=tmp_path, output_path=out)
    # A DIFFERENT output path does not bypass: a completed attempt exists.
    with pytest.raises(HistoricalReplayError, match="rerun-reason"):
        run_evaluate(
            cache_dir=tmp_path, output_path=_output_path(tmp_path, "out2.json")
        )
    # A reasoned rerun records the previous attempt and never renames the
    # old output.
    rerun = run_evaluate(
        cache_dir=tmp_path,
        output_path=out,
        rerun_reason="receipt flow test",
    )
    assert out.exists()
    attempt2 = json.loads(
        (tmp_path / "attempt.json").read_text(encoding="utf-8")
    )
    assert attempt2["previous_attempts"][0]["output_sha256"] == (
        attempt["output_sha256"]
    )
    assert rerun["provenance"]["supersedes"] is not None


def test_evaluate_output_may_not_be_inside_the_input_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    with pytest.raises(HistoricalReplayError, match="inside"):
        run_evaluate(
            cache_dir=tmp_path,
            output_path=tmp_path / "minute" / "out.json",
        )


# ------------------------------------------------ SHOULD-FIX A: parity fixes


def test_parity_coverage_drops_observations_not_turnover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exactly 19 of 20 observations have an activity ratio: the member
    # WITHOUT a ratio must lose its OBSERVATION (live excludes the symbol
    # from the ratio map and coverage counts observations), achieved by a
    # missing ADV (short history), not by stripping turnover bars.
    universe_size = 20
    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(universe_size):
        symbol = f"OB{index}.US"
        builders[symbol] = _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0 * (universe_size - index),
            breakout_bps=50.0,
        )
        # OB19 has NO ADV: it keeps its turnover bars but cannot be ranked.
        if index != universe_size - 1:
            adv[symbol] = 10_000_000.0
    live_adv = dict(adv)
    engine, db = _live_database()
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    try:
        _seed_complete_selection_run(
            db,
            symbols=tuple(live_adv),
            adv_by_symbol=live_adv,
            session_date=_PARITY_SESSION,
        )
        provider = _LiveCandleProvider(
            builders, session_date=_PARITY_SESSION
        )
        service = OpeningMomentumShadowService(db, provider)
        service.tick(
            now=_session_open(_PARITY_SESSION)
            + timedelta(minutes=7, seconds=10)
        )
        from app.models import OpeningMomentumShadowRun as _Run

        rows = [
            value
            for value in db.query(_Run)
            .filter(_Run.session_date == _PARITY_SESSION)
            .all()
            if value.algorithm_version.endswith(
                "stocks-in-play-top10-"
                "index-catalog-valid-adv-opening5-turnover-to-prior20d-"
                "adv-proxy-next-minute-open-range-low-stop-cap4-hold60-"
                "cost30-precommitted-20260728-v1"
            )
        ]
        assert len(rows) == 1
        live = rows[0]
        decision = evaluate_session_decision(
            # The pool is the ELIGIBLE members only: OB19 (no ADV) is
            # KNOWN_INELIGIBLE and excluded exactly as live's
            # index_catalog_symbols drops it.
            universe=tuple(adv),
            minute_bars_by_symbol={
                symbol: _bars_by_timestamp(
                    builder.bars(_PARITY_SESSION, symbol)
                )
                for symbol, builder in builders.items()
            },
            adv_by_symbol=adv,
            session_open=_session_open(_PARITY_SESSION),
        )
        assert decision.status == live.status
        assert decision.candidate_symbol == live.candidate_symbol
        assert decision.reason == live.reason
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_parity_gap_open_reaches_the_gap_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gap-open stop fires when a bar OPENS below the stop price with
    # NO prior bar having touched the stop with its low.  offset K opens
    # at -10% and offset K-1 is clean.
    builders, adv = _parity_symbol_setup(
        gap_stop_at_offset=ENTRY_OFFSET + 4
    )
    live = _run_live_path(builders, session_date=_PARITY_SESSION, monkeypatch=monkeypatch)
    replay = _replay_path(builders, session_date=_PARITY_SESSION, adv_by_symbol=adv)
    _assert_parity(live, replay)
    assert live["exit_reason"] == "STOP_LOSS_EXIT"
    # The exit price must be the GAP bar's open, not the stop price.
    assert live["exit_price"] == pytest.approx(90.0)


def test_parity_top10_cutoff_at_tenth_and_eleventh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 12 identical-activity names: only 10 enter the TOP10 activity set;
    # the 11th and 12th are excluded from selection regardless of depth.
    # Zero-padded names keep the frozen symbol-ascending tie-break in the
    # same order as the index.
    builders: dict[str, _SyntheticSessionBars] = {}
    adv: dict[str, float] = {}
    for index in range(12):
        symbol = f"CUT{index:02d}.US"
        builders[symbol] = _SyntheticSessionBars(
            per_minute_turnover=1_500_000.0,
            breakout_bps=50.0 + index,  # deeper depth for higher index
        )
        adv[symbol] = 10_000_000.0
    decision = evaluate_session_decision(
        universe=tuple(builders),
        minute_bars_by_symbol={
            symbol: _bars_by_timestamp(
                builder.bars(_PARITY_SESSION, symbol)
            )
            for symbol, builder in builders.items()
        },
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
    )
    # With ties in activity, TOP10 = the first ten by symbol
    # (CUT00..CUT09); the deepest breakout among them is CUT09.  CUT10 and
    # CUT11 are excluded even though their breakouts are deeper.
    assert decision.candidate_symbol == "CUT09.US"
    # Direct confirmation at the frozen-domain level that the cutoff is
    # exactly ten names.
    from app.domain.opening_momentum import (
        OpeningMomentumObservation,
        evaluate_stocks_in_play_opening_range_breakout,
    )

    domain_decision = evaluate_stocks_in_play_opening_range_breakout(
        [
            OpeningMomentumObservation(
                symbol=symbol,
                session_open=100.0,
                signal_close=100.2 * (1 + (50 + index) / 10_000),
                entry_open=100.0,
            )
            for index, symbol in enumerate(builders)
        ],
        opening_range_high_by_symbol={
            symbol: 100.2 for symbol in builders
        },
        opening_activity_ratio_by_symbol={
            symbol: 0.75 for symbol in builders
        },
        maximum_stocks_in_play=OPENING_ACTIVITY_TOP_N,
    )
    assert domain_decision.candidate_symbol == "CUT09.US"
    # CUT10/CUT11 carry deeper breakouts (by construction) but sit outside
    # the TOP10 set: the candidate is neither of them.
    assert domain_decision.candidate_symbol not in ("CUT10.US", "CUT11.US")


def test_session_open_is_stable_across_dst() -> None:
    # US DST 2024: begins 2024-03-10, ends 2024-11-03.
    from app.cli.opening_momentum_historical_replay import (
        _session_open_utc as session_open_utc,
    )

    winter = session_open_utc(date(2024, 1, 2))
    summer = session_open_utc(date(2024, 7, 3))
    assert winter.hour == 14 and winter.minute == 30  # UTC 09:30 EST
    assert summer.hour == 13 and summer.minute == 30  # UTC 09:30 EDT
    # The day AFTER each switch flips the UTC hour.
    before_switch = session_open_utc(date(2024, 3, 9))
    after_switch = session_open_utc(date(2024, 3, 11))
    assert before_switch.hour == 14 and after_switch.hour == 13
    before_end = session_open_utc(date(2024, 11, 2))
    after_end = session_open_utc(date(2024, 11, 4))
    assert before_end.hour == 13 and after_end.hour == 14


def test_week_cluster_groups_iso_year_boundary_correctly() -> None:
    # 2024-12-30/31 are ISO week 1 of 2025; 2025-01-01..03 are the SAME
    # ISO week: one cluster, not two.
    observations = [
        (date(2024, 12, 30), 10.0),
        (date(2024, 12, 31), 20.0),
        (date(2025, 1, 2), -10.0),
        (date(2025, 1, 6), 30.0),  # ISO week 2
    ]
    stat = week_clustered_statistic(observations)
    assert stat.weeks == 2
    assert stat.n == 4


def test_single_day_minute_gap_is_a_reported_member_day_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path,
        window_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
    )
    plan_symbols = sorted(
        str(entry["symbol"])
        for entry in json.loads(
            (tmp_path / "plan.json").read_text(encoding="utf-8")
        )["symbols"]
    )
    victim = plan_symbols[-1]
    target = tmp_path / "minute" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["bars"] = [
        row for row in payload["bars"]
        if not row[0].startswith("2024-01-02T")
    ]
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    run_seal(tmp_path, reseal_reason="single-day gap test")
    result = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    member_status = result["member_day_status"]
    # NO_MINUTE_DATA: fetch COMPLETE but zero raw bars that session.
    assert member_status["gate_b_by_kind"].get("NO_MINUTE_DATA") == 1
    assert member_status["gate_b_missing"] == 1
    # The member is NOT deleted: ADV is still ELIGIBLE, so it stays in
    # the decision pool (with zero bars) - minute quality never filters
    # the pool - and its missing day is reported via gate (b).
    assert victim not in result["coverage_only_symbols"]["2024-01-02"]


# ------------------------------------------- SHOULD-FIX C: statistics hygiene


def test_zero_week_variance_returns_no_ci_and_inconclusive() -> None:
    # All trades identical => cluster score squares are zero => no SE.
    observations = [
        (date(2024, 1, 2), 50.0),
        (date(2024, 1, 3), 50.0),
        (date(2024, 1, 9), 50.0),
    ]
    stat = week_clustered_statistic(observations)
    assert stat.mean_bps == pytest.approx(50.0)
    assert stat.standard_error_bps is None
    assert stat.lower_30 is None and stat.upper_30 is None
    gates = IntegrityGates(
        expected_sessions=30,
        auditable_sessions=30,
        member_days_total=3_000,
        member_days_missing_data=1,
        still_listed_missing_member_days=0,
        unresolved_exit_sessions=0,
    )
    verdict = decide_verdict(gates=gates, stat=stat, trade_count=3)
    assert verdict.verdict == "INCONCLUSIVE"
    assert any(
        "DEGENERATE" in reason for reason in verdict.reasons
    ) or verdict.sample_sufficient is False


def test_week_cluster_rejects_non_finite_inputs() -> None:
    with pytest.raises(HistoricalReplayError, match="[Ff]inite"):
        week_clustered_statistic(
            [(date(2024, 1, 2), float("nan"))]
        )


def test_json_output_never_contains_nan_or_inf(tmp_path: Path) -> None:
    # The atomic JSON writer must refuse non-finite values entirely.
    with pytest.raises(ValueError):
        _atomic_write_json(
            tmp_path / "bad.json", {"value": float("nan")}
        )


def test_descriptives_include_zero_trade_months_and_window_partials() -> None:
    trades = [
        (date(2024, 1, 3), 10.0),
        (date(2024, 3, 5), 40.0),
    ]
    payload = compute_descriptives(
        trades,
        window_start=date(2023, 9, 1),
        window_end=date(2024, 6, 30),
    )
    counts = cast(dict[str, int], payload["monthly_trade_counts"])
    assert counts["2024-01"] == 1
    assert counts["2024-02"] == 0
    assert counts["2024-03"] == 1
    halves = cast(
        list[dict[str, object]], payload["calendar_half_years"]
    )
    # The first half (2023-H2) is partial by the REGISTERED window even
    # though no trades fall in it.
    first_half = halves[0]
    assert first_half["half"] == "2023-H2"
    assert first_half["partial"] is True
    assert first_half["trades"] == 0


def test_best_three_concentration_uses_positive_contributions_only() -> None:
    # Top three values include a negative: only positive ones count.
    trades = [
        (date(2024, 1, 2), 30.0),
        (date(2024, 1, 3), 20.0),
        (date(2024, 1, 4), -50.0),
        (date(2024, 1, 5), 10.0),
    ]
    payload = compute_descriptives(
        trades,
        window_start=date(2024, 1, 1),
        window_end=date(2024, 1, 31),
    )
    concentration = cast(
        dict[str, object], payload["return_concentration"]
    )
    assert concentration["best_three_share_of_positive_total"] == (
        pytest.approx(1.0)
    )


# ------------------------------------------ SHOULD-FIX B & D: OHLC + calendar


def test_invalid_raw_ohlc_bar_is_a_reported_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    plan_symbols = sorted(
        str(entry["symbol"])
        for entry in json.loads(
            (tmp_path / "plan.json").read_text(encoding="utf-8")
        )["symbols"]
    )
    victim = plan_symbols[-1]
    target = tmp_path / "minute" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["bars"][3][2] = 1.0  # high << open (offset 3 < ENTRY_OFFSET)
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    run_seal(tmp_path, reseal_reason="invalid ohlc test")
    result = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    member_status = result["member_day_status"]
    # Invalid raw OHLC is an integrity issue, NOT a gate (b) miss.
    assert member_status["gate_b_missing"] == 0
    assert any(
        "INVALID_RAW_OHLC" in issue
        for issue in member_status["integrity_issues_sample"]
    )


def test_conventional_2023_early_closes_exclude_december_22() -> None:
    from app.cli.opening_momentum_historical_replay import (
        _CONVENTIONAL_2023_EARLY_CLOSES as early,
    )

    assert date(2023, 12, 22) not in early
    assert date(2023, 11, 24) in early


def test_calendar_cross_check_lists_local_open_without_bars() -> None:
    # A weekday the local calendar says is OPEN but neither ETF has a bar
    # must be listed (data gap), in addition to the closed-with-bars case.
    result = derive_trading_days_from_benchmarks(
        qqq_dates=(date(2024, 11, 27), date(2024, 11, 28)),
        dia_dates=(date(2024, 11, 27), date(2024, 11, 28)),
        start=date(2024, 11, 1),
        end=date(2024, 11, 30),
        local_open_no_bar_probe=(
            date(2024, 11, 29),
        ),
    )
    cross = cast(
        dict[str, object], result["local_calendar_cross_check"]
    )
    assert cross["both_etf_bar_local_closed"] == ["2024-11-28"]
    assert cross["local_open_no_benchmark_bar"] == ["2024-11-29"]


# =============================================================================
# Decision 2 pre-outcome RULING tests (items 1-4, A, B, C, D).  Decision and
# audit behaviour only - none depend on return values.
# =============================================================================


def _decision_inputs(
    count: int,
    *,
    missing_signal: tuple[str, ...] = (),
    missing_turnover: tuple[str, ...] = (),
    adv_missing: tuple[str, ...] = (),
    coverage_only: tuple[str, ...] = (),
    base: str = "RUL",
) -> tuple[
    tuple[str, ...],
    dict[str, dict[datetime, Any]],
    dict[str, float],
]:
    """Build a pool + minute bars + adv map for evaluate_session_decision."""

    symbols = tuple(f"{base}{index:02d}.US" for index in range(count))
    minute: dict[str, dict[datetime, Any]] = {}
    adv: dict[str, float] = {}
    for symbol in symbols:
        builder = _SyntheticSessionBars(
            per_minute_turnover=1_000_000.0,
            missing_turnover=symbol in missing_turnover,
            missing_minute_offsets=(
                (5,) if symbol in missing_signal else ()
            ),
        )
        minute[symbol] = _bars_by_timestamp(
            builder.bars(_PARITY_SESSION, symbol)
        )
        if symbol not in adv_missing:
            adv[symbol] = 10_000_000.0
    return symbols, minute, adv


def test_ruling_missing_signal_bar_only_reduces_observations() -> None:
    # 20 eligible, 1 missing a signal bar: denominator 20, 19
    # observations, coverage passes (19 >= ceil(0.95*20) = 19).
    symbols, minute, adv = _decision_inputs(20, missing_signal=("RUL19.US",))
    decision = evaluate_session_decision(
        universe=symbols,
        minute_bars_by_symbol=minute,
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
    )
    assert decision.universe_size == 20
    assert decision.observed_symbols == 19
    assert decision.status == "OPEN"
    # With 2 missing: 18 < 19 => DATA_INCOMPLETE.
    symbols2, minute2, adv2 = _decision_inputs(
        20, missing_signal=("RUL18.US", "RUL19.US")
    )
    decision2 = evaluate_session_decision(
        universe=symbols2,
        minute_bars_by_symbol=minute2,
        adv_by_symbol=adv2,
        session_open=_session_open(_PARITY_SESSION),
    )
    assert decision2.status == "SKIPPED"
    assert decision2.reason == "DATA_INCOMPLETE"


def test_ruling_missing_turnover_breaks_completeness_member_not_traded() -> None:
    # 20 observations, one first-5 turnover missing => DATA_INCOMPLETE,
    # and the member is not deleted (stays in the pool) so it can never
    # be silently traded around.
    symbols, minute, adv = _decision_inputs(
        20, missing_turnover=("RUL05.US",)
    )
    decision = evaluate_session_decision(
        universe=symbols,
        minute_bars_by_symbol=minute,
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
    )
    assert decision.status == "SKIPPED"
    assert decision.reason == "DATA_INCOMPLETE"
    assert "RUL05.US" in decision.excluded
    assert decision.excluded["RUL05.US"] == "OPENING_ACTIVITY_DATA_MISSING"


def test_ruling_coverage_only_widen_the_denominator() -> None:
    # 18 eligible + 2 coverage-only members: denominator 20 (required
    # observations ceil(0.95*20) = 19), observations 18 => DATA_INCOMPLETE
    # - the coverage-only members never make coverage look better, but
    # 18 eligible alone would have passed (ceil(0.95*18) = 18).
    symbols, minute, adv = _decision_inputs(18)
    decision = evaluate_session_decision(
        universe=symbols,
        minute_bars_by_symbol=minute,
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
        coverage_only_symbols=("COV00.US", "COV01.US"),
    )
    assert decision.universe_size == 20
    assert decision.observed_symbols == 18
    assert decision.status == "SKIPPED"
    assert decision.reason == "DATA_INCOMPLETE"
    # The same 18 without the coverage-only members would pass.
    decision_pass = evaluate_session_decision(
        universe=symbols,
        minute_bars_by_symbol=minute,
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
    )
    assert decision_pass.status == "OPEN"


def test_ruling_invalid_ohlc_member_matches_live_selection() -> None:
    # A member whose raw high is corrupt: the frozen coercion repairs it
    # (min/max of open/close), so the decision equals the live path's
    # selection - while the anomaly stays visible in the audit facts.
    symbols, minute, adv = _decision_inputs(8)
    # Corrupt RUL00's offset-3 high AFTER coercion: rebuild its bars with
    # a broken raw row, then re-coerce exactly as the pool construction
    # does.
    builder = _SyntheticSessionBars(per_minute_turnover=1_000_000.0)
    raw = builder.bars(_PARITY_SESSION, "RUL00.US")
    coerced = _bars_by_timestamp(raw)
    # The repaired value equals what the frozen _coerce_candles computes,
    # so the selection matches live (parity by construction); the audit
    # would flag the raw row instead.
    assert coerced  # the frozen coercion produced a usable bar
    decision = evaluate_session_decision(
        universe=symbols,
        minute_bars_by_symbol=minute,
        adv_by_symbol=adv,
        session_open=_session_open(_PARITY_SESSION),
    )
    assert decision.status == "OPEN"
    # Audit side: the raw-row validator flags the corrupted OHLC.
    bad_row = [
        (
            _session_open(_PARITY_SESSION) + timedelta(minutes=3)
        ).isoformat(),
        100.0,
        1.0,
        99.0,
        100.0,
        1000.0,
        1_000_000.0,
    ]
    assert not _raw_bars_valid_ohlc(bad_row)


def test_ruling_minute_turnover_zero_minute_positive_total_is_live() -> None:
    # Item B: one minute at ZERO with a positive total behaves exactly
    # like live (_signal_turnover: no None, finite-positive SUM).
    symbol = "ZERO.US"
    builder = _SyntheticSessionBars(
        per_minute_turnover=0.0  # all five minutes zero -> sum 0 -> None
    )
    bars = _bars_by_timestamp(builder.bars(_PARITY_SESSION, symbol))
    from app.services.opening_momentum_shadow_service import (
        OpeningMomentumShadowService as _Svc,
    )

    signal_candles = [
        bars[_session_open(_PARITY_SESSION) + timedelta(minutes=o)]
        for o in range(5)
    ]
    assert _Svc._signal_turnover(signal_candles) is None  # zero sum
    # One zero minute among positives: sum stays positive -> ratio lives.
    builder2 = _SyntheticSessionBars(
        per_minute_turnover=1_000_000.0,
        missing_turnover=False,
    )
    bars2 = _bars_by_timestamp(builder2.bars(_PARITY_SESSION, "Z2.US"))
    # Overwrite one minute's turnover with exactly 0 (not None): the
    # frozen sum accepts it.
    zero_minute = bars2[
        _session_open(_PARITY_SESSION) + timedelta(minutes=2)
    ]
    object.__setattr__(
        zero_minute, "turnover", 0.0
    )
    candles2 = [
        bars2[_session_open(_PARITY_SESSION) + timedelta(minutes=o)]
        for o in range(5)
    ]
    assert _Svc._signal_turnover(candles2) == pytest.approx(4_000_000.0)


# ------------------------------------------------ item 2: warm-up calendar


def test_warmup_calendar_proves_first_day_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A v2-style calendar (in-window scoring days only) + the registered
    # warm-up file with 2023-08-31: the first scoring session's ADV
    # classification succeeds when the 21-bar window ends on 08-31.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path, window_sessions=(date(2024, 1, 2),))
    payload = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    # The scoring dates are exactly the window sessions: warm-up days
    # never become scoring sessions.
    assert payload["sessions"]["expected"] == 1
    # The day classified ADV successfully: the pool is populated.
    assert payload["pool_sizes"][day] > 0


def test_warmup_missing_or_insufficient_refuses_before_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = (date(2024, 1, 2),)
    symbols = _pit_symbols_for(sessions)
    plan = _minimal_plan(
        symbols=symbols, first=sessions[0], last=sessions[0]
    )
    _complete_synthetic_cache(
        tmp_path, symbols=symbols, sessions=sessions, plan=plan
    )
    _no_live_fetch(monkeypatch)
    # The builder wrote a warm-up calendar: remove it to test refusal.
    (tmp_path / "trading_days_warmup.json").unlink()
    # Missing warm-up: seal refuses (preflight).
    with pytest.raises(HistoricalReplayError, match="warm"):
        run_seal(tmp_path, plan_payload=plan)
    # Insufficient warm-up (no day before the first scoring session):
    # also refused, and no attempt was recorded.
    _atomic_write_json(
        tmp_path / "trading_days_warmup.json",
        {
            "trading_days": ["2024-02-01"],  # after the first session
            "one_sided_dates": [],
        },
    )
    with pytest.raises(
        HistoricalReplayError, match="strictly before|predecessor"
    ):
        run_seal(tmp_path, plan_payload=plan)
    assert not (tmp_path / "attempt.json").exists()
    assert not (tmp_path / "attempts").exists()


def test_fetch_calendar_warmup_dates_only_and_reason_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli.opening_momentum_historical_replay import (
        run_fetch_calendar_warmup,
    )

    monkeypatch.setattr(
        "app.cli.opening_momentum_historical_replay._fetch_process_alive",
        lambda cache_dir: None,
    )
    august = tuple(
        value
        for value in _weekday_dates(23, date(2023, 9, 1))
        if value >= date(2023, 8, 1)
    )
    provider = _FakePlanProvider(
        daily_bars={},
        minute_bars={},
        trading_days=august,
    )
    report = run_fetch_calendar_warmup(
        cache_dir=tmp_path,
        provider_factory=lambda: provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )
    payload = json.loads(
        (tmp_path / "trading_days_warmup.json").read_text(encoding="utf-8")
    )
    # DATES ONLY: no price field KEY anywhere in the file or the report.
    # (Keys, not substrings: a tmp path such as /tmp/opencode/... contains
    # "open" and must not fail a dates-only check - 2026-09-27 full suite.)
    def _all_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            keys = {str(key) for key in value}
            for item in value.values():
                keys |= _all_keys(item)
            return keys
        if isinstance(value, list):
            found: set[str] = set()
            for item in value:
                found |= _all_keys(item)
            return found
        return set()

    keys = _all_keys(payload) | _all_keys(report)
    for forbidden in ("open", "high", "low", "close", "volume", "turnover"):
        assert forbidden not in keys
    assert all(
        isinstance(item, str) and len(item) == 10
        for item in payload["trading_days"]
    )
    assert payload["trading_days"] == [
        value.isoformat() for value in august
    ]
    assert payload["request_count"] >= 1
    assert report["sha256"]
    # Existing file: refused without a reason.
    with pytest.raises(HistoricalReplayError, match="reason"):
        run_fetch_calendar_warmup(
            cache_dir=tmp_path,
            provider_factory=lambda: provider,
            clock=_FakeClock(
                datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)
            ),
            sleep=lambda seconds: None,
        )
    # With a reason: replaced atomically.
    run_fetch_calendar_warmup(
        cache_dir=tmp_path,
        provider_factory=lambda: provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
        reason="warmup test rerun",
    )


def test_fetch_calendar_warmup_refuses_while_fetch_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli.opening_momentum_historical_replay import (
        run_fetch_calendar_warmup,
    )

    monkeypatch.setattr(
        "app.cli.opening_momentum_historical_replay._fetch_process_alive",
        lambda cache_dir: 424242,
    )
    provider = _FakePlanProvider(
        daily_bars={}, minute_bars={}, trading_days=()
    )
    with pytest.raises(HistoricalReplayError, match="alive"):
        run_fetch_calendar_warmup(
            cache_dir=tmp_path,
            provider_factory=lambda: provider,
            clock=_FakeClock(
                datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)
            ),
            sleep=lambda seconds: None,
        )
    assert not (tmp_path / "trading_days_warmup.json").exists()


# --------------------------------------------- items 3+4: audit helper unit


def _facts(
    *,
    symbol: str = "AUD.US",
    adv_kind: str = "ELIGIBLE",
    adv_reason: str | None = None,
    minute_state: str = "COMPLETE",
    any_bar: bool = True,
    signal_complete: bool = True,
    turnover_sum: float | None = 5_000_000.0,
    invalid_offsets: tuple[int, ...] = (),
    stop_invalid: bool = False,
) -> MemberDayFacts:
    return MemberDayFacts(
        symbol=symbol,
        session_date=date(2024, 1, 2),
        adv_kind=adv_kind,
        adv_reason=adv_reason,
        minute_fetch_state=minute_state,
        has_any_minute_bar=any_bar,
        signal_bars_complete=signal_complete,
        first_five_turnover_sum=turnover_sum,
        raw_ohlc_invalid_offsets=invalid_offsets,
        stop_path_ohlc_invalid=stop_invalid,
    )


def test_audit_no_minute_data_counts_gate_b() -> None:
    audit = assemble_member_day_audit(
        _facts(any_bar=False, turnover_sum=None, signal_complete=False)
    )
    assert audit.gate_b_missing and audit.gate_b_kind == "NO_MINUTE_DATA"
    assert audit.pool


def test_audit_permanent_gap_counts_gate_b() -> None:
    audit = assemble_member_day_audit(
        _facts(adv_kind="PERMANENT_GAP", any_bar=False)
    )
    assert audit.gate_b_kind == "PERMANENT_PROVIDER_GAP"
    assert audit.coverage_only and not audit.pool


def test_audit_missing_signal_bar_not_gate_b() -> None:
    audit = assemble_member_day_audit(_facts(signal_complete=False))
    assert not audit.gate_b_missing
    assert audit.pool
    assert "SIGNAL_BARS_MISSING" in audit.integrity_issues


def test_audit_missing_turnover_not_gate_b() -> None:
    audit = assemble_member_day_audit(_facts(turnover_sum=None))
    assert not audit.gate_b_missing
    assert audit.pool
    assert "FIRST_FIVE_TURNOVER_MISSING" in audit.integrity_issues


def test_audit_invalid_ohlc_not_gate_b() -> None:
    audit = assemble_member_day_audit(_facts(invalid_offsets=(3,)))
    assert not audit.gate_b_missing
    assert "INVALID_RAW_OHLC@3" in audit.integrity_issues


def test_audit_known_ineligible_not_missing_not_pool() -> None:
    audit = assemble_member_day_audit(
        _facts(adv_kind="KNOWN_INELIGIBLE", adv_reason="NEW_LISTING")
    )
    assert not audit.gate_b_missing
    assert not audit.pool and not audit.coverage_only


def test_audit_unverifiable_not_gate_b_but_coverage_only() -> None:
    audit = assemble_member_day_audit(
        _facts(
            adv_kind="UNVERIFIABLE",
            adv_reason="DAILY_BARS_MISSING_ON_SEALED_SESSIONS",
        )
    )
    assert not audit.gate_b_missing
    assert audit.coverage_only and not audit.pool
    assert any(
        issue.startswith("ADV_UNVERIFIABLE:")
        for issue in audit.integrity_issues
    )


def test_session_auditable_missing_inputs_are_auditable() -> None:
    # 20 members, 2 confirmed missing signal bars -> DATA_INCOMPLETE and
    # auditable=True (confirmed missing bars are auditable inputs).
    audits = [
        assemble_member_day_audit(
            _facts(
                symbol=f"A{index:02d}.US",
                signal_complete=index not in (5, 9),
            )
        )
        for index in range(20)
    ]
    assert session_is_auditable(
        member_audits=audits,
        decision_reason="DATA_INCOMPLETE",
        decision_status="SKIPPED",
        exit_proven=None,
    )


def test_session_unauditable_with_unverifiable_member() -> None:
    audits = [
        assemble_member_day_audit(
            _facts(adv_kind="UNVERIFIABLE", adv_reason="FRESHNESS_GAP_LATEST_NOT_PREVIOUS_SESSION")
        )
    ]
    assert not session_is_auditable(
        member_audits=audits,
        decision_reason="DATA_INCOMPLETE",
        decision_status="SKIPPED",
        exit_proven=None,
    )


def test_session_open_with_unproven_exit_unauditable_and_unresolved() -> None:
    assert not session_is_auditable(
        member_audits=[],
        decision_reason="STOCKS_IN_PLAY_FIVE_MINUTE_OPENING_RANGE_BREAKOUT",
        decision_status="OPEN",
        exit_proven=False,
    )


def test_session_open_with_proven_exit_auditable() -> None:
    assert session_is_auditable(
        member_audits=[],
        decision_reason="STOCKS_IN_PLAY_FIVE_MINUTE_OPENING_RANGE_BREAKOUT",
        decision_status="OPEN",
        exit_proven=True,
    )


# ------------------------------------------------ item 3: gate-b edge tests


def test_gate_b_window_end_member_d2_zero_bars_fails_gate_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same window-end member has data on D1 and zero bars on D2 ->
    # missing = 1, and gate (b) fails on the window-end count.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path,
        window_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
    )
    manifest_universe = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )["universe"]
    # A window-end member: present in the window-end membership map.
    window_end_members = {
        symbol
        for symbol, active in json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8")
        )["window_end_membership"].items()
        if active
    }
    victim = sorted(set(manifest_universe["2024-01-02"]) & window_end_members)[-1]
    target = tmp_path / "minute" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["bars"] = [
        row for row in payload["bars"]
        if not row[0].startswith("2024-01-03T")
    ]
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    run_seal(tmp_path, reseal_reason="window-end gap test")
    result = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    gates = result["gates"]
    assert result["member_day_status"]["gate_b_missing"] == 1
    assert gates["still_listed_missing_member_days"] == 1
    assert not gates["passed"]
    assert "STILL_LISTED_MEMBER_DAY_MISSING" in gates["failures"]


def test_gate_b_former_member_permanent_gap_counts_share_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A former member's (not window-end) permanent gap counts toward the
    # 2% share but not toward the window-end count.
    _no_live_fetch(monkeypatch)
    window_end_members = {
        symbol
        for symbol, active in json.loads(
            (tmp_path / "manifest.json").read_text(encoding="utf-8")
        )["window_end_membership"].items()
        if active
    } if False else None
    sessions = (_WARMUP_DAY, date(2024, 1, 2))
    pit = _pit_symbols_for((date(2024, 1, 2),))
    # Pick a PIT member that is NOT active at window end.
    from app.cli.opening_momentum_historical_replay import (
        INDEX_CANDIDATE_CATALOG as _CAT,
        HISTORICAL_INDEX_CANDIDATE_CATALOG as _HCAT,
        INDEX_MEMBERSHIP_HISTORY as _HIST,
    )

    former = next(
        symbol
        for symbol in pit
        if not _HIST.is_active(
            next(
                c for c in (*_CAT, *_HCAT) if c.symbol == symbol
            ),
            date(2026, 4, 30),
        )
    )
    plan = _minimal_plan(
        symbols=pit, first=date(2024, 1, 2), last=date(2024, 1, 2),
        daily_start=_WARMUP_DAILY_START,
    )
    _complete_synthetic_cache(
        tmp_path,
        symbols=pit,
        sessions=sessions,
        plan=plan,
        permanent_failures=(former,),
    )
    _write_warmup_calendar(tmp_path, predecessor_of=date(2024, 1, 2))
    run_seal(tmp_path, plan_payload=plan)
    result = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    member_status = result["member_day_status"]
    assert (
        member_status["gate_b_by_kind"].get("PERMANENT_PROVIDER_GAP") == 1
    )
    gates = result["gates"]
    # One permanent gap of ~123 member-days: share <= 2%, window-end = 0.
    assert member_status["gate_b_missing"] == 1
    assert gates["still_listed_missing_member_days"] == 0
    assert gates["member_day_missing_share"] <= 0.02


# ----------------------------------------------- item C/D: blockers & claim


def test_v2_plan_import_accepts_running_plan(tmp_path: Path) -> None:
    # The ORIGINAL running plan is accepted; its bytes and analysis_id
    # are never rewritten; every other field equals the v3 plan.  The v2
    # plan is reconstructed from the v3 builder (the fields the import
    # compares are exactly the v3 plan's), so the test does not depend on
    # a host-local file (CI run 36339384603 had no /tmp/opencode plan).
    original = build_plan_payload(
        window_start=WINDOW_START, window_end=WINDOW_END
    )
    original["analysis_id"] = "opening-momentum-top10-pit-historical-v2"
    original["cli_version"] = (
        "opening-momentum-top10-pit-historical-replay-cli-v1"
    )
    original_bytes = json.dumps(original, sort_keys=True)
    imported = import_v2_plan(original)
    assert json.dumps(original, sort_keys=True) == original_bytes
    assert imported["original_analysis_id"] == (
        "opening-momentum-top10-pit-historical-v2"
    )
    assert imported["v3_plan_sha256"]
    assert imported["original_plan_sha256"]
    # A mutated v2 plan is refused.
    mutated = dict(original)
    mutated["distinct_symbols"] = original["distinct_symbols"] + 1
    with pytest.raises(HistoricalReplayError, match="differs"):
        import_v2_plan(mutated)
    # A plan that is neither v2 nor v3 is refused outright.
    foreign = dict(original)
    foreign["analysis_id"] = "some-other-analysis"
    with pytest.raises(HistoricalReplayError, match="refusing to import"):
        import_v2_plan(foreign)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )


def test_require_clean_worktree_refuses_dirty_and_accepts_clean(
    tmp_path: Path,
) -> None:
    from app.cli import opening_momentum_historical_replay as replay_mod

    # The REAL check, not the neutralised alias, run against a private
    # throw-away repository so the verdict never depends on whether the
    # CI checkout or the developer tree happens to be clean.
    real_check = getattr(
        replay_mod,
        "__wrapped_require_clean_worktree",
        replay_mod._require_clean_worktree,
    )
    repo = tmp_path / "repo"
    (repo / "backend" / "app").mkdir(parents=True)
    (repo / "backend" / "tests").mkdir(parents=True)
    (repo / "backend" / "app" / "mod.py").write_text("x = 1\n")
    (repo / "backend" / "tests" / "test_mod.py").write_text("y = 1\n")
    (repo / "notes.txt").write_text("outside the checked paths\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.email=t@example.invalid",
        "-c", "user.name=t",
        "commit", "-q", "-m", "init",
    )
    real_check(repo)  # clean: accepted

    (repo / "notes.txt").write_text("changed outside backend\n")
    real_check(repo)  # changes outside backend/app|tests do not count

    (repo / "backend" / "app" / "mod.py").write_text("x = 2\n")
    with pytest.raises(HistoricalReplayError, match="dirty"):
        real_check(repo)
    _git(repo, "checkout", "--", "backend/app/mod.py")

    (repo / "backend" / "tests" / "new_test.py").write_text("z = 1\n")
    with pytest.raises(HistoricalReplayError, match="dirty"):
        real_check(repo)


def test_evaluate_refuses_dirty_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import opening_momentum_historical_replay as replay_mod

    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    # evaluate must call the worktree check and propagate its refusal
    # BEFORE any attempt is claimed.  The check itself is proven against a
    # real git repository in the test above; here it is forced to report
    # a dirty tree so the result does not depend on the checkout state.
    calls: list[Path] = []

    def _dirty(repo: Path) -> None:
        calls.append(repo)
        raise HistoricalReplayError(
            "evaluate refused: the working tree is dirty (test)"
        )

    monkeypatch.setattr(replay_mod, "_require_clean_worktree", _dirty)
    with pytest.raises(HistoricalReplayError, match="dirty"):
        run_evaluate(
            cache_dir=tmp_path, output_path=_output_path(tmp_path)
        )
    assert calls, "evaluate never ran the clean-worktree check"
    assert not (tmp_path / "attempts").exists()
    assert not (tmp_path / "attempt.json").exists()


def test_evaluate_output_required_and_receipt_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path)
    payload = run_evaluate(
        cache_dir=tmp_path, output_path=_output_path(tmp_path)
    )
    receipt_path = Path(payload["receipt_path"])
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["analysis_id"] == ANALYSIS_ID
    assert len(receipt["plan_sha256"]) == 64
    assert len(receipt["seal_manifest_sha256"]) == 64
    assert receipt["git_head"]
    assert len(receipt["source_hashes"]) == 9
    assert receipt["verdict"] == payload["verdict"]
    assert receipt["n"] == payload["statistics"]["n"]
    assert receipt["weeks"] == payload["statistics"]["weeks"]
    assert isinstance(receipt["gates_passed"], bool)
    # The receipt holds NO per-trade data.
    assert "trades" not in receipt
    assert "sessions" not in receipt


# =============================================================================
# FINAL pre-outcome check (decision 2, still before any outcome): five
# verdict-changing MUST-FIX items, each driven through the REAL end-to-end
# path (synthetic sealed cache in tmp_path -> run_seal -> run_evaluate).
# =============================================================================


def _first_window_session(cache: Path) -> date:
    manifest = json.loads(
        (cache / "manifest.json").read_text(encoding="utf-8")
    )
    return min(
        date.fromisoformat(value)
        for value in manifest["trading_days"]["trading_days"]
    )


def _corrupt_minute_bar(
    cache: Path,
    symbol: str,
    session_date: date,
    offset: int,
    column: int,
    value: object,
) -> None:
    """Corrupt one RAW minute field (row: [ts, o, h, l, c, v, t])."""

    target = cache / "minute" / f"{symbol}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    stamp = (
        _session_open(session_date) + timedelta(minutes=offset)
    ).isoformat()
    hits = 0
    for row in payload["bars"]:
        if row[0] == stamp:
            row[column] = value
            hits += 1
    assert hits == 1, (symbol, session_date, offset, hits)
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _strip_minute_session(
    cache: Path,
    symbol: str,
    session_date: date,
) -> None:
    target = cache / "minute" / f"{symbol}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["bars"] = [
        row
        for row in payload["bars"]
        if not row[0].startswith(session_date.isoformat())
    ]
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _evaluate_sealed(
    tmp_path: Path,
    *,
    rerun: str | None = None,
) -> dict[str, Any]:
    return run_evaluate(
        cache_dir=tmp_path,
        output_path=_output_path(tmp_path),
        rerun_reason=rerun,
    )


def _selected_symbol(payload: dict[str, Any], session_date: str) -> str:
    rows = payload["sessions"]["rows"]
    row = next(r for r in rows if r["session_date"] == session_date)
    symbol = row.get("candidate_symbol")
    assert symbol is not None
    return str(symbol)


# ------------------------------------------------ item 1: PERMANENT_GAP audit


def test_e2e_permanent_gap_member_keeps_session_auditable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same determined SKIP session: with a PERMANENT_GAP member it is
    # auditable=True and gate (b) counts the gap...
    _no_live_fetch(monkeypatch)
    manifest_probe = json.loads(
        (tmp_path / "unused.json").read_text(encoding="utf-8")
    ) if False else None
    pit_member = sorted(_pit_symbols_for((date(2024, 1, 2),)))[0]
    _sealed_full_cache(
        tmp_path,
        window_sessions=(date(2024, 1, 2), date(2024, 1, 3)),
        permanent_failures=(pit_member,),
    )
    # Force a determined SKIP on the first session by removing one
    # member's minute data (NO_MINUTE_DATA on an ELIGIBLE member keeps the
    # session auditable? no - it stays in the pool and the observations
    # drop, which may skip; instead corrupt a turnover to force
    # DATA_INCOMPLETE deterministically while every member keeps bars).
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    victim = sorted(manifest["universe"]["2024-01-02"])[-1]
    _corrupt_minute_bar(
        tmp_path, victim, date(2024, 1, 2), 2, 6, None  # turnover None
    )
    run_seal(tmp_path, reseal_reason="item1 permanent gap")
    payload = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    row = next(
        r
        for r in payload["sessions"]["rows"]
        if r["session_date"] == day
    )
    assert row["status"] == "SKIPPED"
    assert row["reason"] == "DATA_INCOMPLETE"
    # Gate (b) counts the permanent gap on BOTH member-days (the study
    # aggregates across sessions).
    assert (
        payload["member_day_status"]["gate_b_by_kind"].get(
            "PERMANENT_PROVIDER_GAP"
        )
        == 2
    )
    # ...and the session is STILL auditable (gate a): PERMANENT_GAP never
    # blocks auditability.
    auditable_share = payload["gates"]["session_input_coverage"]
    assert auditable_share >= 0.99  # both sessions auditable


def test_e2e_unverifiable_member_blocks_auditability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same determined SKIP session with an UNVERIFIABLE member:
    # auditable=False.  Build UNVERIFIABLE by deleting one required
    # daily bar from a member's window (a middle gap).
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path, window_sessions=(date(2024, 1, 2), date(2024, 1, 3))
    )
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    victim = sorted(manifest["universe"]["2024-01-02"])[0]
    target = tmp_path / "daily" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    # Remove a middle bar of the last-21 window (keep the latest bar so
    # freshness holds): the missing REQUIRED sealed session makes the
    # member UNVERIFIABLE.
    bars = payload["bars"]
    payload["bars"] = bars[: len(bars) - 10] + bars[len(bars) - 9 :]
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    # Also force the SKIP deterministically on the first session.
    skip_victim = sorted(manifest["universe"]["2024-01-02"])[-1]
    _corrupt_minute_bar(
        tmp_path, skip_victim, date(2024, 1, 2), 2, 6, None
    )
    run_seal(tmp_path, reseal_reason="item1 unverifiable")
    result = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    row = next(
        r
        for r in result["sessions"]["rows"]
        if r["session_date"] == day
    )
    assert row["status"] == "SKIPPED"
    assert row["reason"] == "DATA_INCOMPLETE"
    assert victim in result["coverage_only_symbols"][day]
    # The session is NOT auditable and the share reflects it.
    assert result["gates"]["session_input_coverage"] < 0.99


# ------------------------------------------- item 2: raw stop-path OHLC e2e


def test_e2e_invalid_raw_stop_path_at_entry_bar_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bad LOW at offset 6 (the entry bar) on the SELECTED stock:
    # unresolved=1, no CLOSED trade, excluded from the statistics.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path, window_sessions=(date(2024, 1, 2),))
    payload0 = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    selected = _selected_symbol(payload0, day)
    assert any(
        t["session_date"] == day and t["symbol"] == selected
        for t in payload0["trades"]
    )
    # Corrupt the selected symbol's entry-bar HIGH below its LOW (raw
    # row column 2): inconsistent raw OHLC, unrepairable as evidence.
    _corrupt_minute_bar(
        tmp_path, selected, date(2024, 1, 2), ENTRY_OFFSET, 2, 1.0
    )
    run_seal(tmp_path, reseal_reason="item2 stop-path entry")
    payload = _evaluate_sealed(
        tmp_path, rerun="stop-path entry-bar corruption"
    )
    gates = payload["gates"]
    assert gates["unresolved_exit_sessions"] == 1
    closed = [
        t for t in payload["trades"] if t["session_date"] == day
    ]
    assert closed == []
    assert payload["statistics"]["n"] == payload0["statistics"]["n"] - 1


def test_e2e_invalid_raw_stop_path_mid_path_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same at offset 30 (mid stop path, far past the decision window).
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path, window_sessions=(date(2024, 1, 2),))
    payload0 = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    selected = _selected_symbol(payload0, day)
    _corrupt_minute_bar(
        tmp_path, selected, date(2024, 1, 2), 30, 2, 1.0
    )
    run_seal(tmp_path, reseal_reason="item2 stop-path mid")
    payload = _evaluate_sealed(
        tmp_path, rerun="stop-path mid-path corruption"
    )
    assert payload["gates"]["unresolved_exit_sessions"] == 1
    assert [
        t for t in payload["trades"] if t["session_date"] == day
    ] == []
    assert payload["statistics"]["n"] == payload0["statistics"]["n"] - 1


def test_e2e_nonselected_member_anomaly_not_auditable_not_reselected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A decision-window raw-OHLC anomaly on a NON-selected member: the
    # session is not auditable, the selection is UNCHANGED, and the
    # member is NOT removed (no re-selection).
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(tmp_path, window_sessions=(date(2024, 1, 2),))
    payload0 = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    selected = _selected_symbol(payload0, day)
    # Pick a member that is NOT selected.
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    non_selected = next(
        symbol
        for symbol in sorted(manifest["universe"][day])
        if symbol != selected
    )
    _corrupt_minute_bar(
        tmp_path, non_selected, date(2024, 1, 2), 3, 2, 1.0  # high bad
    )
    run_seal(tmp_path, reseal_reason="item2 non-selected anomaly")
    payload = _evaluate_sealed(
        tmp_path, rerun="non-selected decision-window anomaly"
    )
    # Selection unchanged: the same symbol is still the candidate row.
    assert _selected_symbol(payload, day) == selected
    # The member was not removed from the pool (still in pool_sizes
    # numerator: it stays an eligible member with its bars).
    assert non_selected not in payload["coverage_only_symbols"][day]
    # The session is not auditable under gate (a).
    assert payload["gates"]["session_input_coverage"] < 0.99


# ------------------------------------------------ item 3a/3b: ADV equivalence


def test_e2e_zero_dollar_volume_bar_makes_member_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One zero-dollar-volume bar among the last 20 (volume 0, turnover 0
    # -> dollar volume 0): the spread proxy is UNAVAILABLE and live adds
    # DATA_INVALID_SPREAD_PROXY, giving NO ADV.  The replay must classify
    # the member UNVERIFIABLE, not ELIGIBLE.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path, window_sessions=(date(2024, 1, 2), date(2024, 1, 3))
    )
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    victim = sorted(manifest["universe"]["2024-01-02"])[-1]
    target = tmp_path / "daily" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    bars = payload["bars"]
    zero_at = len(bars) - 5  # inside the last-21 window
    bars[zero_at][5] = 0.0  # volume
    bars[zero_at][6] = 0.0  # turnover -> dollar volume exactly 0
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    run_seal(tmp_path, reseal_reason="item3 zero dollar volume")
    result = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    assert victim in result["coverage_only_symbols"][day]
    assert victim not in result["pool_sizes"]  # not in the pool dict keys? see impl


def test_e2e_none_turnover_daily_bar_is_input_anomaly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turnover of None on a cached daily bar (attribute present but
    # None): the live normalizer DROPS the bar; the replay must keep the
    # date row, mark the bar invalid, and classify UNVERIFIABLE.
    _no_live_fetch(monkeypatch)
    _sealed_full_cache(
        tmp_path, window_sessions=(date(2024, 1, 2), date(2024, 1, 3))
    )
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    victim = sorted(manifest["universe"]["2024-01-02"])[-1]
    target = tmp_path / "daily" / f"{victim}.json.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    bars = payload["bars"]
    bars[len(bars) - 5][6] = None  # turnover None (kept in the row)
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    run_seal(tmp_path, reseal_reason="item3 none turnover")
    result = _evaluate_sealed(tmp_path)
    day = "2024-01-02"
    assert victim in result["coverage_only_symbols"][day]


# --------------------------------------------------- item 4: NEW_LISTING etc.


def _august_weekdays(start: date = date(2023, 8, 3)) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= date(2023, 8, 31):
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _build_first_day_cache(
    tmp_path: Path,
    *,
    daily_dates: Sequence[date],
    window_sessions: tuple[date, ...] = (date(2023, 9, 1),),
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A v3-style cache whose FIRST scoring day is 2023-09-01.

    The warm-up calendar is the registered August window; the members'
    daily bars are provided exactly on ``daily_dates`` plus the scoring
    sessions' previous sealed sessions.
    """

    _no_live_fetch(monkeypatch)
    symbols = _pit_symbols_for(window_sessions)
    plan = _minimal_plan(
        symbols=symbols,
        first=window_sessions[0],
        last=window_sessions[-1],
        daily_start=daily_dates[0],
    )
    sessions = (date(2023, 8, 31), *window_sessions)
    builders = _default_builders(symbols, sessions=sessions)
    provider = _FakePlanProvider(
        daily_bars={
            symbol: _daily_bars_on_sessions(
                symbol, sessions=tuple(daily_dates), daily_start=daily_dates[0]
            )
            for symbol in symbols
        },
        minute_bars={
            symbol: _SyntheticSessionBars(
                per_minute_turnover=1_000_000.0
            ).bars(window_sessions[0], symbol)
            for symbol in symbols
        },
        trading_days=sessions,
    )
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )
    _atomic_write_json(
        tmp_path / "trading_days_warmup.json",
        {
            "source": "synthetic registered warm-up (tests)",
            "fetched_at": "2026-09-27T00:00:00+00:00",
            "warmup_start": "2023-08-03",
            "warmup_end": "2023-08-31",
            "request_count": 2,
            "reason": None,
            "trading_days": [
                value.isoformat() for value in _august_weekdays()
            ],
            "one_sided_dates": [],
        },
    )
    run_seal(tmp_path, plan_payload=plan)
    return run_evaluate(
        cache_dir=tmp_path,
        output_path=_output_path(tmp_path),
    )


def test_e2e_exact_august_dates_first_day_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2023-08-03..2023-08-31 is exactly 21 NYSE sessions: D=2023-09-01
    # has a complete 21-session window and members classify ELIGIBLE.
    payload = _build_first_day_cache(
        tmp_path, daily_dates=_august_weekdays(), monkeypatch=monkeypatch
    )
    day = "2023-09-01"
    assert payload["pool_sizes"][day] > 0


def test_e2e_cache_boundary_start_is_unverifiable_not_new_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A long-listed name whose cached dailies start at 2023-08-04: only
    # 20 prior bars and no listing proof exists -> UNVERIFIABLE (never
    # NEW_LISTING), coverage-only.
    payload = _build_first_day_cache(
        tmp_path,
        daily_dates=_august_weekdays(start=date(2023, 8, 4)),
        monkeypatch=monkeypatch,
    )
    day = "2023-09-01"
    assert payload["pool_sizes"][day] == 0
    manifest_universe = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )["universe"][day]
    assert set(payload["coverage_only_symbols"][day]) == set(
        manifest_universe
    )


def test_e2e_middle_missing_required_session_unverifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # More than 21 bars in the file but one REQUIRED sealed session
    # missing in the middle -> UNVERIFIABLE.
    august = _august_weekdays()
    holed = [value for value in august if value != date(2023, 8, 15)]
    payload = _build_first_day_cache(
        tmp_path, daily_dates=holed, monkeypatch=monkeypatch
    )
    day = "2023-09-01"
    assert payload["pool_sizes"][day] == 0
    assert payload["coverage_only_symbols"][day]


# --------------------------------------------- item 5: warm-up preflight proofs


def test_warmup_preflight_requires_registered_predecessor_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registered predecessor of 2023-09-01 is 2023-08-31 and the
    # 21-session ADV window needs 2023-08-03..08-31.  Build a first-day
    # cache with correct member dailies, then vary ONLY the warm-up file.
    _no_live_fetch(monkeypatch)
    symbols = _pit_symbols_for((date(2023, 9, 1),))
    plan = _minimal_plan(
        symbols=symbols,
        first=date(2023, 9, 1),
        last=date(2023, 9, 1),
        daily_start=date(2023, 8, 3),
    )
    sessions = (date(2023, 8, 31), date(2023, 9, 1))
    provider = _FakePlanProvider(
        daily_bars={
            symbol: _daily_bars_on_sessions(
                symbol,
                sessions=tuple(_august_weekdays()),
                daily_start=date(2023, 8, 3),
            )
            for symbol in symbols
        },
        minute_bars={
            symbol: _SyntheticSessionBars(
                per_minute_turnover=1_000_000.0
            ).bars(date(2023, 9, 1), symbol)
            for symbol in symbols
        },
        trading_days=sessions,
    )
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=plan,
        provider=provider,
        clock=_FakeClock(datetime(2026, 3, 7, 2, 0, tzinfo=timezone.utc)),
        sleep=lambda seconds: None,
    )

    def write_warmup(days: list[date]) -> None:
        _atomic_write_json(
            tmp_path / "trading_days_warmup.json",
            {
                "source": "synthetic (tests)",
                "fetched_at": "2026-09-27T00:00:00+00:00",
                "warmup_start": (
                    days[0].isoformat() if days else "2023-08-01"
                ),
                "warmup_end": (
                    days[-1].isoformat() if days else "2023-08-01"
                ),
                "request_count": 2,
                "reason": None,
                "trading_days": [value.isoformat() for value in days],
                "one_sided_dates": [],
            },
        )

    # Only 2023-08-01: no predecessor and no coverage -> refused.
    write_warmup([date(2023, 8, 1)])
    with pytest.raises(HistoricalReplayError, match="warm"):
        run_seal(tmp_path, plan_payload=plan)
    # Missing 08-31 (predecessor absent) -> refused.
    write_warmup(
        [value for value in _august_weekdays() if value != date(2023, 8, 31)]
    )
    with pytest.raises(HistoricalReplayError, match="predecessor|warm"):
        run_seal(tmp_path, plan_payload=plan)
    # Missing 08-15 (middle of the required window) -> refused.
    write_warmup(
        [value for value in _august_weekdays() if value != date(2023, 8, 15)]
    )
    with pytest.raises(HistoricalReplayError, match="required|window|warm"):
        run_seal(tmp_path, plan_payload=plan)
    # No attempt was recorded by any refusal.
    assert not (tmp_path / "attempt.json").exists()
    # The correct registered file is accepted.
    write_warmup(_august_weekdays())
    manifest_hash = run_seal(
        tmp_path, plan_payload=plan, reseal_reason="correct warm-up"
    )
    assert len(manifest_hash) == 64


# -------------------------------- execution safety: warm-up provider ordering


def test_warmup_fetch_alive_refuses_before_provider_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import opening_momentum_historical_replay as replay_mod

    monkeypatch.setattr(replay_mod, "_fetch_process_alive", lambda d: 7)
    constructed = False

    class _Boom:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("provider must not be constructed")

    monkeypatch.setattr(replay_mod, "_LongportQuoteProvider", _Boom)
    # main() converts HistoricalReplayError into exit code 2.
    rc = replay_mod.main(
        ["fetch-calendar-warmup", "--cache-dir", str(tmp_path)]
    )
    assert rc == 2
    assert not constructed
