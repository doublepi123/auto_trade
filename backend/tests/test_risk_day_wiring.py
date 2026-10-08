"""Wiring tests for the US overnight risk-day start (20:00 ET).

Owner decision: the daily RISK day (``daily_pnl``, ``consecutive_losses``,
the daily loss limit, and their reconciliation replay) starts when the US
overnight session opens at 20:00 ET, instead of at ET midnight.

Every test here drives an existing entry point — ``AppRunner`` construction,
the ``RiskController`` day rollover through its injected provider, the
runner's ``_sync_risk_from_order_ledger`` replay, ``RuntimeStateService.load``,
and ``GET /api/status`` — with a frozen wall clock, so old code and new code
are exercised at the same frozen instant with no wall-clock dependence.

Frozen instants are written as UTC strings (the existing house pattern,
``tz_offset=0``); ET wall-clock times are built via ``ZoneInfo`` so DST is
real.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from app import database
from app.core.market_calendar import trade_day_for
from app.database import SessionLocal
from app.models import OrderRecord, RuntimeState, StrategyConfig
from app.runner import AppRunner
from app.services.strategy_service import StrategyService

database.init_db()

ET = ZoneInfo("America/New_York")


def _et(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """UTC instant for an ET wall-clock time (DST-correct via ZoneInfo)."""
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=ET
    ).astimezone(timezone.utc)


def _fill(
    broker_order_id: str,
    side: str,
    quantity: int,
    price: int,
    filled_at: datetime,
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=broker_order_id,
        symbol="NVDA.US",
        side=side,
        quantity=quantity,
        price=price,
        executed_quantity=quantity,
        executed_price=price,
        status="FILLED",
        created_at=filled_at,
        filled_at=filled_at,
    )


def _clean() -> None:
    with SessionLocal() as db:
        db.query(OrderRecord).delete()
        db.query(RuntimeState).delete()
        db.query(StrategyConfig).delete()
        db.commit()


def _seed_strategy(market: str = "US") -> None:
    with SessionLocal() as db:
        db.query(StrategyConfig).delete()
        db.add(
            StrategyConfig(
                symbol="NVDA.US",
                market=market,
                buy_low=100.0,
                sell_high=200.0,
            )
        )
        db.commit()


class TestRunnerRiskDayWiring:
    """The AppRunner wires its RiskController to the overnight risk day."""

    def test_fresh_runner_risk_day_starts_at_20_et(self) -> None:
        # 2026-10-08 20:30 ET = 2026-10-09 00:30 UTC.
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            runner = AppRunner()
            runner.engine.params.market = "US"

            assert runner.risk.daily_pnl_date == date(2026, 10, 9)

    def test_risk_controller_rolls_daily_losses_at_overnight_open(self) -> None:
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            runner = AppRunner()
            runner.engine.params.market = "US"
            runner.risk.replace_daily_pnl(-120.0, 2, date(2026, 10, 8))

            runner.risk.check()

            assert runner.risk.daily_pnl == 0.0
            assert runner.risk.consecutive_losses == 0
            assert runner.risk.daily_pnl_date == date(2026, 10, 9)

    def test_funnel_day_stays_market_day_while_risk_day_rolls(self) -> None:
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            runner = AppRunner()
            runner.engine.params.market = "US"
            runner.risk.replace_daily_pnl(-120.0, 2, date(2026, 10, 8))
            runner.risk.check()
            runner.decision_funnel.record_evaluation()
            snapshot = runner.decision_funnel.snapshot()

            assert runner.risk.daily_pnl_date == date(2026, 10, 9)
            # The decision funnel keeps the exchange-local calendar day.
            assert snapshot.session_date == "2026-10-08"

    def test_before_overnight_open_no_rollover(self) -> None:
        # 2026-10-08 19:30 ET = 2026-10-08 23:30 UTC.
        with freeze_time("2026-10-08 23:30:00", tz_offset=0):
            runner = AppRunner()
            runner.engine.params.market = "US"
            runner.risk.replace_daily_pnl(-120.0, 2, date(2026, 10, 8))

            runner.risk.check()

            assert runner.risk.daily_pnl == -120.0
            assert runner.risk.consecutive_losses == 2
            assert runner.risk.daily_pnl_date == date(2026, 10, 8)

    def test_hk_risk_day_unchanged(self) -> None:
        # 2026-10-09 00:30 UTC = 2026-10-09 08:30 HKT: the HK day is 10-09
        # there either way, so HK keeps trade_day_for semantics.
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            runner = AppRunner()
            runner.engine.params.market = "HK"
            runner.risk.check()

            assert runner.risk.daily_pnl_date == trade_day_for("HK")


class TestLedgerReplayRiskDayWindow:
    """The replay target day and per-fill mapping use the risk day.

    Frozen now: 2026-10-08 12:00 ET (= 16:00 UTC), whose risk day is 10-08.
    Risk day 10-08 then covers exactly [10-07 20:00 ET, 10-08 20:00 ET).
    Every exit is a distinct loss so the applied replay total identifies
    exactly which fills were counted.
    """

    def test_replay_window_is_overnight_epoch_to_overnight_epoch(self) -> None:
        _clean()
        _seed_strategy()
        with freeze_time("2026-10-08 16:00:00", tz_offset=0):
            with SessionLocal() as db:
                db.add_all(
                    [
                        # Entry filled two days before the window still pairs
                        # with in-window exits (FIFO inventory).
                        _fill("seed-buy", "BUY", 10, 100, _et(date(2026, 10, 6), 15, 0)),
                        # 10-07 19:59 ET -> risk day 10-07: outside the window.
                        _fill("exit-a", "SELL", 2, 99, _et(date(2026, 10, 7), 19, 59)),
                        # 10-07 20:00 ET -> risk day 10-08: first in-window exit.
                        _fill("exit-b", "SELL", 2, 96, _et(date(2026, 10, 7), 20, 0)),
                        # 10-08 00:00 ET -> risk day 10-08: window interior.
                        _fill("exit-c", "SELL", 2, 97, _et(date(2026, 10, 8), 0, 0)),
                        # 10-08 19:59 ET -> risk day 10-08: last in-window exit.
                        _fill("exit-d", "SELL", 2, 98, _et(date(2026, 10, 8), 19, 59)),
                        # 10-08 20:00 ET -> risk day 10-09: outside the window.
                        _fill("exit-e", "SELL", 2, 95, _et(date(2026, 10, 8), 20, 0)),
                    ]
                )
                db.commit()

            runner = AppRunner()
            runner.engine.params.market = "US"
            runner.engine.params.fee_rate_us = 0.0
            runner.engine.params.fee_rate_hk = 0.0

            assert runner._sync_risk_from_order_ledger() is True
            # Exactly exit-b + exit-c + exit-d: (96-100)*2 + (97-100)*2
            # + (98-100)*2 = -18. The calendar-day set (c+d+e) would give
            # -20, and the 20:00-to-20:00 window must exclude e.
            assert runner.risk.daily_pnl == pytest.approx(-18.0)
            assert runner.risk.consecutive_losses == 3
            assert runner.risk.daily_pnl_date == date(2026, 10, 8)

    def test_exit_at_overnight_open_counts_toward_new_risk_day(self) -> None:
        _clean()
        _seed_strategy()
        with freeze_time("2026-10-08 16:00:00", tz_offset=0):
            with SessionLocal() as db:
                db.add_all(
                    [
                        _fill("seed-buy", "BUY", 2, 100, _et(date(2026, 10, 7), 15, 0)),
                        # 10-07 20:00 ET belongs to risk day 10-08 under the
                        # overnight rule, while its calendar day is 10-07.
                        _fill("exit-b", "SELL", 2, 98, _et(date(2026, 10, 7), 20, 0)),
                    ]
                )
                db.commit()

            runner = AppRunner()
            runner.engine.params.market = "US"
            runner.engine.params.fee_rate_us = 0.0
            runner.engine.params.fee_rate_hk = 0.0

            assert runner._sync_risk_from_order_ledger() is True
            assert runner.risk.daily_pnl == pytest.approx(-4.0)
            assert runner.risk.consecutive_losses == 1


class TestRestartResetsDailyCounters:
    """RuntimeStateService.load honours the risk day across a restart."""

    def test_load_resets_daily_counters_across_overnight_boundary(self) -> None:
        _clean()
        _seed_strategy()
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            with SessionLocal() as db:
                StrategyService(db).update_runtime_state(
                    symbol="NVDA.US",
                    daily_pnl=-500.0,
                    daily_pnl_date=date(2026, 10, 8),
                    consecutive_losses=3,
                    paused=True,
                    pause_reason="manual hold",
                    pause_auto_resumable=False,
                    kill_switch=False,
                    cumulative_realized_pnl=250.0,
                    peak_realized_pnl=300.0,
                )
                db.commit()

            runner = AppRunner()
            runner.engine.params.market = "US"
            with SessionLocal() as db:
                runner._state_svc.load(db, runner.engine, runner.risk)

            assert runner.risk.daily_pnl == 0.0
            assert runner.risk.consecutive_losses == 0
            assert runner.risk.daily_pnl_date == date(2026, 10, 9)
            # Only day-scoped counters reset; the safety latches and the
            # cumulative drawdown state survive the restart.
            assert runner.risk.paused is True
            assert runner.risk.pause_reason == "manual hold"
            assert runner.risk.kill_switch is False
            assert runner.risk.cumulative_realized_pnl == 250.0
            assert runner.risk.peak_realized_pnl == 300.0

    def test_load_keeps_daily_counters_when_still_same_risk_day(self) -> None:
        _clean()
        _seed_strategy()
        # 2026-10-09 00:30 ET = 04:30 UTC is still risk day 10-09.
        with freeze_time("2026-10-09 04:30:00", tz_offset=0):
            with SessionLocal() as db:
                StrategyService(db).update_runtime_state(
                    symbol="NVDA.US",
                    daily_pnl=-500.0,
                    daily_pnl_date=date(2026, 10, 9),
                    consecutive_losses=3,
                )
                db.commit()

            runner = AppRunner()
            runner.engine.params.market = "US"
            with SessionLocal() as db:
                runner._state_svc.load(db, runner.engine, runner.risk)

            assert runner.risk.daily_pnl == -500.0
            assert runner.risk.consecutive_losses == 3
            assert runner.risk.daily_pnl_date == date(2026, 10, 9)


class TestStatusApiPersistsRiskDay:
    """GET /api/status persists daily_pnl_date as the risk day."""

    def test_get_status_at_overnight_open_persists_next_risk_day(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        _clean()
        _seed_strategy()
        with freeze_time("2026-10-09 00:30:00", tz_offset=0):
            with SessionLocal() as db:
                # Fills at 16:00/16:05 ET belong to risk day 10-08; the
                # request happens at 20:30 ET, whose risk day is 10-09.
                db.add(_fill("status-buy", "BUY", 2, 100, _et(date(2026, 10, 8), 16, 0)))
                db.add(_fill("status-sell", "SELL", 2, 103, _et(date(2026, 10, 8), 16, 5)))
                db.commit()

            client = TestClient(app)
            resp = client.get("/api/status")

            assert resp.status_code == 200
            with SessionLocal() as db:
                state = (
                    db.query(RuntimeState)
                    .filter(RuntimeState.symbol == "NVDA.US")
                    .one()
                )
                assert state.daily_pnl_date == date(2026, 10, 9)


class TestGuardsStateGreenBeforeAndAfter:
    """Nothing except the live risk day moves."""

    def test_market_calendar_trade_day_unchanged_at_21_et(self) -> None:
        instant = _et(date(2026, 10, 8), 21, 0)
        assert trade_day_for("US", instant) == date(2026, 10, 8)

    def test_market_calendar_trade_day_unchanged_at_19_et(self) -> None:
        instant = _et(date(2026, 10, 8), 19, 0)
        assert trade_day_for("US", instant) == date(2026, 10, 8)
