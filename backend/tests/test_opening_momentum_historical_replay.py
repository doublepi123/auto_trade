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
    WeekClusteredStat,
    _CandleView,
    _LongportQuoteProvider,
    _atomic_write_json,
    _load_status,
    _symbol_state,
    _write_bars_file,
    build_plan_payload,
    build_session_observation,
    classify_provider_error,
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
    adv = rebuild_session_adv(bars, as_of=as_of)
    assert adv is not None
    # The frozen selector averages the last 20 of >= 21 completed bars.
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
    assert rebuild_session_adv(bars, as_of=as_of) is None
    # A bar ON as_of is not a completed bar and must not enter the window.
    prior = _weekday_dates(21, as_of)
    bars = [
        _fake_daily_bar(value, close=100.0, volume=1_000.0) for value in prior
    ]
    with_today = [
        *bars,
        _fake_daily_bar(as_of, close=999.0, volume=1_000.0),
    ]
    assert rebuild_session_adv(with_today, as_of=as_of) == pytest.approx(
        100_000.0
    )


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


def test_seal_writes_manifest_and_hash(tmp_path: Path) -> None:
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
    run_fetch(
        cache_dir=tmp_path,
        plan_payload=_minimal_plan(),
        provider=provider,
        clock=clock,
        sleep=lambda seconds: None,
    )
    manifest_hash = run_seal(tmp_path)
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
    # Sealing is refused before any fetch produced trading days.
    with pytest.raises(HistoricalReplayError, match="trading_days"):
        run_seal(tmp_path / "empty")


# --------------------------------------------------------------- evaluate


def _seeded_cache(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...],
    sessions: tuple[date, ...],
    builders_by_symbol: dict[str, _SyntheticSessionBars],
    adv_per_symbol: dict[str, float],
) -> None:
    minute_dir = tmp_path / "minute"
    daily_dir = tmp_path / "daily"
    for symbol in symbols:
        minute_rows: list[FetchedBar] = []
        for session_date in sessions:
            for bar in builders_by_symbol[symbol].bars(
                session_date, symbol
            ):
                minute_rows.append(
                    FetchedBar(
                        timestamp=bar.timestamp,
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=float(bar.volume),
                        turnover=float(bar.turnover) if bar.turnover else None,
                    )
                )
        _write_bars_file(
            tmp_path,
            "minute",
            symbol,
            period="MIN_1",
            adjustment="NoAdjust",
            bars=minute_rows,
        )
        # Daily bars: 25 sessions before the first replay session, with
        # turnover chosen so the rebuilt ADV equals adv_per_symbol.
        first = sessions[0]
        prior = _weekday_dates(25, first)
        daily_rows = [
            FetchedBar(
                timestamp=_session_open(value),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000.0,
                turnover=adv_per_symbol.get(symbol, 100_000_000.0),
            )
            for value in prior
        ]
        _write_bars_file(
            tmp_path,
            "daily",
            symbol,
            period="DAY",
            adjustment="ForwardAdjust",
            bars=daily_rows,
        )
    status = _load_status(tmp_path)
    for symbol in symbols:
        state = _symbol_state(status, symbol)
        state["daily"] = {"state": "COMPLETE"}
        state["minute"] = {"state": "COMPLETE"}
    _atomic_write_json(tmp_path / "status.json", status)
    _atomic_write_json(
        tmp_path / "trading_days.json",
        {
            "trading_days": [value.isoformat() for value in sessions],
            "half_trading_days": [],
        },
    )
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "window": {"start": "2023-09-01", "end": "2026-04-30"},
        "files": [
            {
                "path": path.relative_to(tmp_path).as_posix(),
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(
                (*minute_dir.glob("*.json.gz"), *daily_dir.glob("*.json.gz"))
            )
        ],
        "trading_days": {
            "trading_days": [value.isoformat() for value in sessions],
            "half_trading_days": [],
        },
        "universe": {
            value.isoformat(): list(symbols) for value in sessions
        },
        "member_days_total": len(sessions) * len(symbols),
        "fetch_status_snapshot": status,
        "calendar_cross_check": cross_check_trading_days(sessions),
    }
    _atomic_write_json(tmp_path / "manifest.json", manifest)


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_evaluate_refuses_without_seal(tmp_path: Path) -> None:
    with pytest.raises(HistoricalReplayError, match="seal"):
        run_evaluate(
            cache_dir=tmp_path,
            output_path=tmp_path / "out.json",
        )


def test_evaluate_refuses_silent_overwrite_and_keeps_original(
    tmp_path: Path,
) -> None:
    sessions = (date(2024, 1, 2),)
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
    output = tmp_path / "out.json"
    first = run_evaluate(cache_dir=tmp_path, output_path=output)
    assert output.exists()
    with pytest.raises(HistoricalReplayError, match="rerun-reason"):
        run_evaluate(cache_dir=tmp_path, output_path=output)
    rerun = run_evaluate(
        cache_dir=tmp_path, output_path=output, rerun_reason="test rerun"
    )
    assert rerun["verdict"] == first["verdict"]
    assert output.exists()
    superseded = list(output.parent.glob("*.superseded-1.json"))
    assert len(superseded) == 1
    preserved = json.loads(superseded[0].read_text(encoding="utf-8"))
    assert preserved == first


def test_evaluate_provenance_records_hashes_and_frozen_rule(
    tmp_path: Path,
) -> None:
    sessions = (date(2024, 1, 2),)
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
        cache_dir=tmp_path, output_path=tmp_path / "out.json"
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
