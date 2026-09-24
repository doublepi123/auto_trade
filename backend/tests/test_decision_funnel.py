# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Decision funnel — live-path stage counters for the zero-order diagnosis.

The funnel answers one question: at which stage does the trading pipeline
stop? These tests drive scripted quote sequences through the runner and
assert each stage increments exactly as the interpretation contract on
``DecisionFunnelTracker`` promises.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_decision_funnel_{os.getpid()}.db"
)

import pytest

from app import database
from app import runner as runner_module
from app.core.broker import Quote
from app.core import engine as engine_module
from app.core.engine import EngineState, StrategyParams
from app.models import DecisionFunnelSessionSummary
from app.runner import AppRunner
from app.schemas import DiagnosticsResponse
from app.services.decision_funnel_service import (
    DecisionFunnelTracker,
    persist_session_summary,
)
from app.services.trade_execution_service import OrderStatus


database.init_db()


def _fresh_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote(symbol: str, price: float) -> Quote:
    return Quote(symbol, price, price - 0.01, price + 0.01, _fresh_timestamp())


class _NoopNotifier:
    dedup_suppressed_total = 0
    dedup_window_seconds = 0.0

    def notify_order(self, *_args: object) -> bool:
        return True

    def notify_risk_event(self, *_args: object) -> bool:
        return True


def _runner() -> AppRunner:
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(
        symbol="NVDA.US",
        market="US",
        buy_low=100.0,
        sell_high=110.0,
    )
    runner._symbol_runtimes = {
        "NVDA.US": runner._build_symbol_runtime("NVDA.US", "US", primary=True)
    }
    runner.notifier = _NoopNotifier()
    return runner


class TestDecisionFunnelPipeline:
    @pytest.fixture(autouse=True)
    def _disable_entry_crossing_requirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            runner_module.settings,
            "live_entry_crossing_required",
            False,
        )

    def test_funnel_counts_each_stage_of_a_full_entry(self) -> None:
        # Given: a running runner whose FLAT engine watches NVDA.US 100/110,
        # and an execution seam that acks and persists like the real service.
        runner = _runner()
        submissions: list[str] = []

        def _fake_execute(*, action, symbol, quote, **_kwargs):
            submissions.append(action)
            runner.decision_funnel.record_sized_quantity_positive()
            runner._record_order(
                "FUNNEL-ORDER-1",
                symbol,
                action,
                1.0,
                float(quote.last_price),
                status="SUBMITTED",
            )
            return OrderStatus("FUNNEL-ORDER-1", "SUBMITTED")

        runner._trade_svc.execute = _fake_execute

        # When: a mid-range quote (no crossing) then a quote below buy_low.
        runner._on_quote(_quote("NVDA.US", 105.0))
        runner._on_quote(_quote("NVDA.US", 99.5))

        # Then: every funnel stage advanced exactly as far as the pipeline did.
        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.fresh_primary_quote == 2
        assert snapshot.evaluations == 2
        assert snapshot.threshold_crossings == 1
        assert snapshot.triggers == 1
        assert snapshot.sized_quantity_positive == 1
        assert snapshot.submit_attempts == 1
        assert snapshot.broker_acks == 1
        assert snapshot.persisted == 1
        assert snapshot.pre_submit_risk_check_invocations == 0
        assert submissions == ["BUY"]
        assert all(count == 0 for count in snapshot.skips_by_category.values())

        # And: the funnel is exposed through the existing diagnostics payload.
        payload = runner.diagnostics()
        assert payload["decision_funnel"]["triggers"] == 1
        parsed = DiagnosticsResponse.model_validate(payload)
        assert parsed.decision_funnel.persisted == 1
        assert parsed.decision_funnel.skips_by_category == {
            "FEE": 0,
            "REPRICING": 0,
            "COOLDOWN": 0,
            "REGIME": 0,
            "RISK": 0,
            "PENDING": 0,
            "POSITION": 0,
            "SESSION": 0,
        }

    def test_record_skip_regime_is_counted(self) -> None:
        # Given: a tracker observing one exchange-local trading day.
        tracker = DecisionFunnelTracker(
            trade_day_provider=lambda: date(2026, 8, 28)
        )

        # When: the live entry-policy regime gate blocks an entry.
        tracker.record_skip("REGIME")

        # Then: diagnostics retain the regime-blocked decision.
        assert tracker.snapshot().skips_by_category["REGIME"] == 1

    def test_risk_skip_increments_risk_category_but_not_triggers(self) -> None:
        # Given: a paused risk controller, so the entry is suppressed before
        # any trigger can fire.
        runner = _runner()
        runner.risk.pause("test pause")
        execute_calls: list[str] = []
        runner._trade_svc.execute = lambda **kwargs: execute_calls.append("called")

        # When: a quote crosses buy_low while risk rejects everything.
        runner._on_quote(_quote("NVDA.US", 99.5))

        # Then: the skip is attributed to RISK and no trigger is counted —
        # this discrimination is what the whole instrument depends on.
        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.fresh_primary_quote == 1
        assert snapshot.evaluations == 1
        assert snapshot.threshold_crossings == 1
        assert snapshot.skips_by_category["RISK"] == 1
        assert snapshot.triggers == 0
        assert snapshot.sized_quantity_positive == 0
        assert snapshot.submit_attempts == 0
        assert snapshot.broker_acks == 0
        assert snapshot.persisted == 0
        assert execute_calls == []


class TestDecisionFunnelCooldown:
    @pytest.fixture
    def cooldown_runner(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[AppRunner, list[datetime], list[str]]:
        current = [datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)]
        started_at = current[0]

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return cls.fromtimestamp(current[0].timestamp(), tz)

        monkeypatch.setattr(runner_module, "datetime", _FakeDatetime)
        monkeypatch.setattr(engine_module, "datetime", _FakeDatetime)
        monkeypatch.setattr(
            engine_module, "time", SimpleNamespace(
                monotonic=lambda: 1000.0 + (current[0] - started_at).total_seconds()
            ),
        )
        monkeypatch.setattr(runner_module.settings, "engine_cooldown_seconds", 60)
        monkeypatch.setattr(runner_module.settings, "live_entry_crossing_required", True)
        monkeypatch.setattr(runner_module.settings, "live_entry_crossing_max_age_seconds", 30)
        runner = _runner()
        executions: list[str] = []

        def _fake_execute(*, action: str, **_kwargs: object) -> OrderStatus:
            executions.append(action)
            runner.decision_funnel.record_skip("SESSION")
            return OrderStatus("", "SKIPPED", reason="session blocked")

        monkeypatch.setattr(runner._trade_svc, "execute", _fake_execute)
        runner._on_quote(Quote("NVDA.US", 105.0, 104.99, 105.01, current[0].isoformat()))
        runner._on_quote(Quote("NVDA.US", 99.5, 99.49, 99.51, current[0].isoformat()))
        assert executions == ["BUY"]
        assert runner.engine.snapshot().state == EngineState.FLAT
        assert runner.engine.snapshot().last_trigger_at == started_at
        assert not runner.engine.long_entry_rearm_required
        assert runner.decision_funnel.snapshot().skips_by_category["SESSION"] == 1
        return runner, current, executions

    def test_cooldown_suppressed_crossing_is_counted_without_retriggering(
        self, cooldown_runner: tuple[AppRunner, list[datetime], list[str]],
    ) -> None:
        # Given: the real SKIPPED restore path retained the first trigger.
        runner, current, executions = cooldown_runner
        preserved = runner.engine.snapshot()
        trigger_monotonic = runner.engine._last_trigger_monotonic
        current[0] += timedelta(seconds=10)
        before = runner.decision_funnel.snapshot()

        # When: another threshold quote arrives within crossing freshness.
        runner._on_quote(Quote("NVDA.US", 99.5, 99.49, 99.51, current[0].isoformat()))

        # Then: only the crossing and cooldown attribution advance.
        after = runner.decision_funnel.snapshot()
        assert after.threshold_crossings - before.threshold_crossings == 1
        assert (
            after.skips_by_category["COOLDOWN"] - before.skips_by_category["COOLDOWN"] == 1
        ), "cooldown-suppressed crossing must increment COOLDOWN"
        assert after.triggers == before.triggers
        assert after.submit_attempts == before.submit_attempts
        assert after.entry_crossing_blocks == before.entry_crossing_blocks
        assert runner.engine.snapshot() == preserved
        assert runner.engine._last_trigger_monotonic == trigger_monotonic
        assert runner.engine.in_cooldown
        assert executions == ["BUY"]

    def test_fresh_crossing_triggers_after_cooldown_expires(
        self, cooldown_runner: tuple[AppRunner, list[datetime], list[str]],
    ) -> None:
        # Given: cooldown expired and a fresh outside-threshold quote arrived.
        runner, current, executions = cooldown_runner
        current[0] += timedelta(seconds=61)
        assert not runner.engine.in_cooldown
        runner._on_quote(Quote("NVDA.US", 105.0, 104.99, 105.01, current[0].isoformat()))
        before = runner.decision_funnel.snapshot()

        # When: a fresh valid downcross reaches the engine.
        runner._on_quote(Quote("NVDA.US", 99.5, 99.49, 99.51, current[0].isoformat()))

        # Then: entry evaluation and execution resume normally.
        after = runner.decision_funnel.snapshot()
        assert after.evaluations - before.evaluations == 1
        assert after.threshold_crossings - before.threshold_crossings == 1
        assert after.triggers - before.triggers == 1
        assert after.skips_by_category["COOLDOWN"] == before.skips_by_category["COOLDOWN"]
        assert after.entry_crossing_blocks == before.entry_crossing_blocks
        assert runner.engine.snapshot().last_trigger_at == current[0]
        assert executions == ["BUY", "BUY"]

    @pytest.mark.parametrize("case", ["rearm", "reclaim", "floor", "midrange", "invalid_band"])
    def test_other_untriggered_quotes_are_not_counted_as_cooldown(
        self, cooldown_runner: tuple[AppRunner, list[datetime], list[str]], case: str,
    ) -> None:
        # Given: a non-cooldown reason for withholding an entry.
        runner, current, executions = cooldown_runner
        current[0] += timedelta(seconds=10)
        price = 99.5
        if case in {"rearm", "reclaim"}:
            runner.engine.restore_long_entry_rearm(True)
        if case in {"reclaim", "midrange"}:
            price = 105.0
        if case == "floor":
            current[0] += timedelta(seconds=51)
            runner.engine.params.stop_loss_pct = 1.0
            runner._on_quote(Quote("NVDA.US", 105.0, 104.99, 105.01, current[0].isoformat()))
            price = 98.0
        if case == "invalid_band":
            runner.engine.params.sell_high = 90.0
        before = runner.decision_funnel.snapshot()

        # When: the engine receives the non-triggering quote.
        runner._on_quote(Quote("NVDA.US", price, price - 0.01, price + 0.01, current[0].isoformat()))

        # Then: timer presence alone never produces cooldown attribution.
        after = runner.decision_funnel.snapshot()
        assert after.skips_by_category["COOLDOWN"] == before.skips_by_category["COOLDOWN"]
        assert after.triggers == before.triggers
        assert executions == ["BUY"]
        if case == "floor":
            assert runner.engine.long_entry_rearm_required
        if case == "reclaim":
            assert not runner.engine.long_entry_rearm_required


class TestDecisionFunnelSessionPersistence:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        try:
            db.query(DecisionFunnelSessionSummary).delete()
            db.commit()
        finally:
            db.close()

    def test_simulated_session_produces_exactly_one_summary_row(self) -> None:
        # Given: a tracker whose exchange-local day can be advanced.
        current_day = [date(2026, 8, 28)]
        tracker = DecisionFunnelTracker(trade_day_provider=lambda: current_day[0])

        # When: counters accumulate on day 1, then the day rolls over.
        tracker.record_fresh_primary_quote()
        tracker.record_evaluation()
        tracker.record_threshold_crossing()
        tracker.record_skip("RISK")
        current_day[0] = date(2026, 8, 29)
        tracker.record_evaluation()

        # Then: exactly one closed session is drained for day 1.
        closed = tracker.drain_closed_sessions()
        assert len(closed) == 1
        assert closed[0].session_date == "2026-08-28"
        assert closed[0].fresh_primary_quote == 1
        assert closed[0].evaluations == 1
        assert closed[0].threshold_crossings == 1
        assert closed[0].skips_by_category["RISK"] == 1
        assert tracker.drain_closed_sessions() == []

        # And: persisting it — even twice — yields exactly one durable row.
        db = database.SessionLocal()
        try:
            persist_session_summary(db, closed[0], symbol="TSLA.US", market="US")
            db.commit()
            persist_session_summary(db, closed[0], symbol="TSLA.US", market="US")
            db.commit()
            rows = (
                db.query(DecisionFunnelSessionSummary)
                .filter(
                    DecisionFunnelSessionSummary.session_date == date(2026, 8, 28)
                )
                .all()
            )
            assert len(rows) == 1
            assert rows[0].symbol == "TSLA.US"
            assert rows[0].market == "US"
            assert rows[0].fresh_primary_quote == 1
            assert rows[0].evaluations == 1
            assert rows[0].threshold_crossings == 1
            assert rows[0].pre_submit_risk_check_invocations == 0
            assert db.query(DecisionFunnelSessionSummary).count() == 1
        finally:
            db.close()

    def test_regime_skip_round_trips_through_session_summary(self) -> None:
        # Given: a tracker whose completed session has a regime-blocked entry.
        current_day = [date(2026, 8, 28)]
        tracker = DecisionFunnelTracker(trade_day_provider=lambda: current_day[0])
        tracker.record_skip("REGIME")
        current_day[0] = date(2026, 8, 29)

        # When: the completed session is persisted.
        closed = tracker.drain_closed_sessions()
        db = database.SessionLocal()
        try:
            persist_session_summary(db, closed[0], symbol="TSLA.US", market="US")
            db.commit()
            row = db.query(DecisionFunnelSessionSummary).one()

            # Then: the serialized durable summary retains the regime count.
            assert json.loads(row.skips_json)["REGIME"] == 1
        finally:
            db.close()

    def test_cooldown_skip_round_trips_and_old_summary_remains_readable(self) -> None:
        # Given: one historical summary without COOLDOWN and one new session.
        tracker = DecisionFunnelTracker(trade_day_provider=lambda: date(2026, 9, 14))
        tracker.record_skip("COOLDOWN")
        with database.SessionLocal() as db:
            db.add(DecisionFunnelSessionSummary(
                session_date=date(2026, 9, 11), symbol="NVDA.US", market="US",
                skips_json='{"SESSION": 2}',
            ))
            db.commit()

            # When: the normal writer persists the new session and both are read.
            persist_session_summary(db, tracker.snapshot(), symbol="NVDA.US", market="US")
            db.commit()
            rows = db.query(DecisionFunnelSessionSummary).order_by(
                DecisionFunnelSessionSummary.session_date
            ).all()

            # Then: the new counter serializes; the old payload needs no migration.
            assert len(rows) == 2
            assert json.loads(rows[1].skips_json)["COOLDOWN"] == 1
            assert json.loads(rows[0].skips_json) == {"SESSION": 2}
            assert rows[0].skips_json == '{"SESSION": 2}'


class TestDecisionFunnelRestartDurability:
    """A mid-session restart must not erase the session's funnel evidence.

    Counters live in memory and used to reach the database only at day
    rollover, so every restart during RTH silently dropped the counts before
    it (observed: 09-10/16/17/18 summaries undercounted the event log). The
    runner now adds each checkpoint's delta to the durable row.
    """

    _DAY = date(2026, 9, 24)

    def setup_method(self) -> None:
        with database.SessionLocal() as db:
            db.query(DecisionFunnelSessionSummary).delete()
            db.commit()

    def _bound(
        self,
        symbol: str = "TSLA.US",
        clock: list[date] | None = None,
    ) -> AppRunner:
        day = clock if clock is not None else [self._DAY]
        runner = _runner()
        runner.engine.params = StrategyParams(
            symbol=symbol, market="US", buy_low=1.0, sell_high=2.0
        )
        runner.decision_funnel = DecisionFunnelTracker(trade_day_provider=lambda: day[0])
        runner._bind_decision_funnel()
        return runner

    @staticmethod
    def _switch(runner: AppRunner, symbol: str) -> None:
        runner.engine.params = StrategyParams(
            symbol=symbol, market="US", buy_low=1.0, sell_high=2.0
        )
        runner._bind_decision_funnel()

    @staticmethod
    def _count(runner: AppRunner, times: int) -> None:
        for _ in range(times):
            runner.decision_funnel.record_skip("REGIME")

    def _store(self, symbol: str, regime: int) -> None:
        tracker = DecisionFunnelTracker(trade_day_provider=lambda: self._DAY)
        for _ in range(regime):
            tracker.record_skip("REGIME")
        with database.SessionLocal() as db:
            persist_session_summary(db, tracker.snapshot(), symbol=symbol, market="US")
            db.commit()

    def _stored_regime(self, symbol: str, day: date | None = None) -> int | None:
        with database.SessionLocal() as db:
            row = (
                db.query(DecisionFunnelSessionSummary)
                .filter(
                    DecisionFunnelSessionSummary.symbol == symbol,
                    DecisionFunnelSessionSummary.session_date == (day or self._DAY),
                )
                .one_or_none()
            )
        return None if row is None else json.loads(row.skips_json).get("REGIME", 0)

    @staticmethod
    def _drain(runner: AppRunner) -> None:
        for _ in range(50):
            runner._decision_funnel_write_once()
            if not runner._decision_funnel_pending:
                return
        raise AssertionError("pending funnel writes never drained")

    def test_restart_mid_session_adds_to_the_durable_row(self) -> None:
        # Given a process that counted part of a session and checkpointed it
        first = self._bound()
        self._count(first, 3)
        self._drain(first)

        # When a new process counts more in the same session and checkpoints
        second = self._bound()
        self._count(second, 1)
        self._drain(second)

        # Then the durable row holds the whole session, not just the tail
        assert self._stored_regime("TSLA.US") == 4, (
            "a mid-session restart erased the session's earlier funnel counts"
        )

    def test_repeated_checkpoints_and_rebinds_never_double_count(self) -> None:
        # Given a checkpointed binding
        runner = self._bound()
        self._count(runner, 3)
        self._drain(runner)

        # When it checkpoints again, and the same process stops and starts
        self._drain(runner)
        runner._bind_decision_funnel()
        self._drain(runner)
        self._count(runner, 1)
        self._drain(runner)

        # Then every count reached the row exactly once
        assert self._stored_regime("TSLA.US") == 4

    def test_primary_switch_keeps_each_symbols_counts(self) -> None:
        runner = self._bound("TSLA.US")
        self._count(runner, 5)
        self._drain(runner)

        self._switch(runner, "NVDA.US")
        self._count(runner, 2)
        self._drain(runner)

        assert self._stored_regime("TSLA.US") == 5
        assert self._stored_regime("NVDA.US") == 2

    def test_switch_before_any_checkpoint_adds_to_the_old_symbols_row(self) -> None:
        # Given 4 stored for TSLA by an earlier process, 3 more unwritten here
        self._store("TSLA.US", 4)
        runner = self._bound("TSLA.US")
        self._count(runner, 3)

        # When the primary switches before any checkpoint ran
        self._switch(runner, "NVDA.US")
        self._count(runner, 1)
        self._drain(runner)

        assert self._stored_regime("TSLA.US") == 7
        assert self._stored_regime("NVDA.US") == 1

    def test_switching_back_and_forth_in_one_day_is_exact(self) -> None:
        runner = self._bound("TSLA.US")
        self._count(runner, 5)
        self._drain(runner)
        self._switch(runner, "NVDA.US")
        self._count(runner, 2)
        self._switch(runner, "TSLA.US")
        self._count(runner, 3)
        self._drain(runner)

        assert self._stored_regime("TSLA.US") == 8
        assert self._stored_regime("NVDA.US") == 2

    def test_strategy_reload_switch_rebinds_the_funnel(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.services.strategy_service import StrategyService

        # Given a bound TSLA session with unwritten counts
        runner = self._bound("TSLA.US")
        self._count(runner, 3)
        config = SimpleNamespace(
            symbol="NVDA.US", market="US", buy_low=400.0, sell_high=410.0,
            short_selling=False, min_profit_amount=0.0, auto_resume_minutes=3,
            max_daily_loss=5000.0, max_consecutive_losses=3, fee_rate_us=0.0005,
            fee_rate_hk=0.003, min_repricing_pct=0.003,
            llm_action_cooldown_seconds=60, trading_session_mode="RTH_ONLY",
            margin_safety_factor=0.35,
        )
        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", lambda _self: config)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner, "_sync_symbol_runtimes", lambda _db: None)
        runner._quotes_subscribed = False

        # When the live strategy reload switches the primary symbol
        runner.reload_strategy()
        self._count(runner, 1)
        self._drain(runner)

        # Then the reload handed TSLA's counts off before NVDA counted
        assert self._stored_regime("TSLA.US") == 3
        assert self._stored_regime("NVDA.US") == 1

    def test_failed_hand_off_never_credits_the_new_symbol(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Given 5 unwritten TSLA counts and a hand-off that fails
        runner = self._bound("TSLA.US")
        self._count(runner, 5)

        def _broken() -> list[object]:
            raise RuntimeError("tracker failure")

        monkeypatch.setattr(runner.decision_funnel, "take_all_sessions", _broken)

        # When the primary switches anyway and NVDA counts one
        import logging

        with caplog.at_level(logging.WARNING):
            self._switch(runner, "NVDA.US")
        self._count(runner, 1)
        self._drain(runner)

        # Then TSLA's counts are a reported gap, never NVDA's
        assert self._stored_regime("NVDA.US") == 1
        assert self._stored_regime("TSLA.US") is None
        assert "telemetry gap" in caplog.text

    def test_commit_then_teardown_failure_never_double_counts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import contextmanager

        # Given a session whose teardown fails after a successful commit
        runner = self._bound()
        self._count(runner, 3)

        @contextmanager
        def _teardown_fails():
            db = database.SessionLocal()
            try:
                yield db
            finally:
                db.close()
            raise RuntimeError("teardown failed")

        monkeypatch.setattr(runner, "_db_session", _teardown_fails)
        with pytest.raises(RuntimeError, match="teardown failed"):
            runner._decision_funnel_write_once()
        assert self._stored_regime("TSLA.US") == 3

        # When the next pass runs normally
        monkeypatch.undo()
        self._drain(runner)

        # Then the committed slice was not applied a second time
        assert self._stored_regime("TSLA.US") == 3

    def test_failed_write_keeps_the_counts_for_the_next_pass(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = self._bound()
        self._count(runner, 3)

        def _database_down(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(runner_module, "add_session_counts", _database_down)
        with pytest.raises(RuntimeError, match="database unavailable"):
            runner._decision_funnel_write_once()
        self._count(runner, 1)
        monkeypatch.undo()
        self._drain(runner)

        assert self._stored_regime("TSLA.US") == 4

    def test_one_failing_row_never_blocks_the_others(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given pending counts for two symbols, the first of which cannot be written
        runner = self._bound("TSLA.US")
        self._count(runner, 2)
        self._switch(runner, "NVDA.US")
        self._count(runner, 3)
        real_add = runner_module.add_session_counts

        def _tsla_row_fails(db: object, delta: object, *, symbol: str, market: str) -> None:
            if symbol == "TSLA.US":
                raise RuntimeError("TSLA row unwritable")
            real_add(db, delta, symbol=symbol, market=market)  # pyright: ignore[reportArgumentType]

        monkeypatch.setattr(runner_module, "add_session_counts", _tsla_row_fails)

        # When a writer pass runs
        with pytest.raises(RuntimeError, match="TSLA row unwritable"):
            runner._decision_funnel_write_once()

        # Then the other symbol still reached the database, and TSLA stays queued
        assert self._stored_regime("NVDA.US") == 3
        assert self._stored_regime("TSLA.US") is None
        monkeypatch.undo()
        self._drain(runner)
        assert self._stored_regime("TSLA.US") == 2
        assert self._stored_regime("NVDA.US") == 3

    def test_backlog_merges_per_symbol_and_day(self) -> None:
        # Given the database is never written while symbols alternate
        runner = self._bound("TSLA.US")
        for index in range(1000):
            self._switch(runner, "TSLA.US" if index % 2 == 0 else "NVDA.US")
            self._count(runner, 1)
        for _ in range(1000):  # zero-count switches must queue nothing
            self._switch(runner, "AAPL.US")
            self._switch(runner, "MSFT.US")

        # Then the backlog holds one entry per symbol and day
        assert len(runner._decision_funnel_pending) == 2
        self._drain(runner)
        assert self._stored_regime("TSLA.US") == 500
        assert self._stored_regime("NVDA.US") == 500

    def test_backlog_is_capped_and_the_gap_is_reported(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        limit = runner_module._DECISION_FUNNEL_MAX_PENDING
        runner = self._bound("S0.US")
        with caplog.at_level(logging.WARNING):
            for index in range(limit + 10):
                self._count(runner, 1)
                self._switch(runner, f"S{index + 1}.US")

        assert len(runner._decision_funnel_pending) <= limit
        assert "telemetry gap" in caplog.text

    def test_each_pass_writes_a_bounded_batch(self) -> None:
        batch = runner_module._DECISION_FUNNEL_WRITES_PER_PASS
        runner = self._bound("S0.US")
        for index in range(batch + 5):
            self._count(runner, 1)
            self._switch(runner, f"S{index + 1}.US")

        runner._decision_funnel_write_once()
        with database.SessionLocal() as db:
            written = db.query(DecisionFunnelSessionSummary).count()
        assert written == batch

        self._drain(runner)
        with database.SessionLocal() as db:
            assert db.query(DecisionFunnelSessionSummary).count() == batch + 5

    def test_day_rollover_keeps_each_day_exact(self) -> None:
        clock = [self._DAY]
        next_day = date(2026, 9, 25)
        runner = self._bound(clock=clock)
        self._count(runner, 2)
        self._drain(runner)
        self._count(runner, 1)

        clock[0] = next_day
        self._count(runner, 1)
        self._drain(runner)

        assert self._stored_regime("TSLA.US") == 3
        assert self._stored_regime("TSLA.US", next_day) == 1

    def test_rollover_racing_the_writer_never_double_counts(self) -> None:
        # Given a day whose clock turns over mid-pass: the next read of the
        # trade day still returns D, every later read returns D+1
        next_day = date(2026, 9, 25)
        current = [self._DAY]
        one_more_read_of_old_day: list[date] = []

        def _trade_day() -> date:
            return one_more_read_of_old_day.pop(0) if one_more_read_of_old_day else current[0]

        runner = _runner()
        runner.engine.params = StrategyParams(
            symbol="TSLA.US", market="US", buy_low=1.0, sell_high=2.0
        )
        runner.decision_funnel = DecisionFunnelTracker(trade_day_provider=_trade_day)
        runner._bind_decision_funnel()
        self._count(runner, 2)
        self._drain(runner)
        self._count(runner, 1)

        # When midnight lands between the closed-session drain and the live
        # snapshot of one writer pass
        current[0] = next_day
        one_more_read_of_old_day.append(self._DAY)
        runner._decision_funnel_write_once()
        self._count(runner, 1)
        self._drain(runner)

        # Then each day holds exactly its own counts
        assert self._stored_regime("TSLA.US") == 3
        assert self._stored_regime("TSLA.US", next_day) == 1

    def test_binding_never_touches_the_database_or_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _no_db(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("binding performed database I/O")

        monkeypatch.setattr(runner_module, "add_session_counts", _no_db)
        runner = self._bound("TSLA.US")
        self._count(runner, 1)

        def _broken() -> list[object]:
            raise RuntimeError("tracker failure")

        monkeypatch.setattr(runner.decision_funnel, "take_all_sessions", _broken)
        monkeypatch.setattr(runner.decision_funnel, "snapshot", _broken)

        self._switch(runner, "NVDA.US")

        assert runner._decision_funnel_binding == ("NVDA.US", "US")

    def test_unrecoverable_rebind_leaves_the_funnel_unbound_not_misattributed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        # Given 5 unwritten TSLA counts whose tracker cannot be read or cleared
        runner = self._bound("TSLA.US")
        self._count(runner, 5)

        def _broken() -> None:
            raise RuntimeError("tracker failure")

        monkeypatch.setattr(runner.decision_funnel, "take_all_sessions", _broken)
        monkeypatch.setattr(runner.decision_funnel, "discard_all", _broken)

        # When the primary switches: the call must not raise
        self._switch(runner, "NVDA.US")

        # Then the funnel is unbound, so TSLA's counts can never land on NVDA
        assert runner._decision_funnel_binding is None

        # And once the tracker works again the writer rebinds and reports the
        # counts it could not attribute as a gap instead of persisting them
        monkeypatch.undo()
        with caplog.at_level(logging.WARNING):
            self._drain(runner)
        assert runner._decision_funnel_binding == ("NVDA.US", "US")
        assert self._stored_regime("NVDA.US") is None
        assert self._stored_regime("TSLA.US") is None
        assert "gathered while unbound" in caplog.text
        self._count(runner, 1)
        self._drain(runner)
        assert self._stored_regime("NVDA.US") == 1

    def test_run_loop_housekeeping_never_waits_on_the_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threading
        import time as time_module

        runner = self._bound()
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def _blocked_write() -> None:
            calls.append(threading.current_thread().name)
            entered.set()
            release.wait(5)

        monkeypatch.setattr(runner, "_decision_funnel_write_once", _blocked_write)
        try:
            started = time_module.monotonic()
            runner._decision_funnel_housekeeping()
            assert entered.wait(5)
            runner._decision_funnel_checkpoint_at = 0.0
            runner._decision_funnel_housekeeping()
            elapsed = time_module.monotonic() - started
        finally:
            release.set()
            if runner._decision_funnel_writer is not None:
                runner._decision_funnel_writer.join(5)

        assert elapsed < 1.0, "housekeeping blocked the run loop on the database"
        assert calls == ["decision-funnel-writer"]


class TestDecisionFunnelSuppressionVisibility:
    """The funnel must explain a zero-order session, not just report zero.

    Two suppressions were previously invisible: a quote the live quality gate
    rejects never reaches any counter, and a crossing blocked by the fresh-
    crossing evidence gate is counted as a crossing and then vanishes. Both
    render as "crossings 0/N, every skip 0", which is exactly the reading that
    made a stalled session look like an idle one.
    """

    def _stale_quote(self, symbol: str, price: float) -> Quote:
        return Quote(symbol, price, price - 0.01, price + 0.01, "2020-01-01T00:00:00+00:00")

    def test_quality_gate_rejection_is_counted_with_its_reason(self) -> None:
        runner = _runner()
        runner._on_quote(self._stale_quote("NVDA.US", 105.0))

        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.primary_quotes_seen == 1
        assert snapshot.evaluations == 0
        assert snapshot.quality_rejections == 1
        assert snapshot.quality_rejections_by_reason["source_timestamp_fresh"] == 1

    def test_healthy_quote_records_no_rejection(self) -> None:
        runner = _runner()
        runner._on_quote(_quote("NVDA.US", 105.0))

        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.primary_quotes_seen == 1
        assert snapshot.evaluations == 1
        assert snapshot.quality_rejections == 0
        assert all(v == 0 for v in snapshot.quality_rejections_by_reason.values())

    def test_entry_crossing_block_is_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            runner_module.settings, "live_entry_crossing_required", True
        )
        runner = _runner()
        # A first quote already below buy_low gives the crossing gate no
        # outside-the-zone predecessor, so it withholds the entry.
        runner._on_quote(_quote("NVDA.US", 99.5))

        snapshot = runner.decision_funnel.snapshot()
        assert snapshot.threshold_crossings == 1
        assert snapshot.triggers == 0
        assert snapshot.entry_crossing_blocks == 1

    def test_diagnostics_exposes_the_new_counters(self) -> None:
        runner = _runner()
        runner._on_quote(self._stale_quote("NVDA.US", 105.0))

        payload = runner.diagnostics()
        funnel = payload["decision_funnel"]
        assert funnel["primary_quotes_seen"] == 1
        assert funnel["quality_rejections"] == 1
        assert funnel["quality_rejections_by_reason"]["source_timestamp_fresh"] == 1
        parsed = DiagnosticsResponse.model_validate(payload)
        assert parsed.decision_funnel.quality_rejections == 1
