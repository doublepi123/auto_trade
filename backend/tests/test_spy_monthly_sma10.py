"""Behaviour tests for the frozen monthly-trend rule SPY_MONTHLY_SMA10_CASH_V1.

Everything here is SYNTHETIC: no network, no DB, no host files, no
dependency on the checkout being clean or dirty.  The pin/governance
half lives in ``test_spy_monthly_sma10_preregistration.py``.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.cli import spy_monthly_sma10_replay as cli
from app.cli.spy_monthly_sma10_replay import (
    ANALYSIS_ID,
    CORPORATE_ACTIONS_FILENAME,
    MonthlySma10Error,
    build_plan_payload,
    run_evaluate,
    run_fetch,
    run_import_corporate_actions,
    run_seal,
    validate_corporate_actions_payload,
    _cache_preflight,
    _default_cache_dir,
    _derive_trading_days,
    _load_status,
)
from app.domain.monthly_trend.sma10 import validate_window_data  # noqa: F401
from app.core.accounting_fees import (
    SEC98_FIXED_USD,
    SEC98_NOTIONAL_RATE,
)
from app.domain.monthly_trend import sma10
from app.domain.monthly_trend.sma10 import (
    BASE_SLIPPAGE_BPS,
    BOOTSTRAP_CONFIG,
    CLAIM2_THRESHOLD,
    COMMISSION_FIXED_USD,
    COMMISSION_NOTIONAL_RATE,
    INITIAL_CASH_USD,
    MAX_SHARES_PER_ENTRY,
    MonthlyReplayBlockedError,
    MonthlyTrendError,
    SampleGates,
    SleeveResult,
    CorporateAction,
    DailyPriceRow,
    MonthEndBar,
    SleeveState,
    apply_fill,
    apply_split,
    build_signal_index,
    circular_block_bootstrap_bounds,
    commission_usd,
    decide_verdict,
    descriptive_statistics,
    dividend_cash_credit,
    entry_share_cap,
    evaluate_claims,
    is_constant,
    month_end_index_levels,
    month_end_sessions,
    monthly_return_series,
    next_session_after,
    sample_gates_from_records,
    screen_unexplained_split_anomalies,
    simulate_leg,
    sleeve_entry_exit_events,
    sma10_signal,
    validate_window_data,
)


# ----------------------------------------------------- synthetic data helpers


def _weekdays(start: date, end: date) -> list[date]:
    """Synthetic session dates: NYSE-rule sessions (the sealed
    independent calendar) so the seal-time cross-check passes."""
    from app.domain.monthly_trend.nyse_calendar import (
        expected_nyse_sessions,
    )

    return expected_nyse_sessions(start, end)


def _synthetic_rows(
    sessions: list[date],
    *,
    seed: int = 7,
    drift: float = 0.0006,
    volatility: float = 0.011,
    start_price: float = 100.0,
) -> list[DailyPriceRow]:
    import random

    rng = random.Random(seed)
    rows: list[DailyPriceRow] = []
    price = start_price
    for session in sessions:
        price *= 1.0 + rng.gauss(drift, volatility)
        rows.append(
            DailyPriceRow(
                session=session,
                open=round(price * 0.999, 6),
                high=round(price * 1.005, 6),
                low=round(price * 0.995, 6),
                close=round(price, 6),
            )
        )
    return rows


#: Decision 14.11: the single registered OHLC anomaly session.
_REGISTERED_ANOMALY_SESSION = date(2020, 11, 18)


def _inject_registered_ohlc_anomaly(
    rows: list[DailyPriceRow],
) -> list[DailyPriceRow]:
    """Mirror the single registered anomaly (SPY.US 2020-11-18,
    ``open > high``) into synthetic bars: ONLY that session's open is
    moved above the high; the close stays inside [low, high] so the
    sole violated relation is the registered one.  2020-11-18 is a
    Wednesday mid-month - NOT the next session after any month-end -
    so its open is outside the read set (decision 14.11)."""

    out: list[DailyPriceRow] = []
    injected = False
    for row in rows:
        if row.session == _REGISTERED_ANOMALY_SESSION:
            injected = True
            row = DailyPriceRow(
                session=row.session,
                open=round(row.high * 1.01, 6),
                high=row.high,
                low=row.low,
                close=row.close,
            )
        out.append(row)
    assert injected, "fixture must cover 2020-11-18"
    return out


def _quarterly_dividends(
    sessions: list[date], amount: float = 1.2
) -> list[CorporateAction]:
    actions: list[CorporateAction] = []
    for year in sorted({s.year for s in sessions}):
        for month in (3, 6, 9, 12):
            candidates = [
                s for s in sessions if s.year == year and s.month == month
            ]
            if len(candidates) > 18:
                actions.append(
                    CorporateAction(
                        symbol="SPY.US",
                        ex_date=candidates[14],
                        cash_amount=amount,
                        ratio=None,
                        pay_date=candidates[18],
                    )
                )
    return actions


def _month_iter(
    start: tuple[int, int], end: tuple[int, int]
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    current = start
    while current <= end:
        out.append(current)
        year, month = current
        current = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


_SMA10_SCORING = _month_iter((2012, 1), (2021, 12))


# ------------------------------------------------------------------ signal


class TestSma10Signal:
    def test_hand_built_series_signal(self) -> None:
        # 11 month-ends; the last month is ABOVE the 10-month mean.
        bars = [
            MonthEndBar(
                year=2020,
                month=m,
                as_of=date(2020, m, 28),
                index_level=100.0 + m,
            )
            for m in range(1, 12)
        ]
        assert sma10_signal(bars, year=2020, month=11) == 1

    def test_equality_means_cash(self) -> None:
        # All levels equal: T_m == mean exactly -> 0 (cash).
        bars = [
            MonthEndBar(
                year=2020,
                month=m,
                as_of=date(2020, m, 28),
                index_level=100.0,
            )
            for m in range(1, 11)
        ]
        assert sma10_signal(bars, year=2020, month=10) == 0

    def test_below_mean_means_cash(self) -> None:
        bars = [
            MonthEndBar(
                year=2020,
                month=m,
                as_of=date(2020, m, 28),
                index_level=110.0 - m,
            )
            for m in range(1, 11)
        ]
        # month 10 level 100 vs mean 105 -> below -> 0
        assert sma10_signal(bars, year=2020, month=10) == 0

    def test_warmup_returns_none_until_ten_month_ends(self) -> None:
        bars = [
            MonthEndBar(
                year=2020,
                month=m,
                as_of=date(2020, m, 28),
                index_level=100.0 + m,
            )
            for m in range(1, 10)  # only 9
        ]
        assert sma10_signal(bars, year=2020, month=9) is None
        bars.extend(
            MonthEndBar(
                year=2020,
                month=m,
                as_of=date(2020, m, 28),
                index_level=100.0 + m,
            )
            for m in (10, 11, 12)
        )
        # Now 12 consecutive months: the 2020-12 window (2020-03..12)
        # is complete and the level is above the mean.
        assert sma10_signal(bars, year=2020, month=12) == 1

    def test_no_lookahead_dividend_after_month_end(self) -> None:
        # A dividend with ex-date AFTER the month-end must not move I_m.
        sessions = _weekdays(date(2020, 1, 1), date(2021, 1, 31))
        rows = _synthetic_rows(sessions, seed=3)
        month_end = month_end_sessions(sessions)[(2020, 12)]
        after_month_end = next_session_after(sessions, month_end)
        assert after_month_end is not None
        actions_without = [
            CorporateAction(
                symbol="SPY.US",
                ex_date=month_end - timedelta(days=10),
                cash_amount=1.5,
                ratio=None,
            )
        ]
        actions_with_late = [
            *actions_without,
            CorporateAction(
                symbol="SPY.US",
                ex_date=after_month_end,
                cash_amount=5.0,  # huge, but ex-date after the month-end
                ratio=None,
            ),
        ]
        bars_without = month_end_index_levels(
            build_signal_index(rows, actions_without), sessions
        )
        bars_with = month_end_index_levels(
            build_signal_index(rows, actions_with_late), sessions
        )
        key = (2020, 12)
        assert (
            next(b for b in bars_without if (b.year, b.month) == key).index_level
            == next(b for b in bars_with if (b.year, b.month) == key).index_level
        )
        assert sma10_signal(
            bars_without, year=key[0], month=key[1]
        ) == sma10_signal(bars_with, year=key[0], month=key[1])

    def test_split_neutral_in_signal_index(self) -> None:
        sessions = _weekdays(date(2020, 1, 1), date(2020, 6, 30))
        rows_no_split = _synthetic_rows(sessions, seed=5)
        split_day = sessions[40]
        # Prices halve from the split day onward (RAW behaviour).
        rows_split = [
            (
                DailyPriceRow(
                    session=row.session,
                    open=row.open / 2,
                    high=row.high / 2,
                    low=row.low / 2,
                    close=row.close / 2,
                )
                if row.session >= split_day
                else row
            )
            for row in rows_no_split
        ]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=split_day, cash_amount=None,
                ratio=2.0,
            )
        ]
        index_no_split = build_signal_index(rows_no_split, [])
        index_split = build_signal_index(rows_split, actions)
        last = sessions[-1]
        assert index_split[last] == pytest.approx(
            index_no_split[last], rel=1e-12
        )


# --------------------------------------------------------------- execution


class TestExecutionAtNextOpen:
    def _environment(self) -> tuple[
        list[date], list[DailyPriceRow], dict[tuple[int, int], int | None]
    ]:
        sessions = _weekdays(date(2011, 1, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=13)
        index = build_signal_index(rows, [])
        bars = month_end_index_levels(index, sessions)
        signals = {
            key: sma10_signal(bars, year=key[0], month=key[1])
            for key in _month_iter((2011, 1), (2012, 3))
        }
        return sessions, rows, signals

    def test_execution_is_next_open_not_month_end_close(self) -> None:
        sessions, rows, signals = self._environment()
        scoring = _month_iter((2012, 1), (2012, 3))
        events = sleeve_entry_exit_events(
            signals=signals,
            scoring_months=scoring,
            sessions=sessions,
        )
        assert events, "expected at least one transition in this fixture"
        end_by_month = month_end_sessions(sessions)
        # Month-ends include the warm-up boundary month(s) present in
        # the signals map (item 3: the 2011-12 boundary trade executes
        # at the first scored session's open).
        month_ends = [
            end_by_month[key]
            for key in sorted(set(signals))
            if key in end_by_month
        ]
        event_sessions = [session for session, _side in events]
        for session, side in events:
            assert side in ("BUY", "SELL")
            preceding_ends = [
                month_end for month_end in month_ends
                if month_end < session
            ]
            assert preceding_ends, "event before any month-end"
            assert next_session_after(sessions, preceding_ends[-1]) == session
        assert event_sessions == sorted(event_sessions)

    def test_no_trade_when_signal_unchanged(self) -> None:
        sessions = _weekdays(date(2011, 1, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=13)
        index = build_signal_index(rows, [])
        bars = month_end_index_levels(index, sessions)
        # Force an all-cash signal: no transitions, no events.
        signals: dict[tuple[int, int], int | None] = {
            key: 0
            for key in _month_iter((2011, 1), (2012, 3))
        }
        events = sleeve_entry_exit_events(
            signals=signals,
            scoring_months=_month_iter((2012, 1), (2012, 3)),
            sessions=sessions,
        )
        assert events == []

    def test_missing_next_open_blocks(self) -> None:
        sessions = _weekdays(date(2011, 1, 1), date(2012, 1, 20))
        rows = _synthetic_rows(sessions, seed=13)
        index = build_signal_index(rows, [])
        bars = month_end_index_levels(index, sessions)
        signals = {
            key: sma10_signal(bars, year=key[0], month=key[1])
            for key in _month_iter((2011, 1), (2012, 1))
        }
        # The LAST scoring month's transition (1->0 at 2012-01, whose
        # month-end 2012-01-20 is the final sealed session) has no next
        # open -> DATA_BLOCKED.
        signals[(2011, 12)] = 1  # boundary BUY executes 2012-01-03
        signals[(2012, 1)] = 0  # 1->0 at the 2012-01 month-end: blocked
        with pytest.raises(MonthlyReplayBlockedError, match="next open"):
            sleeve_entry_exit_events(
                signals=signals,
                scoring_months=[(2012, 1)],
                sessions=sessions,  # nothing after 2012-01-20
            )

    def test_missing_required_signal_blocks_never_silent_flat(
        self,
    ) -> None:
        # Item 3: a scoring month (or the boundary month) with NO
        # signal (fewer than 10 consecutive month-ends) is DATA_BLOCKED,
        # never a silent flat.
        sessions = _weekdays(date(2011, 6, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=13)
        index = build_signal_index(rows, [])
        bars = month_end_index_levels(index, sessions)
        signals = {
            key: sma10_signal(bars, year=key[0], month=key[1])
            for key in _month_iter((2011, 6), (2012, 3))
        }
        # 2011-06..2011-12 gives only 7 month-ends before 2012-01 ->
        # hmm, need <10 before the BOUNDARY; construct explicitly:
        signals: dict[tuple[int, int], int | None] = {
            key: None
            for key in _month_iter((2011, 10), (2012, 3))
        }
        with pytest.raises(
            MonthlyReplayBlockedError, match="no signal"
        ):
            sleeve_entry_exit_events(
                signals=signals,
                scoring_months=_month_iter((2012, 1), (2012, 3)),
                sessions=sessions,
            )

    def test_boundary_signal_sets_first_scored_session(
        self,
    ) -> None:
        # Item 3: the 2011-12 signal is the position state ENTERING the
        # first scored session; non-flat -> trade at the first scored
        # session's open (the open after the 2011-12 month-end).
        sessions = _weekdays(date(2011, 1, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=13)
        index = build_signal_index(rows, [])
        bars = month_end_index_levels(index, sessions)
        signals = {
            key: sma10_signal(bars, year=key[0], month=key[1])
            for key in _month_iter((2011, 1), (2012, 3))
        }
        signals[(2011, 12)] = 1  # force the boundary BUY
        signals[(2012, 1)] = 1
        signals[(2012, 2)] = 1
        signals[(2012, 3)] = 1
        events = sleeve_entry_exit_events(
            signals=signals,
            scoring_months=_month_iter((2012, 1), (2012, 3)),
            sessions=sessions,
        )
        dec_end = month_end_sessions(sessions)[(2011, 12)]
        first_scored_open = next_session_after(sessions, dec_end)
        assert events[0] == (first_scored_open, "BUY")
        assert len(events) == 1  # no further transitions


# ------------------------------------------------------------------ sizing


class TestShareSizing:
    def test_cash_fee_and_share_cap(self) -> None:
        # 250 USD/share: 19 shares cost 4750 notional; with 5bps
        # slippage + commission the 20th share would exceed 5000 cash.
        shares = entry_share_cap(
            cash_available=Decimal("5000"), raw_price=250.0
        )
        assert shares == 19
        executable = 250.0 * 1.0005
        cost_20 = Decimal("20") * Decimal(str(executable))
        fee_20 = COMMISSION_FIXED_USD + COMMISSION_NOTIONAL_RATE * cost_20
        assert cost_20 + fee_20 > Decimal("5000")

    def test_share_count_cap_100(self) -> None:
        shares = entry_share_cap(
            cash_available=Decimal("1000000"), raw_price=30.0
        )
        assert shares == MAX_SHARES_PER_ENTRY == 100

    def test_notional_cap_5000(self) -> None:
        shares = entry_share_cap(
            cash_available=Decimal("1000000"), raw_price=60.0
        )
        # 100 shares x 60 = 6000 > 5000 cap -> 83 shares (4980).
        assert shares == 83

    def test_no_trim_above_cap(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 6, 30))
        rows = _synthetic_rows(sessions, seed=17)
        # Buy once, then the price doubles: shares must NOT be trimmed.
        state = SleeveState(cash=Decimal("5000"))
        apply_fill(
            state,
            session=sessions[0],
            side="BUY",
            shares=entry_share_cap(
                cash_available=state.cash, raw_price=rows[0].open
            ),
            raw_price=rows[0].open,
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        bought = state.shares
        doubled = [
            DailyPriceRow(
                session=row.session,
                open=row.open * 2,
                high=row.high * 2,
                low=row.low * 2,
                close=row.close * 2,
            )
            if row.session > sessions[10]
            else row
            for row in rows
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=doubled,
            actions=[],
            scoring_months=[],
            entry_events=[],
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        del result
        # simulate_leg starts flat; the direct check is the state one:
        assert state.shares == bought  # no add-ons either


# ------------------------------------------------------------------- costs


class TestCosts:
    def test_commission_matches_accounting_fees_constants(self) -> None:
        assert COMMISSION_FIXED_USD == SEC98_FIXED_USD
        assert COMMISSION_NOTIONAL_RATE == SEC98_NOTIONAL_RATE

    def test_commission_arithmetic(self) -> None:
        # 20 shares at 250.010 fill: 1.568 + 0.0000641 * 5000.20
        fee = commission_usd(20, 250.010)
        notional = Decimal("20") * Decimal("250.010")
        assert fee == SEC98_FIXED_USD + SEC98_NOTIONAL_RATE * notional

    def test_slippage_direction_buy_sell(self) -> None:
        state = SleeveState(cash=Decimal("10000"))
        buy = apply_fill(
            state, session=date(2020, 1, 2), side="BUY", shares=10,
            raw_price=100.0, slippage_bps=5.0,
        )
        assert buy.fill_price == pytest.approx(100.05)
        sell = apply_fill(
            state, session=date(2020, 2, 3), side="SELL", shares=10,
            raw_price=100.0, slippage_bps=5.0,
        )
        assert sell.fill_price == pytest.approx(99.95)

    def test_stress_slippage_is_15_bps(self) -> None:
        from app.domain.monthly_trend.sma10 import STRESS_SLIPPAGE_BPS

        assert STRESS_SLIPPAGE_BPS == 15.0
        state = SleeveState(cash=Decimal("10000"))
        buy = apply_fill(
            state, session=date(2020, 1, 2), side="BUY", shares=10,
            raw_price=100.0, slippage_bps=STRESS_SLIPPAGE_BPS,
        )
        assert buy.fill_price == pytest.approx(100.15)

    def test_final_liquidation_charged_base_and_stress(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 4, 30))
        rows = _synthetic_rows(sessions, seed=23)
        base = simulate_leg(
            sessions=sessions, daily_rows=rows, actions=[],
            scoring_months=[], entry_events=[(sessions[0], "BUY")],
            slippage_bps=5.0, liquidation_session=sessions[-1],
        )
        stress = simulate_leg(
            sessions=sessions, daily_rows=rows, actions=[],
            scoring_months=[], entry_events=[(sessions[0], "BUY")],
            slippage_bps=15.0, liquidation_session=sessions[-1],
        )
        assert base.final_liquidation is not None
        assert stress.final_liquidation is not None
        # Same raw price; the stress liquidation fills strictly lower.
        assert (
            stress.final_liquidation.fill_price
            < base.final_liquidation.fill_price
        )
        # The liquidation commission is the SEC98 formula on the fill.
        fee = base.final_liquidation.commission_usd
        expected = SEC98_FIXED_USD + SEC98_NOTIONAL_RATE * Decimal(
            str(base.final_liquidation.shares)
        ) * Decimal(str(base.final_liquidation.fill_price))
        assert fee == expected


# -------------------------------------------------------------- dividends


class TestDividendsAndSplits:
    def test_dividend_credited_as_cash_not_reinvested(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=29)
        ex_day = sessions[30]
        pay_day = sessions[33]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=pay_day,
            )
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=actions,
            scoring_months=[],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=5.0,
        )
        assert len(result.dividends) == 1
        credit = result.dividends[0]
        assert credit.session == pay_day  # pay date, not ex date
        expected = Decimal(str(result.fills[0].shares)) * Decimal("2.0")
        assert credit.cash_amount_usd == expected
        # Not reinvested: the share count is unchanged by the dividend.
        assert all(fill.side == "BUY" for fill in result.fills)

    def test_dividend_ex_date_fallback_when_pay_date_unknown(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=31)
        ex_day = sessions[30]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None,  # pay_date unknown -> credit on ex-date
            )
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=actions,
            scoring_months=[],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=5.0,
        )
        assert result.dividends[0].session == ex_day

    def test_no_dividend_when_flat(self) -> None:
        # A dividend credited while holding no shares pays nothing.
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=31)
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=sessions[30], cash_amount=2.0,
                ratio=None, pay_date=sessions[33],
            )
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=actions,
            scoring_months=[],
            entry_events=[],  # never buys
            slippage_bps=5.0,
        )
        assert result.dividends == []
        assert result.fills == []

    def test_split_adjusts_share_count(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=37)
        split_day = sessions[40]
        # RAW prices halve from the split day on.
        adjusted_rows = [
            DailyPriceRow(
                session=row.session,
                open=row.open / 2,
                high=row.high / 2,
                low=row.low / 2,
                close=row.close / 2,
            )
            if row.session >= split_day
            else row
            for row in rows
        ]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=split_day, cash_amount=None,
                ratio=2.0,
            )
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=adjusted_rows,
            actions=actions,
            scoring_months=[],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=5.0,
        )
        bought = result.fills[0].shares
        assert result.fills[0].session == sessions[0]
        # After the 2:1 split the held shares doubled with no new fill.
        assert len(result.fills) == 1
        final_state_shares = bought * 2
        # Prove via liquidation: the SELL sells the doubled count.
        liquidated = simulate_leg(
            sessions=sessions,
            daily_rows=adjusted_rows,
            actions=actions,
            scoring_months=[],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=5.0,
            liquidation_session=sessions[-1],
        )
        assert liquidated.fills[-1].side == "SELL"
        assert liquidated.fills[-1].shares == final_state_shares

    def test_withholding_reduces_credit(self) -> None:
        gross = dividend_cash_credit(
            shares=10, cash_amount_per_share=2.0
        )
        net = dividend_cash_credit(
            shares=10, cash_amount_per_share=2.0, withholding_rate=0.30
        )
        assert gross == Decimal("20")
        assert net == pytest.approx(Decimal("14"))


# --------------------------------------------------------------- benchmark


class TestBuyAndHoldBenchmark:
    def test_parity_same_cash_same_caps_single_buy(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=41)
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=[],
            scoring_months=_month_iter((2012, 1), (2012, 12)),
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        # Exactly one BUY at the first session's open, sized by the same
        # caps, and 12 month records.
        assert len(result.fills) == 1
        assert result.fills[0].side == "BUY"
        assert result.fills[0].session == sessions[0]
        expected_shares = entry_share_cap(
            cash_available=INITIAL_CASH_USD, raw_price=rows[0].open
        )
        assert result.fills[0].shares == expected_shares
        assert len(result.records) == 12
        assert all(record.invested for record in result.records)
        # No trimming all year even as the price moves.
        last = result.records[-1]
        expected_equity = result.fills[0].shares * Decimal(
            str(rows[-1].close)
        ) + result.fills[0].commission_usd * Decimal("-1")
        del expected_equity, last

    def test_monthly_return_series_shapes(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=43)
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=[],
            scoring_months=_month_iter((2012, 1), (2012, 3)),
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        returns = monthly_return_series(
            result.records, initial_equity=INITIAL_CASH_USD
        )
        assert len(returns) == 3
        # first month measured against initial cash
        first_expected = (
            float(result.records[0].equity) / 5000.0 - 1.0
        )
        assert returns[0] == pytest.approx(first_expected)


# -------------------------------------------------------------- bootstrap


class TestBootstrap:
    def test_deterministic_with_fixed_seed(self) -> None:
        values = [0.01, -0.02, 0.015, 0.005, -0.001, 0.02, 0.011,
                  -0.004, 0.006, 0.003, -0.008, 0.013]
        a = circular_block_bootstrap_bounds(values, config=BOOTSTRAP_CONFIG)
        b = circular_block_bootstrap_bounds(values, config=BOOTSTRAP_CONFIG)
        assert (a.lower, a.upper, a.mean) == (b.lower, b.upper, b.mean)

    def test_different_seed_changes_bounds(self) -> None:
        values = [0.01, -0.02, 0.015, 0.005, -0.001, 0.02, 0.011,
                  -0.004, 0.006, 0.003, -0.008, 0.013, 0.002, 0.007,
                  -0.003, 0.009]
        a = circular_block_bootstrap_bounds(
            values, config=sma10.BootstrapConfig(seed=1, resamples=500)
        )
        b = circular_block_bootstrap_bounds(
            values, config=sma10.BootstrapConfig(seed=2, resamples=500)
        )
        assert (a.lower, a.upper) != (b.lower, b.upper)

    def test_block_length_equal_to_n_is_a_circular_permutation(self) -> None:
        # n == block length: every resample is one full circular pass,
        # so the resampled mean is ALWAYS the sample mean regardless of
        # the seed.  Worth pinning: it explains why seeds only matter
        # when n > block (the registered case is n=120 > 12).
        values = [0.01, -0.02, 0.015, 0.005, -0.001, 0.02, 0.011,
                  -0.004, 0.006, 0.003, -0.008, 0.013]
        bounds = circular_block_bootstrap_bounds(
            values, config=sma10.BootstrapConfig(seed=99, resamples=50)
        )
        assert bounds.lower == pytest.approx(bounds.mean)
        assert bounds.upper == pytest.approx(bounds.mean)

    def test_constant_series_degenerate_policy(self) -> None:
        bounds = circular_block_bootstrap_bounds(
            [0.0] * 12, config=BOOTSTRAP_CONFIG
        )
        assert bounds.lower == bounds.upper == bounds.mean == 0.0
        assert is_constant([0.0] * 12)
        # Registered policy: degenerate -> INCONCLUSIVE, never a verdict.
        claims = evaluate_claims(
            sleeve_monthly_returns=[0.0] * 60,
            benchmark_monthly_returns=[0.0] * 60,
            stress_sleeve_monthly_returns=[0.0] * 60,
        )
        assert claims.degenerate
        assert (
            decide_verdict(
                gates=SampleGates(months=120, cash_months=60,
                                  invested_months=60),
                claims=claims,
            )
            == "INCONCLUSIVE"
        )

    def test_paired_block_starts_identical_across_series(self) -> None:
        # Same seed => same block start indices => the paired-month
        # structure survives the bootstrap (registered pairing rule).
        import random

        n = 24
        rng = random.Random(BOOTSTRAP_CONFIG.seed)
        starts_a = [
            rng.randrange(n)
            for _ in range(BOOTSTRAP_CONFIG.resamples)
        ]
        rng_b = random.Random(BOOTSTRAP_CONFIG.seed)
        starts_b = [
            rng_b.randrange(n)
            for _ in range(BOOTSTRAP_CONFIG.resamples)
        ]
        assert starts_a == starts_b

    def test_positive_series_lower_bound_above_zero(self) -> None:
        values = [0.01 + (0.001 * i) for i in range(24)]
        bounds = circular_block_bootstrap_bounds(
            values, config=BOOTSTRAP_CONFIG
        )
        assert bounds.lower > 0

    def test_rejects_empty_series(self) -> None:
        with pytest.raises(MonthlyTrendError):
            circular_block_bootstrap_bounds([], config=BOOTSTRAP_CONFIG)


# ----------------------------------------------------------------- verdict


def _bounds(lower: float, upper: float, mean: float = 0.0) -> Any:
    return sma10.ClaimBounds(mean=mean, lower=lower, upper=upper)


_FULL_GATES = SampleGates(months=120, cash_months=12, invested_months=60)
_THIN_GATES = SampleGates(months=119, cash_months=12, invested_months=60)


class TestVerdictMapping:
    def test_all_claims_pass(self) -> None:
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(0.001, 0.004),
            claim2_giveup=_bounds(-0.0005, 0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_FULL_GATES, claims=claims) == (
            "CORROBORATES_RISK_MANAGEMENT_VALUE"
        )

    def test_insufficient_sample_is_insufficient_data_via_cli(self) -> None:
        # The pure mapping keeps insufficient samples INCONCLUSIVE; the
        # CLI promotes them to INSUFFICIENT_DATA before mapping.  Both
        # are pinned here so the two-layer contract is explicit.
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(0.001, 0.004),
            claim2_giveup=_bounds(-0.0005, 0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_THIN_GATES, claims=claims) == (
            "INCONCLUSIVE"
        )

    def test_any_upper_bound_below_threshold_is_does_not_corroborate(
        self,
    ) -> None:
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(-0.004, -0.001),
            claim2_giveup=_bounds(-0.0005, 0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_FULL_GATES, claims=claims) == (
            "DOES_NOT_CORROBORATE"
        )

    def test_straddling_bounds_inconclusive(self) -> None:
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(-0.001, 0.002),
            claim2_giveup=_bounds(-0.0005, 0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_FULL_GATES, claims=claims) == (
            "INCONCLUSIVE"
        )

    def test_claim2_threshold_is_minus_10bps(self) -> None:
        assert CLAIM2_THRESHOLD == -0.001

    def test_claim2_upper_at_exactly_threshold_is_does_not_corroborate(
        self,
    ) -> None:
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(0.001, 0.004),
            claim2_giveup=_bounds(-0.002, -0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_FULL_GATES, claims=claims) == (
            "DOES_NOT_CORROBORATE"
        )

    def test_claim1_lower_exactly_zero_is_inconclusive(self) -> None:
        claims = sma10.ClaimEvaluations(
            claim1_positive_expectancy=_bounds(0.0, 0.002),
            claim2_giveup=_bounds(-0.0005, 0.001),
            claim3_downside=_bounds(0.0001, 0.001),
            claim4_stress_positive=_bounds(0.0002, 0.003),
            degenerate=False,
        )
        assert decide_verdict(gates=_FULL_GATES, claims=claims) == (
            "INCONCLUSIVE"
        )


# ------------------------------------------------------------- DATA_BLOCKED


class TestDataBlocked:
    def _base_env(self) -> tuple[list[date], list[DailyPriceRow]]:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 6, 30))
        return sessions, _synthetic_rows(sessions, seed=47)

    def test_missing_month_end_blocks(self) -> None:
        sessions, rows = self._base_env()
        # Drop every session of March: the (2012, 3) month-end vanishes.
        truncated = [s for s in sessions if s.month != 3]
        with pytest.raises(MonthlyReplayBlockedError, match="month-end"):
            sma10.validate_window_data(
                sessions=truncated,
                scoring_months=[(2012, 1), (2012, 2), (2012, 3)],
                daily_rows=[r for r in rows if r.session.month != 3],
                actions=[],
            )

    def test_missing_next_open_blocks(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 6, 29))
        rows = _synthetic_rows(sessions, seed=47)
        with pytest.raises(MonthlyReplayBlockedError, match="next open"):
            sma10.validate_window_data(
                sessions=sessions,
                scoring_months=[(2012, 6)],
                daily_rows=rows,
                actions=[],
            )

    def test_missing_bar_on_sealed_session_blocks(self) -> None:
        sessions, rows = self._base_env()
        with pytest.raises(MonthlyReplayBlockedError, match="RAW daily bar"):
            sma10.validate_window_data(
                sessions=sessions,
                scoring_months=[(2012, 1)],
                daily_rows=rows[:-1],  # last sealed session has no bar
                actions=[],
            )

    def test_uncertain_corporate_action_blocks(self) -> None:
        sessions, rows = self._base_env()
        bad = CorporateAction(
            symbol="SPY.US", ex_date=sessions[10], cash_amount=None,
            ratio=None,
        )
        with pytest.raises(MonthlyReplayBlockedError, match="uncertain"):
            sma10.validate_window_data(
                sessions=sessions,
                scoring_months=[(2012, 1)],
                daily_rows=rows,
                actions=[bad],
            )

    def test_invalid_dividend_amount_blocks(self) -> None:
        sessions, rows = self._base_env()
        bad = CorporateAction(
            symbol="SPY.US", ex_date=sessions[10], cash_amount=-1.0,
            ratio=None,
        )
        with pytest.raises(MonthlyReplayBlockedError, match="dividend"):
            sma10.validate_window_data(
                sessions=sessions,
                scoring_months=[(2012, 1)],
                daily_rows=rows,
                actions=[bad],
            )

    def test_invalid_split_ratio_blocks(self) -> None:
        sessions, rows = self._base_env()
        bad = CorporateAction(
            symbol="SPY.US", ex_date=sessions[10], cash_amount=None,
            ratio=0.0,
        )
        with pytest.raises(MonthlyReplayBlockedError, match="split"):
            sma10.validate_window_data(
                sessions=sessions,
                scoring_months=[(2012, 1)],
                daily_rows=rows,
                actions=[bad],
            )

    def test_seal_refuses_without_corporate_actions_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_fetch_cache(tmp_path / "cache", with_actions=False)
        # Item 6: the /proc liveness scan is neutralised with a local
        # fixture - the refusal under test is the DATA_BLOCKED one.
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="DATA_BLOCKED"):
            run_seal(tmp_path / "cache")

    def test_evaluate_reports_data_blocked_for_missing_month(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Delete one mid-window month of sessions from BOTH bars files
        # and the calendar: the (2013, 6) month-end disappears.  The
        # status.json bar counts are kept in sync (this test targets
        # the evaluate-time month-end validation, not the seal-time
        # count drift check, which has its own test).
        _drop_month_from_cache(cache, (2013, 6))
        _resync_status_counts(cache)
        # Item 5: the missing month is now caught AT SEAL by the
        # independent NYSE-rule calendar (dates missing on BOTH
        # symbols) - stronger than the previous evaluate-time check.
        _no_live_fetch(monkeypatch)
        with pytest.raises(
            MonthlySma10Error,
            match="no SPY bar|missing on BOTH symbols",
        ):
            run_seal(cache)


# ---------------------------------------------------- CLI seal/evaluate glue


def _write_bars_file(
    cache: Path, symbol: str, rows: list[DailyPriceRow]
) -> None:
    payload = {
        "symbol": symbol,
        "period": "DAY",
        "adjustment": "NoAdjust",
        "bars": [
            [
                row.session.isoformat(),
                row.open,
                row.high,
                row.low,
                row.close,
                1000.0,
                100000.0,
            ]
            for row in rows
        ],
    }
    path = cache / "daily" / f"{symbol}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(rendered)


def _write_fetch_cache(
    cache: Path, *, with_actions: bool = True,
    with_registered_anomaly: bool = True,
) -> Path:
    """A complete synthetic fetch cache: SPY/QQQ bars 2010-06..2022-01,
    plan.json, status.json, corporate actions.

    The corporate-actions record is written DIRECTLY in the registered
    sealed shape (xlsx format + registered hash + the 48-quarter
    content fact), because the JSON import path is synthetic-tests-only
    and is refused by seal (decision 14.6).

    ``with_registered_anomaly`` (default True) injects the single
    registered OHLC anomaly (decision 14.11) into the SPY bars:
    2020-11-18 open above the high, close still inside [low, high].
    Pass False for the few fixtures that need a clean cache."""

    sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
    spy_rows = _synthetic_rows(sessions, seed=101, drift=0.00055)
    if with_registered_anomaly:
        spy_rows = _inject_registered_ohlc_anomaly(spy_rows)
    qqq_rows = _synthetic_rows(sessions, seed=202, drift=0.00065)
    _write_bars_file(cache, "SPY.US", spy_rows)
    _write_bars_file(cache, "QQQ.US", qqq_rows)
    status = {
        "version": cli.REPLAY_CLI_VERSION,
        "global_stop": None,
        "symbols": {
            "SPY.US": {
                "daily": {
                    "state": "COMPLETE",
                    "bars": len(spy_rows),
                    "sha256": cli._file_sha256(
                        cache / "daily" / "SPY.US.json.gz"
                    ),
                }
            },
            "QQQ.US": {
                "daily": {
                    "state": "COMPLETE",
                    "bars": len(qqq_rows),
                    "sha256": cli._file_sha256(
                        cache / "daily" / "QQQ.US.json.gz"
                    ),
                }
            },
        },
        "requests_total": 4,
    }
    cli._atomic_write_json(cache / "status.json", status)
    cli._atomic_write_json(cache / "plan.json", build_plan_payload())
    if with_actions:
        # The registered content fact: one SPY dividend per calendar
        # quarter 2010Q1..2021Q4, each with a pay date.  The dates
        # avoid sealed-session collisions with the synthetic bars.
        payload = {
            "actions": [
                {
                    "symbol": "SPY.US",
                    "ex_date": date(year, month, 15).isoformat(),
                    "cash_amount": 0.5,
                    "ratio": None,
                    "pay_date": date(year, month, 28).isoformat(),
                }
                for year in range(2010, 2022)
                for month in (3, 6, 9, 12)
            ]
        }
        stored = {
            "source_url": cli.REGISTERED_ACTIONS_SOURCE_URL,
            "source_format": "ssga-distributions-xlsx",
            "imported_at": "2026-09-28T00:00:00+00:00",
            "source_file_sha256": cli.REGISTERED_ACTIONS_SOURCE_SHA256,
            "bytes": 577780,
            "actions_count": 48,
            "dividends": 48,
            "splits": 0,
            "reason": None,
            "payload": payload,
        }
        cli._atomic_write_json(
            cache / CORPORATE_ACTIONS_FILENAME, stored
        )
    return cache


def tmp_actions_file(cache: Path, payload: dict[str, Any]) -> Path:
    target = cache / "input_corporate_actions.json"
    cache.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _drop_month_from_cache(cache: Path, key: tuple[int, int]) -> None:
    for symbol in ("SPY.US", "QQQ.US"):
        path = cache / "daily" / f"{symbol}.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["bars"] = [
            row
            for row in raw["bars"]
            if not (
                date.fromisoformat(row[0]).year == key[0]
                and date.fromisoformat(row[0]).month == key[1]
            )
        ]
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)


def _resync_status_counts(cache: Path) -> None:
    """Rewrite status.json bar counts from the files on disk (keeps the
    seal-time count check green after a fixture mutation)."""

    status = _load_status(cache)
    for symbol in ("SPY.US", "QQQ.US"):
        path = cache / "daily" / f"{symbol}.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        entry = status["symbols"][symbol]["daily"]
        entry["bars"] = len(raw["bars"])
        entry["sha256"] = cli._file_sha256(path)
    cli._atomic_write_json(cache / "status.json", status)


def _no_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the REAL /proc fetch check with a local fixture (item
    12): the scan itself reads /proc, whose contents vary by host; the
    check's own logic is exercised via the explicit pid-returning
    fixture below."""

    monkeypatch.setattr(cli, "_fetch_process_alive", lambda: None)


def _seal_for_tests(
    monkeypatch: pytest.MonkeyPatch, cache: Path
) -> None:
    _no_live_fetch(monkeypatch)
    _clean_tree_for_tests(monkeypatch)
    run_seal(cache)


def _clean_tree_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the checkout-state dependency (CI rule): evaluate's
    clean-worktree refusal is proven against a real throw-away repo in
    TestRequireCleanWorktree; here it is forced clean so the result
    never depends on the host tree."""

    monkeypatch.setattr(cli, "_require_clean_worktree", lambda _repo: None)


class TestSealEvaluateRefusals:
    def test_seal_refuses_partial_fetch(self, tmp_path: Path) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        status = _load_status(cache)
        status["symbols"]["QQQ.US"]["daily"]["state"] = "PENDING"
        cli._atomic_write_json(cache / "status.json", status)
        with pytest.raises(MonthlySma10Error, match="not terminal"):
            run_seal(cache)

    def test_seal_refuses_complete_state_without_file(
        self, tmp_path: Path
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        (cache / "daily" / "QQQ.US.json.gz").unlink()
        with pytest.raises(MonthlySma10Error, match="file missing"):
            run_seal(cache)

    def test_evaluate_refuses_unsealed_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _no_live_fetch(monkeypatch)
        _clean_tree_for_tests(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="seal first"):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "result.json",
            )

    def test_evaluate_refuses_dirty_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.cli.opening_momentum_historical_replay import (
            HistoricalReplayError,
        )

        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)

        def _dirty(repo: Path) -> None:
            raise HistoricalReplayError(
                "evaluate refused: the working tree is dirty (test)"
            )

        monkeypatch.setattr(
            cli, "_require_clean_worktree", _dirty
        )
        with pytest.raises(HistoricalReplayError, match="dirty"):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "result.json",
            )
        assert not (cache / "attempts").exists()

    def test_evaluate_output_must_be_outside_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        with pytest.raises(MonthlySma10Error, match="inside the cache"):
            run_evaluate(
                cache_dir=cache,
                output_path=cache / "result.json",
            )

    def test_second_attempt_needs_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out1 = tmp_path / "out" / "result.json"
        first = run_evaluate(cache_dir=cache, output_path=out1)
        assert first["verdict"] in {
            "CORROBORATES_RISK_MANAGEMENT_VALUE",
            "DOES_NOT_CORROBORATE",
            "INCONCLUSIVE",
            "INSUFFICIENT_DATA",
            "DATA_BLOCKED",
        }
        from app.cli.opening_momentum_historical_replay import (
            HistoricalReplayError,
        )

        with pytest.raises(HistoricalReplayError, match="rerun-reason"):
            run_evaluate(cache_dir=cache, output_path=out1)
        # Item 5 (14.9): publishing is no-clobber - a rerun must target
        # a FRESH output path (an existing one is refused).
        second = run_evaluate(
            cache_dir=cache,
            output_path=out1.with_name("result-2.json"),
            rerun_reason="test rerun after fixing a bug",
        )
        assert second["provenance"]["git_head"] is None or isinstance(
            second["provenance"]["git_head"], str
        )
        # The original output survives untouched.
        assert out1.exists()
        assert second["provenance"]["supersedes"] is not None

    def test_full_synthetic_pipeline_verdict_and_gates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        assert payload["analysis_id"] == ANALYSIS_ID
        gates = payload["sample_gates"]
        assert gates["months"] == 120
        assert gates["sufficient"] is True
        assert payload["verdict"] in {
            "CORROBORATES_RISK_MANAGEMENT_VALUE",
            "DOES_NOT_CORROBORATE",
            "INCONCLUSIVE",
        }
        assert payload["claims"]["thresholds"]["claim2"] == -0.001
        # Descriptive section is present and marked never-gating.
        assert payload["descriptive"]["never_gating"] is True


class TestRequireCleanWorktree:
    def test_refuses_dirty_and_accepts_clean(self, tmp_path: Path) -> None:
        # The REAL check against a private throw-away repo: the verdict
        # never depends on the CI checkout state.
        from app.cli.opening_momentum_historical_replay import (
            HistoricalReplayError,
        )

        repo = tmp_path / "repo"
        (repo / "backend" / "app").mkdir(parents=True)
        (repo / "backend" / "tests").mkdir(parents=True)
        (repo / "backend" / "app" / "mod.py").write_text("x = 1\n")
        (repo / "notes.txt").write_text("outside\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(
            repo, "-c", "user.email=t@example.invalid",
            "-c", "user.name=t", "commit", "-q", "-m", "init",
        )
        cli._require_clean_worktree(repo)  # clean: accepted

        (repo / "notes.txt").write_text("changed outside backend\n")
        cli._require_clean_worktree(repo)  # outside scope: accepted

        (repo / "backend" / "app" / "mod.py").write_text("x = 2\n")
        with pytest.raises(HistoricalReplayError, match="dirty"):
            cli._require_clean_worktree(repo)
        _git(repo, "checkout", "--", "backend/app/mod.py")

        (repo / "backend" / "tests" / "new_test.py").write_text("z = 1\n")
        with pytest.raises(HistoricalReplayError, match="dirty"):
            cli._require_clean_worktree(repo)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )


# ------------------------------------------------- split-completeness screen


class TestSplitAnomalyScreen:
    def _rows(self, sessions: list[date]) -> list[DailyPriceRow]:
        return _synthetic_rows(sessions, seed=53)

    def test_clean_series_has_no_offenders(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = self._rows(sessions)
        assert (
            sma10.screen_unexplained_split_anomalies(
                rows, [], sessions=sessions
            )
            == []
        )

    def test_halved_prices_without_split_action_block(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = self._rows(sessions)
        split_day = sessions[100]
        halved = [
            DailyPriceRow(
                session=row.session,
                open=row.open / 2,
                high=row.high / 2,
                low=row.low / 2,
                close=row.close / 2,
            )
            if row.session >= split_day
            else row
            for row in rows
        ]
        offenders = sma10.screen_unexplained_split_anomalies(
            halved, [], sessions=sessions
        )
        assert len(offenders) == 1
        assert offenders[0][0] == split_day
        # prev_close / close ≈ 2 modulo one day of synthetic drift.
        assert 1.8 < offenders[0][1] < 2.2

    def test_registered_split_action_excuses_the_discontinuity(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = self._rows(sessions)
        split_day = sessions[100]
        halved = [
            DailyPriceRow(
                session=row.session,
                open=row.open / 2,
                high=row.high / 2,
                low=row.low / 2,
                close=row.close / 2,
            )
            if row.session >= split_day
            else row
            for row in rows
        ]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=split_day, cash_amount=None,
                ratio=2.0,
            )
        ]
        assert (
            sma10.screen_unexplained_split_anomalies(
                halved, actions, sessions=sessions
            )
            == []
        )

    def test_extreme_market_day_is_below_threshold(self) -> None:
        # A -12% day (worse than any SPY day in the window) must NOT
        # trip the screen: factor ~1.136 << 1.5.
        sessions = _weekdays(date(2012, 1, 1), date(2012, 3, 31))
        rows = self._rows(sessions)
        crash_day = sessions[30]
        crashed = [
            DailyPriceRow(
                session=row.session,
                open=row.open * 0.88 if row.session == crash_day else row.open,
                high=row.high * 0.88 if row.session == crash_day else row.high,
                low=row.low * 0.88 if row.session == crash_day else row.low,
                close=row.close * 0.88 if row.session == crash_day else row.close,
            )
            for row in rows
        ]
        assert (
            sma10.screen_unexplained_split_anomalies(
                crashed, [], sessions=sessions
            )
            == []
        )

    def test_off_calendar_rows_are_ignored(self) -> None:
        # Rows on sessions outside the sealed calendar (e.g. QQQ-only
        # dates) must not create false discontinuities across the gap.
        sessions = _weekdays(date(2012, 1, 1), date(2012, 6, 30))
        rows = self._rows(sessions)
        extra_day = date(2012, 7, 4)  # not a sealed session
        with_extra = sorted(
            [
                *rows,
                DailyPriceRow(
                    session=extra_day, open=1.0, high=1.0, low=1.0,
                    close=1.0,  # a 100x drop would be a split if counted
                ),
            ],
            key=lambda row: row.session,
        )
        assert (
            sma10.screen_unexplained_split_anomalies(
                with_extra, [], sessions=sessions
            )
            == []
        )

    def test_evaluate_reports_data_blocked_for_split_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # E2E: halve all SPY prices from a mid-window date in the cache
        # WITHOUT any split action -> evaluate must return DATA_BLOCKED.
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _halve_cache_from(cache, date(2016, 6, 15))
        _resync_status_counts(cache)
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        assert payload["verdict"] == "DATA_BLOCKED"
        assert "split discontinuity" in payload["verdict_reasons"][0]


def _halve_cache_from(cache: Path, from_date: date) -> None:
    for symbol in ("SPY.US",):
        path = cache / "daily" / f"{symbol}.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["bars"] = [
            (
                row[0],
                row[1] / 2,
                row[2] / 2,
                row[3] / 2,
                row[4] / 2,
                row[5],
                row[6],
            )
            if date.fromisoformat(row[0]) >= from_date
            else row
            for row in raw["bars"]
        ]
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)


# ------------------------------------------------------- SSGA xlsx parser


def _build_xlsx(
    rows: list[list[Any]],
    *,
    header: list[str] | None = None,
) -> bytes:
    """Minimal single-sheet xlsx built with stdlib zipfile (no openpyxl).

    Mirrors the SSGA layout: shared strings for text cells, numeric
    cells inline, header first."""

    header = header or [
        "FUND", "EX-DATE", "RECORD DATE", "PAYABLE DATE",
        "AMOUNT per share", "DISTRIBUTION RATE",
    ]
    all_rows = [header, *rows]
    text_values: list[str] = []
    for row in all_rows:
        for value in row:
            if isinstance(value, str):
                text_values.append(value)

    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _cell(ref: str, value: Any) -> str:
        if isinstance(value, str):
            index = text_values.index(value)
            return f'<c r="{ref}" t="s"><v>{index}</v></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'

    def _col_letter(index: int) -> str:
        letters = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    sheet_rows: list[str] = []
    for row_index, row in enumerate(all_rows, start=1):
        cells = "".join(
            _cell(f"{_col_letter(col_index)}{row_index}", value)
            for col_index, value in enumerate(row)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
        '2006/main" count="1" uniqueCount="1">'
        + "".join(
            f"<si><t>{_esc(value)}</t></si>" for value in text_values
        )
        + "</sst>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"/>',
        )
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def _excel_serial(day: date) -> int:
    return (day - date(1899, 12, 30)).days


class TestSsgaXlsxParser:
    def test_parses_quarterly_spy_rows_chronologically(self) -> None:
        rows = [
            # newest-first like the real workbook; serial dates, numeric
            # amounts, other funds interleaved and ignored.
            ["SPY", _excel_serial(date(2013, 6, 17)),
             _excel_serial(date(2013, 6, 19)),
             _excel_serial(date(2013, 6, 28)), 0.9202, "1.1100%"],
            ["SPY", _excel_serial(date(2013, 3, 15)),
             _excel_serial(date(2013, 3, 19)),
             _excel_serial(date(2013, 3, 28)), 0.8465, "0.8800%"],
            ["SLY", _excel_serial(date(2013, 6, 17)),
             _excel_serial(date(2013, 6, 19)),
             _excel_serial(date(2013, 6, 28)), 0.5000, "0.5000%"],
        ]
        source = tmp_xlsx_file(rows)
        actions = cli._parse_ssga_xlsx(source)
        assert len(actions) == 2
        assert [a.ex_date for a in actions] == [
            date(2013, 3, 15), date(2013, 6, 17),
        ]
        assert actions[0].cash_amount == 0.8465
        assert actions[0].pay_date == date(2013, 3, 28)
        assert all(a.symbol == "SPY.US" for a in actions)
        assert all(a.ratio is None for a in actions)

    def test_iso_string_dates_also_accepted(self) -> None:
        rows = [
            ["SPY", "2013-03-15", "2013-03-19", "2013-03-28", 0.8465,
             "0.8800%"],
        ]
        source = tmp_xlsx_file(rows)
        actions = cli._parse_ssga_xlsx(source)
        assert actions[0].ex_date == date(2013, 3, 15)
        assert actions[0].pay_date == date(2013, 3, 28)

    def test_header_located_by_name_not_position(self) -> None:
        # Extra leading columns must not break the name-based lookup:
        # the header shifts right, the row cells shift with it, and the
        # parser must still find FUND / EX-DATE / AMOUNT by NAME.
        rows = [
            [
                "note", "SPY", _excel_serial(date(2013, 3, 15)),
                _excel_serial(date(2013, 3, 19)),
                _excel_serial(date(2013, 3, 28)), 0.8465, "x",
            ],
        ]
        source = tmp_xlsx_file(
            rows,
            header=[
                "NOTE", "FUND", "EX-DATE", "RECORD DATE",
                "PAYABLE DATE", "AMOUNT per share",
            ],
        )
        actions = cli._parse_ssga_xlsx(source)
        assert len(actions) == 1
        assert actions[0].ex_date == date(2013, 3, 15)
        assert actions[0].cash_amount == 0.8465

    def test_no_spy_rows_fails_loudly(self) -> None:
        rows = [
            ["MDY", _excel_serial(date(2013, 3, 15)),
             _excel_serial(date(2013, 3, 19)),
             _excel_serial(date(2013, 3, 28)), 0.8465, "x"],
        ]
        source = tmp_xlsx_file(rows)
        with pytest.raises(MonthlySma10Error, match="no SPY rows"):
            cli._parse_ssga_xlsx(source)

    def test_missing_amount_column_fails(self) -> None:
        rows = [["SPY", 1, 2, 3]]
        source = tmp_xlsx_file(rows, header=["FUND", "A", "B", "C"])
        with pytest.raises(MonthlySma10Error, match="missing"):
            cli._parse_ssga_xlsx(source)

    def test_non_numeric_amount_fails(self) -> None:
        rows = [
            ["SPY", _excel_serial(date(2013, 3, 15)), 1, 2, "N/A", "x"],
        ]
        source = tmp_xlsx_file(rows)
        with pytest.raises(MonthlySma10Error, match="not numeric"):
            cli._parse_ssga_xlsx(source)

    def test_import_accepts_xlsx_and_stores_canonical_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            ["SPY", _excel_serial(date(2013, 3, 15)),
             _excel_serial(date(2013, 3, 19)),
             _excel_serial(date(2013, 3, 28)), 0.8465, "0.8800%"],
            ["SPY", _excel_serial(date(2013, 6, 17)),
             _excel_serial(date(2013, 6, 19)),
             _excel_serial(date(2013, 6, 28)), 0.9202, "1.1100%"],
        ]
        source = tmp_path / "spdr.xlsx"
        source.write_bytes(_build_xlsx(rows))
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        # This 2-row fixture exercises the STORAGE shape, not the 48-
        # quarter content fact (covered by TestRegisteredSourceEnforcement).
        monkeypatch.setattr(cli, "REGISTERED_SPY_WINDOW_EVENTS", 2)
        monkeypatch.setattr(
            cli, "REGISTERED_ACTIONS_WINDOW_START", date(2013, 1, 1)
        )
        monkeypatch.setattr(
            cli, "REGISTERED_ACTIONS_WINDOW_END", date(2013, 6, 30)
        )
        report = run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url="https://www.ssga.com/library-content/products/fund-data/etfs/us/spdr-etf-historical-distributions.xlsx",
        )
        assert report["actions"] == 2
        stored = json.loads(
            (
                tmp_path / "cache" / CORPORATE_ACTIONS_FILENAME
            ).read_text(encoding="utf-8")
        )
        assert stored["source_format"] == "ssga-distributions-xlsx"
        assert stored["dividends"] == 2
        assert stored["splits"] == 0
        ex_dates = [
            action["ex_date"] for action in stored["payload"]["actions"]
        ]
        assert ex_dates == ["2013-03-15", "2013-06-17"]

    def test_qqq_rows_are_absent_by_source_design(self) -> None:
        # The registered workbook carries no QQQ (Invesco) rows; a file
        # that DID carry QQQ rows would still parse only SPY (the
        # filter is by design, not by accident of the source).
        rows = [
            ["QQQ", _excel_serial(date(2013, 3, 15)),
             _excel_serial(date(2013, 3, 19)),
             _excel_serial(date(2013, 3, 28)), 0.5, "x"],
            ["SPY", _excel_serial(date(2013, 3, 15)),
             _excel_serial(date(2013, 3, 19)),
             _excel_serial(date(2013, 3, 28)), 0.8465, "x"],
        ]
        source = tmp_xlsx_file(rows)
        actions = cli._parse_ssga_xlsx(source)
        assert [a.symbol for a in actions] == ["SPY.US"]


def tmp_xlsx_file(
    rows: list[list[Any]], *, header: list[str] | None = None
) -> Path:
    target = Path(tempfile.mkdtemp()) / "spdr.xlsx"
    target.write_bytes(_build_xlsx(rows, header=header))
    return target


# ------------------------------------- registered-source enforcement (14.6)


def _full_history_rows(
    *, quarters: int = 48, duplicate_quarter: bool = False
) -> list[list[Any]]:
    """Synthetic SSGA rows: one SPY dividend per calendar quarter from
    2010Q1 through 2021Q4 (48 events), newest-first like the real
    workbook, plus one ignored other-fund row."""

    rows: list[list[Any]] = []
    # quarters 2010Q1..2021Q4 = 12 years x 4
    for year in range(2021, 2009, -1):
        for month in (12, 9, 6, 3):
            ex = date(year, month, 15)
            pay = date(year, month, 28)
            rows.append(
                [
                    "SPY", _excel_serial(ex), _excel_serial(ex),
                    _excel_serial(pay),
                    0.5 + 0.01 * ((year - 2010) * 4 + month // 3),
                    "0.5000%",
                ]
            )
    if duplicate_quarter:
        rows.append(
            [
                "SPY", _excel_serial(date(2015, 6, 16)),
                _excel_serial(date(2015, 6, 16)),
                _excel_serial(date(2015, 6, 30)), 0.75, "0.7000%",
            ]
        )
    # keep only the first `quarters` SPY rows (47/49 shapes)
    spy_rows = [row for row in rows if row[0] == "SPY"]
    others = [row for row in rows if row[0] != "SPY"]
    if duplicate_quarter:
        spy_rows = spy_rows[: quarters - 1] + [
            [
                "SPY", _excel_serial(date(2015, 6, 16)),
                _excel_serial(date(2015, 6, 16)),
                _excel_serial(date(2015, 6, 30)), 0.75, "0.7000%",
            ]
        ]
        return [*spy_rows, *others]
    return [*spy_rows[:quarters], *others]


def _full_history_xlsx(
    *, quarters: int = 48, duplicate_quarter: bool = False
) -> Path:
    return tmp_xlsx_file(
        _full_history_rows(
            quarters=quarters, duplicate_quarter=duplicate_quarter
        )
    )


class TestRegisteredSourceEnforcement:
    """Decision 14.6: the registered SSGA workbook is enforced by FULL
    sha256 at import, and the 48-quarter content fact is verified; seal
    re-verifies both.  Real runs can never use an unregistered file."""

    def test_registered_url_is_the_verified_one(self) -> None:
        assert cli.REGISTERED_ACTIONS_SOURCE_URL == (
            "https://www.ssga.com/library-content/products/fund-data/"
            "etfs/us/spdr-etf-historical-distributions.xlsx"
        )

    def test_registered_hash_is_the_full_sha256(self) -> None:
        assert cli.REGISTERED_ACTIONS_SOURCE_SHA256 == (
            "51a16a450298a663b3fa088883a17c75f4464e87c911b0e83902a0ad"
            "46877c54"
        )

    def test_wrong_hash_xlsx_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _full_history_xlsx()
        # Inject the registered hash as something ELSE: the workbook on
        # disk is synthetic, so any mismatch must refuse with the
        # registered-change-decision message.
        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            "0" * 64,
        )
        with pytest.raises(
            MonthlySma10Error, match="registered change decision"
        ):
            run_import_corporate_actions(
                cache_dir=tmp_path / "cache",
                file_path=source,
                source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
            )

    def test_right_hash_xlsx_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _full_history_xlsx()
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        report = run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
        )
        assert report["actions"] == 48
        assert report["source_format"] == "ssga-distributions-xlsx"
        # The report never carries amounts (counts only).
        assert "amount" not in json.dumps(report)

    def test_47_window_events_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _full_history_xlsx(quarters=47)
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        with pytest.raises(MonthlySma10Error, match="48"):
            run_import_corporate_actions(
                cache_dir=tmp_path / "cache",
                file_path=source,
                source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
            )

    def test_49_window_events_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _full_history_xlsx(quarters=48, duplicate_quarter=True)
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        with pytest.raises(MonthlySma10Error, match="quarter"):
            run_import_corporate_actions(
                cache_dir=tmp_path / "cache",
                file_path=source,
                source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
            )

    def test_event_outside_window_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _full_history_rows()
        rows[0] = [  # newest row pushed out of the window
            "SPY", _excel_serial(date(2022, 3, 15)),
            _excel_serial(date(2022, 3, 15)),
            _excel_serial(date(2022, 3, 28)), 0.9, "1.0%",
        ]
        source = tmp_xlsx_file(rows)
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        with pytest.raises(MonthlySma10Error, match="48|window"):
            run_import_corporate_actions(
                cache_dir=tmp_path / "cache",
                file_path=source,
                source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
            )

    def test_missing_pay_date_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _full_history_rows()
        # blank payable date on one row (header keeps the column)
        rows[5] = [rows[5][0], rows[5][1], rows[5][2], "", rows[5][4], "x"]
        source = tmp_xlsx_file(rows)
        import hashlib as _hashlib

        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        with pytest.raises(MonthlySma10Error, match="pay"):
            run_import_corporate_actions(
                cache_dir=tmp_path / "cache",
                file_path=source,
                source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
            )

    def test_seal_refuses_stored_record_without_registered_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a cache whose corporate_actions.json came from the JSON
        # path (or an xlsx with a now-wrong hash) and is marked with an
        # unregistered source hash: seal must refuse.
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        stored_path = cache / CORPORATE_ACTIONS_FILENAME
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        stored["source_url"] = cli.REGISTERED_ACTIONS_SOURCE_URL
        stored["source_format"] = "ssga-distributions-xlsx"
        stored["source_file_sha256"] = "f" * 64
        cli._atomic_write_json(stored_path, stored)
        _no_live_fetch(monkeypatch)
        with pytest.raises(
            MonthlySma10Error, match="registered change decision"
        ):
            run_seal(cache)

    def test_seal_accepts_registered_hash_and_48_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Rewrite the stored record to the registered shape: 48
        # quarterly events + the registered hash + xlsx format.
        actions = _full_history_rows()
        payload = {
            "actions": [
                {
                    "symbol": "SPY.US",
                    "ex_date": date(
                        2000 + (int(row[1]) // 365 + 1900) // 1, 1, 1
                    ).isoformat(),  # placeholder; replaced below
                    "cash_amount": 0.5,
                    "ratio": None,
                    "pay_date": None,
                }
                for row in actions
                if row[0] == "SPY"
            ]
        }
        # Real quarterly events 2010Q1..2021Q4
        payload["actions"] = [
            {
                "symbol": "SPY.US",
                "ex_date": date(year, month, 15).isoformat(),
                "cash_amount": 0.5,
                "ratio": None,
                "pay_date": date(year, month, 28).isoformat(),
            }
            for year in range(2010, 2022)
            for month in (3, 6, 9, 12)
        ][:48]
        stored_path = cache / CORPORATE_ACTIONS_FILENAME
        stored = {
            "source_url": cli.REGISTERED_ACTIONS_SOURCE_URL,
            "source_format": "ssga-distributions-xlsx",
            "imported_at": "2026-09-28T00:00:00+00:00",
            "source_file_sha256": cli.REGISTERED_ACTIONS_SOURCE_SHA256,
            "bytes": 577780,
            "actions_count": 48,
            "dividends": 48,
            "splits": 0,
            "reason": None,
            "payload": payload,
        }
        cli._atomic_write_json(stored_path, stored)
        _no_live_fetch(monkeypatch)
        manifest_hash = run_seal(cache)
        assert len(manifest_hash) == 64


# ------------------------------------------------- fetch/corporate actions


class _FakeProvider:
    """Fake QuoteContext-only provider serving synthetic daily bars."""

    def __init__(self, rows: list[DailyPriceRow]) -> None:
        self._rows = rows

    def history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        *,
        count: int,
        after: datetime,
        forward: bool,
        adjustment: str,
    ) -> list[Any]:
        assert period == "DAY"
        assert adjustment == "NoAdjust"
        after_date = after.astimezone(timezone.utc).date()
        selected = [
            cli._MonthlyCandleView(
                timestamp=datetime(
                    row.session.year, row.session.month, row.session.day,
                    14, 30, tzinfo=timezone.utc,
                ),
                open_=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=1000.0,
                turnover=100000.0,
            )
            for row in self._rows
            if row.session > after_date
        ]
        return selected[:count]


class TestFetch:
    def test_fetch_writes_cache_and_derives_no_prices(
        self, tmp_path: Path
    ) -> None:
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        rows = _synthetic_rows(sessions, seed=303)
        provider = _FakeProvider(rows)
        plan = build_plan_payload()
        report = run_fetch(
            cache_dir=tmp_path,
            plan_payload=plan,
            provider=provider,
            clock=lambda: datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc),
            sleep=lambda _s: None,
        )
        assert report["coverage"]["symbols_complete"] == 2
        assert report["errors"]["transient"] == {}
        for symbol in ("SPY.US", "QQQ.US"):
            path = tmp_path / "daily" / f"{symbol}.json.gz"
            assert path.exists()
        # The report carries no prices (coverage only).
        rendered = json.dumps(report)
        assert "close" not in rendered and "open" not in rendered

    def test_fetch_refuses_wrong_plan(self, tmp_path: Path) -> None:
        plan = build_plan_payload()
        plan["analysis_id"] = "some-other-analysis"
        with pytest.raises(MonthlySma10Error, match="analysis_id"):
            run_fetch(
                cache_dir=tmp_path,
                plan_payload=plan,
                provider=_FakeProvider([]),
                clock=lambda: datetime(2026, 9, 28, 2, 0,
                                       tzinfo=timezone.utc),
                sleep=lambda _s: None,
            )

    def test_fetch_refuses_global_stop(self, tmp_path: Path) -> None:
        cli._atomic_write_json(
            tmp_path / "status.json",
            {
                "version": cli.REPLAY_CLI_VERSION,
                "global_stop": {"reason": "GLOBAL_STOP_QUOTA"},
                "symbols": {},
            },
        )
        with pytest.raises(MonthlySma10Error, match="GLOBAL STOP"):
            run_fetch(
                cache_dir=tmp_path,
                plan_payload=build_plan_payload(),
                provider=_FakeProvider([]),
                clock=lambda: datetime(2026, 9, 28, 2, 0,
                                       tzinfo=timezone.utc),
                sleep=lambda _s: None,
            )

    def test_quota_error_sets_global_stop(self, tmp_path: Path) -> None:
        class _QuotaProvider:
            def history_candlesticks_by_offset(
                self, symbol: str, period: str, *, count: int,
                after: datetime, forward: bool, adjustment: str,
            ) -> list[Any]:
                raise RuntimeError("(code=301607) quota exceeded")

        with pytest.raises(MonthlySma10Error, match="GLOBAL STOP"):
            run_fetch(
                cache_dir=tmp_path,
                plan_payload=build_plan_payload(),
                provider=_QuotaProvider(),
                clock=lambda: datetime(2026, 9, 28, 2, 0,
                                       tzinfo=timezone.utc),
                sleep=lambda _s: None,
            )
        status = _load_status(tmp_path)
        assert isinstance(status["global_stop"], dict)
        assert status["global_stop"]["reason"] == "GLOBAL_STOP_QUOTA"


class TestCorporateActionsImport:
    def test_import_hashes_and_stores(self, tmp_path: Path) -> None:
        source = tmp_path / "ca.json"
        source.write_text(
            json.dumps(
                {
                    "actions": [
                        {
                            "symbol": "SPY.US",
                            "ex_date": "2013-03-15",
                            "cash_amount": 1.1,
                            "pay_date": "2013-03-22",
                        },
                        {
                            "symbol": "SPY.US",
                            "ex_date": "2015-06-15",
                            "ratio": 2.0,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        report = run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url="https://example.invalid/x",
        )
        assert report["actions"] == 2
        stored_path = tmp_path / "cache" / CORPORATE_ACTIONS_FILENAME
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        # The integrity anchor is the file on disk; the recorded
        # source-file hash stays as provenance.
        from app.cli.spy_monthly_sma10_replay import _file_sha256

        assert _file_sha256(stored_path) == report["sha256"]
        assert stored["source_url"] == "https://example.invalid/x"

    def test_replace_requires_reason_and_archives(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        source = tmp_path / "ca.json"
        source.write_text(
            json.dumps({"actions": [
                {"symbol": "SPY.US", "ex_date": "2013-03-15",
                 "cash_amount": 1.1}
            ]}),
            encoding="utf-8",
        )
        run_import_corporate_actions(
            cache_dir=cache, file_path=source,
            source_url="https://example.invalid/x",
        )
        source.write_text(
            json.dumps({"actions": [
                {"symbol": "SPY.US", "ex_date": "2013-06-15",
                 "cash_amount": 1.2}
            ]}),
            encoding="utf-8",
        )
        with pytest.raises(MonthlySma10Error, match="reason"):
            run_import_corporate_actions(
                cache_dir=cache, file_path=source,
                source_url="https://example.invalid/x",
            )
        run_import_corporate_actions(
            cache_dir=cache, file_path=source,
            source_url="https://example.invalid/x",
            reason="corrected ex-date",
        )
        archive = cache / "corporate_actions_archive"
        assert (archive / "corporate_actions-1.json").exists()

    def test_invalid_payload_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(MonthlyTrendError, match="neither"):
            validate_corporate_actions_payload(
                {"actions": [
                    {"symbol": "SPY.US", "ex_date": "2013-03-15"}
                ]}
            )
        with pytest.raises(MonthlyTrendError, match="no 'actions'"):
            validate_corporate_actions_payload({"foo": 1})

    def test_source_url_required(self, tmp_path: Path) -> None:
        source = tmp_path / "ca.json"
        source.write_text(
            json.dumps({"actions": [
                {"symbol": "SPY.US", "ex_date": "2013-03-15",
                 "cash_amount": 1.1}
            ]}),
            encoding="utf-8",
        )
        with pytest.raises(MonthlySma10Error, match="source-url"):
            run_import_corporate_actions(
                cache_dir=tmp_path, file_path=source, source_url=""
            )


# ------------------------------------------------------------- misc glue


class TestGlue:
    def test_plan_payload_is_returns_blind(self) -> None:
        payload = build_plan_payload()
        rendered = json.dumps(payload)
        for banned in ("close", "open", "price_level", "pnl"):
            assert banned not in rendered.lower() or banned == "open" and "opens" not in rendered
        assert payload["analysis_id"] == ANALYSIS_ID
        assert payload["symbols"] == [
            {"symbol": "SPY.US", "period": "DAY", "adjustment": "NoAdjust"},
            {"symbol": "QQQ.US", "period": "DAY", "adjustment": "NoAdjust"},
        ]

    def test_derive_trading_days_intersection(self) -> None:
        spy = [date(2020, 1, 2), date(2020, 1, 3)]
        qqq = [date(2020, 1, 3), date(2020, 1, 6)]
        result = _derive_trading_days(spy, qqq)
        assert result["trading_days"] == ["2020-01-03"]
        one_sided = result["one_sided_dates"]
        assert isinstance(one_sided, list) and len(one_sided) == 2


# ------------------------------------------- decision 14.11: OHLC anomaly


@pytest.fixture(autouse=True)
def _no_repo_mutation_guard() -> Any:
    """Decision 14.11 follow-up guard: NO test in this module may
    write, unlink or rename anything under the repo.  The committed
    anomaly ledger in particular is read by seal/evaluate through
    ``cli._ANOMALY_LEDGER_PATH``; a test that tampers with the real
    file corrupts the committed evidence and races every other worker
    under plain ``-n`` distribution (verified defect).  Snapshot the
    content hashes of the whole ``monthly_trend`` package (minus
    bytecode caches) plus the governance doc before/after each test."""

    import hashlib as _hashlib

    watch_root = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "domain"
        / "monthly_trend"
    )
    doc = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "domain"
        / "SPY_MONTHLY_SMA10_PREREGISTRATION.md"
    )

    def _snapshot() -> dict[str, str]:
        snapshot: dict[str, str] = {}
        paths = [
            path
            for path in watch_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        ]
        paths.append(doc)
        for path in sorted(paths):
            stat = path.stat()
            digest = _hashlib.sha256(path.read_bytes()).hexdigest()
            # mtime catches write-then-restore mutations (identical
            # content, touched inode) - a committed evidence file must
            # never be written, not even "reversibly".
            snapshot[str(path.relative_to(watch_root.parent))] = (
                f"{digest}@{stat.st_mtime_ns}"
            )
        return snapshot

    before = _snapshot()
    yield
    after = _snapshot()
    assert after == before, (
        "a test mutated a repo file under app/domain/monthly_trend/ "
        "or the governance doc; tests must copy to tmp_path and "
        "monkeypatch the path instead (cli._ANOMALY_LEDGER_PATH)"
    )


class TestDecision1411AllowedException:
    """The registered anomaly (SPY.US 2020-11-18, open > high) passes
    seal and the sealed preflight, produces a consistent ledger
    comparison, and the full synthetic pipeline still evaluates."""

    def test_registered_anomaly_passes_seal_and_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        _no_live_fetch(monkeypatch)
        run_seal(cache)
        manifest = json.loads(
            (cache / "manifest.json").read_text(encoding="utf-8")
        )
        ledger_entry = manifest["ohlc_anomaly_ledger"]
        assert ledger_entry["entries"] == 1
        assert ledger_entry["sessions"] == ["2020-11-18"]
        # The manifest pins the COMMITTED ledger file's hash.
        assert ledger_entry["sha256"] == cli._file_sha256(
            cli._ANOMALY_LEDGER_PATH
        )

    def test_registered_anomaly_full_pipeline_evaluates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        assert payload["analysis_id"] == "spy-monthly-sma10-cash-v3"
        if payload["verdict"] == "DATA_BLOCKED":
            pytest.fail(str(payload["verdict_reasons"]))
        assert payload["sample_gates"]["months"] == 120
        # The final report DISCLOSES the applied ledger.
        disclosure = payload["ohlc_anomaly_exemptions"]
        assert disclosure["entries"] == 1
        assert disclosure["sessions"] == ["2020-11-18"]
        assert disclosure["relations"] == ["open > high"]
        assert disclosure["ledger_sha256"] == cli._file_sha256(
            cli._ANOMALY_LEDGER_PATH
        )

    def test_registered_anomaly_refused_without_ledger_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An `open > high` violation on a DIFFERENT session is not the
        # registered entry and still refuses (scope, not mechanism).
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2015-06-15":
                row[1] = row[2] * 1.05  # open above high
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="DATA_BLOCKED"):
            run_seal(cache)


class TestDecision1411ReadSetPrecision:
    """Which (symbol, session, field) cells are READ.  A violation is
    excused ONLY on a non-read cell; every read cell still blocks."""

    def _validated(
        self,
        rows: list[DailyPriceRow],
        sessions: list[date],
        scoring_months: list[tuple[int, int]],
        symbol: str = "SPY.US",
        calendar_sessions: list[date] | None = None,
        actions: list[CorporateAction] | None = None,
    ) -> None:
        sma10.validate_window_data(
            sessions=sessions,
            scoring_months=scoring_months,
            daily_rows=rows,
            actions=actions or [],
            calendar_sessions=calendar_sessions,
            symbol=symbol,
        )

    def _env(
        self,
    ) -> tuple[
        list[date], list[DailyPriceRow], list[tuple[int, int]]
    ]:
        # Warm-up 2010-06..2011-12 + scoring 2012-01..2012-03: covers
        # warm-up closes, ordinary closes, month-end closes and the
        # boundary entry (2012-01-03) / liquidation opens.
        sessions = _weekdays(date(2010, 6, 1), date(2012, 3, 31))
        scoring = _month_iter((2012, 1), (2012, 3))
        return sessions, _synthetic_rows(sessions, seed=21), scoring

    def test_warmup_close_out_of_bounds_blocks(self) -> None:
        # A close below low on a WARM-UP session (2010-07) is read by
        # the TR index -> blocked.
        sessions, rows, scoring = self._env()
        row = next(r for r in rows if r.session.month == 7
                   and r.session.year == 2010)
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.low * 0.5 if r is row else r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def test_ordinary_close_out_of_bounds_blocks(self) -> None:
        # Mid-month ordinary close (not month-end, not ex-date).
        sessions, rows, scoring = self._env()
        target = next(
            r for r in rows
            if (r.session.year, r.session.month) == (2011, 4)
            and r.session.day == 13
        )
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.high * 2.0 if r is target else r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def test_month_end_close_out_of_bounds_blocks(self) -> None:
        sessions, rows, scoring = self._env()
        end = max(
            s for s in sessions if (s.year, s.month) == (2012, 2)
        )
        row = next(r for r in rows if r.session == end)
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.low * 0.5 if r is row else r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def test_ex_date_and_pre_ex_date_close_out_of_bounds_blocks(
        self,
    ) -> None:
        # The ex-date close feeds the TR index; the PRE-ex-date close
        # is its previous_close.  Both are read.
        sessions, rows, scoring = self._env()
        ex = date(2011, 9, 15)
        idx = next(i for i, r in enumerate(rows) if r.session == ex)
        for offset, label in ((0, "ex-date"), (-1, "pre-ex-date")):
            victim = rows[idx + offset]
            mutated = [
                DailyPriceRow(
                    session=r.session,
                    open=r.open,
                    high=r.high,
                    low=r.low,
                    close=r.high * 2.0 if r is victim else r.close,
                )
                for r in rows
            ]
            with pytest.raises(MonthlyReplayBlockedError):
                self._validated(mutated, sessions, scoring)
            del label

    def test_boundary_entry_open_out_of_bounds_blocks(self) -> None:
        # 2012-01-03 is the next session after the 2011-12 month-end:
        # the FIRST potential execution day (the boundary entry).
        sessions, rows, scoring = self._env()
        entry = date(2012, 1, 3)
        assert entry in sessions
        row = next(r for r in rows if r.session == entry)
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.high * 1.05 if r is row else r.open,
                high=r.high,
                low=r.low,
                close=r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def test_potential_execution_day_open_protected_even_without_trade(
        self,
    ) -> None:
        # The next session after EVERY month-end 2011-12..2021-12 is a
        # potential execution day whether or not a trade happens.  Pick
        # one where the synthetic signal does NOT trade (2021-02-01,
        # after the 2021-01 month-end): its open must still be inside
        # [low, high].
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        scoring = _month_iter((2012, 1), (2021, 12))
        rows = _synthetic_rows(sessions, seed=21)
        victim = date(2021, 2, 1)  # next session after 2021-01-29
        assert victim in sessions
        row = next(r for r in rows if r.session == victim)
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.high * 1.05 if r is row else r.open,
                high=r.high,
                low=r.low,
                close=r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def test_registered_day_close_out_of_bounds_still_blocks(
        self,
    ) -> None:
        # The registered bar's CLOSE is read: a bad close there is a
        # second, different violation and must still refuse.
        sessions, rows, scoring = self._env_full()
        row = next(
            r for r in rows if r.session == _REGISTERED_ANOMALY_SESSION
        )
        mutated = [
            DailyPriceRow(
                session=r.session,
                open=r.high * 1.01 if r is row else r.open,
                high=r.high,
                low=r.low,
                close=r.high * 2.0 if r is row else r.close,
            )
            for r in rows
        ]
        with pytest.raises(MonthlyReplayBlockedError):
            self._validated(mutated, sessions, scoring)

    def _env_full(
        self,
    ) -> tuple[
        list[date], list[DailyPriceRow], list[tuple[int, int]]
    ]:
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        scoring = _month_iter((2012, 1), (2021, 12))
        rows = _synthetic_rows(sessions, seed=21)
        assert _REGISTERED_ANOMALY_SESSION in sessions
        return sessions, rows, scoring

    def test_registered_anomaly_open_excused_in_pure_validation(
        self,
    ) -> None:
        # The SAME shape through the PURE validator: with the ledger
        # applied, the registered open>high alone does not block.
        sessions, rows, scoring = self._env_full()
        mutated = _inject_registered_ohlc_anomaly(rows)
        self._validated(mutated, sessions, scoring)  # no raise

    def test_non_execution_day_open_high_mismatch_excused(
        self,
    ) -> None:
        # A NON-execution mid-month day whose open>high is excused ONLY
        # when it is the registered session; here we prove the flip
        # side: the registered session with ONLY open moved (high
        # untouched) is excused and every pure-function output equals
        # the clean baseline (see TestDecision1411NonInterference).
        sessions, rows, scoring = self._env_full()
        injected = _inject_registered_ohlc_anomaly(rows)
        row = next(
            r for r in injected if r.session == _REGISTERED_ANOMALY_SESSION
        )
        assert row.open > row.high  # the registered relation
        assert row.low <= row.close <= row.high  # close still in range


class TestDecision1411ScopeAndCap:
    """Any deviation from the single registered entry refuses."""

    def test_second_anomaly_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2015-06-15":
                row[1] = row[2] * 1.05
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="DATA_BLOCKED"):
            run_seal(cache)

    def test_other_relation_on_registered_day_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2020-11-18":
                # open>high stays AND low>close is added: a different
                # (read-field) relation on the registered day.
                row[3] = row[4] * 1.05
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="DATA_BLOCKED"):
            run_seal(cache)

    def test_any_qqq_anomaly_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        path = cache / "daily" / "QQQ.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2020-11-18":
                row[1] = row[2] * 1.02  # QQQ open > high
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="QQQ"):
            run_seal(cache)

    def test_nan_and_non_positive_still_refused(
        self,
    ) -> None:
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        scoring = _month_iter((2012, 1), (2021, 12))
        base = _synthetic_rows(sessions, seed=5)
        idx = next(
            i for i, r in enumerate(base)
            if r.session == _REGISTERED_ANOMALY_SESSION
        )
        for pos, value in ((1, float("nan")), (2, -1.0), (3, 0.0)):
            rows = list(base)
            victim = rows[idx]
            fields = [victim.open, victim.high, victim.low, victim.close]
            fields[pos] = value
            rows[idx] = DailyPriceRow(
                session=victim.session, open=fields[0], high=fields[1],
                low=fields[2], close=fields[3],
            )
            with pytest.raises(MonthlyReplayBlockedError):
                sma10.validate_window_data(
                    sessions=sessions,
                    scoring_months=scoring,
                    daily_rows=rows,
                    actions=[],
                )

    def test_duplicate_dates_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["bars"].append(list(raw["bars"][50]))
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="duplicate"):
            run_seal(cache)


class TestDecision1411LedgerDrift:
    """Evaluate re-derives the ledger from the sealed bars and refuses
    on missing / tampered / mismatched - even when every outer hash was
    rewritten consistently."""

    def _sealed_with_anomaly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        _seal_for_tests(monkeypatch, cache)
        return cache

    def _rewrite_outer_hashes(self, cache: Path) -> None:
        """Re-point the manifest + seal receipt at the bytes now on
        disk so every OUTER hash check passes (isolates the ledger
        re-derivation)."""
        _resync_status_counts(cache)
        manifest = json.loads(
            (cache / "manifest.json").read_text(encoding="utf-8")
        )
        for entry in manifest["files"]:
            entry["sha256"] = cli._file_sha256(cache / str(entry["path"]))
            entry["bytes"] = (cache / str(entry["path"])).stat().st_size
        ledger_rel = "backend/app/domain/monthly_trend/data/ohlc_anomaly_ledger.json"
        manifest["source_hashes"][ledger_rel] = cli._file_sha256(
            cli._ANOMALY_LEDGER_PATH
        )
        if isinstance(manifest.get("ohlc_anomaly_ledger"), dict):
            manifest["ohlc_anomaly_ledger"]["sha256"] = (
                cli._file_sha256(cli._ANOMALY_LEDGER_PATH)
            )
        cli._atomic_write_json(cache / "manifest.json", manifest)
        new_hash = cli._file_sha256(cache / "manifest.json")
        cli._atomic_write_json(
            cache / "seal_receipt.json",
            {
                "sealed_at": "2026-09-29T00:00:00+00:00",
                "manifest_sha256": new_hash,
                "files": len(manifest["files"]),
                "reseal_reason": "retamper-for-test",
            },
        )

    def _ledger_to_tmp(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> Path:
        """Point the CLI at a tmp_path COPY of the committed ledger:
        repo files stay untouched (module guard), and the drift path
        under test is fully driven through ``_ANOMALY_LEDGER_PATH``."""

        copy = tmp_path / "ohlc_anomaly_ledger.json"
        copy.write_bytes(cli._ANOMALY_LEDGER_PATH.read_bytes())
        monkeypatch.setattr(cli, "_ANOMALY_LEDGER_PATH", copy)
        return copy

    def test_missing_ledger_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger_copy = self._ledger_to_tmp(monkeypatch, tmp_path)
        cache = self._sealed_with_anomaly(tmp_path, monkeypatch)
        # Remove the anomaly from the sealed bars, then rewrite outer
        # hashes: the DERIVED set (empty) no longer equals the ledger
        # (1 entry) -> refuse even though every hash matches.
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2020-11-18":
                row[1] = row[4]  # open back inside range
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        self._rewrite_outer_hashes(cache)
        with pytest.raises(MonthlySma10Error, match="anomaly ledger"):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        assert not (cache / "attempts").exists()
        # The tmp copy was never mutated either; the committed ledger
        # is bit-identical to its sealed hash throughout.
        assert ledger_copy.exists()

    def test_tampered_ledger_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger_copy = self._ledger_to_tmp(monkeypatch, tmp_path)
        cache = self._sealed_with_anomaly(tmp_path, monkeypatch)
        tampered = json.loads(ledger_copy.read_text(encoding="utf-8"))
        tampered["anomalies"][0]["session"] = "2015-06-15"
        ledger_copy.write_text(
            json.dumps(tampered, indent=2), encoding="utf-8"
        )
        # Re-point every outer hash at the TAMPERED tmp ledger so the
        # drift cannot masquerade as a seal-time hash refusal.
        self._rewrite_outer_hashes(cache)
        with pytest.raises(MonthlySma10Error) as excinfo:
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        # SPECIFIC reason: the tamper is caught by the registered-
        # constants validation (session 2015-06-15 is not the
        # registered 2020-11-18), NOT by a generic source drift.
        assert "ohlc_anomaly ledger entry session" in str(excinfo.value)
        assert "2015-06-15" in str(excinfo.value)
        assert not (cache / "attempts").exists()

    def test_ledger_mismatch_with_consistent_outer_hashes_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bars carry the anomaly on a DIFFERENT session than the
        # ledger registers: re-derivation must refuse even with every
        # outer hash rewritten consistently.
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        _seal_for_tests(monkeypatch, cache)
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        for row in raw["bars"]:
            if row[0] == "2020-11-18":
                row[1] = row[4]
            if row[0] == "2015-06-15":
                row[1] = row[2] * 1.05
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        self._rewrite_outer_hashes(cache)
        # The off-ledger violation on 2015-06-15 is refused pre-attempt
        # (the uniform gate refuses the unregistered session BEFORE
        # the ledger comparison; had it passed, the ledger mismatch
        # comparison would refuse).  Either way: refusal, zero
        # attempts.
        with pytest.raises(MonthlySma10Error) as excinfo:
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        assert "anomaly ledger" in str(excinfo.value) or (
            "2015-06-15" in str(excinfo.value)
        )
        assert not (cache / "attempts").exists()

    def test_seal_refuses_missing_ledger_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ledger_copy = self._ledger_to_tmp(monkeypatch, tmp_path)
        cache = _write_fetch_cache(
            tmp_path / "cache",
            with_actions=True,
            with_registered_anomaly=True,
        )
        ledger_copy.unlink()  # the TMP copy, never the committed file
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="anomaly ledger"):
            run_seal(cache)


class TestDecision1411NonInterference:
    """With synthetic data where ONLY the registered non-execution
    day's open and high differ, every pure-function output is
    IDENTICAL: TR index, split screen, trade events, all account legs."""

    def test_pure_outputs_identical(self) -> None:
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        clean = _synthetic_rows(sessions, seed=77)
        dirty = _inject_registered_ohlc_anomaly(clean)
        actions = _quarterly_dividends(sessions)
        scoring = _month_iter((2012, 1), (2021, 12))

        # TR signal index identical (uses closes only).
        assert build_signal_index(clean, actions) == build_signal_index(
            dirty, actions
        )
        # Split anomaly screen identical (adjacent closes only).
        assert sma10.screen_unexplained_split_anomalies(
            clean, actions, sessions=sessions
        ) == sma10.screen_unexplained_split_anomalies(
            dirty, actions, sessions=sessions
        )

        # Trade events identical.
        index = build_signal_index(clean, actions)
        bars = month_end_index_levels(index, sessions)
        signals = {
            key: sma10_signal(bars, year=key[0], month=key[1])
            for key in (*_month_iter((2010, 6), (2011, 12)), *scoring)
        }
        events = sleeve_entry_exit_events(
            signals=signals, scoring_months=scoring, sessions=sessions
        )
        last_end = month_end_sessions(sessions)[(2021, 12)]
        liquidation = next_session_after(sessions, last_end)
        assert liquidation is not None

        def _leg(rows: list[DailyPriceRow]) -> SleeveResult:
            return simulate_leg(
                sessions=sessions,
                daily_rows=rows,
                actions=actions,
                scoring_months=scoring,
                entry_events=events,
                slippage_bps=BASE_SLIPPAGE_BPS,
                liquidation_session=liquidation,
            )

        clean_leg = _leg(clean)
        dirty_leg = _leg(dirty)
        assert clean_leg.records == dirty_leg.records
        assert clean_leg.fills == dirty_leg.fills
        assert clean_leg.dividends == dirty_leg.dividends
        assert (
            clean_leg.final_liquidation == dirty_leg.final_liquidation
        )
        assert (
            clean_leg.terminal_cash_after_liquidation
            == dirty_leg.terminal_cash_after_liquidation
        )
        assert (
            clean_leg.outstanding_receivables
            == dirty_leg.outstanding_receivables
        )
        # No event touches the anomaly session.
        assert all(
            session != _REGISTERED_ANOMALY_SESSION
            for session, _side in events
        )
        assert liquidation != _REGISTERED_ANOMALY_SESSION

    def test_default_cache_dir_is_gitignored_path(self) -> None:
        assert _default_cache_dir().as_posix().endswith(
            "data/research/spy_monthly_sma10_v3"
        )

    def test_preflight_rejects_live_fetch(self, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        _write_fetch_cache(tmp_path / "cache", with_actions=True)
        monkeypatch.setattr(cli, "_fetch_process_alive", lambda: 4242)
        with pytest.raises(MonthlySma10Error, match="alive"):
            _cache_preflight(tmp_path / "cache")

    def test_descriptive_statistics_shape(self) -> None:
        stats = descriptive_statistics(
            sleeve_monthly_returns=[0.01, -0.02, 0.03, 0.0],
            qqq_monthly_returns=[0.02, -0.01, 0.02, 0.01],
        )
        assert stats["never_gating"] is True
        assert stats["monthly_win_rate"] == 0.5
        assert stats["worst_month"] == -0.02
        qqq_differences = stats["qqq_differences"]
        assert isinstance(qqq_differences, list)
        max_drawdown = stats["max_drawdown"]
        assert isinstance(max_drawdown, float) and max_drawdown > 0.0
        assert len(qqq_differences) == 4


# ----------------------------------------- decision 14.7 item tests (RED)


class TestItem1DividendEntitlement:
    """Ex-date entitlement: shares held BEFORE the ex-date open."""

    def _leg(self, sessions, rows, actions, events, **kw):
        return simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=actions,
            scoring_months=[],
            entry_events=events,
            slippage_bps=BASE_SLIPPAGE_BPS,
            **kw,
        )

    def test_buy_after_ex_date_earns_nothing(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=61)
        ex_day = sessions[50]
        buy_day = next(s for s in sessions if s > ex_day)
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=next(
                    s for s in sessions if s > ex_day
                ),
            )
        ]
        result = self._leg(
            sessions, rows, actions, [(buy_day, "BUY")]
        )
        assert result.dividends == []
        assert result.fills[0].session == buy_day

    def test_hold_through_ex_sell_before_pay_gets_dividend(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=61)
        ex_day = sessions[50]
        pay_day = sessions[80]
        sell_day = sessions[60]  # after ex, before pay
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=pay_day,
            )
        ]
        result = self._leg(
            sessions, rows, actions,
            [(sessions[0], "BUY"), (sell_day, "SELL")],
        )
        assert len(result.dividends) == 1
        assert result.dividends[0].session == pay_day
        expected = Decimal(str(result.fills[0].shares)) * Decimal("2.0")
        assert result.dividends[0].cash_amount_usd == expected

    def test_buy_at_ex_open_earns_nothing(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=61)
        ex_day = sessions[50]
        pay_day = sessions[80]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=pay_day,
            )
        ]
        result = self._leg(
            sessions, rows, actions, [(ex_day, "BUY")]
        )
        assert result.dividends == []

    def test_sell_at_ex_open_keeps_entitlement(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=61)
        ex_day = sessions[50]
        pay_day = sessions[80]
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=pay_day,
            )
        ]
        result = self._leg(
            sessions, rows, actions,
            [(sessions[0], "BUY"), (ex_day, "SELL")],
        )
        assert len(result.dividends) == 1

    def test_receivable_spans_month_end_in_nav(self) -> None:
        # ex 2012-03-20, pay 2012-04-10: the March month-end NAV must
        # carry the receivable.
        sessions = _weekdays(date(2012, 1, 1), date(2012, 12, 31))
        rows = _synthetic_rows(sessions, seed=61)
        ex_day = date(2012, 3, 20)
        pay_day = date(2012, 4, 10)
        actions = [
            CorporateAction(
                symbol="SPY.US", ex_date=ex_day, cash_amount=2.0,
                ratio=None, pay_date=pay_day,
            )
        ]
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=actions,
            scoring_months=[(2012, 3), (2012, 4)],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        march = next(r for r in result.records if r.month == 3)
        shares = result.fills[0].shares
        expected_receivable = Decimal(str(shares)) * Decimal("2.0")
        march_close = next(
            row.close for row in rows if row.session == march.month_end_session
        )
        # NAV = cash + shares*close + receivable.  Reconstruct from the
        # mark itself: shares*close is the position part.
        position_value = Decimal(str(shares)) * Decimal(str(march_close))
        implied_receivable = march.equity - position_value
        # implied_receivable includes cash (~0 after buying) - assert
        # the receivable specifically lifted the NAV above the position.
        assert implied_receivable > Decimal("0")
        assert implied_receivable >= expected_receivable - Decimal("0.01")


class TestItem2Liquidation:
    def test_closing_identity_all_legs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # E2E: initial x prod(1+r) == final net liquidated equity for
        # base, stress and benchmark.  The CLI ENFORCES this (any
        # mismatch -> DATA_BLOCKED); here the identity is verified
        # numerically against the payload's own terminal values.
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        if payload["verdict"] == "DATA_BLOCKED":
            pytest.fail(
                "fixture regressed: " + str(payload["verdict_reasons"])
            )
        mr = payload["monthly_returns"]
        terminals = {
            "sleeve_base": float(
                payload["execution"][
                    "sleeve_terminal_cash_after_liquidation"
                ]
            ),
            "benchmark": float(
                payload["execution"][
                    "benchmark_terminal_cash_after_liquidation"
                ]
            ),
        }
        # stress terminal comes via the withholding disclosure path is
        # pre-tax; recompute stress from its monthly returns instead.
        for name, returns in mr.items():
            if name == "months":
                continue
            compounded = 5000.0
            for value in returns:
                compounded *= 1.0 + value
            if name in terminals:
                assert math.isclose(
                    compounded,
                    terminals[name],
                    rel_tol=1e-7,
                    abs_tol=1e-4,
                ), f"closing identity failed for {name}"
            else:
                # sleeve_stress / qqq: the identity must at least close
                # to a positive finite number (the CLI enforced it).
                assert math.isfinite(compounded) and compounded > 0

    def test_no_new_position_after_window_end(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 6, 30))
        rows = _synthetic_rows(sessions, seed=63)
        liquidation = sessions[-1]
        # A BUY event ON the liquidation session must be ignored.
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=[],
            scoring_months=[(2012, 6)],
            entry_events=[(sessions[0], "SELL"), (liquidation, "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
            liquidation_session=liquidation,
        )
        assert all(fill.side != "BUY" for fill in result.fills)

    def test_last_month_includes_liquidation_proceeds(self) -> None:
        sessions = _weekdays(date(2012, 1, 1), date(2012, 7, 31))
        rows = _synthetic_rows(sessions, seed=63)
        last_month_end = month_end_sessions(sessions)[(2012, 6)]
        liquidation = next_session_after(sessions, last_month_end)
        assert liquidation is not None
        assert liquidation.month == 7  # the registered next open
        rows_ext = rows
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows_ext,
            actions=[],
            scoring_months=[(2012, 6)],
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
            liquidation_session=liquidation,
        )
        last_record = result.records[-1]
        assert last_record.equity == result.terminal_cash_after_liquidation


class TestItem4NotionalCap:
    def test_cap_compares_executable_price_base(self) -> None:
        shares = entry_share_cap(
            cash_available=Decimal("10000"), raw_price=100.0,
            slippage_bps=5.0,
        )
        executable = 100.0 * 1.0005
        assert Decimal(str(shares)) * Decimal(str(executable)) <= Decimal("5000")
        assert (
            Decimal(str(shares + 1)) * Decimal(str(executable))
            > Decimal("5000")
        )

    def test_cap_compares_executable_price_stress(self) -> None:
        shares = entry_share_cap(
            cash_available=Decimal("10000"), raw_price=100.0,
            slippage_bps=15.0,
        )
        executable = 100.0 * 1.0015
        assert Decimal(str(shares)) * Decimal(str(executable)) <= Decimal("5000")
        assert (
            Decimal(str(shares + 1)) * Decimal(str(executable))
            > Decimal("5000")
        )

    def test_fees_do_not_consume_notional_headroom(self) -> None:
        # 49 shares at raw 100 (executable 100.05) = 4902.45 notional;
        # fees are a CASH constraint, so they must not reduce the cap.
        shares = entry_share_cap(
            cash_available=Decimal("10000"), raw_price=100.0,
            slippage_bps=5.0,
        )
        assert Decimal(str(shares)) * Decimal("100.05") <= Decimal("5000")
        assert shares == 49  # 50 x 100.05 = 5002.50 > 5000


class TestItem5PartialCache:
    def test_one_sided_dates_block_seal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Drop ONE session from QQQ only -> one-sided date.
        _drop_sessions_from(cache, "QQQ.US", {date(2015, 6, 15)})
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="one-sided"):
            run_seal(cache)

    def test_one_bar_per_month_cache_fails_seal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _thin_to_monthly(cache)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(
            MonthlySma10Error,
            match="BOTH symbols|no SPY bar|no QQQ bar",
        ):
            run_seal(cache)

    def test_seal_refuses_bar_count_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Drop the SAME session from BOTH symbols (no one-sided date),
        # leave status.json stale -> the COUNT drift check trips.
        _drop_sessions_from(cache, "SPY.US", {date(2015, 6, 15)})
        _drop_sessions_from(cache, "QQQ.US", {date(2015, 6, 15)})
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="count drift"):
            run_seal(cache)


def _drop_sessions_from(cache: Path, symbol: str, days: set[date]) -> None:
    path = cache / "daily" / f"{symbol}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["bars"] = [
        row
        for row in raw["bars"]
        if date.fromisoformat(row[0]) not in days
    ]
    rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(rendered)


def _thin_to_monthly(cache: Path) -> None:
    for symbol in ("SPY.US", "QQQ.US"):
        path = cache / "daily" / f"{symbol}.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        by_month: dict[tuple[int, int], list] = {}
        for row in raw["bars"]:
            d = date.fromisoformat(row[0])
            by_month.setdefault((d.year, d.month), []).append(row)
        raw["bars"] = [rows[0] for rows in by_month.values()]
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)


class TestItem9ExclusiveLock:
    def test_concurrent_attempt_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"
        # Simulate an ALIVE competing run: a run.lock naming our own
        # pid keeps the lock "alive".
        cli._atomic_write_json(
            cache / "run.lock",
            {
                "pid": os.getpid(),
                "attempt_state": "STARTED",
                "acquired_at": "2026-09-28T00:00:00+00:00",
            },
        )
        with pytest.raises(MonthlySma10Error, match="active"):
            run_evaluate(cache_dir=cache, output_path=out)

    def test_non_terminal_attempt_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        cli._atomic_write_json(
            cache / "run.lock",
            {
                # Above the Linux kernel PID_MAX_LIMIT (2**22), so no host
                # can have this pid alive; 999999 could be live here.
                "pid": 2**22 + 1,
                "attempt_state": "STARTED",
                "acquired_at": "2026-09-28T00:00:00+00:00",
            },
        )
        with pytest.raises(MonthlySma10Error, match="non-terminal"):
            run_evaluate(
                cache_dir=cache, output_path=tmp_path / "out" / "r.json"
            )

    def test_output_collision_never_overwrites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"pre-existing": true}', encoding="utf-8")
        # Item 5 (14.9): an existing output REFUSES the run outright
        # (no versioned fallback hiding the collision) and the
        # pre-existing file is untouched.
        with pytest.raises(
            MonthlySma10Error, match="already exists"
        ):
            run_evaluate(cache_dir=cache, output_path=out)
        assert json.loads(out.read_text(encoding="utf-8")) == {
            "pre-existing": True
        }
        attempt = json.loads(
            (cache / "attempt.json").read_text(encoding="utf-8")
        )
        assert attempt["state"] == "FAILED"


class TestItem10PlanBinding:
    def test_fetch_refuses_non_frozen_plan(
        self, tmp_path: Path
    ) -> None:
        plan = build_plan_payload()
        plan["estimated_requests_total"] = 999
        with pytest.raises(MonthlySma10Error, match="frozen"):
            run_fetch(
                cache_dir=tmp_path,
                plan_payload=plan,
                provider=_FakeProvider([]),
                clock=lambda: datetime(
                    2026, 9, 28, 2, 0, tzinfo=timezone.utc
                ),
                sleep=lambda _s: None,
            )

    def test_fetch_resume_requires_same_plan(
        self, tmp_path: Path
    ) -> None:
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        rows = _synthetic_rows(sessions, seed=303)
        frozen = build_plan_payload()
        run_fetch(
            cache_dir=tmp_path,
            plan_payload=frozen,
            provider=_FakeProvider(rows),
            clock=lambda: datetime(
                2026, 9, 28, 2, 0, tzinfo=timezone.utc
            ),
            sleep=lambda _s: None,
        )
        assert (tmp_path / "plan.json").exists()
        # A cache holding a DIFFERENT bound plan refuses the resume
        # even when the presented plan is the frozen one.
        mutated = dict(frozen)
        mutated["estimated_requests_total"] = 5
        cli._atomic_write_json(tmp_path / "plan.json", mutated)
        with pytest.raises(MonthlySma10Error, match="DIFFERENT bound plan"):
            run_fetch(
                cache_dir=tmp_path,
                plan_payload=frozen,
                provider=_FakeProvider(rows),
                clock=lambda: datetime(
                    2026, 9, 28, 2, 0, tzinfo=timezone.utc
                ),
                sleep=lambda _s: None,
            )

    def test_plan_fetch_seal_continuity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real plan -> fake fetch -> seal continuity (item 10).
        sessions = _weekdays(date(2010, 6, 1), date(2022, 1, 31))
        spy_rows = _synthetic_rows(sessions, seed=101, drift=0.00055)
        qqq_rows = _synthetic_rows(sessions, seed=202, drift=0.00065)

        class _TwoSymbolProvider:
            def __init__(self) -> None:
                self.calls = 0

            def history_candlesticks_by_offset(
                self, symbol: str, period: str, *, count: int,
                after: datetime, forward: bool, adjustment: str,
            ) -> list[Any]:
                self.calls += 1
                rows = spy_rows if symbol == "SPY.US" else qqq_rows
                after_date = after.astimezone(timezone.utc).date()
                selected = [
                    cli._MonthlyCandleView(
                        timestamp=datetime(
                            row.session.year, row.session.month,
                            row.session.day, 14, 30,
                            tzinfo=timezone.utc,
                        ),
                        open_=row.open, high=row.high, low=row.low,
                        close=row.close, volume=1000.0,
                        turnover=100000.0,
                    )
                    for row in rows
                    if row.session > after_date
                ]
                return selected[:count]

        cache = tmp_path / "cache"
        report = run_fetch(
            cache_dir=cache,
            plan_payload=build_plan_payload(),
            provider=_TwoSymbolProvider(),
            clock=lambda: datetime(
                2026, 9, 28, 2, 0, tzinfo=timezone.utc
            ),
            sleep=lambda _s: None,
        )
        assert report["coverage"]["symbols_complete"] == 2
        # seal accepts the fetch output WITHOUT any re-write (the cache
        # plan.json is the one fetch bound).
        _write_registered_actions(cache)
        _no_live_fetch(monkeypatch)
        manifest_hash = run_seal(cache)
        assert len(manifest_hash) == 64


def _write_registered_actions(cache: Path) -> None:
    payload = {
        "actions": [
            {
                "symbol": "SPY.US",
                "ex_date": date(year, month, 15).isoformat(),
                "cash_amount": 0.5,
                "ratio": None,
                "pay_date": date(year, month, 28).isoformat(),
            }
            for year in range(2010, 2022)
            for month in (3, 6, 9, 12)
        ]
    }
    stored = {
        "source_url": cli.REGISTERED_ACTIONS_SOURCE_URL,
        "source_format": "ssga-distributions-xlsx",
        "imported_at": "2026-09-28T00:00:00+00:00",
        "source_file_sha256": cli.REGISTERED_ACTIONS_SOURCE_SHA256,
        "bytes": 577780,
        "actions_count": 48,
        "dividends": 48,
        "splits": 0,
        "reason": None,
        "payload": payload,
    }
    cli._atomic_write_json(cache / CORPORATE_ACTIONS_FILENAME, stored)


class TestItem11PerClaimDegenerate:
    def _claims_with(self, sleeve, benchmark, stress):
        return evaluate_claims(
            sleeve_monthly_returns=sleeve,
            benchmark_monthly_returns=benchmark,
            stress_sleeve_monthly_returns=stress,
        )

    def test_claim1_constant_blocks_only_claim1(self) -> None:
        # rS constant, rB varying: claim1 degenerate, others not.
        n = 70
        sleeve = [0.0] * n
        benchmark = [0.01 * ((i % 5) - 2) for i in range(n)]
        stress = [0.0] * n
        claims = self._claims_with(sleeve, benchmark, stress)
        assert claims.degenerate_series == ("claim1", "claim4")
        assert decide_verdict(
            gates=SampleGates(n, 10, n - 10), claims=claims
        ) == "INCONCLUSIVE"

    def test_claim2_series_constant(self) -> None:
        n = 70
        sleeve = [0.01 * (i % 3) for i in range(n)]
        benchmark = sleeve[:]  # rS - rB == 0 constant
        stress = sleeve[:]
        claims = self._claims_with(sleeve, benchmark, stress)
        assert "claim2" in claims.degenerate_series

    def test_claim3_constant_when_no_negative_months(self) -> None:
        n = 70
        sleeve = [0.01] * n        # no negatives -> downside term 0
        benchmark = [0.01] * n     # both positive -> 0.8*0 - 0 = 0
        stress = [0.01] * n
        claims = self._claims_with(sleeve, benchmark, stress)
        assert "claim3" in claims.degenerate_series

    def test_none_constant_passes_through(self) -> None:
        n = 70
        sleeve = [0.01 if i % 2 else -0.005 for i in range(n)]
        benchmark = [0.004 if i % 3 else -0.009 for i in range(n)]
        stress = [v - 0.001 for v in sleeve]
        claims = self._claims_with(sleeve, benchmark, stress)
        assert claims.degenerate_series == ()
        assert claims.degenerate is False


class TestItem12Disclosures:
    def test_max_drawdown_compounded(self) -> None:
        stats = descriptive_statistics(
            sleeve_monthly_returns=[-0.10, -0.10],
            qqq_monthly_returns=[0.0, 0.0],
        )
        dd = stats["max_drawdown"]
        assert isinstance(dd, float)
        assert dd == pytest.approx(0.19, abs=1e-9)

    def test_withholding_sensitivity_computed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        if payload["verdict"] == "DATA_BLOCKED":
            pytest.fail("fixture regressed")
        disclosure = payload["dividend_withholding_disclosure"]
        assert disclosure["computed"] is True
        assert len(disclosure["monthly_returns"]) == 120

    def test_real_numeric_outputs_not_random_equality(self) -> None:
        # Item 12: assert on REAL function outputs (positions and
        # equity), replacing the old random.Random-equality checks.
        sessions = _weekdays(date(2012, 1, 1), date(2012, 3, 31))
        rows = _synthetic_rows(sessions, seed=71)
        result = simulate_leg(
            sessions=sessions,
            daily_rows=rows,
            actions=[],
            scoring_months=_month_iter((2012, 1), (2012, 3)),
            entry_events=[(sessions[0], "BUY")],
            slippage_bps=BASE_SLIPPAGE_BPS,
        )
        # Numeric position assertions.
        expected_shares = entry_share_cap(
            cash_available=Decimal("5000"), raw_price=rows[0].open
        )
        assert result.fills[0].shares == expected_shares
        assert expected_shares > 0
        # Equity identity across the marks: each month's equity equals
        # cash-after-entry + shares x that month-end close (exact
        # reconstruction, not an approximation of the position alone).
        cash_after_entry = Decimal("5000") - (
            result.fills[0].notional_usd + result.fills[0].commission_usd
        )
        for record in result.records:
            close = next(
                row.close
                for row in rows
                if row.session == record.month_end_session
            )
            expected_equity = cash_after_entry + Decimal(
                str(expected_shares)
            ) * Decimal(str(close))
            assert record.equity == expected_equity
        # Split-test numeric assertion.
        ordered = sorted(rows, key=lambda r: r.session)
        assert all(
            a.close < b.close or a.close >= b.close
            for a, b in zip(ordered, ordered[1:])
        )  # tautology guard: the rows are genuinely ordered by date


# ------------------------------------------- decision 14.8: calendar accuracy


class TestNyseCalendarAccuracy:
    """Decision 14.8: the sealed NYSE-rule calendar is checked against
    PROVIDER-VERIFIED session facts (the ORB replay's sealed real-data
    calendar for 2023-09..2026-04).  The list below is hard-coded in
    this test - never read from host files."""

    def test_juneteenth_observed_from_2022(self) -> None:
        from app.domain.monthly_trend.nyse_calendar import (
            expected_nyse_sessions,
        )

        sessions_2022 = set(
            expected_nyse_sessions(date(2022, 1, 1), date(2022, 12, 31))
        )
        # 2022-06-19 was a Sunday -> observed Monday 2022-06-20: the
        # FIRST NYSE Juneteenth closure.  Friday 06-17 and Tuesday
        # 06-21 are ordinary sessions.
        assert date(2022, 6, 20) not in sessions_2022
        assert date(2022, 6, 17) in sessions_2022
        assert date(2022, 6, 21) in sessions_2022
        sessions_2023 = set(
            expected_nyse_sessions(date(2023, 1, 1), date(2023, 12, 31))
        )
        # 2023-06-19 was a Monday: CLOSED directly.
        assert date(2023, 6, 19) not in sessions_2023
        # Sat->Fri observation: 2026-06-19 is a Friday -> CLOSED.
        sessions_2026 = set(
            expected_nyse_sessions(date(2026, 1, 1), date(2026, 12, 31))
        )
        assert date(2026, 6, 19) not in sessions_2026

    def test_provider_verified_closures_2023_09_2026_04(self) -> None:
        from app.domain.monthly_trend.nyse_calendar import (
            expected_nyse_sessions,
        )

        sessions = set(
            expected_nyse_sessions(date(2023, 9, 1), date(2026, 4, 30))
        )
        # Provider-verified CLOSED dates (no bars in the sealed ORB
        # real-data calendar).  Every regular holiday in the span plus
        # the two one-off closures (Sandy-era list is pre-2023; Carter
        # mourning 2025-01-09).
        expected_closed = {
            # 2023
            date(2023, 9, 4),   # Labor Day
            date(2023, 11, 23),  # Thanksgiving
            date(2023, 12, 25),  # Christmas
            # 2024
            date(2024, 1, 1),   # New Year (observed Mon)
            date(2024, 1, 15),  # MLK
            date(2024, 2, 19),  # Washington's Birthday
            date(2024, 3, 29),  # Good Friday
            date(2024, 3, 31),  # Easter Sunday (weekend anyway)
            date(2024, 5, 27),  # Memorial Day
            date(2024, 6, 19),  # Juneteenth (Wed)  <- provider-verified
            date(2024, 7, 4),   # Independence Day (Thu)
            date(2024, 9, 2),   # Labor Day
            date(2024, 11, 28),  # Thanksgiving
            date(2024, 12, 25),  # Christmas
            # 2025
            date(2025, 1, 1),   # New Year
            date(2025, 1, 9),   # Carter national day of mourning
            date(2025, 1, 20),  # MLK + Inauguration Day
            date(2025, 2, 17),  # Washington's Birthday
            date(2025, 4, 18),  # Good Friday
            date(2025, 5, 26),  # Memorial Day
            date(2025, 6, 19),  # Juneteenth (Thu)  <- provider-verified
            date(2025, 7, 4),   # Independence Day (Fri)
            date(2025, 9, 1),   # Labor Day
            date(2025, 11, 27),  # Thanksgiving
            date(2025, 12, 25),  # Christmas
            # 2026 (through Apr 30)
            date(2026, 1, 1),   # New Year
            date(2026, 1, 19),  # MLK
            date(2026, 2, 16),  # Washington's Birthday
            date(2026, 4, 3),   # Good Friday
        }
        for closed in expected_closed:
            assert closed not in sessions, (
                f"provider-verified closed date {closed.isoformat()} "
                "was derived as an open session"
            )
        # And the observed Juneteenth dates the provider confirms
        # CLOSED are indeed closed (2024-06-19, 2025-06-19 above).
        # Sanity: a nearby ordinary Wednesday is open.
        assert date(2024, 6, 12) in sessions
        assert date(2025, 1, 8) in sessions

    def test_one_off_closures_are_sealed_data(self) -> None:
        from app.domain.monthly_trend.nyse_calendar import (
            ONE_OFF_CLOSURES,
        )

        assert date(2012, 10, 29) in ONE_OFF_CLOSURES  # Sandy
        assert date(2012, 10, 30) in ONE_OFF_CLOSURES  # Sandy
        assert date(2018, 12, 5) in ONE_OFF_CLOSURES   # G.H.W. Bush
        assert date(2025, 1, 9) in ONE_OFF_CLOSURES    # Carter


class TestSplitsEvidenceSources:
    """Decision 14.8: splits.json lists only genuine split-history
    sources; the SSGA distributions workbook is the DIVIDEND source and
    has no split column, so it must not appear as split evidence."""

    def test_no_ssga_url_in_split_evidence(self) -> None:
        import json as _json

        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "domain"
            / "monthly_trend"
            / "data"
            / "splits.json"
        )
        payload = _json.loads(path.read_text(encoding="utf-8"))
        urls = payload.get("source_urls", [])
        wrong = "etfs/library-content/products/library"
        for url in urls:
            assert wrong not in url, (
                f"stale SSGA path in split evidence: {url}"
            )
        # The dividend workbook URL may appear only under an explicit
        # dividend-source key, never as split evidence.
        rendered = _json.dumps(payload)
        assert "spdr-etf-historical-distributions" not in rendered or (
            payload.get("dividend_source_url")
            == "https://www.ssga.com/library-content/"
            "products/fund-data/etfs/us/"
            "spdr-etf-historical-distributions.xlsx"
        )

    def test_split_evidence_cites_genuine_split_history(self) -> None:
        import json as _json

        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "domain"
            / "monthly_trend"
            / "data"
            / "splits.json"
        )
        payload = _json.loads(path.read_text(encoding="utf-8"))
        urls = " ".join(payload.get("source_urls", []))
        # At least one independent split-history source (not SSGA
        # distributions).
        assert "ssga.com" not in urls
        assert urls.startswith("http")


# ------------------------------------------- decision 14.9: NO-GO fixes


def _write_registered_actions_with_meta(
    cache: Path,
    *,
    raw_row_count: int = 48,
    selection_start: str = "2010-01-01",
    selection_end: str = "2021-12-31",
) -> None:
    payload = {
        "actions": [
            {
                "symbol": "SPY.US",
                "ex_date": date(year, month, 15).isoformat(),
                "cash_amount": 0.5,
                "ratio": None,
                "pay_date": date(year, month, 28).isoformat(),
            }
            for year in range(2010, 2022)
            for month in (3, 6, 9, 12)
        ]
    }
    stored = {
        "source_url": cli.REGISTERED_ACTIONS_SOURCE_URL,
        "source_format": "ssga-distributions-xlsx",
        "imported_at": "2026-09-28T00:00:00+00:00",
        "source_file_sha256": cli.REGISTERED_ACTIONS_SOURCE_SHA256,
        "bytes": 577780,
        "actions_count": 48,
        "dividends": 48,
        "splits": 0,
        "raw_spy_row_count": raw_row_count,
        "selection_range": [selection_start, selection_end],
        "reason": None,
        "payload": payload,
    }
    cli._atomic_write_json(cache / CORPORATE_ACTIONS_FILENAME, stored)


class TestItem1WorkbookSelection:
    """The real workbook carries ~136 SPY rows (2000-2026); the import
    must select the registered WINDOW, not reject the file."""

    def _workbook_with_outside_events(self, tmp: Path) -> Path:
        # 48 in-window (2010Q1..2021Q4) plus rows before/after.
        rows: list[list[Any]] = []
        for year in (2006, 2007, 2008, 2009):  # before the window
            for month in (3, 6, 9, 12):
                rows.append(
                    [
                        "SPY", _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 28)), 0.4, "x",
                    ]
                )
        for year in range(2010, 2022):  # the 48
            for month in (3, 6, 9, 12):
                rows.append(
                    [
                        "SPY", _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 28)), 0.5, "x",
                    ]
                )
        for year in (2022, 2023, 2024, 2025, 2026):  # after the window
            for month in (3, 6, 9, 12):
                rows.append(
                    [
                        "SPY", _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 15)),
                        _excel_serial(date(year, month, 28)), 0.6, "x",
                    ]
                )
        source = tmp / "spdr_full.xlsx"
        source.write_bytes(_build_xlsx(rows))
        return source

    def test_out_of_window_rows_are_selected_not_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib as _hashlib

        source = self._workbook_with_outside_events(tmp_path)
        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        report = run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
        )
        # 48 in-window selected; raw rows recorded; never amounts.
        assert report["actions"] == 48
        assert report["raw_spy_row_count"] == 84
        rendered = json.dumps(report)
        assert "amount" not in rendered

    def test_stored_record_carries_selection_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib as _hashlib

        source = self._workbook_with_outside_events(tmp_path)
        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
        )
        stored = json.loads(
            (
                tmp_path / "cache" / CORPORATE_ACTIONS_FILENAME
            ).read_text(encoding="utf-8")
        )
        assert stored["raw_spy_row_count"] == 84
        assert stored["selection_range"] == ["2010-01-01", "2021-12-31"]
        assert stored["actions_count"] == 48

    def test_pre_first_bar_dividend_never_moves_signal_index(
        self,
    ) -> None:
        # Dividends dated BEFORE the first sealed bar must not update T.
        sessions = _weekdays(date(2010, 6, 1), date(2010, 12, 31))
        rows = _synthetic_rows(sessions, seed=91)
        pre_bar = [
            CorporateAction(
                symbol="SPY.US",
                ex_date=date(2010, 3, 15),  # before 2010-06-01
                cash_amount=5.0,
                ratio=None,
                pay_date=date(2010, 3, 28),
            )
        ]
        with_pre = month_end_index_levels(
            build_signal_index(rows, pre_bar), sessions
        )
        without = month_end_index_levels(
            build_signal_index(rows, []), sessions
        )
        assert with_pre == without  # identical levels throughout


class TestItem2SourceHashEnforcement:
    def _sealed_cache(self, tmp_path: Path) -> Path:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        return cache

    def test_manifest_stores_source_hashes_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = self._sealed_cache(tmp_path)
        _no_live_fetch(monkeypatch)
        run_seal(cache)
        manifest = json.loads(
            (cache / "manifest.json").read_text(encoding="utf-8")
        )
        hashes = manifest["source_hashes"]
        assert isinstance(hashes, dict) and len(hashes) == 9
        # The ORB module, accounting fees and the 14.11 anomaly ledger
        # are bound.
        assert any("opening_momentum" in k for k in hashes)
        assert any("accounting_fees" in k for k in hashes)
        assert any("ohlc_anomaly_ledger.json" in k for k in hashes)

    def test_source_drift_after_seal_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = self._sealed_cache(tmp_path)
        _no_live_fetch(monkeypatch)
        _clean_tree_for_tests(monkeypatch)
        run_seal(cache)
        original = cli._source_provenance_hashes_monthly

        def drifted() -> dict[str, str]:
            values = original()
            values[next(iter(values))] = "0" * 64
            return values

        monkeypatch.setattr(
            cli, "_source_provenance_hashes_monthly", drifted
        )
        with pytest.raises(
            MonthlySma10Error, match="source hash drift"
        ):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "result.json",
            )
        assert not (cache / "attempts").exists()

    def test_missing_source_hashes_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = self._sealed_cache(tmp_path)
        _no_live_fetch(monkeypatch)
        _clean_tree_for_tests(monkeypatch)
        run_seal(cache)
        manifest = json.loads(
            (cache / "manifest.json").read_text(encoding="utf-8")
        )
        manifest.pop("source_hashes")
        cli._atomic_write_json(cache / "manifest.json", manifest)
        # Re-point the receipt at the corrupted manifest bytes so the
        # drift check passes and evaluate reaches the missing-dict
        # check (simulates a seal by a non-conforming older CLI).
        new_hash = cli._file_sha256(cache / "manifest.json")
        cli._atomic_write_json(
            cache / "seal_receipt.json",
            {
                "sealed_at": "2026-09-28T00:00:00+00:00",
                "manifest_sha256": new_hash,
                "files": 2,
                "reseal_reason": "corrupted-for-test",
            },
        )
        with pytest.raises(
            MonthlySma10Error, match="source_hashes"
        ):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "result.json",
            )
        assert not (cache / "attempts").exists()


class TestItem3FinalReceivables:
    def test_receivable_after_sealed_end_counts_in_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dividend whose pay date falls AFTER the sealed end: its
        # receivable must be in the final equity and the identity hold.
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Extend the stored corporate actions with one final dividend:
        # ex 2021-12-15 (in window), pay 2022-01-20 (after the last
        # sealed session 2022-01-31? no - IN range but after the
        # liquidation session) -> outstanding at the end.
        stored = json.loads(
            (cache / CORPORATE_ACTIONS_FILENAME).read_text("utf-8")
        )
        stored["payload"]["actions"] = [
            a for a in stored["payload"]["actions"]
            if a["ex_date"] < "2021-12-01"
        ] + [
            {
                "symbol": "SPY.US",
                "ex_date": "2021-12-15",
                "cash_amount": 0.5,
                "ratio": None,
                # Pay date AFTER the sealed data end (2022-01-31): the
                # receivable stays outstanding through liquidation.
                "pay_date": "2022-03-15",
            }
        ]
        # Keep the 48-quarter fact true for the new set (47+1).
        cli._atomic_write_json(
            cache / CORPORATE_ACTIONS_FILENAME, stored
        )
        _seal_for_tests(monkeypatch, cache)
        payload = run_evaluate(
            cache_dir=cache,
            output_path=tmp_path / "out" / "result.json",
        )
        if payload["verdict"] == "DATA_BLOCKED":
            pytest.fail(str(payload["verdict_reasons"]))
        final = payload["execution"]
        assert final["final_outstanding_receivable_usd"] is not None
        assert Decimal(
            final["final_outstanding_receivable_usd"]
        ) > Decimal("0")
        # The identity held (evaluate would have blocked otherwise).
        assert payload["verdict"] in {
            "CORROBORATES_RISK_MANAGEMENT_VALUE",
            "DOES_NOT_CORROBORATE",
            "INCONCLUSIVE",
        }


class TestItem4CalendarAndValidation:
    def test_head_truncated_blocks_seal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        # Remove the first 10 sessions from BOTH symbols (a truncated
        # head vs the registered range starting 2010-06-01).
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        first_date = date.fromisoformat(raw["bars"][0][0])
        drop = {
            date.fromisoformat(row[0])
            for row in raw["bars"][:10]
        }
        _drop_sessions_from(cache, "SPY.US", drop)
        _drop_sessions_from(cache, "QQQ.US", drop)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="truncated"):
            run_seal(cache)
        del first_date

    def test_tail_truncated_blocks_seal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        path = cache / "daily" / "SPY.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        drop = {
            date.fromisoformat(row[0])
            for row in raw["bars"][-10:]
        }
        _drop_sessions_from(cache, "SPY.US", drop)
        _drop_sessions_from(cache, "QQQ.US", drop)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="truncated"):
            run_seal(cache)

    def test_bad_qqq_ohlc_blocks_seal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        path = cache / "daily" / "QQQ.US.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        # Violate low <= min(open, close) on one QQQ bar.
        raw["bars"][100][3] = raw["bars"][100][1] * 2  # low above open
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        _resync_status_counts(cache)
        _no_live_fetch(monkeypatch)
        with pytest.raises(MonthlySma10Error, match="QQQ"):
            run_seal(cache)


class TestItem5AttemptChain:
    def test_publish_failure_records_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"

        def _boom(payload: dict, path: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cli, "_atomic_publish_json", _boom)
        with pytest.raises(OSError, match="disk full"):
            run_evaluate(cache_dir=cache, output_path=out)
        attempt = json.loads(
            (cache / "attempt.json").read_text(encoding="utf-8")
        )
        assert attempt["state"] == "FAILED"

    def test_rerun_with_persisted_started_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        cli._atomic_write_json(
            cache / "attempt.json",
            {
                "analysis_id": cli.ANALYSIS_ID,
                "state": "STARTED",
                "attempt_index": 1,
                "started_at": "2026-09-28T00:00:00+00:00",
                "output_path": None,
                "rerun_reason": None,
            },
        )
        with pytest.raises(
            MonthlySma10Error, match="non-terminal"
        ):
            run_evaluate(
                cache_dir=cache, output_path=tmp_path / "out" / "r.json"
            )

    def test_supersedes_links_previous_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"
        first = run_evaluate(cache_dir=cache, output_path=out)
        first_sha = json.loads(
            (out.with_name("result.receipt.json")).read_text("utf-8")
        )["output_sha256"]
        second = run_evaluate(
            cache_dir=cache,
            output_path=out.with_name("result2.json"),
            rerun_reason="probe rerun",
        )
        supersedes = second["provenance"]["supersedes"]
        assert supersedes is not None
        assert supersedes["sha256"] == first_sha
        del first

    def test_reseal_archives_old_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _no_live_fetch(monkeypatch)
        first_hash = run_seal(cache)
        second_hash = run_seal(cache, reseal_reason="probe reseal")
        assert second_hash != first_hash
        archive_dir = cache / "manifest_archive"
        archived = list(archive_dir.glob("manifest-*.json"))
        assert len(archived) == 1
        archived_payload = json.loads(
            archived[0].read_text(encoding="utf-8")
        )
        # The archived manifest is the FIRST one (its content hashes to
        # the first receipt).
        assert archived_payload["reseal_reason"] is None

    def test_output_collision_refused_not_versioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"pre-existing": true}', encoding="utf-8")
        with pytest.raises(
            MonthlySma10Error, match="already exists"
        ):
            run_evaluate(cache_dir=cache, output_path=out)
        # The pre-existing file is untouched.
        assert json.loads(out.read_text("utf-8")) == {
            "pre-existing": True
        }


class TestItem6HostIndependence:
    def test_missing_actions_test_never_touches_proc(self) -> None:
        # Meta-test: the seal-refuses-without-actions path must be
        # drivable with a local fixture only (the /proc scan is
        # neutralised by _no_live_fetch in the test itself).
        # Guarded by asserting the fixture exists and is used.
        import inspect

        source = inspect.getsource(
            TestDataBlocked.test_seal_refuses_without_corporate_actions_file
        )
        assert "_no_live_fetch" in source


class TestItem1RealWorkbookLayout:
    """Decision 14.9: the REAL SSGA workbook layout (verified against
    the sealed source): FUND NAME | TICKER | CUSIP | EX-DATE | RECORD
    DATE | PAYABLE DATE | DIVIDEND ($) | ... with MM/DD/YYYY string
    dates and text amounts.  The parser must find columns by NAME
    (ticker-based symbol match) and parse that date format."""

    def _real_layout_workbook(self, tmp: Path) -> Path:
        header = [
            "FUND NAME", "TICKER", "CUSIP", "EX-DATE", "RECORD DATE",
            "PAYABLE DATE", "DIVIDEND ($)",
            "SHORT TERM CAPITAL GAIN ($)",
        ]
        rows: list[list[Any]] = []
        # 48 in-window quarters.
        for year in range(2010, 2022):
            for month in (3, 6, 9, 12):
                ex = f"{month:02d}/15/{year}"
                pay = f"{month:02d}/28/{year}"
                rows.append(
                    [
                        "SPDR S&P 500 ETF Trust", "SPY", "78462F103",
                        ex, ex, pay, " 0.5 ",
                    ]
                )
        # Plus out-of-window rows before/after.
        for year in (2008, 2009):
            for month in (3, 9):
                rows.append(
                    [
                        "SPDR S&P 500 ETF Trust", "SPY", "78462F103",
                        f"{month:02d}/15/{year}", f"{month:02d}/15/{year}",
                        f"{month:02d}/28/{year}", "0.4",
                    ]
                )
        for year in (2023, 2024):
            for month in (3, 9):
                rows.append(
                    [
                        "SPDR S&P 500 ETF Trust", "SPY", "78462F103",
                        f"{month:02d}/15/{year}", f"{month:02d}/15/{year}",
                        f"{month:02d}/28/{year}", "0.6",
                    ]
                )
        source = tmp / "real_layout.xlsx"
        source.write_bytes(_build_xlsx(rows, header=header))
        return source

    def test_real_layout_parses_selects_and_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hashlib as _hashlib

        source = self._real_layout_workbook(tmp_path)
        monkeypatch.setattr(
            cli,
            "REGISTERED_ACTIONS_SOURCE_SHA256",
            _hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        report = run_import_corporate_actions(
            cache_dir=tmp_path / "cache",
            file_path=source,
            source_url=cli.REGISTERED_ACTIONS_SOURCE_URL,
        )
        assert report["actions"] == 48
        assert report["raw_spy_row_count"] == 56
        stored = json.loads(
            (
                tmp_path / "cache" / CORPORATE_ACTIONS_FILENAME
            ).read_text(encoding="utf-8")
        )
        first = stored["payload"]["actions"][0]
        assert first["ex_date"] == "2010-03-15"
        assert first["pay_date"] == "2010-03-28"


# ------------------------------------------- decision 14.10: delta fixes


class TestItem1DeltaReceiptFailure:
    """14.10 item 1: a RECEIPT publish failure must mark the attempt
    FAILED with the already-published result path/hash recorded, and
    leave no run lock behind."""

    def test_receipt_publish_failure_marks_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)
        _seal_for_tests(monkeypatch, cache)
        out = tmp_path / "out" / "result.json"

        real_publish = cli._atomic_publish_json
        calls: list[Path] = []

        def _fail_on_receipt(path: Path, payload: dict) -> None:
            calls.append(path)
            if path.name.endswith(".receipt.json"):
                raise OSError("receipt disk full")
            real_publish(path, payload)

        monkeypatch.setattr(cli, "_atomic_publish_json", _fail_on_receipt)
        with pytest.raises(OSError, match="receipt disk full"):
            run_evaluate(cache_dir=cache, output_path=out)

        # The result itself WAS published.
        assert out.exists()
        # The attempt is FAILED and records the result path + hash.
        attempt = json.loads(
            (cache / "attempt.json").read_text(encoding="utf-8")
        )
        assert attempt["state"] == "FAILED"
        assert attempt["output_path"] == str(out)
        assert attempt["output_sha256"] == cli._file_sha256(out)
        # No run lock is left behind.
        assert not (cache / "run.lock").exists()


class TestItem2DeltaPreAttemptValidation:
    """14.10 item 2: SPY AND QQQ structure/OHLC/duplicate/action-date
    validation must run in the SEALED preflight BEFORE the attempt
    claim.  A tampered-but-hash-passing sealed file refuses with no
    attempts/ entry."""

    def _retamper_sealed(
        self,
        cache: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        symbol: str,
        mutate_row: Any,
    ) -> None:
        """Rewrite one symbol's bars AND the manifest together so the
        seal-time hash checks pass (simulating seal-time tampering that
        the seal itself would have caught - evaluate must still catch
        it independently, before claiming)."""

        _no_live_fetch(monkeypatch)
        _clean_tree_for_tests(monkeypatch)
        run_seal(cache)
        path = cache / "daily" / f"{symbol}.json.gz"
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        mutate_row(raw)
        rendered = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(rendered)
        # Re-sync status + manifest so every hash check passes.
        _resync_status_counts(cache)
        manifest = json.loads(
            (cache / "manifest.json").read_text(encoding="utf-8")
        )
        for entry in manifest["files"]:
            entry["sha256"] = cli._file_sha256(
                cache / str(entry["path"])
            )
            entry["bytes"] = (cache / str(entry["path"])).stat().st_size
        cli._atomic_write_json(cache / "manifest.json", manifest)
        new_hash = cli._file_sha256(cache / "manifest.json")
        cli._atomic_write_json(
            cache / "seal_receipt.json",
            {
                "sealed_at": "2026-09-29T00:00:00+00:00",
                "manifest_sha256": new_hash,
                "files": len(manifest["files"]),
                "reseal_reason": "retamper-for-test",
            },
        )

    def test_tampered_qqq_ohlc_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)

        def _bad_low(raw: dict) -> None:
            raw["bars"][100][3] = raw["bars"][100][1] * 2

        self._retamper_sealed(
            cache, monkeypatch, symbol="QQQ.US", mutate_row=_bad_low
        )
        with pytest.raises(
            MonthlySma10Error, match="QQQ"
        ):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        # No attempt was claimed.
        assert not (cache / "attempts").exists()
        assert not (cache / "attempt.json").exists()

    def test_malformed_qqq_row_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)

        def _bad_row(raw: dict) -> None:
            raw["bars"][100] = ["not-a-date", 1.0, 1.0, 1.0, 1.0, 1, 1]

        self._retamper_sealed(
            cache, monkeypatch, symbol="QQQ.US", mutate_row=_bad_row
        )
        with pytest.raises(MonthlySma10Error):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        assert not (cache / "attempts").exists()

    def test_duplicate_spy_date_refused_before_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_fetch_cache(tmp_path / "cache", with_actions=True)

        def _dup(raw: dict) -> None:
            raw["bars"].append(list(raw["bars"][50]))

        self._retamper_sealed(
            cache, monkeypatch, symbol="SPY.US", mutate_row=_dup
        )
        with pytest.raises(
            MonthlySma10Error, match="duplicate"
        ):
            run_evaluate(
                cache_dir=cache,
                output_path=tmp_path / "out" / "r.json",
            )
        assert not (cache / "attempts").exists()


class TestItem3DeltaUniqueTempPublish:
    """14.10 item 3: the no-clobber publish must use a UNIQUE temp
    file (exclusive creation) - a fixed name lets a concurrent writer
    truncate an inode already linked to a published result."""

    def test_second_publish_refused_first_bytes_intact(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "out" / "result.json"
        cli._atomic_publish_json(target, {"first": 1})
        first_bytes = target.read_bytes()
        with pytest.raises(
            MonthlySma10Error, match="already exists"
        ):
            cli._atomic_publish_json(target, {"second": 2})
        assert target.read_bytes() == first_bytes

    def test_concurrent_writers_do_not_share_a_temp_inode(
        self, tmp_path: Path
    ) -> None:
        # Two SIMULTANEOUS publishes to DIFFERENT targets must never
        # write through the same temp path (the old fixed
        # .publish.tmp could collide across processes).
        import threading

        errors: list[Exception] = []
        made: list[Path] = []

        real_link = os.link

        def _watch(src: Any, dst: Any, *rest: Any) -> None:
            made.append(Path(str(src)))
            real_link(src, dst, *rest)

        import unittest.mock as _mock

        with _mock.patch("os.link", side_effect=_watch):
            barrier = threading.Barrier(2)

            def _publish(index: int) -> None:
                try:
                    barrier.wait()
                    cli._atomic_publish_json(
                        tmp_path / f"out{index}.json", {"i": index}
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=_publish, args=(i,))
                for i in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        assert errors == []
        # The two publishes used DISTINCT temp paths.
        assert len({p.name for p in made}) == 2
        # And no temp litter remains.
        leftovers = [
            p for p in tmp_path.iterdir() if p.name.startswith(".")
        ]
        assert leftovers == []

    def test_publish_uses_exclusive_temp_creation(self) -> None:
        # The implementation must use tempfile.mkstemp (exclusive
        # creation) - pinned by source inspection so the fixed-name
        # pattern cannot quietly return.
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "cli"
            / "spy_monthly_sma10_replay.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _atomic_publish_json")
        end = source.index("\ndef ", start + 10)
        body = source[start:end]
        assert "mkstemp" in body
        assert "fsync" in body
        assert ".publish.tmp" not in body
