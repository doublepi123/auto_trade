# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import patch

import pytest

from app import database
from app import runner as runner_module
from app.core.broker import OrderResult, Position, Quote
from app.core.engine import EngineSnapshot, EngineState, StrategyParams
from app.runner import AppRunner, _ReductionIntent, get_runner
from app.services import trade_execution_service as trade_execution_service_module


database.init_db()


def _fresh_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class _NoopNotifier:
    def notify_order(self, *args: object) -> bool:
        return True

    def notify_risk_event(self, *args: object) -> bool:
        return True


class TestAppRunner:
    @pytest.fixture(autouse=True)
    def _enable_live_llm_policy_for_execution_tests(self, monkeypatch) -> None:
        monkeypatch.setattr(runner_module.settings, "llm_shadow_mode", False)
        monkeypatch.setattr(
            trade_execution_service_module,
            "is_trading_hours",
            lambda _market: True,
        )
        monkeypatch.setattr(
            trade_execution_service_module,
            "is_opening_warmup",
            lambda _market, _minutes: False,
        )
        monkeypatch.setattr(
            trade_execution_service_module,
            "is_closing_window",
            lambda _market, _minutes: False,
        )

    def _stub_trade_callbacks(self, runner: AppRunner) -> None:
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner._trade_svc._final_order_quote_check = lambda *_args: None
        runner._llm_order_execution_enabled = True
        if not callable(getattr(runner.broker, "get_positions", None)):
            setattr(runner.broker, "get_positions", lambda: [])

    def _execute_buy(self, runner: AppRunner, symbol: str, quote: Quote):
        return runner._trade_svc._execute_buy(
            symbol,
            quote,
            runner.broker,
            runner.risk,
            runner.notifier,
            runner._cash_currency(),
        )

    def test_repeated_reconciliation_hazard_revokes_protective_exits(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda: None)
        monkeypatch.setattr(runner, "_record_risk_event", lambda _reason: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        inventory = {"AAPL.US": ["live-1", "live-2"]}
        assert runner._latch_live_order_reconciliation(inventory, []) is True
        assert runner.risk.permit_protective_exits() is True

        assert runner._latch_live_order_reconciliation(inventory, []) is True
        assert runner.risk.protective_exit_permitted is False

    def test_incomplete_pnl_ledger_blocks_operational_resume(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")

        class IncompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=False,
                )

        class Broker:
            @staticmethod
            def get_positions() -> list[object]:
                return []

        runner = AppRunner()
        runner.broker = cast(Any, Broker())
        reason = "ORDER_EXECUTION_BLOCKED: broker acknowledgement needs review"
        runner.risk.pause(reason, auto_resumable=False)
        runner._last_order_sync_succeeded = True
        monkeypatch.setattr(
            runner,
            "sync_today_orders_from_broker",
            lambda *, force: 0,
        )
        monkeypatch.setattr(
            runner_module,
            "DailyPnlService",
            IncompletePnlService,
        )
        monkeypatch.setattr(
            runner,
            "_reconcile_tracked_entries_with_broker",
            lambda _db, **_kwargs: [],
        )
        monkeypatch.setattr(
            runner._state_svc,
            "persist",
            lambda _db, _engine, _risk: None,
        )

        safe, error = runner.verify_operational_resume()

        assert safe is False
        assert error == (
            "realized PnL ledger remains incomplete for trade day "
            f"{trade_day}"
        )
        assert runner.risk.paused is True

    def test_incomplete_order_ledger_latches_pnl_operational_pause(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")

        class IncompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=False,
                )

        runner = AppRunner()
        monkeypatch.setattr(
            runner_module,
            "DailyPnlService",
            IncompletePnlService,
        )
        monkeypatch.setattr(
            runner,
            "_persist_risk_pause_best_effort",
            lambda: None,
        )
        monkeypatch.setattr(runner, "_record_risk_event", lambda _reason: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        assert runner._sync_risk_from_order_ledger() is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "PNL_RECONCILIATION_UNCERTAIN:"
        )
        assert runner.risk.check().approved is False

    def test_order_ledger_calculation_error_is_deferred_only_during_startup(
        self,
        monkeypatch,
    ) -> None:
        class FailingPnlService:
            def __init__(self, _db: object) -> None:
                pass

            def calculate(self, **_kwargs: object) -> object:
                raise RuntimeError("ledger unavailable")

        runner = AppRunner()
        monkeypatch.setattr(runner_module, "DailyPnlService", FailingPnlService)
        monkeypatch.setattr(
            runner,
            "_persist_risk_pause_best_effort",
            lambda: None,
        )
        monkeypatch.setattr(runner, "_record_risk_event", lambda _reason: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        runner._defer_incomplete_pnl_latch = True
        assert runner._sync_risk_from_order_ledger() is False
        assert runner.risk.paused is False

        runner._defer_incomplete_pnl_latch = False
        assert runner._sync_risk_from_order_ledger() is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "PNL_RECONCILIATION_UNCERTAIN:"
        )

    def test_pnl_latch_preserves_existing_operational_diagnosis(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        existing = "ORDER_SUBMISSION_UNCERTAIN: broker acknowledgement missing"
        runner.risk.pause(existing, auto_resumable=False)
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(
            runner,
            "_persist_risk_pause_best_effort",
            lambda: pytest.fail("existing operational pause must be preserved"),
        )

        runner._latch_pnl_reconciliation_uncertain(
            runner_module.trade_day_for("US"),
        )

        assert runner.risk.pause_reason == existing
        assert runner.risk.protective_exit_permitted is True
        assert runner.risk.check().approved is False

    def test_post_fill_incomplete_ledger_latches_pnl_pause(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")
        latched_days: list[object] = []

        class IncompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def refresh_execution_outcomes(self, *, symbol: str | None) -> int:
                return 0

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=False,
                )

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs: object) -> None:
                self._target = target

            def start(self) -> None:
                self._target()

        runner = AppRunner()
        monkeypatch.setattr(
            runner_module,
            "DailyPnlService",
            IncompletePnlService,
        )
        monkeypatch.setattr(runner_module.threading, "Thread", ImmediateThread)
        monkeypatch.setattr(
            runner,
            "_latch_pnl_reconciliation_uncertain",
            latched_days.append,
        )

        runner._mark_fill_processed("AAPL.US")

        assert latched_days == [trade_day]

    def test_post_fill_blocks_entries_until_complete_replay(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")
        targets: list[Callable[[], None]] = []

        class CompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def refresh_execution_outcomes(self, *, symbol: str | None) -> int:
                return 0

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=True,
                    trades=[],
                    realized_pnl=0.0,
                    consecutive_losses=0,
                )

            @staticmethod
            def reconcile_risk_state(
                current_pnl: float,
                current_losses: int,
                _current_trade_day: object,
                _result: object,
            ) -> tuple[float, int]:
                return current_pnl, current_losses

        class DeferredThread:
            def __init__(self, *, target: Callable[[], None], **_kwargs: object) -> None:
                targets.append(target)

            def start(self) -> None:
                pass

        runner = AppRunner()
        monkeypatch.setattr(runner_module, "DailyPnlService", CompletePnlService)
        monkeypatch.setattr(runner_module.threading, "Thread", DeferredThread)
        monkeypatch.setattr(
            runner._state_svc,
            "persist",
            lambda _db, _engine, _risk: None,
        )
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        runner._mark_fill_processed("AAPL.US")

        assert len(targets) == 1
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "post-fill PnL reconciliation in progress:"
        )
        assert runner.risk.check().approved is False
        assert runner._risk_rejection_allows_action("BUY") is False
        assert runner._risk_rejection_allows_action("SELL") is True

        targets[0]()

        assert runner.risk.paused is False
        assert runner.risk.entry_reconciliation_count == 0

    def test_post_fill_waits_for_all_concurrent_replays_before_resuming(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")
        targets: list[Callable[[], None]] = []

        class CompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def refresh_execution_outcomes(self, *, symbol: str | None) -> int:
                return 0

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=True,
                    trades=[],
                    realized_pnl=0.0,
                    consecutive_losses=0,
                )

            @staticmethod
            def reconcile_risk_state(
                current_pnl: float,
                current_losses: int,
                _current_trade_day: object,
                _result: object,
            ) -> tuple[float, int]:
                return current_pnl, current_losses

        class DeferredThread:
            def __init__(self, *, target: Callable[[], None], **_kwargs: object) -> None:
                targets.append(target)

            def start(self) -> None:
                pass

        runner = AppRunner()
        monkeypatch.setattr(runner_module, "DailyPnlService", CompletePnlService)
        monkeypatch.setattr(runner_module.threading, "Thread", DeferredThread)
        monkeypatch.setattr(
            runner._state_svc,
            "persist",
            lambda _db, _engine, _risk: None,
        )
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        runner._mark_fill_processed("AAPL.US")
        runner._mark_fill_processed("AAPL.US")
        assert len(targets) == 2

        targets[0]()
        assert runner.risk.paused is True
        assert runner.risk.entry_reconciliation_count == 1

        targets[1]()
        assert runner.risk.paused is False
        assert runner.risk.entry_reconciliation_count == 0

    def test_partial_protective_fill_keeps_next_reduction_permitted(
        self,
        monkeypatch,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")
        targets: list[Callable[[], None]] = []

        class CompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def refresh_execution_outcomes(self, *, symbol: str | None) -> int:
                return 0

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=True,
                    trades=[],
                    realized_pnl=0.0,
                    consecutive_losses=0,
                )

            @staticmethod
            def reconcile_risk_state(
                current_pnl: float,
                current_losses: int,
                _current_trade_day: object,
                _result: object,
            ) -> tuple[float, int]:
                return current_pnl, current_losses

        class DeferredThread:
            def __init__(self, *, target: Callable[[], None], **_kwargs: object) -> None:
                targets.append(target)

            def start(self) -> None:
                pass

        runner = AppRunner()
        runner._trade_svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("3"),
                Decimal("300"),
                "LONG",
                datetime.now(timezone.utc),
            )
        })
        pause_reason = "PNL_RECONCILIATION_UNCERTAIN: incomplete ledger"
        runner.risk.pause(pause_reason)
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(runner_module, "DailyPnlService", CompletePnlService)
        monkeypatch.setattr(runner_module.threading, "Thread", DeferredThread)
        monkeypatch.setattr(
            runner._state_svc,
            "persist",
            lambda _db, _engine, _risk: None,
        )

        runner._mark_fill_processed("AAPL.US", "SELL")

        assert len(targets) == 1
        assert runner.risk.entry_reconciliation_count == 1
        assert runner.risk.protective_exit_permitted is True
        assert runner._risk_rejection_allows_action("SELL") is True
        assert runner._risk_rejection_allows_action("BUY") is False

        targets[0]()

        assert runner.risk.entry_reconciliation_count == 0
        assert runner.risk.pause_reason == pause_reason
        assert runner.risk.protective_exit_permitted is True
        assert runner._risk_rejection_allows_action("SELL") is True

    def test_post_fill_thread_start_failure_becomes_durable_pnl_pause(
        self,
        monkeypatch,
    ) -> None:
        class FailingThread:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread unavailable")

        runner = AppRunner()
        monkeypatch.setattr(runner_module.threading, "Thread", FailingThread)
        monkeypatch.setattr(
            runner,
            "_persist_risk_pause_best_effort",
            lambda: None,
        )
        monkeypatch.setattr(runner, "_record_risk_event", lambda _reason: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        runner._mark_fill_processed("AAPL.US")

        assert runner.risk.entry_reconciliation_count == 0
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "PNL_RECONCILIATION_UNCERTAIN:"
        )

    def test_order_snapshot_fetch_failure_latches_exact_recovery_reason(
        self,
        monkeypatch,
    ) -> None:
        class Broker:
            def get_today_orders(self) -> list[object]:
                raise RuntimeError("snapshot unavailable")

        runner = AppRunner()
        runner.broker = cast(Any, Broker())
        monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda: None)
        monkeypatch.setattr(runner, "_record_risk_event", lambda _reason: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        assert runner.sync_today_orders_from_broker(force=True) == 0
        assert runner.risk.paused is True
        assert (
            runner.risk.pause_reason
            == runner_module._EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
        )
        assert runner.risk.pause_auto_resumable is False
        assert runner.diagnostics()["order_sync_succeeded"] is False

    def test_failed_protective_exit_reverification_revokes_prior_permission(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            lambda **_kwargs: (False, "broker state changed"),
        )
        monkeypatch.setattr(
            runner,
            "_protective_exit_runtime_health",
            lambda: (True, ""),
        )

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is False
        assert error == "broker state changed"
        assert runner.risk.protective_exit_permitted is False

    def test_protective_exit_verification_rejects_changed_pause_reason(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: initial review")

        def change_operational_pause(**_kwargs: object) -> tuple[bool, str]:
            runner.risk.pause(
                "POSITION_RECONCILIATION_UNCERTAIN: broker state changed"
            )
            return True, ""

        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            change_operational_pause,
        )
        monkeypatch.setattr(
            runner,
            "_protective_exit_runtime_health",
            lambda: (True, ""),
        )

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is False
        assert error == "protective exits require an unchanged operational pause"
        assert runner.risk.protective_exit_permitted is False

    def test_protective_exit_verification_rejects_same_reason_aba(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        reason = "ORDER_EXECUTION_BLOCKED: repeated broker failure"
        runner.risk.pause(reason)

        def repeat_operational_pause(**_kwargs: object) -> tuple[bool, str]:
            runner.risk.pause(reason)
            return True, ""

        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            repeat_operational_pause,
        )
        monkeypatch.setattr(
            runner,
            "_protective_exit_runtime_health",
            lambda: (True, ""),
        )

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is False
        assert error == "protective exits require an unchanged operational pause"
        assert runner.risk.paused is True
        assert runner.risk.protective_exit_permitted is False

    def test_protective_exit_verification_rejects_stopped_runner(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            lambda: pytest.fail("broker verification must not run while stopped"),
        )

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is False
        assert "runner thread" in error
        assert runner.risk.protective_exit_permitted is False

    def test_protective_exit_verification_rechecks_quote_loop_health(
        self,
        monkeypatch,
    ) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        runner._running = True
        runner._thread = cast(Any, AliveThread())
        runner._quotes_subscribed = True
        runner._last_quote_at = time.monotonic()

        def make_quote_loop_stale(**_kwargs: object) -> tuple[bool, str]:
            runner._last_quote_at = time.monotonic() - 31
            return True, ""

        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            make_quote_loop_stale,
        )

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is False
        assert "healthy quote loop" in error
        assert runner.risk.protective_exit_permitted is False

    @pytest.mark.parametrize("pause_kind", ["PNL", "ORDER"])
    def test_incomplete_pnl_can_arm_verified_protective_exits(
        self,
        monkeypatch,
        pause_kind: str,
    ) -> None:
        trade_day = runner_module.trade_day_for("US")
        pnl_reason = (
            "PNL_RECONCILIATION_UNCERTAIN: incomplete order ledger "
            f"for trade day {trade_day}; new entries remain blocked until "
            "the realized PnL can be reconciled"
        )
        reason = (
            pnl_reason
            if pause_kind == "PNL"
            else "ORDER_EXECUTION_BLOCKED: broker acknowledgement needs review"
        )

        class Broker:
            @staticmethod
            def get_positions() -> list[object]:
                return []

        class IncompletePnlService:
            def __init__(self, _db: object) -> None:
                pass

            def calculate(self, **_kwargs: object) -> object:
                return SimpleNamespace(
                    trade_day=trade_day,
                    is_complete=False,
                )

        runner = AppRunner()
        runner.broker = cast(Any, Broker())
        runner.risk.pause(reason, auto_resumable=False)
        runner._last_order_sync_succeeded = True

        def sync(*, force: bool) -> int:
            assert force is True
            runner._last_order_sync_succeeded = True
            runner._latch_pnl_reconciliation_uncertain(trade_day)
            return 0

        monkeypatch.setattr(runner, "sync_today_orders_from_broker", sync)
        monkeypatch.setattr(
            runner_module,
            "DailyPnlService",
            IncompletePnlService,
        )
        monkeypatch.setattr(
            runner,
            "_reconcile_tracked_entries_with_broker",
            lambda _db, **_kwargs: [],
        )
        monkeypatch.setattr(
            runner._state_svc,
            "persist",
            lambda _db, _engine, _risk: None,
        )
        monkeypatch.setattr(
            runner,
            "_protective_exit_runtime_health",
            lambda: (True, ""),
        )
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        safe, error = runner.permit_protective_exits_after_verification()

        assert safe is True
        assert error == ""
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == reason
        assert runner.risk.check().approved is False
        assert runner.risk.protective_exit_permitted is True
        assert runner._risk_rejection_allows_action("BUY") is False
        assert runner._risk_rejection_allows_action("SELL") is True

        assert runner._sync_risk_from_order_ledger() is False
        assert runner.risk.pause_reason == reason
        assert runner.risk.protective_exit_permitted is True
        assert runner._risk_rejection_allows_action("BUY") is False
        assert runner._risk_rejection_allows_action("SELL") is True

    def test_protective_exit_health_requires_fresh_primary_quote(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        runner = AppRunner()
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )
        runner._symbol_runtimes = {
            "NVDA.US": runner._build_symbol_runtime("NVDA.US", "US", primary=True),
            "AAPL.US": runner._build_symbol_runtime("AAPL.US", "US"),
        }
        runner._running = True
        runner._thread = cast(Any, AliveThread())
        runner._quotes_subscribed = True
        runner._last_quote_at = time.monotonic() - 31

        runner._remember_quote(
            Quote(
                symbol="AAPL.US",
                last_price=210.0,
                bid=209.9,
                ask=210.1,
                timestamp=_fresh_timestamp(),
            )
        )
        healthy, error = runner._protective_exit_runtime_health()

        assert healthy is False
        assert "healthy quote loop" in error

    def test_failed_resume_reverification_revokes_protective_exits(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            lambda: (False, "broker state changed"),
        )

        safe, error = runner.resume_after_verification()

        assert safe is False
        assert error == "broker state changed"
        assert runner.risk.paused is True
        assert runner.risk.protective_exit_permitted is False

    def test_resume_verification_does_not_clear_changed_operational_pause(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: initial review")

        def change_operational_pause() -> tuple[bool, str]:
            runner.risk.pause(
                "POSITION_RECONCILIATION_UNCERTAIN: broker state changed"
            )
            return True, ""

        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            change_operational_pause,
        )

        safe, error = runner.resume_after_verification()

        assert safe is False
        assert error == "operational pause changed during verification"
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == (
            "POSITION_RECONCILIATION_UNCERTAIN: broker state changed"
        )
        assert runner.risk.protective_exit_permitted is False

    def test_resume_verification_rejects_same_reason_aba(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        reason = "ORDER_EXECUTION_BLOCKED: repeated broker failure"
        runner.risk.pause(reason)

        def repeat_operational_pause() -> tuple[bool, str]:
            runner.risk.pause(reason)
            return True, ""

        monkeypatch.setattr(
            runner,
            "verify_operational_resume",
            repeat_operational_pause,
        )

        safe, error = runner.resume_after_verification()

        assert safe is False
        assert error == "operational pause changed during verification"
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == reason

    def test_fee_enrichment_runs_after_submission_guard_is_released(self) -> None:
        runner = AppRunner()
        guard_active = False

        class Guard:
            def __enter__(self) -> None:
                nonlocal guard_active
                guard_active = True

            def __exit__(self, *_args: object) -> None:
                nonlocal guard_active
                guard_active = False

        def sync(*, force: bool = False) -> tuple[int, list[object]]:
            assert force is True
            assert guard_active is True
            return 2, [object()]

        def enrich(_orders: object) -> None:
            assert guard_active is False

        runner._trade_svc.submission_guard = lambda: Guard()
        runner._sync_today_orders_from_broker_serialized = sync
        runner._enrich_broker_order_costs = enrich

        assert runner.sync_today_orders_from_broker(force=True) == 2

    def test_final_order_quote_gate_blocks_stale_quote_before_submit(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def get_positions(self) -> list[Position]:
                return []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                stale = datetime.now(timezone.utc) - timedelta(minutes=2)
                return [Quote(symbols[0], 100.0, 99.9, 100.1, stale.isoformat())]

            def estimate_margin_max_quantity(self, *_args, **_kwargs) -> Decimal:
                return Decimal("5")

            def submit_limit_order(self, *_args, **_kwargs) -> OrderResult:
                self.submissions += 1
                raise AssertionError("stale final quote must block broker submission")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None

        result = self._execute_buy(
            runner,
            "AAPL.US",
            Quote("AAPL.US", 100.0, 99.9, 100.1, _fresh_timestamp()),
        )

        assert result is not None
        assert result.status == "SKIPPED"
        assert broker.submissions == 0

    def test_reduce_only_final_quote_rebinds_submission_to_fresh_bid(self) -> None:
        class Broker:
            submitted_price: Decimal | None = None

            def get_positions(self) -> list[Position]:
                return [
                    Position(
                        "AAPL.US",
                        "LONG",
                        Decimal("2"),
                        Decimal("220"),
                        available_quantity=Decimal("2"),
                    )
                ]

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [
                    Quote(
                        symbols[0],
                        214.76,
                        214.73,
                        214.79,
                        _fresh_timestamp(),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submitted_price = price
                return OrderResult(
                    "fresh-protective-exit",
                    symbol,
                    side,
                    quantity,
                    price,
                    "FILLED",
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = cast(Any, broker)
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner._trade_svc.load_tracked_entries(
            {
                "AAPL.US": (
                    Decimal("2"),
                    Decimal("440"),
                    "LONG",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            }
        )

        result = runner._trade_svc.execute(
            "SELL",
            "AAPL.US",
            Quote("AAPL.US", 215.0, 214.9, 215.1, _fresh_timestamp()),
            cast(Any, broker),
            runner.risk,
            runner.notifier,
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert result is not None
        assert result.status == "FILLED"
        assert broker.submitted_price == Decimal("214.73")

    @pytest.mark.parametrize(
        ("action", "engine_state", "position_side", "bid", "ask", "order_price", "expected_price"),
        [
            ("SELL", EngineState.LONG, "LONG", 98.0, 98.1, 98.4, Decimal("98.00")),
            (
                "BUY_TO_COVER",
                EngineState.SHORT,
                "SHORT",
                101.0,
                101.1,
                101.5,
                Decimal("101.10"),
            ),
        ],
    )
    def test_llm_reduction_rebinds_submission_to_fresh_bbo(
        self,
        action: str,
        engine_state: EngineState,
        position_side: str,
        bid: float,
        ask: float,
        order_price: float,
        expected_price: Decimal,
    ) -> None:
        class Broker:
            submitted_price: Decimal | None = None

            def get_positions(self) -> list[Position]:
                return [
                    Position(
                        "AAPL.US",
                        position_side,
                        Decimal("2"),
                        Decimal("100"),
                        available_quantity=Decimal("2"),
                    )
                ]

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [
                    Quote(
                        symbols[0],
                        (bid + ask) / 2,
                        bid,
                        ask,
                        _fresh_timestamp(),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submitted_price = price
                return OrderResult(
                    "llm-reduce-only",
                    symbol,
                    side,
                    quantity,
                    price,
                    "SUBMITTED",
                )

        runner = AppRunner()
        broker = Broker()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.engine.state = engine_state
        runner.broker = cast(Any, broker)
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner._trade_svc.load_tracked_entries(
            {
                "AAPL.US": (
                    Decimal("2"),
                    Decimal("200"),
                    position_side,
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            }
        )

        result = runner._execute_llm_trade_action(
            action,
            order_price,
            allow_loss_exit=True,
        )

        assert result["status"] == "SUBMITTED"
        assert broker.submitted_price == expected_price

    @pytest.mark.parametrize("pnl_source", ["LEDGER_REPLAY", "UNKNOWN", ""])
    def test_execution_outcome_recompute_preserves_non_authoritative_pnl_source(
        self,
        pnl_source: str,
    ) -> None:
        order = SimpleNamespace(
            filled_at=datetime.now(timezone.utc),
            submit_started_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            executed_price=101.0,
            executed_quantity=2.0,
            side="SELL",
            cost_basis_price=100.0,
            position_quantity_before=2.0,
            pnl_fee_rate=0.0005,
            actual_fee=None,
            pnl_source=pnl_source,
            decision_bid=101.0,
            decision_ask=101.1,
        )

        AppRunner._update_execution_outcome_fields(order)

        assert order.gross_pnl == pytest.approx(2.0)
        assert order.pnl_source == pnl_source

    def test_immediate_filled_order_is_atomically_recorded_with_execution(self) -> None:
        from app.database import SessionLocal
        from app.models import OrderRecord, TradeEvent

        order_id = "atomic-immediate-fill"
        with SessionLocal() as db:
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == order_id
            ).delete()
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == order_id
            ).delete()
            db.commit()

        runner = AppRunner()
        runner._record_order(
            order_id,
            "AAPL.US",
            "BUY",
            5.0,
            100.0,
            "FILLED",
        )

        with SessionLocal() as db:
            order = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == order_id)
                .one()
            )
            assert order.status == "FILLED"
            assert order.filled_at is not None
            assert order.executed_quantity == 5.0
            assert order.executed_price == 100.0
            assert (
                db.query(TradeEvent)
                .filter(
                    TradeEvent.event_type == "ORDER_SUBMITTED",
                    TradeEvent.broker_order_id == order_id,
                )
                .count()
                == 1
            )
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == order_id
            ).delete()
            db.delete(order)
            db.commit()

    def test_terminal_update_preserves_first_partial_execution_time(
        self,
    ) -> None:
        from app.database import SessionLocal
        from app.models import OrderRecord, TradeEvent

        order_id = "partial-cross-day-terminal"
        first_fill = datetime(
            2026,
            7,
            23,
            19,
            tzinfo=timezone.utc,
        )
        terminal_update = datetime(
            2026,
            7,
            24,
            15,
            tzinfo=timezone.utc,
        )
        with SessionLocal() as db:
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == order_id
            ).delete(synchronize_session=False)
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == order_id
            ).delete(synchronize_session=False)
            db.add(
                OrderRecord(
                    broker_order_id=order_id,
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10,
                    price=200,
                    executed_quantity=3,
                    executed_price=199.5,
                    status="PARTIAL_FILLED",
                    created_at=first_fill - timedelta(minutes=1),
                    filled_at=first_fill,
                )
            )
            db.commit()

        runner = AppRunner()
        runner._update_order_status(
            order_id,
            "CANCELLED",
            terminal_update,
            3,
            199.5,
            {"broker_updated_at": terminal_update},
        )

        with SessionLocal() as db:
            order = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == order_id)
                .one()
            )
            assert order.status == "CANCELLED"
            assert order.executed_quantity == 3
            assert order.filled_at is not None
            assert runner._as_utc(order.filled_at) == first_fill
            assert order.broker_updated_at is not None
            assert runner._as_utc(order.broker_updated_at) == terminal_update
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == order_id
            ).delete(synchronize_session=False)
            db.delete(order)
            db.commit()

    def test_order_ledger_persists_decision_context_and_slippage(self) -> None:
        from app.database import SessionLocal
        from app.models import OrderRecord, TradeEvent

        order_id = "execution-ledger-context"
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == order_id
            ).delete()
            db.commit()

        runner = AppRunner()
        runner._record_order(
            order_id,
            "AAPL.US",
            "BUY",
            5.0,
            100.2,
            "FILLED",
            now,
            5.0,
            100.2,
            {
                "decision_at": now,
                "decision_bid": 99.9,
                "decision_ask": 100.0,
                "config_version": "abc123",
                "estimated_fee": 0.25,
                "fee_source": "ESTIMATED",
                "submit_started_at": now,
                "ack_latency_ms": 12.5,
            },
        )

        with SessionLocal() as db:
            order = db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == order_id
            ).one()
            assert order.config_version == "abc123"
            assert order.estimated_fee == 0.25
            assert order.ack_latency_ms == 12.5
            assert order.slippage_amount == pytest.approx(1.0)
            assert order.slippage_bps == pytest.approx(20.0)
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == order_id
            ).delete()
            db.delete(order)
            db.commit()

    def test_sync_symbol_runtimes_loads_watchlist_without_replacing_primary_engine(self, monkeypatch) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [
                    SimpleNamespace(symbol="AAPL.US", market="US"),
                    SimpleNamespace(symbol="NVDA.US", market="US"),
                ]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)

        runner._sync_symbol_runtimes(object())

        assert set(runner._symbol_runtimes) == {"NVDA.US", "AAPL.US"}
        assert runner._symbol_runtimes["NVDA.US"].engine is runner.engine
        assert runner._symbol_runtimes["AAPL.US"].engine is not runner.engine
        assert runner._symbol_runtimes["AAPL.US"].engine.params.symbol == "AAPL.US"
        assert runner.engine.params.symbol == "NVDA.US"

    def test_secondary_quote_is_read_only_without_mutating_primary_engine(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions: list[tuple[str, str, Decimal]] = []

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions.append((symbol, side, quantity))
                return OrderResult(f"order-{len(self.submissions)}", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="US")]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)
        runner._sync_symbol_runtimes(object())
        secondary = runner._symbol_runtimes["AAPL.US"]
        secondary.engine.params.buy_low = 100.0
        secondary.engine.params.sell_high = 200.0

        runner._on_quote(Quote("AAPL.US", 99.0, 98.9, 99.1, _fresh_timestamp()))

        assert broker.submissions == []
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 0.0
        assert secondary.engine.state == EngineState.FLAT
        assert secondary.engine.last_price == 99.0
        assert len(secondary.recent_quotes) == 1
        assert secondary.recent_quotes[0]["symbol"] == "AAPL.US"

    def test_secondary_quotes_never_create_pending_orders(self, monkeypatch) -> None:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.submissions = 0
                self.status_checks = 0

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(f"order-{self.submissions}", symbol, side, quantity, price, "SUBMITTED")

            def get_order_status(self, order_id: str) -> OrderStatusResult:
                self.status_checks += 1
                return OrderStatusResult(order_id, "SUBMITTED")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="US")]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)
        runner._sync_symbol_runtimes(object())
        secondary = runner._symbol_runtimes["AAPL.US"]
        secondary.engine.params.buy_low = 100.0
        secondary.engine.params.sell_high = 200.0
        quote = Quote("AAPL.US", 99.0, 98.9, 99.1, _fresh_timestamp())

        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 0
        assert broker.status_checks == 0
        assert runner._trade_svc.pending_order_for("AAPL.US") is None


    def test_diagnostics_reports_runner_symbol_and_pending_health(self) -> None:
        runner = AppRunner()
        runner._running = True
        runner._quotes_subscribed = True
        runner._trigger_in_flight = True
        runner._last_push_quote_at = time.monotonic() - 12.0
        runner._last_quote_at = time.monotonic() - 5.0
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)
        runner.engine.last_price = 220.5
        secondary = runner._build_symbol_runtime("AAPL.US", "US")
        secondary.engine.last_price = 199.5
        secondary.recent_quotes.append({"symbol": "AAPL.US"})
        runner._symbol_runtimes = {
            "NVDA.US": runner._build_symbol_runtime("NVDA.US", "US", primary=True),
            "AAPL.US": secondary,
        }
        runner._trade_svc.max_position_quantity = 100
        runner._trade_svc.max_position_notional = Decimal("5000")
        runner._trade_svc.max_risk_per_trade = Decimal("250")
        runner._trade_svc.stop_loss_pct = Decimal("1")
        runner._trade_svc.load_tracked_entries(
            {"NVDA.US": (Decimal("1088"), Decimal("224485.952"))}
        )
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("9"), Decimal("99"), "SUBMITTED"),
            runner.broker,
            None,
        )
        runner.risk.pause("manual")

        diagnostics = runner.diagnostics()

        assert diagnostics["runner_running"] is False
        assert diagnostics["thread_alive"] is False
        assert diagnostics["quotes_subscribed"] is True
        assert diagnostics["trigger_in_flight"] is True
        assert diagnostics["pending_order_symbols"] == ["AAPL.US"]
        assert diagnostics["dedup_suppressed_total"] == 0
        assert diagnostics["dedup_window_seconds"] == runner.notifier.dedup_window_seconds
        assert diagnostics["quote_stream"]["last_push_age_seconds"] >= 12.0
        assert diagnostics["quote_stream"]["last_quote_age_seconds"] >= 5.0
        assert diagnostics["risk"]["paused"] is True
        assert diagnostics["risk"]["pause_reason"] == "manual"
        assert diagnostics["live_safety"] == {
            "full_buying_power_usage_enabled": (
                runner._trade_svc.full_buying_power_usage_enabled
            ),
            "buying_power_usage_pct": 90.0,
            "short_entries_enabled": (
                runner._trade_svc.short_entries_enabled and runner.engine.params.short_selling
            ),
            "allow_position_addons": (
                runner._trade_svc.allow_position_addons
                and runner.engine.params.allow_position_addons
            ),
            "max_position_quantity": runner._trade_svc.max_position_quantity,
            "max_position_notional": runner._trade_svc.max_position_notional,
            "max_risk_per_trade": runner._trade_svc.max_risk_per_trade,
            "stop_loss_pct": runner._trade_svc.stop_loss_pct,
            "max_holding_minutes": runner.engine.params.max_holding_minutes,
            "entry_cutoff_minutes_before_close": (
                runner._trade_svc.entry_cutoff_minutes_before_close
            ),
            "flatten_minutes_before_close": runner.engine.params.flatten_minutes_before_close,
            "llm_shadow_mode": runner_module.settings.llm_shadow_mode,
            "llm_order_execution_enabled": runner._llm_order_execution_enabled,
            "live_regime_gate_enabled": (
                runner_module.settings.live_regime_gate_enabled
            ),
            "live_regime_max_data_age_seconds": (
                runner_module.settings.live_regime_max_data_age_seconds
            ),
            "live_max_entries_per_symbol_per_day": (
                runner_module.settings.live_max_entries_per_symbol_per_day
            ),
        }
        by_symbol = {item["symbol"]: item for item in diagnostics["symbol_runtimes"]}
        assert by_symbol["NVDA.US"]["is_primary"] is True
        assert by_symbol["NVDA.US"]["last_price"] == 220.5
        assert by_symbol["AAPL.US"]["is_primary"] is False
        assert by_symbol["AAPL.US"]["last_price"] == 199.5
        assert by_symbol["AAPL.US"]["recent_quote_count"] == 1
        assert by_symbol["AAPL.US"]["has_pending_order"] is True
        # Quote quality: primary has no recent quotes; secondary has a quote with missing prices.
        assert by_symbol["NVDA.US"]["quote_quality"]["has_quote"] is False
        assert by_symbol["NVDA.US"]["position_quantity"] == 1088.0
        assert by_symbol["NVDA.US"]["position_avg_price"] == 206.329
        assert by_symbol["NVDA.US"]["position_limit_breaches"] == [
            "MAX_POSITION_QUANTITY",
            "MAX_POSITION_NOTIONAL",
            "MAX_RISK_PER_TRADE",
        ]
        assert by_symbol["AAPL.US"]["quote_quality"]["has_quote"] is True
        assert by_symbol["AAPL.US"]["quote_quality"]["price_positive"] is False

    def test_quote_quality_evaluates_positive_prices_and_reasonable_spread(self) -> None:
        runner = AppRunner()
        good = {
            "last_price": 100.0,
            "bid": 99.9,
            "ask": 100.1,
            "timestamp": _fresh_timestamp(),
        }
        assert runner._evaluate_quote_quality(good) == {
            "has_quote": True,
            "price_positive": True,
            "spread_reasonable": True,
            "last_bbo_consistent": True,
            "source_timestamp_fresh": True,
            "last_price": 100.0,
            "bid": 99.9,
            "ask": 100.1,
        }
        zero_price = {"last_price": 0.0, "bid": 0.0, "ask": 0.0}
        assert runner._evaluate_quote_quality(zero_price)["price_positive"] is False
        wide_spread = {"last_price": 100.0, "bid": 90.0, "ask": 110.0}
        assert runner._evaluate_quote_quality(wide_spread)["spread_reasonable"] is False
        missing_timestamp = {"last_price": 100.0, "bid": 99.9, "ask": 100.1}
        assert (
            runner._evaluate_quote_quality(missing_timestamp)["source_timestamp_fresh"]
            is False
        )
        off_market_last = {
            "last_price": 101.0,
            "bid": 99.9,
            "ask": 100.1,
            "timestamp": _fresh_timestamp(),
        }
        assert (
            runner._evaluate_quote_quality(off_market_last)["last_bbo_consistent"]
            is False
        )
        none_quote = None
        assert runner._evaluate_quote_quality(none_quote) == {
            "has_quote": False,
            "price_positive": False,
            "spread_reasonable": False,
            "last_bbo_consistent": False,
            "source_timestamp_fresh": False,
        }

    def test_live_quote_quality_gate_blocks_trigger_on_bad_bbo(self) -> None:
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        decision = runner._evaluate_quote_trigger(
            Quote("AAPL.US", 99.0, 50.0, 150.0, _fresh_timestamp())
        )

        assert decision.early_return is True
        assert decision.result is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 0.0
        assert runner._last_quote_at == 0.0
        assert "quality gate" in runner.last_action_message

    def test_extended_hours_quote_does_not_rearm_regular_session_entry(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner._running = True
        runner._trading_session_mode = "RTH_ONLY"
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        runner.engine.restore_long_entry_rearm(True)
        monkeypatch.setattr("app.runner.is_trading_hours", lambda _market: False)

        decision = runner._evaluate_quote_trigger(
            Quote("AAPL.US", 101.0, 100.99, 101.01, _fresh_timestamp())
        )

        assert decision.result is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 101.0
        assert runner.engine.long_entry_rearm_required is True

    def test_risk_rejection_still_observes_regular_session_reclaim(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner._running = True
        runner._trading_session_mode = "RTH_ONLY"
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        runner.engine.restore_long_entry_rearm(True)
        runner.risk.consecutive_losses = runner.risk.config.max_consecutive_losses
        assert runner.risk.check().approved is False
        monkeypatch.setattr("app.runner.is_trading_hours", lambda _market: True)

        decision = runner._evaluate_quote_trigger(
            Quote("AAPL.US", 101.0, 100.99, 101.01, _fresh_timestamp())
        )

        assert decision.result is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.long_entry_rearm_required is False

    def test_bad_quote_cannot_update_price_or_trigger_unrealized_loss_pause(self) -> None:
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=90.0,
            sell_high=110.0,
        )
        runner.engine.last_price = 100.0
        runner._symbol_runtimes["AAPL.US"] = runner._build_symbol_runtime(
            "AAPL.US", "US", primary=True
        )
        runner._trade_svc.load_tracked_entries(
            {
                "AAPL.US": (
                    Decimal("10"),
                    Decimal("1000"),
                    "LONG",
                    datetime.now(timezone.utc),
                )
            }
        )
        runner.risk.config.max_daily_loss = 1.0

        runner._on_quote(Quote("AAPL.US", 1.0, 0.1, 100.0, _fresh_timestamp()))

        assert runner.engine.last_price == 100.0
        assert runner.risk.paused is False
        assert runner.fresh_market_price("AAPL.US") is None

    def test_fresh_market_price_requires_recent_executable_quote(self) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._symbol_runtimes["AAPL.US"] = runner._build_symbol_runtime(
            "AAPL.US", "US", primary=True
        )

        runner._remember_quote(Quote("AAPL.US", 100.0, 99.9, 100.1, _fresh_timestamp()))
        assert runner.fresh_market_price("AAPL.US") == 100.0

        runner._symbol_runtimes["AAPL.US"].recent_quotes[-1]["observed_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=31)
        )
        assert runner.fresh_market_price("AAPL.US") is None

    def test_remember_quote_logs_warning_for_zero_or_wide_spread(self, caplog) -> None:
        import logging
        runner = AppRunner()
        runner._running = True
        with caplog.at_level(logging.WARNING):
            runner._remember_quote(Quote(symbol="TSLA.US", last_price=0.0, bid=0.0, ask=0.0, timestamp=_fresh_timestamp()))
        assert "quote_quality: non-positive price" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            runner._remember_quote(Quote(symbol="TSLA.US", last_price=100.0, bid=80.0, ask=130.0, timestamp=_fresh_timestamp()))
        assert "quote_quality: wide spread" in caplog.text

    def test_llm_policy_quote_rejects_wrong_symbol_and_wide_spread(self) -> None:
        class Broker:
            def __init__(self, quote: Quote) -> None:
                self.quote = quote

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [self.quote]

        runner = AppRunner()
        runner.broker = Broker(Quote("MSFT.US", 100.0, 99.9, 100.1, _fresh_timestamp()))
        assert runner._trusted_quote_for_llm_policy("AAPL.US") is None

        runner.broker = Broker(Quote("AAPL.US", 100.0, 90.0, 110.0, _fresh_timestamp()))
        assert runner._trusted_quote_for_llm_policy("AAPL.US") is None

        good = Quote("AAPL.US", 100.0, 99.9, 100.1, _fresh_timestamp())
        runner.broker = Broker(good)
        assert runner._trusted_quote_for_llm_policy("AAPL.US") is good

    def test_sync_symbol_runtimes_loads_persisted_secondary_engine_state(self, monkeypatch) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100.0, sell_high=200.0)

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="US")]

        loaded: list[tuple[str, str]] = []

        def fake_load_symbol_runtime(db, engine, symbol: str) -> None:
            loaded.append((engine.params.symbol, symbol))
            if symbol == "AAPL.US":
                engine.state = EngineState.SHORT
                engine.last_price = 199.5

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", fake_load_symbol_runtime)

        runner._sync_symbol_runtimes(object())

        secondary = runner._symbol_runtimes["AAPL.US"]
        assert secondary.engine.state == EngineState.SHORT
        assert secondary.engine.last_price == 199.5
        assert loaded == [("AAPL.US", "AAPL.US")]

    def test_sync_symbol_runtimes_refreshes_secondary_strategy_params(self, monkeypatch) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
            short_selling=True,
            min_profit_amount=5.0,
            fee_rate_us=0.001,
            fee_rate_hk=0.004,
            min_repricing_pct=0.01,
            llm_action_cooldown_seconds=120,
        )

        class FakeWatchlistService:
            def __init__(self, db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="HK")]

        monkeypatch.setattr("app.runner.WatchlistService", FakeWatchlistService)

        runner._sync_symbol_runtimes(object())

        secondary = runner._symbol_runtimes["AAPL.US"]
        assert secondary.market == "HK"
        assert secondary.engine.params.market == "HK"
        assert secondary.engine.params.buy_low == 100.0
        assert secondary.engine.params.sell_high == 200.0
        assert secondary.engine.params.short_selling is True
        assert secondary.engine.params.min_profit_amount == 5.0
        assert secondary.engine.params.fee_rate_us == 0.001
        assert secondary.engine.params.fee_rate_hk == 0.004
        assert secondary.engine.params.min_repricing_pct == 0.01
        assert secondary.engine.params.llm_action_cooldown_seconds == 120

    def test_remember_symbol_runtime_quote_does_not_create_unknown_runtime(self) -> None:
        runner = AppRunner()

        runner._remember_symbol_runtime_quote(
            Quote(symbol="UNKNOWN.US", last_price=10.0, bid=9.9, ask=10.1, timestamp=_fresh_timestamp()),
            datetime.now(timezone.utc),
        )

        assert "UNKNOWN.US" not in runner._symbol_runtimes

    def _execute_sell(self, runner: AppRunner, symbol: str, quote: Quote):
        return runner._trade_svc._execute_sell(
            symbol,
            quote,
            runner.broker,
            runner.risk,
            runner.notifier,
        )

    def test_runner_init_defaults(self) -> None:
        runner = AppRunner()
        assert runner._running is False
        assert runner._thread is None
        assert runner.engine.state == EngineState.FLAT
        assert runner.risk.kill_switch is False

    def test_broker_gets_audit_logger(self) -> None:
        runner = AppRunner()
        assert runner.broker._audit is runner._audit

    def test_credential_switch_requires_stopped_flat_runner(self) -> None:
        runner = AppRunner()
        runner._running = True

        with pytest.raises(
            runner_module.CredentialSwitchBlockedError,
            match="runner must be stopped",
        ):
            runner.assert_credential_switch_safe()

    def test_credential_switch_rejects_lingering_runner_thread(self) -> None:
        runner = AppRunner()
        runner._running = False
        runner._thread = SimpleNamespace(is_alive=lambda: True)

        with pytest.raises(
            runner_module.CredentialSwitchBlockedError,
            match="previous runner thread is still alive",
        ):
            runner.assert_credential_switch_safe()

    def test_submission_provenance_is_scoped_to_broker_identity(self) -> None:
        now = datetime.now(timezone.utc)
        runner = AppRunner()
        runner._broker_identity_fingerprint = "current-account"
        order = SimpleNamespace(
            broker_order_id="reused-id",
            symbol="AAPL.US",
            side="BUY",
            created_at=now,
        )
        old_account_event = runner_module.TradeEvent(
            event_type="ORDER_SUBMITTED",
            broker_order_id="reused-id",
            symbol="AAPL.US",
            side="BUY",
            payload_json=(
                '{"broker_identity_fingerprint":"previous-account"}'
            ),
            created_at=now,
        )
        current_account_event = runner_module.TradeEvent(
            event_type="ORDER_SUBMITTED",
            broker_order_id="reused-id",
            symbol="AAPL.US",
            side="BUY",
            payload_json=(
                '{"broker_identity_fingerprint":"current-account"}'
            ),
            created_at=now,
        )

        assert runner._submission_event_matches_order(old_account_event, order) is False
        assert runner._submission_event_matches_order(current_account_event, order) is True

    def test_credential_switch_rejects_current_account_exposure(self) -> None:
        runner = AppRunner()
        runner._running = False
        runner.broker.get_positions = lambda: [
            Position("NVDA.US", "LONG", Decimal("1"), Decimal("100"))
        ]
        runner.broker.get_today_orders = lambda: []

        with pytest.raises(
            runner_module.CredentialSwitchBlockedError,
            match="still has positions",
        ):
            runner.assert_credential_switch_safe()

    def test_credential_switch_rejects_exposed_new_account_and_restores_env(
        self,
        monkeypatch,
    ) -> None:
        from app.services.credentials_service import PlainCredentials

        class NewBroker:
            closed = False

            def register_disconnect_hook(self, _hook) -> None:
                pass

            def get_positions(self) -> list[Position]:
                return [
                    Position(
                        "NVDA.US",
                        "LONG",
                        Decimal("1"),
                        Decimal("100"),
                    )
                ]

            def get_today_orders(self):
                return []

            def close(self) -> None:
                self.closed = True

        runner = AppRunner()
        old_broker = runner.broker
        new_broker = NewBroker()
        monkeypatch.setenv("LONGPORT_APP_KEY", "old-key")
        monkeypatch.setenv("LONGPORT_APP_SECRET", "old-secret")
        monkeypatch.setenv("LONGPORT_ACCESS_TOKEN", "old-token")
        monkeypatch.setattr(runner, "_build_broker", lambda _audit: new_broker)

        with pytest.raises(
            runner_module.CredentialSwitchBlockedError,
            match="new broker account already has positions",
        ):
            runner._apply_credentials(
                PlainCredentials(
                    longbridge_app_key="new-key",
                    longbridge_app_secret="new-secret",
                    longbridge_access_token="new-token",
                ),
                resubscribe=False,
                validate_switch=True,
            )

        assert runner.broker is old_broker
        assert new_broker.closed is True
        assert runner_module.os.environ["LONGPORT_APP_KEY"] == "old-key"
        assert runner_module.os.environ["LONGPORT_APP_SECRET"] == "old-secret"
        assert runner_module.os.environ["LONGPORT_ACCESS_TOKEN"] == "old-token"

    def test_ledger_replay_waits_until_partial_pending_is_terminal(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        runner._trade_svc._track_pending_order(
            "SELL",
            OrderResult(
                "partial-exit",
                "NVDA.US",
                "SELL",
                Decimal("5"),
                Decimal("99"),
                "PARTIAL_FILLED",
            ),
            runner.broker,
            None,
        )
        monkeypatch.setattr(
            runner_module.DailyPnlService,
            "calculate",
            lambda *_args, **_kwargs: pytest.fail(
                "live partial fill must not be replayed before service finalization"
            ),
        )

        assert runner._sync_risk_from_order_ledger() is False

    def test_reload_strategy_updates_margin_safety_factor(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService
        from app.models import StrategyConfig

        runner = AppRunner()
        runner._trade_svc.margin_safety_factor = None

        class FakeConfig:
            symbol = "AAPL.US"
            market = "US"
            buy_low = 100.0
            sell_high = 200.0
            short_selling = False
            min_profit_amount = 0.0
            auto_resume_minutes = 3
            max_daily_loss = 5000.0
            max_consecutive_losses = 3
            fee_rate_us = 0.0005
            fee_rate_hk = 0.003
            min_repricing_pct = 0.003
            llm_action_cooldown_seconds = 60
            trading_session_mode = "ANY"
            margin_safety_factor = 0.75

        class FakeSvc:
            def get_config(self):
                return FakeConfig()

        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", FakeSvc().get_config)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner.broker, "unsubscribe_quotes", lambda: None)
        monkeypatch.setattr(runner.broker, "subscribe_quotes", lambda symbol, callback: None)

        runner.reload_strategy()
        assert runner._trade_svc.margin_safety_factor == 0.75

    def test_reload_strategy_keeps_quote_subscription_when_symbols_unchanged(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService

        runner = AppRunner()
        runner._running = True
        runner._quotes_subscribed = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        runner.engine.restore_long_entry_rearm(True)
        runner._last_quote_at = 123.0
        runner._last_push_quote_at = 122.0
        runner._recent_quotes.append({"symbol": "AAPL.US", "last_price": 105.0})

        class FakeConfig:
            symbol = "AAPL.US"
            market = "US"
            buy_low = 101.0
            sell_high = 111.0
            short_selling = False
            min_profit_amount = 0.0
            auto_resume_minutes = 3
            max_daily_loss = 4000.0
            max_consecutive_losses = 4
            fee_rate_us = 0.0005
            fee_rate_hk = 0.003
            min_repricing_pct = 0.003
            llm_action_cooldown_seconds = 60
            trading_session_mode = "RTH_ONLY"
            margin_safety_factor = 0.35

        class FakeSvc:
            def get_config(self):
                return FakeConfig()

        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", FakeSvc().get_config)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner, "_sync_symbol_runtimes", lambda _db: None)
        monkeypatch.setattr(
            runner.broker,
            "unsubscribe_quotes",
            lambda: pytest.fail("unchanged symbols must not unsubscribe quotes"),
        )
        monkeypatch.setattr(
            runner.broker,
            "subscribe_quotes",
            lambda _symbol, _callback: pytest.fail("unchanged symbols must not subscribe quotes"),
        )

        runner.reload_strategy()

        assert runner._quotes_subscribed is True
        assert runner._last_quote_at == 123.0
        assert runner._last_push_quote_at == 122.0
        assert list(runner._recent_quotes) == [{"symbol": "AAPL.US", "last_price": 105.0}]
        assert runner.engine.params.buy_low == 101.0
        assert runner.engine.params.sell_high == 111.0
        assert runner.engine.long_entry_rearm_required is True

    def test_reload_strategy_resubscribes_when_primary_symbol_changes(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService

        runner = AppRunner()
        runner._running = True
        runner._quotes_subscribed = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        broker_calls: list[tuple[str, str]] = []

        class FakeConfig:
            symbol = "MSFT.US"
            market = "US"
            buy_low = 400.0
            sell_high = 410.0
            short_selling = False
            min_profit_amount = 0.0
            auto_resume_minutes = 3
            max_daily_loss = 5000.0
            max_consecutive_losses = 3
            fee_rate_us = 0.0005
            fee_rate_hk = 0.003
            min_repricing_pct = 0.003
            llm_action_cooldown_seconds = 60
            trading_session_mode = "RTH_ONLY"
            margin_safety_factor = 0.35

        class FakeSvc:
            def get_config(self):
                return FakeConfig()

        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", FakeSvc().get_config)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner, "_sync_symbol_runtimes", lambda _db: None)
        monkeypatch.setattr(
            runner.broker,
            "unsubscribe_quotes",
            lambda: broker_calls.append(("unsubscribe", "AAPL.US")),
        )
        monkeypatch.setattr(
            runner.broker,
            "subscribe_quotes_batch",
            lambda symbols, _callback: broker_calls.append(
                ("subscribe", ",".join(symbols))
            ),
        )

        runner.reload_strategy()

        assert broker_calls == [("unsubscribe", "AAPL.US"), ("subscribe", "MSFT.US")]
        assert runner._quotes_subscribed is True
        assert runner.engine.params.symbol == "MSFT.US"

    def test_primary_switch_is_blocked_while_position_is_tracked(self) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._trade_svc.load_tracked_entries(
            {
                "AAPL.US": (
                    Decimal("1"),
                    Decimal("100"),
                    "LONG",
                    datetime.now(timezone.utc),
                )
            }
        )

        with pytest.raises(runner_module.PrimarySwitchBlockedError, match="positions are tracked"):
            runner.assert_primary_switch_safe("MSFT.US", "US")

    def test_flat_primary_switch_detaches_old_engine(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        old_engine = runner.engine
        runner._symbol_runtimes["AAPL.US"] = runner._build_symbol_runtime(
            "AAPL.US", "US", primary=True
        )

        config = SimpleNamespace(
            symbol="MSFT.US",
            market="US",
            buy_low=400.0,
            sell_high=410.0,
            short_selling=False,
            min_profit_amount=0.0,
            auto_resume_minutes=3,
            max_daily_loss=5000.0,
            max_consecutive_losses=3,
            fee_rate_us=0.0005,
            fee_rate_hk=0.003,
            min_repricing_pct=0.003,
            llm_action_cooldown_seconds=60,
            trading_session_mode="RTH_ONLY",
            margin_safety_factor=0.35,
        )

        class FakeWatchlistService:
            def __init__(self, _db) -> None:
                pass

            def list_items(self):
                return [SimpleNamespace(symbol="AAPL.US", market="US")]

        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", lambda _self: config)
        monkeypatch.setattr(runner_module, "WatchlistService", FakeWatchlistService)
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])

        runner.reload_strategy()

        assert runner.engine is not old_engine
        assert runner.engine.params.symbol == "MSFT.US"
        assert runner._symbol_runtimes["AAPL.US"].engine is old_engine
        assert runner._symbol_runtimes["MSFT.US"].engine is runner.engine

    def test_initialize_runner_loads_margin_safety_factor(self, monkeypatch) -> None:
        from contextlib import contextmanager

        runner = AppRunner()
        runner._trade_svc.margin_safety_factor = None

        class FakeConfig:
            margin_safety_factor = 0.65

        @contextmanager
        def fake_db_session():
            yield object()

        monkeypatch.setattr(runner, "_db_session", fake_db_session)
        monkeypatch.setattr(runner._state_svc, "load", lambda db, engine, risk: FakeConfig())
        monkeypatch.setattr(runner, "_load_tracked_entries", lambda db: None)
        monkeypatch.setattr(runner, "_load_credentials", lambda: None)
        monkeypatch.setattr(runner, "_apply_credentials", lambda credentials, *, resubscribe: None)
        monkeypatch.setattr(runner, "_register_broker_disconnect_hook", lambda: None)
        monkeypatch.setattr(runner, "_refresh_trading_session_mode", lambda: None)
        monkeypatch.setattr(runner, "sync_today_orders_from_broker", lambda *, force: None)
        monkeypatch.setattr(runner, "_sync_risk_from_order_ledger", lambda: None)
        monkeypatch.setattr(runner, "_pause_if_unresolved_live_order_exists", lambda db: None)
        monkeypatch.setattr(
            runner,
            "_reconcile_tracked_entries_with_broker",
            lambda db, **_kwargs: [],
        )
        monkeypatch.setattr(runner.broker, "subscribe_quotes", lambda symbol, callback: None)

        runner._initialize_runner()

        assert runner._trade_svc.margin_safety_factor == 0.65

    def test_sync_engine_state_with_no_positions_sets_flat(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()

        changed = runner._sync_engine_state_with_positions()

        assert changed is True
        assert runner.engine.state == EngineState.FLAT

    def test_sync_engine_state_skips_while_pending_order_exists(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "SELL",
            OrderResult("order-pending", "NVDA.US", "SELL", Decimal("1"), Decimal("220"), "SUBMITTED"),
            Broker(),
            runner.engine.snapshot(),
        )

        changed = runner._sync_engine_state_with_positions()

        assert changed is False
        assert runner.engine.state == EngineState.LONG

    def test_recent_unsettled_entry_revokes_protective_exits(
        self,
        monkeypatch,
    ) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=218,
            sell_high=225,
        )
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner._trade_svc.load_tracked_entries({
            "NVDA.US": (
                Decimal("5"),
                Decimal("1000"),
                "LONG",
                datetime.now(timezone.utc),
            )
        })
        runner.risk.pause("ORDER_EXECUTION_BLOCKED: operator review")
        assert runner.risk.permit_protective_exits() is True
        monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda: None)

        assert runner._sync_engine_state_with_positions(force=True) is False
        assert runner.risk.protective_exit_permitted is False

    def test_position_sync_waits_for_submission_guard(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=218,
            sell_high=225,
        )
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        started = threading.Event()
        completed = threading.Event()

        def sync_positions() -> None:
            started.set()
            runner._sync_engine_state_with_positions(force=True)
            completed.set()

        with runner._trade_svc.submission_guard():
            thread = threading.Thread(target=sync_positions)
            thread.start()
            assert started.wait(timeout=1)
            assert completed.wait(timeout=0.05) is False

        assert completed.wait(timeout=1)
        thread.join(timeout=1)
        assert thread.is_alive() is False

    def test_live_safety_invalid_db_values_fall_back_to_hard_limits(self) -> None:
        runner = AppRunner()
        config = SimpleNamespace(
            margin_safety_factor=0.9,
            allow_position_addons=False,
            max_position_quantity=0,
            max_position_notional=float("nan"),
            max_risk_per_trade=float("inf"),
            stop_loss_pct=-1,
            entry_cutoff_minutes_before_close=0,
            llm_order_execution_enabled=False,
        )

        runner._configure_live_safety(config)

        assert runner._trade_svc.max_position_quantity == runner_module.settings.hard_max_position_quantity
        assert runner._trade_svc.max_position_notional == runner_module.settings.hard_max_position_notional
        assert runner._trade_svc.max_risk_per_trade == runner_module.settings.hard_max_risk_per_trade
        assert runner._trade_svc.stop_loss_pct == runner_module.settings.hard_stop_loss_pct
        assert (
            runner._trade_svc.entry_cutoff_minutes_before_close
            == runner_module.settings.hard_entry_cutoff_minutes_before_close
        )

        live_safety = runner.diagnostics()["live_safety"]
        assert live_safety["max_position_quantity"] == runner_module.settings.hard_max_position_quantity
        assert live_safety["max_position_notional"] == runner_module.settings.hard_max_position_notional
        assert live_safety["max_risk_per_trade"] == runner_module.settings.hard_max_risk_per_trade
        assert live_safety["stop_loss_pct"] == runner_module.settings.hard_stop_loss_pct
        assert live_safety["full_buying_power_usage_enabled"] is False

    def test_full_buying_power_mode_is_visible_and_disables_entry_limit_breaches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            runner_module.settings,
            "full_buying_power_usage_enabled",
            True,
        )
        runner = AppRunner()
        config = SimpleNamespace(
            margin_safety_factor=0.35,
            allow_position_addons=False,
            max_position_quantity=100,
            max_position_notional=5000,
            max_risk_per_trade=250,
            stop_loss_pct=1,
            entry_cutoff_minutes_before_close=45,
            llm_order_execution_enabled=False,
        )
        runner._configure_live_safety(config)
        runner.engine.params.symbol = "NVDA.US"
        runner.engine.last_price = Decimal("209.62")
        runner._trade_svc.load_tracked_entries(
            {"NVDA.US": (Decimal("1237"), Decimal("259282.94"))}
        )

        diagnostics = runner.diagnostics()

        assert runner._trade_svc.full_buying_power_usage_enabled is True
        assert (
            diagnostics["live_safety"]["full_buying_power_usage_enabled"]
            is True
        )
        assert diagnostics["live_safety"]["buying_power_usage_pct"] == 100.0
        assert diagnostics["symbol_runtimes"][0]["position_limit_breaches"] == []

    @pytest.mark.parametrize("full_buying_power_usage", [False, True])
    def test_execution_ledger_distinguishes_sizing_caps_from_stop_loss(
        self,
        full_buying_power_usage: bool,
    ) -> None:
        runner = AppRunner()
        runner._trade_svc.full_buying_power_usage_enabled = (
            full_buying_power_usage
        )

        context = runner._execution_ledger_context(
            runner_module._QuoteTriggerDecision(),
            Quote(
                "NVDA.US",
                209.62,
                209.61,
                209.63,
                _fresh_timestamp(),
            ),
            "test",
        )
        snapshot = json.loads(cast(str, context["config_snapshot"]))

        assert (
            snapshot["hard_limits"]["sizing_caps_enforced_for_entry"]
            is not full_buying_power_usage
        )
        assert (
            snapshot["hard_limits"]["stop_loss_required_for_entry"]
            is True
        )
        assert snapshot["buying_power_usage_mode"] == (
            "FULL_BUYING_POWER" if full_buying_power_usage else "GUARDED"
        )

    def test_execute_llm_order_decision_submits_buy_now(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 222.0, 221.9, 222.1, _fresh_timestamp()) for s in symbols]

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 221.75,
            "confidence_score": 0.9,
            "order_reason": "strong signal",
        })

        assert {key: result[key] for key in ("executed", "status", "order_id", "action")} == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-llm-buy",
            "action": "BUY",
        }
        assert result["policy_disposition"] == "ALLOW"
        assert runner.broker.submitted == [("NVDA.US", "BUY", Decimal("9"), Decimal("221.75"))]
        assert runner.engine.state == EngineState.LONG

    def test_execute_llm_order_decision_cancel_replace(self) -> None:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled = []
                self.submitted = []
                self.filled = False

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 225.0, 225.0-0.1, 225.0+0.1, _fresh_timestamp()) for s in symbols]

            def get_positions(self) -> list[Position]:
                if self.filled:
                    return []
                return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("220"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                self.filled = True
                return OrderResult("order-llm-sell", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._final_order_quote_check = runner._validate_final_order_quote
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-pending", "NVDA.US", "BUY", Decimal("5"), Decimal("221"), "SUBMITTED"),
            broker,
            runner.engine.snapshot(),
        )

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "SELL_NOW",
            "replacement_price": 225.0,
            "confidence_score": 0.9,
            "order_reason": "replace stale buy with exit",
        })

        assert {key: result[key] for key in ("executed", "status", "order_id", "action")} == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-llm-sell",
            "action": "SELL",
        }
        assert result["policy_disposition"] == "ALLOW"
        assert broker.cancelled == ["order-pending"]
        assert broker.submitted == [("NVDA.US", "SELL", Decimal("5"), Decimal("224.90"))]
        assert runner._trade_svc.has_pending_order is False
        assert runner.engine.state == EngineState.FLAT

    def test_execute_llm_order_decision_replaces_pending_order_for_new_action(self) -> None:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled = []
                self.submitted = []

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 222.0, 222.0-0.1, 222.0+0.1, _fresh_timestamp()) for s in symbols]

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("12")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-llm-new-buy", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.engine.state = EngineState.LONG
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-old-buy", "NVDA.US", "BUY", Decimal("10"), Decimal("221.0"), "SUBMITTED"),
            broker,
            EngineSnapshot(state=EngineState.FLAT, last_trigger_price=0.0, last_trigger_at=None),
        )

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 221.88,
            "confidence_score": 0.9,
            "order_reason": "US price moved, refresh the resting order",
        })

        assert {key: result[key] for key in (
            "executed", "status", "order_id", "action", "replaced_order_id"
        )} == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-llm-new-buy",
            "action": "BUY",
            "replaced_order_id": "order-old-buy",
        }
        assert result["policy_disposition"] == "ALLOW"
        assert broker.cancelled == ["order-old-buy"]
        assert broker.submitted == [("NVDA.US", "BUY", Decimal("10"), Decimal("221.88"))]
        assert runner._trade_svc.has_pending_order is False
        assert runner.engine.state == EngineState.LONG

    def test_execute_llm_order_decision_stop_loss_sell_bypasses_profit_guard(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []
                self.filled = False

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 215.0, 215.0-0.1, 215.0+0.1, _fresh_timestamp()) for s in symbols]

            def get_positions(self) -> list[Position]:
                if self.filled:
                    return []
                return [Position("NVDA.US", "LONG", Decimal("8"), Decimal("220"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                self.filled = True
                return OrderResult("order-stop-loss", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225, min_profit_amount=50)
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner._llm_order_execution_enabled = True
        monkeypatch.setattr(
            runner,
            "_entry_reference_quantity_for_exit",
            lambda *_args: pytest.fail(
                "stop-loss execution must not query entry reference history"
            ),
        )

        result = runner.execute_llm_order_decision({
            "order_action": "STOP_LOSS_SELL_NOW",
            "order_price": 215.0,
            "confidence_score": 0.9,
            "order_reason": "支撑失效并开始崩盘",
        })

        assert {key: result[key] for key in ("executed", "status", "order_id", "action")} == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-stop-loss",
            "action": "SELL",
        }
        assert result["policy_disposition"] == "ALLOW"
        assert runner.broker.submitted == [("NVDA.US", "SELL", Decimal("8"), Decimal("214.90"))]
        assert runner.engine.state == EngineState.FLAT
        assert runner.last_action_message == "LLM SELL FILLED: order-stop-loss"

    def test_execute_llm_stop_loss_sell_allowed_while_paused(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []
                self.filled = False

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 193.0, 192.9, 193.1, _fresh_timestamp()) for s in symbols]

            def get_positions(self) -> list[Position]:
                if self.filled:
                    return []
                return [Position("NVDA.US", "LONG", Decimal("10"), Decimal("197.74"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                self.filled = True
                return OrderResult("order-stop-loss-paused", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=196, sell_high=199)
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        runner.risk.pause("pending order order-entry timed out after 30s")
        self._stub_trade_callbacks(runner)
        runner._trade_svc._final_order_quote_check = runner._validate_final_order_quote

        result = runner.execute_llm_order_decision({
            "order_action": "STOP_LOSS_SELL_NOW",
            "order_price": 193.0,
            "confidence_score": 0.9,
            "order_reason": "跌破支撑，先减风险",
        })

        assert {key: result[key] for key in ("executed", "status", "order_id", "action")} == {
            "executed": True,
            "status": "FILLED",
            "order_id": "order-stop-loss-paused",
            "action": "SELL",
        }
        assert result["policy_disposition"] == "ALLOW"
        assert runner.broker.submitted == [("NVDA.US", "SELL", Decimal("10"), Decimal("192.90"))]
        assert runner.engine.state == EngineState.FLAT

    def _runner_with_pending_buy(self, price: Decimal) -> AppRunner:
        from app.core.broker import OrderStatusResult

        class Broker:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                self.cancelled.append(order_id)
                return OrderStatusResult(order_id, "CANCELLED")

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(symbol, float(price), float(price) - 0.1, float(price) + 0.1, _fresh_timestamp()) for symbol in symbols]

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-pending", "NVDA.US", "BUY", Decimal("5"), price, "SUBMITTED"),
            runner.broker,
            runner.engine.snapshot(),
        )
        return runner

    def test_llm_cancel_replace_below_repricing_threshold_preserves_pending(self) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))
        runner.engine.params.min_repricing_pct = 0.003
        skipped: list[tuple[Any, ...]] = []
        runner._record_order_skipped = lambda *args: skipped.append(args)

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "BUY_NOW",
            "replacement_price": 221.20,
            "confidence_score": 0.9,
        })

        assert result["status"] == "SKIPPED"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True
        assert skipped[0][3]["skip_category"] == "REPRICING"

    def test_llm_cooldown_rejection_preserves_pending_before_cancel(self, monkeypatch) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))
        runner.engine.params.llm_action_cooldown_seconds = 60
        runner._last_llm_action_at[("NVDA.US", "BUY")] = 100.0
        runner._record_order_skipped = lambda *args: None
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 120.0)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 222.00,
            "confidence_score": 0.9,
        })

        assert result["status"] == "SKIPPED"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True

    def test_successful_llm_submission_records_broker_side_cooldown(self, monkeypatch) -> None:
        class Broker:
            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 222.0, 222.0-0.1, 222.0+0.1, _fresh_timestamp()) for s in symbols]

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult("order-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 100.0)
        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 221.75,
            "confidence_score": 0.9,
        })
        assert result["executed"] is True
        assert runner._last_llm_action_at[("NVDA.US", "BUY")] == 100.0

    def test_llm_order_decision_targets_secondary_symbol_runtime(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 199.0, 199.0-0.1, 199.0+0.1, _fresh_timestamp()) for s in symbols]

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                return OrderResult("order-aapl-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=218, sell_high=225)
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        secondary = runner._build_symbol_runtime("AAPL.US", "US")
        secondary.engine.params.buy_low = 190
        secondary.engine.params.sell_high = 210
        runner._symbol_runtimes = {"AAPL.US": secondary}
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 100.0)

        result = runner.execute_llm_order_decision({
            "symbol": "AAPL.US",
            "order_action": "BUY_NOW",
            "order_price": 199.0,
            "confidence_score": 0.9,
        })

        assert result["status"] == "WATCHLIST_READ_ONLY"
        assert runner.broker.submitted == []
        assert runner.engine.state == EngineState.FLAT
        assert secondary.engine.state == EngineState.FLAT
        assert ("AAPL.US", "BUY") not in runner._last_llm_action_at

    def test_llm_cooldown_is_scoped_to_decision_symbol(self, monkeypatch) -> None:
        class Broker:
            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(s, 199.0, 199.0-0.1, 199.0+0.1, _fresh_timestamp()) for s in symbols]

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult("order-aapl-llm-buy", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=218,
            sell_high=225,
            llm_action_cooldown_seconds=60,
        )
        runner._last_llm_action_at[("NVDA.US", "BUY")] = 100.0
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        secondary = runner._build_symbol_runtime("AAPL.US", "US")
        secondary.engine.params.llm_action_cooldown_seconds = 60
        runner._symbol_runtimes = {"AAPL.US": secondary}
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 120.0)

        result = runner.execute_llm_order_decision({
            "symbol": "AAPL.US",
            "order_action": "BUY_NOW",
            "order_price": 199.0,
            "confidence_score": 0.9,
        })

        assert result["status"] == "WATCHLIST_READ_ONLY"
        assert ("AAPL.US", "BUY") not in runner._last_llm_action_at

    def test_llm_symbol_statuses_report_pending_and_cooldowns(self, monkeypatch) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=218,
            sell_high=225,
            llm_action_cooldown_seconds=60,
        )
        primary = runner._build_symbol_runtime("NVDA.US", "US", primary=True)
        secondary = runner._build_symbol_runtime("AAPL.US", "US")
        secondary.engine.params.llm_action_cooldown_seconds = 120
        runner._symbol_runtimes = {
            "NVDA.US": primary,
            "AAPL.US": secondary,
        }
        runner._trade_svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("3"), Decimal("199"), "SUBMITTED"),
            runner.broker,
            None,
        )
        runner._last_llm_action_at[("NVDA.US", "BUY")] = 100.0
        runner._last_llm_action_at[("AAPL.US", "SELL")] = 90.0
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 130.0)

        statuses = runner.llm_symbol_statuses()

        by_symbol = {item["symbol"]: item for item in statuses}
        assert by_symbol["NVDA.US"]["is_primary"] is True
        assert by_symbol["NVDA.US"]["buy_cooldown_remaining_seconds"] == 30.0
        assert by_symbol["NVDA.US"]["sell_cooldown_remaining_seconds"] is None
        assert by_symbol["AAPL.US"]["is_primary"] is False
        assert by_symbol["AAPL.US"]["has_pending_order"] is True
        assert by_symbol["AAPL.US"]["buy_cooldown_remaining_seconds"] is None
        assert by_symbol["AAPL.US"]["sell_cooldown_remaining_seconds"] == 80.0

    def test_llm_cancel_replace_without_valid_price_preserves_pending(self) -> None:
        runner = self._runner_with_pending_buy(price=Decimal("221.00"))

        result = runner.execute_llm_order_decision({
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "BUY_NOW",
            "replacement_price": None,
            "confidence_score": 0.9,
        })

        assert result["status"] == "POLICY_REJECTED"
        assert result["policy_code"] == "INVALID_ORDER_PRICE"
        assert runner.broker.cancelled == []
        assert runner._trade_svc.has_pending_order is True

    def test_get_runner_singleton(self) -> None:
        r1 = get_runner()
        r2 = get_runner()
        assert r1 is r2

    def test_runner_stop_when_not_running(self) -> None:
        runner = AppRunner()
        runner.stop()
        assert runner._running is False

    def test_runner_start_stop_cycle(self) -> None:
        runner = AppRunner()
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        assert runner._running is True
        runner.stop()
        assert runner._running is False

    def test_runner_double_start(self) -> None:
        runner = AppRunner()
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        first_thread = runner._thread
        with patch.object(runner, '_initialize_runner'):
            runner.start()
        assert runner._thread is first_thread
        runner.stop()

    def test_runner_refuses_restart_while_previous_thread_is_alive(self) -> None:
        class HungThread:
            def __init__(self) -> None:
                self.join_calls: list[float] = []

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float) -> None:
                self.join_calls.append(timeout)

        runner = AppRunner()
        hung_thread = HungThread()
        runner._thread = hung_thread

        with patch.object(runner, "_initialize_runner") as initialize:
            started = runner.start()

        assert started is False
        assert runner._running is False
        assert runner._thread is hung_thread
        assert hung_thread.join_calls == [10]
        initialize.assert_not_called()

    def test_initialize_runner_preserves_existing_loop_when_no_running_loop(self) -> None:
        runner = AppRunner()
        existing_loop = asyncio.new_event_loop()
        runner._loop = existing_loop

        class FakeService:
            def __init__(self, _db) -> None:
                pass

            def get_config(self):
                class Config:
                    symbol = ""
                    market = "US"
                    buy_low = 0.0
                    sell_high = 0.0
                    short_selling = False
                    max_daily_loss = 5000.0
                    max_consecutive_losses = 3

                return Config()

            def get_runtime_state(self):
                class State:
                    engine_state = "flat"
                    last_price = 0.0
                    last_trigger_price = 0.0
                    last_trigger_at = None
                    daily_pnl = 0.0
                    consecutive_losses = 0
                    kill_switch = False
                    paused = False

                return State()

        class FakeDb:
            def query(self, _model):
                class Query:
                    def filter(self, *_args):
                        return self

                    def order_by(self, *_args):
                        return self

                    def first(self):
                        return None

                    def all(self):
                        return []

                return Query()

            def close(self) -> None:
                pass

        with (
            patch("app.runner.SessionLocal", lambda: FakeDb()),
            patch.object(runner._state_svc, "load") as load_state,
            patch.object(runner, "_load_credentials") as load_credentials,
            patch.object(runner, "_apply_credentials") as apply_credentials,
        ):
            load_credentials.return_value = object()
            runner._initialize_runner()

        assert runner._loop is existing_loop
        existing_loop.close()

    def test_initialize_runner_pauses_when_unresolved_live_order_exists(self) -> None:
        runner = AppRunner()

        class FakeService:
            def __init__(self, _db) -> None:
                pass

            def get_config(self):
                class Config:
                    symbol = "AAPL.US"
                    market = "US"
                    buy_low = 100.0
                    sell_high = 200.0
                    short_selling = False
                    max_daily_loss = 5000.0
                    max_consecutive_losses = 3

                return Config()

            def get_runtime_state(self):
                class State:
                    engine_state = "flat"
                    last_price = 0.0
                    last_trigger_price = 0.0
                    last_trigger_at = None
                    daily_pnl = 0.0
                    consecutive_losses = 0
                    kill_switch = False
                    paused = False

                return State()

        class FakeQuery:
            def filter(self, *_args):
                return self

            def order_by(self, *_args):
                return self

            def first(self):
                return SimpleNamespace(broker_order_id="order-live", status="SUBMITTED")

            def all(self):
                return [
                    SimpleNamespace(
                        id=1,
                        broker_order_id="order-live",
                        symbol="AAPL.US",
                        side="BUY",
                        quantity=1.0,
                        price=100.0,
                        status="SUBMITTED",
                        created_at=datetime.now(timezone.utc),
                    )
                ]

        class FakeDb:
            def query(self, _model):
                return FakeQuery()

            def close(self) -> None:
                pass

        with (
            patch("app.runner.SessionLocal", lambda: FakeDb()),
            patch.object(runner._state_svc, "load") as load_state,
            patch.object(runner, "_load_credentials") as load_credentials,
            patch.object(runner, "_apply_credentials") as apply_credentials,
        ):
            load_credentials.return_value = object()
            load_state.side_effect = lambda _db, engine, _risk: setattr(
                engine,
                "params",
                StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0),
            )
            runner._initialize_runner()

        assert runner.risk.paused is True

    def test_load_tracked_entries_restores_in_memory_state(self) -> None:
        runner = AppRunner()

        loaded_rows = [
            SimpleNamespace(symbol="AAPL.US", quantity=10.0, cost=1500.0),
            SimpleNamespace(symbol="0700.HK", quantity=0.0, cost=0.0),  # skipped
        ]

        class FakeQuery:
            def all(self) -> list[Any]:
                return loaded_rows

        class FakeDb:
            def query(self, _model: object) -> FakeQuery:
                return FakeQuery()

        runner._load_tracked_entries(FakeDb())

        snapshot = runner._trade_svc.snapshot_tracked_entries()
        assert "AAPL.US" in snapshot
        assert snapshot["AAPL.US"] == (Decimal("10.0"), Decimal("1500.0"))
        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.side == ""
        assert "0700.HK" not in snapshot

    def test_reconcile_tracked_entries_records_drift_event(self) -> None:
        from app import database
        from app.models import TrackedEntry

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._trade_svc.load_tracked_entries({"AAPL.US": (Decimal("100"), Decimal("15000"))})

        class Broker:
            def get_positions(self):
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("80"), avg_price=Decimal("150"))]

        runner.broker = Broker()

        events: list[dict[str, Any]] = []

        def record_event(_db, **kwargs):
            events.append(kwargs)

        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").delete()
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=100.0,
                    cost=15000.0,
                )
            )
            db.commit()
            with patch("app.runner.record_trade_event", side_effect=record_event):
                runner._reconcile_tracked_entries_with_broker(db)

        assert events, "drift event should be recorded"
        assert events[0]["event_type"] == "TRACKED_ENTRY_DRIFT"
        assert events[0]["payload"]["tracked_quantity"] == 100.0
        assert events[0]["payload"]["broker_quantity"] == 80.0
        assert runner._trade_svc.snapshot_tracked_entries()["AAPL.US"] == (
            Decimal("80.0"),
            Decimal("12000.0"),
        )

    def test_reconcile_tracked_entries_skips_when_within_tolerance(self) -> None:
        from app import database
        from app.models import TrackedEntry

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._trade_svc.load_tracked_entries({"AAPL.US": (Decimal("100"), Decimal("15000"))})

        class Broker:
            def get_positions(self):
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("100"), avg_price=Decimal("150"))]

        runner.broker = Broker()

        events: list[dict[str, Any]] = []

        def record_event(_db, **kwargs):
            events.append(kwargs)

        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").delete()
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=100.0,
                    cost=15000.0,
                )
            )
            db.commit()
            with patch("app.runner.record_trade_event", side_effect=record_event):
                runner._reconcile_tracked_entries_with_broker(db)

        assert events == []

    def test_runtime_reconcile_pauses_on_non_primary_broker_exposure(self) -> None:
        from app.models import TrackedEntry

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")

        class Broker:
            calls = 0

            def get_positions(self):
                self.calls += 1
                return [
                    Position(
                        symbol="NVDA.US",
                        side="LONG",
                        quantity=Decimal("2"),
                        avg_price=Decimal("200"),
                    )
                ]

        runner.broker = Broker()
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "NVDA.US").delete()
            db.commit()

        try:
            assert runner._reconcile_runtime_positions() is True
            assert runner.risk.paused is True
            assert runner.risk.pause_reason.startswith(
                "POSITION_RECONCILIATION_UNCERTAIN:"
            )
            tracked = runner._trade_svc.tracked_position("NVDA.US")
            assert tracked is not None
            assert tracked.quantity == Decimal("2.0")
            assert runner.broker.calls == 1
        finally:
            with database.SessionLocal() as db:
                db.query(TrackedEntry).filter(TrackedEntry.symbol == "NVDA.US").delete()
                db.commit()

    def test_reconcile_preserves_fill_cost_when_broker_average_is_stale(self) -> None:
        from app.models import TrackedEntry

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._trade_svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("10"),
                Decimal("1500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })

        class Broker:
            calls = 0

            def get_positions(self) -> list[Position]:
                self.calls += 1
                broker_avg = (
                    Decimal("160")
                    if self.calls == 1
                    else Decimal("160.0000004")
                )
                return [Position("AAPL.US", "LONG", Decimal("10"), broker_avg)]

        runner.broker = Broker()
        events: list[dict[str, Any]] = []
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").delete()
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=10,
                    cost=1500,
                )
            )
            db.commit()
            with patch(
                "app.runner.record_trade_event",
                side_effect=lambda _db, **kwargs: events.append(kwargs),
            ):
                runner._reconcile_tracked_entries_with_broker(db)
                runner._reconcile_tracked_entries_with_broker(db)
            row = db.query(TrackedEntry).filter(
                TrackedEntry.symbol == "AAPL.US"
            ).one()
            assert row.quantity == 10
            assert row.cost == 1500

        assert runner._trade_svc.snapshot_tracked_entries()["AAPL.US"] == (
            Decimal("10.0"),
            Decimal("1500.0"),
        )
        assert events[-1]["event_type"] == "TRACKED_ENTRY_DRIFT"
        assert events[-1]["payload"]["repaired"] is False
        assert events[-1]["payload"]["preserved"] is True
        assert events[-1]["payload"]["cost_authority"] == "DURABLE_TRACKED_ENTRY"
        assert len(events) == 1

    def test_reconcile_uses_broker_average_when_inventory_grows(self) -> None:
        from app.models import TrackedEntry

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner._trade_svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("10"),
                Decimal("1500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position("AAPL.US", "LONG", Decimal("12"), Decimal("155"))]

        runner.broker = Broker()
        events: list[dict[str, Any]] = []
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").delete()
            db.add(TrackedEntry(
                symbol="AAPL.US",
                side="LONG",
                quantity=10,
                cost=1500,
            ))
            db.commit()
            with patch(
                "app.runner.record_trade_event",
                side_effect=lambda _db, **kwargs: events.append(kwargs),
            ):
                runner._reconcile_tracked_entries_with_broker(db)

        assert runner._trade_svc.snapshot_tracked_entries()["AAPL.US"] == (
            Decimal("12.0"),
            Decimal("1860.0"),
        )
        assert events[-1]["payload"]["repaired"] is True
        assert events[-1]["payload"]["preserved"] is False
        assert events[-1]["payload"]["cost_authority"] == "BROKER_POSITION"

    def test_reconcile_removes_flat_entry_and_clears_confirmed_drift(self) -> None:
        from app.models import TrackedEntry

        symbol = "FLATRECON.US"
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner._trade_svc.load_tracked_entries({
            symbol: (
                Decimal("10"),
                Decimal("1500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })
        runner._tracked_avg_drift_warning_keys[symbol] = ("stale",)

        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner.broker = Broker()
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).delete()
            db.add(
                TrackedEntry(
                    symbol=symbol,
                    side="LONG",
                    quantity=10,
                    cost=1500,
                )
            )
            db.commit()
            with patch("app.runner.record_trade_event"):
                runner._reconcile_tracked_entries_with_broker(db)
            assert (
                db.query(TrackedEntry)
                .filter(TrackedEntry.symbol == symbol)
                .first()
                is None
            )

        assert symbol not in runner._trade_svc.snapshot_tracked_entries()
        assert symbol not in runner._tracked_avg_drift_warning_keys

    def test_reconcile_retries_avg_drift_warning_after_commit_failure(self) -> None:
        from app.models import TrackedEntry

        symbol = "DRIFTRETRY.US"
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner._trade_svc.load_tracked_entries({
            symbol: (
                Decimal("10"),
                Decimal("1500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol, "LONG", Decimal("10"), Decimal("160"))]

        runner.broker = Broker()
        events: list[dict[str, Any]] = []
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).delete()
            db.add(
                TrackedEntry(
                    symbol=symbol,
                    side="LONG",
                    quantity=10,
                    cost=1500,
                )
            )
            db.commit()
            with patch(
                "app.runner.record_trade_event",
                side_effect=lambda _db, **kwargs: events.append(kwargs),
            ):
                with (
                    patch.object(
                        db,
                        "commit",
                        side_effect=RuntimeError("commit failed"),
                    ),
                    pytest.raises(RuntimeError, match="commit failed"),
                ):
                    runner._reconcile_tracked_entries_with_broker(db)
                assert symbol not in runner._tracked_avg_drift_warning_keys
                db.rollback()

                runner._reconcile_tracked_entries_with_broker(db)
                assert symbol in runner._tracked_avg_drift_warning_keys
                runner._reconcile_tracked_entries_with_broker(db)

            db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).delete()
            db.commit()

        drift_events = [
            event
            for event in events
            if event["event_type"] == "TRACKED_ENTRY_DRIFT"
            and event["symbol"] == symbol
        ]
        assert len(drift_events) == 2

    def test_reconcile_preserves_cost_when_quantity_grows_without_broker_avg(
        self,
    ) -> None:
        from app.models import TrackedEntry

        symbol = "COSTMISS.US"
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner._trade_svc.load_tracked_entries(
            {
                symbol: (
                    Decimal("10"),
                    Decimal("1500"),
                    "LONG",
                    opened_at,
                )
            }
        )

        class Broker:
            def get_positions(self) -> list[Position]:
                return [
                    Position(symbol, "LONG", Decimal("12"), Decimal("0"))
                ]

        runner.broker = Broker()
        events: list[dict[str, Any]] = []
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).delete()
            db.add(
                TrackedEntry(
                    symbol=symbol,
                    side="LONG",
                    quantity=10,
                    cost=1500,
                    opened_at=opened_at,
                )
            )
            db.commit()
            with patch(
                "app.runner.record_trade_event",
                side_effect=lambda _db, **kwargs: events.append(kwargs),
            ):
                runner._reconcile_tracked_entries_with_broker(
                    db,
                    source="test_missing_added_cost",
                )
            row = db.query(TrackedEntry).filter(
                TrackedEntry.symbol == symbol
            ).first()
            assert row is not None
            assert row.quantity == 10
            assert row.cost == 1500
            db.delete(row)
            db.commit()

        tracked = runner._trade_svc.tracked_position(symbol)
        assert tracked is not None
        assert tracked.quantity == Decimal("10.0")
        assert tracked.cost == Decimal("1500.0")
        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is False
        assert "added position cost is unavailable" in runner.risk.pause_reason
        assert events[-1]["event_type"] == "TRACKED_ENTRY_RECOVERY_FAILED"
        assert events[-1]["payload"]["preserved"] is True

    def test_reconcile_pauses_for_simultaneous_long_and_short_positions(self) -> None:
        from app.models import TrackedEntry

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100, sell_high=200)
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        runner._trade_svc.load_tracked_entries({
            "AAPL.US": (Decimal("10"), Decimal("1500"), "LONG", opened_at)
        })

        class Broker:
            def get_positions(self) -> list[Position]:
                return [
                    Position("AAPL.US", "LONG", Decimal("10"), Decimal("150")),
                    Position("AAPL.US", "SHORT", Decimal("2"), Decimal("151")),
                ]

        runner.broker = Broker()
        with database.SessionLocal() as db:
            db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").delete()
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=10,
                    cost=1500,
                    opened_at=opened_at,
                )
            )
            db.commit()
            runner._reconcile_tracked_entries_with_broker(db)

        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is False
        assert "both long and short" in runner.risk.pause_reason

    @pytest.mark.parametrize(
        "missing_kind",
        ["flat_local_state", "engine_position", "invalid_tracked"],
    )
    def test_reconcile_broker_failure_pauses_when_exit_baseline_is_missing(
        self,
        missing_kind: str,
    ) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100, sell_high=200)
        if missing_kind == "engine_position":
            runner.engine.state = EngineState.LONG
        elif missing_kind == "invalid_tracked":
            runner._trade_svc.load_tracked_entries({
                "AAPL.US": (Decimal("10"), Decimal("1500"), "UNKNOWN", None)
            })

        class Broker:
            def get_positions(self) -> list[Position]:
                raise RuntimeError("position endpoint unavailable")

        class FakeDb:
            def commit(self) -> None:
                pass

        runner.broker = Broker()
        with (
            patch("app.runner.record_trade_event"),
            patch.object(runner, "_latest_filled_orders_by_symbol", return_value={}),
        ):
            runner._reconcile_tracked_entries_with_broker(FakeDb())

        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is False
        assert "unprotected local state" in runner.risk.pause_reason

    def test_persist_tracked_entry_writes_then_deletes(self) -> None:
        from app.database import SessionLocal
        from app.models import TrackedEntry

        with SessionLocal() as db:
            db.query(TrackedEntry).delete()
            db.commit()

        runner = AppRunner()
        runner._persist_tracked_entry("AAPL.US", Decimal("10"), Decimal("1500"))

        db = SessionLocal()
        try:
            row = db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").first()
            assert row is not None
            assert row.quantity == 10.0
            assert row.cost == 1500.0
        finally:
            db.close()

        runner._persist_tracked_entry("AAPL.US", Decimal("0"), Decimal("0"))

        db = SessionLocal()
        try:
            assert db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").first() is None
        finally:
            db.close()

    def test_resubscribe_quotes_fires_after_silence_threshold(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.unsubscribed = False
                self.subscribed_to: str | None = None

            def unsubscribe_quotes(self) -> None:
                self.unsubscribed = True

            def subscribe_quotes(self, symbol, callback) -> None:
                self.subscribed_to = symbol

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._quotes_subscribed = True
        runner.broker = Broker()

        monkeypatch.setattr(runner_module, "is_trading_hours", lambda _market: True, raising=False)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 1000.0)
        runner._last_push_quote_at = 850.0  # 150s ago, beyond 90s threshold

        result = runner._resubscribe_quotes_if_silent()

        assert result is True
        assert runner.broker.unsubscribed is True
        assert runner.broker.subscribed_to == "AAPL.US"
        assert runner._last_push_quote_at == 1000.0  # bumped by resubscribe

    def test_resubscribe_quotes_noops_when_quote_is_recent(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.unsubscribed = False

            def unsubscribe_quotes(self) -> None:
                self.unsubscribed = True

            def subscribe_quotes(self, *_args) -> None:
                pass

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        runner._quotes_subscribed = True
        runner.broker = Broker()

        monkeypatch.setattr(runner_module, "is_trading_hours", lambda _market: True, raising=False)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 1000.0)
        runner._last_push_quote_at = 990.0  # 10s ago - fresh

        assert runner._resubscribe_quotes_if_silent() is False
        assert runner.broker.unsubscribed is False

    def test_active_refresh_does_not_mask_silent_push_subscription(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.unsubscribed = False
                self.subscribed_to: str | None = None

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(symbol=s, last_price=123.45, bid=123.4, ask=123.5, timestamp=_fresh_timestamp()) for s in symbols]

            def unsubscribe_quotes(self) -> None:
                self.unsubscribed = True

            def subscribe_quotes(self, symbol, _callback) -> None:
                self.subscribed_to = symbol

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner._quotes_subscribed = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner._active_quote_refresh_interval_seconds = 0.0

        monkeypatch.setattr(runner_module, "is_trading_hours", lambda _market: True, raising=False)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 1000.0)
        runner._last_push_quote_at = 850.0
        runner._last_quote_at = 850.0

        runner._refresh_quote_if_stale()
        assert runner._last_quote_at == 1000.0
        assert runner._last_push_quote_at == 850.0

        assert runner._resubscribe_quotes_if_silent() is True
        assert runner.broker.unsubscribed is True
        assert runner.broker.subscribed_to == "AAPL.US"

    def test_resubscribe_quotes_does_not_run_outside_trading_hours(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.unsubscribed = False
                self.subscribed = False

            def unsubscribe_quotes(self) -> None:
                self.unsubscribed = True

            def subscribe_quotes(self, _symbol, _callback) -> None:
                self.subscribed = True

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner._quotes_subscribed = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=200.0)
        runner._last_push_quote_at = 850.0

        monkeypatch.setattr(runner_module, "is_trading_hours", lambda _market: False, raising=False)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 1000.0)

        assert runner._resubscribe_quotes_if_silent() is False
        assert runner.broker.unsubscribed is False
        assert runner.broker.subscribed is False

    def test_broadcast_status_no_connections(self) -> None:
        runner = AppRunner()
        runner._broadcast_status()

    def test_broadcast_status_includes_runner_running(self, monkeypatch) -> None:
        messages = []

        async def broadcast(message):
            messages.append(message)

        def run_coroutine_threadsafe(coro, _loop):
            asyncio.run(coro)
            return None

        class RunningLoop:
            def is_running(self) -> bool:
                return True

        monkeypatch.setattr(runner_module.manager, "broadcast", broadcast)
        monkeypatch.setattr(runner_module.asyncio, "run_coroutine_threadsafe", run_coroutine_threadsafe)
        runner = AppRunner()
        runner._running = True
        runner._thread = SimpleNamespace(is_alive=lambda: True)
        runner._loop = RunningLoop()
        runner.risk.restore_drawdown_state(
            cumulative_realized_pnl=80.0,
            peak_realized_pnl=125.0,
        )

        runner._broadcast_status()

        assert messages[0]["runner_running"] is True
        assert messages[0]["risks"]["cumulative_realized_pnl"] == 80.0
        assert messages[0]["risks"]["peak_realized_pnl"] == 125.0
        assert messages[0]["risks"]["drawdown_amount"] == 45.0

    def test_active_quote_refresh_fetches_quote_when_push_is_stale(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                self.calls.append(list(symbols))
                return [Quote(symbol=s, last_price=123.45, bid=123.4, ask=123.5, timestamp=_fresh_timestamp()) for s in symbols]

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner._active_quote_refresh_interval_seconds = 0.0

        runner._refresh_quote_if_stale()

        assert broker.calls == [["AAPL.US"]]
        assert runner.engine.last_price == 123.45
        assert runner._last_quote_at > 0

    def test_secondary_quote_does_not_mask_stale_primary_quote(
        self,
        monkeypatch,
    ) -> None:
        class Broker:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                self.calls.append(list(symbols))
                return [
                    Quote(
                        symbol=symbol,
                        last_price=123.45,
                        bid=123.4,
                        ask=123.5,
                        timestamp=_fresh_timestamp(),
                    )
                    for symbol in symbols
                ]

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )
        runner._symbol_runtimes = {
            "NVDA.US": runner._build_symbol_runtime("NVDA.US", "US", primary=True),
            "AAPL.US": runner._build_symbol_runtime("AAPL.US", "US"),
        }
        runner._active_quote_refresh_interval_seconds = 15.0
        runner._last_quote_at = 900.0
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 1000.0)

        runner._remember_quote(
            Quote(
                symbol="AAPL.US",
                last_price=210.0,
                bid=209.9,
                ask=210.1,
                timestamp=_fresh_timestamp(),
            )
        )
        runner._refresh_quote_if_stale()

        assert broker.calls == [["NVDA.US"]]
        assert runner._last_quote_at == 1000.0

    def test_active_quote_refresh_skips_when_push_is_fresh(self) -> None:
        class Broker:
            def get_quote(self, _symbol: str) -> Quote:
                raise AssertionError("fresh quote should not trigger active refresh")

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner._active_quote_refresh_interval_seconds = 60.0
        runner._last_quote_at = time.monotonic()

        runner._refresh_quote_if_stale()

    def test_risk_rejection_rolls_back_triggered_engine_state(self) -> None:
        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.risk.pause("testing")
        runner.notifier = _NoopNotifier()
        runner._record_risk_event = lambda reason: None

        runner._on_quote(Quote(symbol="AAPL.US", last_price=99.0, bid=98.5, ask=99.5, timestamp=_fresh_timestamp()))

        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_trigger_at is None
        assert runner.engine.last_trigger_price == 0.0

    def test_on_quote_submits_add_on_buy_when_long_below_buy_low(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions: list[tuple[str, Decimal]] = []

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions.append((side, quantity))
                return OrderResult(f"order-{len(self.submissions)}", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.engine.last_trigger_at = None
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()))

        # P13: LONG + price <= buy_low now triggers add-on BUY
        assert len(broker.submissions) == 1
        assert broker.submissions[0][0] == "BUY"
        assert runner.engine.state == EngineState.LONG
        assert runner.engine.last_price == 99.0

    def test_submit_exception_pauses_runner_instead_of_immediate_retry(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                raise RuntimeError("broker rejected invalid price")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp())
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.risk.paused is True
        assert runner.engine.state == EngineState.FLAT

    def test_rate_limit_submit_exception_latches_uncertain_order_pause(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                raise RuntimeError("429 too many requests")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()))

        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is False
        assert runner.risk.pause_reason.startswith("ORDER_SUBMISSION_UNCERTAIN:")
        assert "429" in runner.risk.pause_reason

    def test_auto_resume_transient_pause_after_configured_delay(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        paused_at = now - timedelta(minutes=3, seconds=1)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=3,
        )
        runner.risk.pause("429 too many requests", auto_resumable=True, paused_at=paused_at)

        resumed = runner._auto_resume_pause_if_due(now=now)

        assert resumed is True
        assert runner.risk.paused is False

    def test_auto_resumed_drawdown_pause_retriggers_with_retained_peak(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 7, 19, 10, 3, tzinfo=timezone.utc)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=1,
        )
        runner.risk.config.max_drawdown_amount = 25.0
        runner.risk.restore_drawdown_state(
            cumulative_realized_pnl=75.0,
            peak_realized_pnl=100.0,
        )
        runner.risk.pause(
            "DRAWDOWN_LIMIT: drawdown=25.00",
            auto_resumable=True,
            paused_at=now - timedelta(minutes=2),
        )

        assert runner._auto_resume_pause_if_due(now=now) is True
        runner.risk.record_trade(-1.0)

        assert runner.risk.paused is True
        assert runner.risk.peak_realized_pnl == 100.0
        assert runner.risk.consume_drawdown_limit_reason() == runner.risk.pause_reason

    def test_auto_resume_persistence_failure_keeps_pause(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        reason = "429 too many requests"
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=1,
        )
        runner.risk.pause(
            reason,
            auto_resumable=True,
            paused_at=now - timedelta(minutes=2),
        )
        monkeypatch.setattr(
            runner._state_svc,
            "stage",
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("runtime state write failed")
            ),
        )
        monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        resumed = runner._auto_resume_pause_if_due(now=now)

        assert resumed is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == reason
        assert runner.risk.check().approved is False

    def test_auto_resume_waits_for_post_fill_pnl_reconciliation(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=1,
        )
        runner.risk.pause(
            "429 too many requests",
            auto_resumable=True,
            paused_at=now - timedelta(minutes=2),
        )
        runner.risk.begin_entry_reconciliation(
            "post-fill PnL reconciliation in progress: AAPL.US"
        )

        assert runner._auto_resume_pause_if_due(now=now) is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == "429 too many requests"

    def test_auto_resume_rechecks_post_fill_gate_at_resume_point(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=1,
        )
        runner.risk.pause(
            "429 too many requests",
            auto_resumable=True,
            paused_at=now - timedelta(minutes=2),
        )

        real_lock = runner._state_lock
        first_check_released = threading.Event()
        allow_resume_check = threading.Event()

        class CoordinatedLock:
            exits = 0

            def __enter__(self) -> None:
                real_lock.acquire()

            def __exit__(self, *_args: object) -> None:
                real_lock.release()
                self.exits += 1
                if self.exits == 1:
                    first_check_released.set()
                    assert allow_resume_check.wait(timeout=2)

        runner._state_lock = cast(Any, CoordinatedLock())
        results: list[bool] = []
        worker = threading.Thread(
            target=lambda: results.append(runner._auto_resume_pause_if_due(now=now))
        )
        worker.start()
        assert first_check_released.wait(timeout=2)
        with real_lock:
            runner.risk.begin_entry_reconciliation(
                "post-fill PnL reconciliation in progress: AAPL.US"
            )
        allow_resume_check.set()
        worker.join(timeout=2)
        runner._state_lock = real_lock

        assert worker.is_alive() is False
        assert results == [False]
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == "429 too many requests"
        runner.risk.finish_entry_reconciliation()

    def test_auto_resume_does_not_resume_manual_pause(self) -> None:
        runner = AppRunner()
        now = datetime(2026, 5, 22, 10, 3, tzinfo=timezone.utc)
        paused_at = now - timedelta(minutes=10)
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            auto_resume_minutes=3,
        )
        runner.risk.pause("manual", auto_resumable=False, paused_at=paused_at)

        resumed = runner._auto_resume_pause_if_due(now=now)

        assert resumed is False
        assert runner.risk.paused is True

    def test_empty_order_snapshot_pause_waits_for_grace_and_never_uses_blind_resume(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        now = datetime.now(timezone.utc)
        reason = runner_module._EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
        runner.engine.params = StrategyParams(
            symbol="AUTO-GRACE.US",
            market="US",
            auto_resume_minutes=0,
        )
        # Even incorrectly persisted legacy metadata must not route this
        # operational pause through the timer-only resume path.
        runner.risk.pause(
            reason,
            auto_resumable=True,
            paused_at=now - timedelta(seconds=59),
        )
        monkeypatch.setattr(
            runner,
            "resume_after_verification",
            lambda *, on_resumed=None: pytest.fail(
                "broker verification ran inside the 60s grace"
            ),
        )

        assert runner._auto_resume_pause_if_due(now=now) is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == reason

    def test_empty_order_snapshot_pause_auto_resumes_after_two_complete_proofs(
        self,
        monkeypatch,
    ) -> None:
        from app.models import (
            OrderRecord,
            RuntimeState,
            RuntimeStateSnapshot,
            TrackedEntry,
            TradeEvent,
        )

        symbol = "AUTO-PROOF.US"
        reason = runner_module._EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON

        def clean() -> None:
            with database.SessionLocal() as db:
                db.query(OrderRecord).filter(
                    OrderRecord.status.in_(["SUBMITTED", "PARTIAL_FILLED"])
                ).delete(synchronize_session=False)
                db.query(TradeEvent).filter(
                    TradeEvent.event_type == "RISK_AUTO_RESUMED",
                    TradeEvent.message == reason,
                ).delete(synchronize_session=False)
                db.query(RuntimeStateSnapshot).filter(
                    RuntimeStateSnapshot.symbol == symbol
                ).delete(synchronize_session=False)
                db.query(RuntimeState).filter(
                    RuntimeState.symbol == symbol
                ).delete(synchronize_session=False)
                db.query(TrackedEntry).filter(
                    TrackedEntry.symbol == symbol
                ).delete(synchronize_session=False)
                db.commit()

        class Broker:
            def __init__(self) -> None:
                self.order_reads = 0
                self.position_reads = 0

            def get_today_orders(self) -> list[object]:
                self.order_reads += 1
                return []

            def get_positions(self) -> list[object]:
                self.position_reads += 1
                return []

        clean()
        try:
            with database.SessionLocal() as db:
                db.add(RuntimeState(symbol=symbol))
                db.commit()
            runner = AppRunner()
            broker = Broker()
            runner.broker = cast(Any, broker)
            monkeypatch.setattr(
                runner,
                "_reconcile_tracked_entries_with_broker",
                lambda *_args, **_kwargs: [],
            )
            runner.engine.params = StrategyParams(
                symbol=symbol,
                market="US",
                auto_resume_minutes=0,
            )
            now = datetime.now(timezone.utc)
            runner.risk.pause(
                reason,
                auto_resumable=False,
                paused_at=now - timedelta(seconds=61),
            )

            assert runner._auto_resume_pause_if_due(now=now) is False
            assert runner.risk.paused is True
            assert runner.risk.pause_reason == reason
            assert runner._unknown_submission_proof_at > 0

            runner._unknown_submission_proof_at -= 6
            assert runner._auto_resume_pause_if_due(now=now) is True
            assert runner.risk.paused is False
            assert broker.order_reads >= 2
            assert broker.position_reads >= 2

            with database.SessionLocal() as db:
                event = (
                    db.query(TradeEvent)
                    .filter(
                        TradeEvent.event_type == "RISK_AUTO_RESUMED",
                        TradeEvent.message == reason,
                    )
                    .one()
                )
                assert (
                    '"source": "verified_empty_order_snapshot_reconciliation"'
                    in event.payload_json
                )
                state = (
                    db.query(RuntimeState)
                    .filter(RuntimeState.symbol == symbol)
                    .one()
                )
                assert state.paused is False
        finally:
            clean()

    def test_empty_order_snapshot_pause_stays_paused_when_proof_changes_state(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        now = datetime.now(timezone.utc)
        original_reason = runner_module._EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
        changed_reason = (
            "ORDER_RECONCILIATION_UNCERTAIN: unresolved live orders require "
            "manual reconciliation; live_orders=AAPL.US=[appeared-1]; "
            "representation_issues=none"
        )
        runner.risk.pause(
            original_reason,
            paused_at=now - timedelta(seconds=61),
        )

        def changed_proof(
            *,
            on_resumed: object | None = None,
        ) -> tuple[bool, str]:
            assert on_resumed is not None
            runner.risk.pause(changed_reason)
            return False, "live or unresolved orders still exist: appeared-1"

        monkeypatch.setattr(runner, "resume_after_verification", changed_proof)

        assert runner._auto_resume_pause_if_due(now=now) is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == changed_reason

    def test_empty_order_snapshot_resume_persistence_failure_repauses(
        self,
        monkeypatch,
    ) -> None:
        from app.models import RuntimeState, RuntimeStateSnapshot, TradeEvent

        symbol = "AUTO-COMMIT-FAIL.US"
        reason = runner_module._EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
        now = datetime.now(timezone.utc)
        paused_at = now - timedelta(seconds=61)

        def clean() -> None:
            with database.SessionLocal() as db:
                db.query(TradeEvent).filter(
                    TradeEvent.event_type == "RISK_AUTO_RESUMED",
                    TradeEvent.message == reason,
                ).delete(synchronize_session=False)
                db.query(RuntimeStateSnapshot).filter(
                    RuntimeStateSnapshot.symbol == symbol
                ).delete(synchronize_session=False)
                db.query(RuntimeState).filter(
                    RuntimeState.symbol == symbol
                ).delete(synchronize_session=False)
                db.commit()

        clean()
        try:
            with database.SessionLocal() as db:
                db.add(
                    RuntimeState(
                        symbol=symbol,
                        paused=True,
                        pause_reason=reason,
                        paused_at=paused_at,
                    )
                )
                db.commit()

            runner = AppRunner()
            runner.engine.params = StrategyParams(symbol=symbol, market="US")
            runner.risk.pause(reason, paused_at=paused_at)
            monkeypatch.setattr(
                runner,
                "verify_operational_resume",
                lambda: (True, ""),
            )
            monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

            @contextmanager
            def failing_commit_session():
                db = database.SessionLocal()

                def fail_after_flush() -> None:
                    db.flush()
                    raise RuntimeError("commit interrupted")

                monkeypatch.setattr(db, "commit", fail_after_flush)
                try:
                    yield db
                finally:
                    db.rollback()
                    db.close()

            monkeypatch.setattr(runner, "_db_session", failing_commit_session)

            assert runner._auto_resume_pause_if_due(now=now) is False
            assert runner.risk.paused is True
            assert runner.risk.pause_reason == reason
            assert runner.risk.paused_at == paused_at

            with database.SessionLocal() as db:
                state = (
                    db.query(RuntimeState)
                    .filter(RuntimeState.symbol == symbol)
                    .one()
                )
                assert state.paused is True
                assert state.pause_reason == reason
                assert state.paused_at is not None
                assert state.paused_at.replace(tzinfo=timezone.utc) == paused_at
                assert (
                    db.query(TradeEvent)
                    .filter(
                        TradeEvent.event_type == "RISK_AUTO_RESUMED",
                        TradeEvent.message == reason,
                    )
                    .count()
                    == 0
                )
        finally:
            clean()

    def test_other_order_reconciliation_pause_never_auto_resumes(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        now = datetime.now(timezone.utc)
        reason = (
            "ORDER_RECONCILIATION_UNCERTAIN: unresolved live orders require "
            "manual reconciliation; live_orders=AAPL.US=[live-1]; "
            "representation_issues=none"
        )
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            auto_resume_minutes=1,
        )
        runner.risk.pause(
            reason,
            auto_resumable=True,
            paused_at=now - timedelta(minutes=10),
        )
        monkeypatch.setattr(
            runner,
            "resume_after_verification",
            lambda *, on_resumed=None: pytest.fail(
                "ambiguous reconciliation pause was verified automatically"
            ),
        )

        assert runner._auto_resume_pause_if_due(now=now) is False
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == reason

    def test_auto_resume_pending_timeout_pause_after_broker_fill(self) -> None:
        from app import database
        from app.models import OrderRecord, RuntimeState, RuntimeStateSnapshot, TradeEvent

        database.init_db()
        with database.SessionLocal() as db:
            db.query(TradeEvent).delete()
            db.query(RuntimeStateSnapshot).delete()
            db.query(RuntimeState).delete()
            db.query(OrderRecord).delete()
            db.add(OrderRecord(
                broker_order_id="order-late-fill",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=197.76,
                executed_quantity=10,
                executed_price=197.74,
                status="FILLED",
                created_at=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
                filled_at=datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
            ))
            db.commit()

            runner = AppRunner()
            runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=196, sell_high=199)
            runner.risk.pause("pending order order-late-fill timed out after 30s")

            runner.risk.begin_entry_reconciliation(
                "post-fill PnL reconciliation in progress: NVDA.US"
            )
            assert runner._resume_pending_timeout_pause_if_filled(db) is False
            assert runner.risk.paused is True
            runner.risk.finish_entry_reconciliation()

            resumed = runner._resume_pending_timeout_pause_if_filled(db)

            assert resumed is True
            assert runner.risk.paused is False
            event = db.query(TradeEvent).filter(TradeEvent.event_type == "RISK_AUTO_RESUMED").first()
            assert event is not None
            assert "order-late-fill" in event.message

    def test_pending_timeout_resume_commit_failure_keeps_pause(
        self,
        monkeypatch,
    ) -> None:
        from app import database
        from app.models import OrderRecord

        order_id = "order-late-fill-commit-failure"
        reason = f"pending order {order_id} timed out after 30s"
        with database.SessionLocal() as db:
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == order_id
            ).delete(synchronize_session=False)
            db.add(OrderRecord(
                broker_order_id=order_id,
                symbol="NVDA.US",
                side="BUY",
                quantity=1,
                price=197.76,
                executed_quantity=1,
                executed_price=197.74,
                status="FILLED",
                created_at=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
                filled_at=datetime(2026, 7, 2, 13, 31, tzinfo=timezone.utc),
            ))
            db.commit()

        try:
            runner = AppRunner()
            runner.engine.params = StrategyParams(
                symbol="NVDA.US",
                market="US",
                buy_low=196,
                sell_high=199,
            )
            runner.risk.pause(reason)
            with database.SessionLocal() as db:
                def fail_commit() -> None:
                    db.flush()
                    raise RuntimeError("commit interrupted")

                monkeypatch.setattr(db, "commit", fail_commit)

                with pytest.raises(RuntimeError, match="commit interrupted"):
                    runner._resume_pending_timeout_pause_if_filled(db)

                assert runner.risk.paused is True
                assert runner.risk.pause_reason == reason
                assert runner.risk.check().approved is False
        finally:
            with database.SessionLocal() as db:
                db.query(OrderRecord).filter(
                    OrderRecord.broker_order_id == order_id
                ).delete(synchronize_session=False)
                db.commit()

    def test_unrealized_loss_guard_pauses_when_combined_daily_loss_reaches_limit(self) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=196, sell_high=199)
        runner.risk.config.max_daily_loss = 5000
        runner.risk.daily_pnl = -200
        runner._trade_svc.load_tracked_entries({"NVDA.US": (Decimal("100"), Decimal("10000"))})

        paused = runner._pause_if_unrealized_loss_limit_reached("NVDA.US", 52.0)

        assert paused is True
        assert runner.risk.paused is True
        assert "unrealized daily loss limit reached" in runner.risk.pause_reason

    def test_paused_runner_updates_price_without_repeated_risk_notification(self) -> None:
        class Notifier:
            def __init__(self) -> None:
                self.risk_events: list[tuple[object, ...]] = []

            def notify_order(self, *args: object) -> bool:
                return True

            def notify_risk_event(self, *args: object) -> bool:
                self.risk_events.append(args)
                return True

        runner = AppRunner()
        notifier = Notifier()
        risk_events: list[str] = []
        runner._running = True
        runner.notifier = notifier
        runner._record_risk_event = lambda reason: risk_events.append(reason)
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.risk.pause("manual")

        runner._on_quote(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()))
        runner._on_quote(Quote("AAPL.US", 98.5, 98.0, 99.0, _fresh_timestamp()))

        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.last_price == 98.5
        assert notifier.risk_events == []
        assert risk_events == []

    def test_unprofitable_sell_skip_preserves_cooldown_to_avoid_position_polling_loop(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)

        class Broker:
            def __init__(self) -> None:
                self.position_checks = 0

            def get_positions(self) -> list[Position]:
                self.position_checks += 1
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("220"))]

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=220.15)
        runner.engine.state = EngineState.LONG
        runner.engine._cooldown_seconds = 60
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        quote = Quote("AAPL.US", 220.16, 220.15, 220.17, _fresh_timestamp())
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.position_checks == 1
        assert runner.engine.state == EngineState.LONG

    def test_missing_position_rolls_back_sell_trigger(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._running = True
        runner.broker = Broker()
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.notifier = _NoopNotifier()
        runner._record_risk_event = lambda reason: None

        runner._on_quote(Quote(symbol="AAPL.US", last_price=201.0, bid=200.5, ask=201.5, timestamp=_fresh_timestamp()))

        assert runner.engine.state == EngineState.LONG
        assert runner.engine.last_trigger_at is None

    def test_sell_uses_matching_symbol_position(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted_quantity: Decimal | None = None

            def get_positions(self) -> list[Position]:
                return [
                    Position(symbol="MSFT.US", side="LONG", quantity=Decimal("2"), avg_price=Decimal("300")),
                    Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150")),
                ]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal):
                self.submitted_quantity = quantity
                return OrderResult("order-1", symbol, side, quantity, price, "FILLED")

            def get_order_status(self, order_id: str):
                raise AssertionError("submitted FILLED result should not poll order status")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        updates: list[tuple[str, str, object]] = []
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "FILLED"
        assert broker.submitted_quantity == Decimal("5")
        assert runner.risk.daily_pnl == 255.0
        assert updates[-1][0] == "order-1"
        assert updates[-1][1] == "FILLED"
        assert updates[-1][2] is not None

    def test_execute_buy_returns_false_for_rejected_order(self) -> None:
        class Broker:
            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-rejected",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="REJECTED",
                )

        runner = AppRunner()
        runner.broker = Broker()
        runner.engine.params.market = "US"
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)

        order_status = self._execute_buy(runner, "AAPL.US", Quote("AAPL.US", 100.0, 99.5, 100.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "REJECTED"

    @pytest.mark.parametrize("terminal_status", ["REJECTED", "CANCELLED"])
    def test_execute_sell_records_terminal_status_without_filled_at(self, terminal_status: str) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                assert order_id == "order-1"
                assert runner.risk.daily_pnl == 0.0
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status=terminal_status,
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 1

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "SUBMITTED"
        assert runner.risk.daily_pnl == 0.0
        assert runner._trade_svc._pending_order is not None

        runner._trade_svc.reconcile(runner.risk, runner.notifier, runner.engine.restore, runner.notifier.notify_risk_event)

        assert updates
        assert updates[-1][0] == "order-1"
        assert updates[-1][1] == terminal_status
        assert updates[-1][2] is None

    def test_execute_sell_filled_records_filled_at_timestamp(self) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                assert order_id == "order-1"
                assert runner.risk.daily_pnl == 0.0
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="FILLED",
                    executed_quantity=Decimal("5"),
                    executed_price=Decimal("201"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 1

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "SUBMITTED"
        assert runner._trade_svc._pending_order is not None

        runner._trade_svc.reconcile(runner.risk, runner.notifier, runner.engine.restore, runner.notifier.notify_risk_event)

        assert updates
        assert updates[-1][0] == "order-1"
        assert updates[-1][1] == "FILLED"
        assert updates[-1][2] is not None

    def test_execute_sell_without_fill_tracks_pending_without_pnl_or_filled_status(self) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "SUBMITTED"
        assert runner.risk.daily_pnl == 0.0
        assert runner._trade_svc._pending_order is not None
        assert runner._trade_svc._pending_order.broker_order_id == "order-1"
        assert all(status != "FILLED" for _order_id, status, _filled_at in updates)

    def test_live_submitted_timeout_keeps_trigger_state_and_skips_duplicate_order(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp())
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is not None

    def test_partial_filled_timeout_keeps_pending_and_skips_duplicate_order(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="PARTIAL_FILLED",
                    executed_quantity=Decimal("1"),
                    executed_price=Decimal("99"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp())
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is not None

    def test_pending_order_rejection_restores_snapshot_and_later_quote_can_retrigger(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0
                self.statuses = ["REJECTED", "REJECTED"]

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submissions += 1
                return OrderResult(
                    broker_order_id=f"order-{self.submissions}",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                status = self.statuses.pop(0)
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status=status,
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp())
        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.engine.state == EngineState.LONG

        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.engine.state == EngineState.FLAT
        assert runner._trade_svc._pending_order is None

        runner._on_quote(quote)
        assert broker.submissions == 1
        assert runner.risk.paused is True

    def test_trigger_uses_symbol_snapshot_if_strategy_changes_before_submit(self) -> None:
        entered_first_broadcast = threading.Event()
        release_first_broadcast = threading.Event()
        broadcast_calls = 0

        class Broker:
            def __init__(self) -> None:
                self.submitted_symbols: list[str] = []

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                self.submitted_symbols.append(symbol)
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="REJECTED",
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            buy_low=100.0,
            sell_high=200.0,
            fee_rate_us=0.007,
        )
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        recorded_metadata: list[dict[str, object]] = []
        runner._trade_svc._record_order = (
            lambda *args: recorded_metadata.append(args[9])
        )

        def broadcast_status() -> None:
            nonlocal broadcast_calls
            broadcast_calls += 1
            if broadcast_calls == 1:
                entered_first_broadcast.set()
                release_first_broadcast.wait(timeout=2)

        runner._broadcast_status = broadcast_status
        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()),))
        thread.start()
        try:
            assert entered_first_broadcast.wait(timeout=1)
            runner.engine.params = StrategyParams(
                symbol="MSFT.US",
                buy_low=50.0,
                sell_high=80.0,
                fee_rate_us=0.0001,
            )
        finally:
            release_first_broadcast.set()
            thread.join(timeout=2)

        assert thread.is_alive() is False
        assert broker.submitted_symbols == ["AAPL.US"]
        assert recorded_metadata
        assert recorded_metadata[0]["expected_exit_price"] == 200.0
        assert recorded_metadata[0]["fee_rate"] == 0.007

    def test_exit_profit_floor_uses_requested_entry_quantity(self) -> None:
        from app.models import OrderRecord

        symbol = "PARTIALREF.US"
        runner = AppRunner()
        filled_at = datetime.now(timezone.utc)
        runner._trade_svc.load_tracked_entries(
            {
                symbol: (
                    Decimal("10"),
                    Decimal("1000"),
                    "LONG",
                    filled_at,
                )
            }
        )
        with runner._db_session() as db:
            db.query(OrderRecord).filter(
                OrderRecord.symbol == symbol
            ).delete()
            db.add(
                OrderRecord(
                    broker_order_id="partial-reference-order",
                    symbol=symbol,
                    side="BUY",
                    quantity=1000,
                    price=100,
                    executed_quantity=10,
                    executed_price=100,
                    status="CANCELLED",
                    filled_at=filled_at,
                )
            )
            db.commit()

        reference = runner._entry_reference_quantity_for_exit(
            symbol,
            "SELL",
        )

        assert reference == Decimal("1000")

    def test_exit_profit_floor_ignores_unrelated_historical_entry(self) -> None:
        from app.models import OrderRecord

        symbol = "STALEPARTIALREF.US"
        runner = AppRunner()
        tracked_opened_at = datetime.now(timezone.utc)
        runner._trade_svc.load_tracked_entries(
            {
                symbol: (
                    Decimal("10"),
                    Decimal("1000"),
                    "LONG",
                    tracked_opened_at,
                )
            }
        )
        with runner._db_session() as db:
            db.query(OrderRecord).filter(
                OrderRecord.symbol == symbol
            ).delete()
            db.add(
                OrderRecord(
                    broker_order_id="stale-partial-reference-order",
                    symbol=symbol,
                    side="BUY",
                    quantity=1000,
                    price=100,
                    executed_quantity=10,
                    executed_price=100,
                    status="CANCELLED",
                    filled_at=tracked_opened_at - timedelta(days=1),
                )
            )
            db.commit()

        reference = runner._entry_reference_quantity_for_exit(
            symbol,
            "SELL",
        )

        assert reference is None

    def test_stop_closes_broker_after_fast_submit_tracking(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.closed = False
                self.status_checks = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                self.status_checks += 1
                raise AssertionError("quote trigger should not poll order status before returning")

            def close(self) -> None:
                self.closed = True

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 2

        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()),))
        thread.start()
        thread.join(timeout=2)

        assert thread.is_alive() is False
        assert broker.status_checks == 0
        assert runner._trade_svc.has_pending_order is True
        runner.stop()
        assert broker.closed is True

    def test_pending_partial_cancel_accounts_fill_and_keeps_residual_position_state(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.statuses = [
                    SimpleNamespace(
                        broker_order_id="order-1",
                        status="PARTIAL_FILLED",
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("205"),
                    ),
                    SimpleNamespace(
                        broker_order_id="order-1",
                        status="CANCELLED",
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("205"),
                    ),
                ]

            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return self.statuses.pop(0)

        runner = AppRunner()
        runner.broker = Broker()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.engine.state = EngineState.LONG
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp())
        runner._on_quote(quote)
        assert runner.engine.state == EngineState.FLAT
        assert runner._trade_svc._pending_order is not None

        runner._on_quote(quote)

        assert runner.risk.daily_pnl == 0.0
        assert runner.engine.state == EngineState.FLAT
        assert runner._trade_svc._pending_order is not None

        runner._on_quote(quote)

        assert runner.risk.daily_pnl == pytest.approx(
            110.0 - (2 * 150 + 2 * 205) * 0.0005
        )
        assert runner.engine.state == EngineState.LONG
        assert runner._trade_svc._pending_order is None

    def test_pending_order_reconcile_is_throttled_between_poll_intervals(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.status_checks = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                self.status_checks += 1
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 60
        runner._trade_svc._order_status_timeout_seconds = 0

        quote = Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp())
        runner._on_quote(quote)
        runner._on_quote(quote)
        runner._on_quote(quote)

        assert broker.status_checks == 0

    def test_on_quote_tracks_pending_order_without_status_poll(self) -> None:
        class Broker:
            def __init__(self) -> None:
                self.status_checks = 0

            def get_cash(self, _currency=None) -> Decimal:
                return Decimal("1000")

            def estimate_margin_max_quantity(self, _symbol, _side, _price, _currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                self.status_checks += 1
                raise AssertionError("quote trigger should not poll order status before returning")

        runner = AppRunner()
        broker = Broker()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(symbol="AAPL.US", buy_low=100.0, sell_high=200.0)
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 2

        thread = threading.Thread(target=runner._on_quote, args=(Quote("AAPL.US", 99.0, 98.5, 99.5, _fresh_timestamp()),))
        thread.start()
        thread.join(timeout=2)

        assert thread.is_alive() is False
        assert broker.status_checks == 0
        assert runner._trade_svc.has_pending_order is True

    def test_execute_sell_rejected_after_submit_updates_status_without_pnl(self) -> None:
        runner = AppRunner()
        updates: list[tuple[str, str, object]] = []

        class Broker:
            def get_positions(self) -> list[Position]:
                return [Position(symbol="AAPL.US", side="LONG", quantity=Decimal("5"), avg_price=Decimal("150"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
                return OrderResult(
                    broker_order_id="order-1",
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status="SUBMITTED",
                )

            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="REJECTED",
                    executed_quantity=Decimal("0"),
                    executed_price=Decimal("0"),
                )

        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        self._stub_trade_callbacks(runner)
        runner._trade_svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status, filled_at))
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc._order_status_timeout_seconds = 1

        order_status = self._execute_sell(runner, "AAPL.US", Quote("AAPL.US", 201.0, 200.5, 201.5, _fresh_timestamp()))

        assert order_status is not None
        assert order_status.status == "SUBMITTED"
        assert runner.risk.daily_pnl == 0.0
        assert runner._trade_svc._pending_order is not None

        runner._trade_svc.reconcile(runner.risk, runner.notifier, runner.engine.restore, runner.notifier.notify_risk_event)

        assert updates[-1][1] == "REJECTED"


def test_kill_switch_endpoint_fans_out_to_all_channels(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from app.api import trade as trade_api
    from app.core.notifiers.multi_channel import MultiChannelNotifier
    from app.main import app

    sc = MagicMock()
    wh = MagicMock()
    sc.send.return_value = True
    wh.send.return_value = True

    runner = AppRunner()
    runner.notifier = MultiChannelNotifier([(sc, "INFO"), (wh, "CRITICAL")])
    runner.risk.disable_kill_switch()

    monkeypatch.setattr(trade_api, "get_runner", lambda: runner)
    test_client = TestClient(app)
    resp = test_client.post("/api/control/kill-switch", json={"reason": "drill"})
    assert resp.status_code == 200
    sc.send.assert_called_once()
    wh.send.assert_called_once()


class TestTradingSessionGuard:
    def _make_runner(self, mode: str = "RTH_ONLY") -> AppRunner:
        runner = AppRunner()
        runner.engine.params = StrategyParams(
            symbol="AAPL.US", market="US", buy_low=100, sell_high=110,
        )
        runner._trading_session_mode = mode
        return runner

    def test_returns_none_when_mode_is_any(self, monkeypatch) -> None:
        runner = self._make_runner(mode="ANY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda market: False)
        assert runner._check_trading_session("BUY") is None

    def test_returns_none_when_rth_only_and_in_hours(self, monkeypatch) -> None:
        runner = self._make_runner(mode="RTH_ONLY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda market: True)
        monkeypatch.setattr(runner_module, "is_opening_warmup", lambda market, minutes: False, raising=False)
        assert runner._check_trading_session("BUY") is None

    def test_blocks_entry_during_opening_warmup(self, monkeypatch) -> None:
        runner = self._make_runner(mode="RTH_ONLY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda market: True)
        monkeypatch.setattr(runner_module, "is_opening_warmup", lambda market, minutes: True, raising=False)

        result = runner._check_trading_session("BUY")

        assert isinstance(result, dict)
        assert result.get("status") == "SKIPPED"
        assert result.get("skip_category") == "SESSION"
        assert "opening warmup" in result.get("reason", "")

    def test_cancel_pending_action_bypasses_gate_outside_hours(self, monkeypatch) -> None:
        runner = self._make_runner(mode="RTH_ONLY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda market: False)
        assert runner._check_trading_session("CANCEL_PENDING") is None

    def test_blocks_and_records_skip_and_audit_outside_hours(self, monkeypatch) -> None:
        runner = self._make_runner(mode="RTH_ONLY")
        monkeypatch.setattr(runner_module, "is_trading_hours", lambda market: False)

        skip_calls: list[tuple[Any, ...]] = []
        audit_calls: list[tuple[Any, ...]] = []
        runner._record_order_skipped = lambda symbol, action, reason, payload: skip_calls.append(
            (symbol, action, reason, payload)
        )

        class _AuditStub:
            def record(self, action, **kw):
                audit_calls.append((action, kw))

        runner._audit = _AuditStub()  # type: ignore[assignment]

        result = runner._check_trading_session("BUY")
        assert isinstance(result, dict)
        assert result.get("status") == "SKIPPED"
        assert result.get("skip_category") == "SESSION"

        assert len(skip_calls) == 1
        symbol, action, reason, payload = skip_calls[0]
        assert symbol == "AAPL.US"
        assert action == "BUY"
        assert "non-RTH" in reason
        assert payload["skip_category"] == "SESSION"

        assert audit_calls == [("TRADING_SESSION_BLOCKED", {
            "severity": "INFO",
            "request_summary": {"symbol": "AAPL.US", "action": "BUY", "market": "US"},
        })]

    def test_refresh_trading_session_mode_pulls_latest_from_db(self) -> None:
        from app import database
        from app.models import StrategyConfig

        database.init_db()
        with database.SessionLocal() as db:
            db.query(StrategyConfig).delete()
            db.add(StrategyConfig(symbol="AAPL.US", market="US", trading_session_mode="RTH_ONLY"))
            db.commit()

        runner = AppRunner()
        assert runner._get_trading_session_mode() == "ANY"
        runner._refresh_trading_session_mode()
        assert runner._get_trading_session_mode() == "RTH_ONLY"


class TestRecentQuotesDequeBound:
    """The hot-path recent-quotes list is now a bounded deque with O(1)
    amortised sliding-window pruning. These tests pin the contract."""

    def test_recent_quotes_is_bounded_deque(self) -> None:
        from collections import deque

        runner = AppRunner()
        assert isinstance(runner._recent_quotes, deque)
        assert runner._recent_quotes.maxlen == runner._recent_quotes_cap

    def test_symbol_runtime_recent_quotes_is_bounded_deque(self) -> None:
        from collections import deque

        runner = AppRunner()
        runtime = runner._build_symbol_runtime("AAPL.US", "US")
        assert isinstance(runtime.recent_quotes, deque)
        assert runtime.recent_quotes.maxlen == runner._recent_quotes_cap

    def test_remember_quote_caps_at_maxlen(self) -> None:
        runner = AppRunner()
        runner._running = True
        cap = runner._recent_quotes_cap
        for i in range(cap + 100):
            runner._remember_quote(Quote(
                symbol="AAPL.US",
                last_price=100.0 + i,
                bid=99.5 + i,
                ask=100.5 + i,
                timestamp=_fresh_timestamp(),
            ))
        assert len(runner._recent_quotes) == cap

    def test_window_prune_drops_stale_entries(self) -> None:
        runner = AppRunner()
        runner._running = True
        # Lower the window for fast testing.
        runner._recent_quote_window_seconds = 0.1
        for i in range(5):
            runner._remember_quote(Quote(
                symbol="AAPL.US",
                last_price=100.0 + i,
                bid=99.5,
                ask=100.5,
                timestamp=_fresh_timestamp(),
            ))
        assert len(runner._recent_quotes) == 5
        import time as _time
        _time.sleep(0.2)
        runner._remember_quote(Quote(
            symbol="AAPL.US",
            last_price=200.0,
            bid=199.5,
            ask=200.5,
            timestamp=_fresh_timestamp(),
        ))
        # All earlier entries were older than 0.1s when this new one arrived.
        # Only the new entry should survive.
        assert len(runner._recent_quotes) == 1
        assert runner._recent_quotes[0]["last_price"] == 200.0

    def test_price_stop_submits_reduce_only_sell_at_executable_bid(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted: list[tuple[str, str, Decimal, Decimal]] = []
                self.filled = False

            def get_positions(self) -> list[Position]:
                if self.filled:
                    return []
                return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [
                    Quote(symbols[0], 98.9, 98.8, 99.0, _fresh_timestamp())
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submitted.append((symbol, side, quantity, price))
                self.filled = True
                return OrderResult("risk-exit", symbol, side, quantity, price, "FILLED")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=95,
            sell_high=110,
            min_profit_amount=1000,
            allow_position_addons=False,
            stop_loss_pct=1,
            max_holding_minutes=60,
        )
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner._trade_svc._on_fill = None
        runner._trade_svc.load_tracked_entries({
            "NVDA.US": (
                Decimal("5"),
                Decimal("500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })
        persisted: list[str] = []
        completed: list[str] = []
        def persist_reduction(intent, symbol: str) -> bool:
            persisted.append(intent.cause)
            runner._reduction_intents[symbol] = intent
            return True

        monkeypatch.setattr(runner, "_persist_reduction", persist_reduction)
        monkeypatch.setattr(
            runner,
            "_entry_reference_quantity_for_exit",
            lambda *_args: pytest.fail(
                "deterministic stop-loss must not query entry reference history"
            ),
        )
        def complete_reduction(symbol: str, cause: str, reason: str) -> None:
            runner._reduction_intents.pop(symbol, None)
            completed.append(cause)

        monkeypatch.setattr(runner, "_complete_reduction", complete_reduction)

        runner._on_quote(Quote("NVDA.US", 98.9, 98.8, 99.0, _fresh_timestamp()))

        assert runner.broker.submitted == [
            ("NVDA.US", "SELL", Decimal("5"), Decimal("98.80"))
        ]
        assert persisted == ["PRICE_STOP"]
        assert completed == ["PRICE_STOP"]
        assert runner.engine.state == EngineState.FLAT
        assert runner.engine.long_entry_rearm_required is True
        assert runner.engine.update_price(94.0).triggered is False

    def test_reduction_persistence_failure_blocks_submission_and_restores_engine(
        self,
        monkeypatch,
    ) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submissions = 0

            def submit_limit_order(self, *_args, **_kwargs):
                self.submissions += 1
                raise AssertionError("order must not be submitted before durable reduction")

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=95.0,
            sell_high=110.0,
            stop_loss_pct=1.0,
        )
        runner.engine.state = EngineState.LONG
        runner._symbol_runtimes["NVDA.US"] = runner._build_symbol_runtime(
            "NVDA.US", "US", primary=True
        )
        runner._trade_svc.load_tracked_entries(
            {
                "NVDA.US": (
                    Decimal("5"),
                    Decimal("500"),
                    "LONG",
                    datetime.now(timezone.utc),
                )
            }
        )
        runner.broker = Broker()
        monkeypatch.setattr(
            runner._state_svc,
            "persist_reduction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
        )

        runner._on_quote(Quote("NVDA.US", 98.9, 98.8, 99.0, _fresh_timestamp()))

        assert runner.broker.submissions == 0
        assert runner.engine.state == EngineState.LONG
        assert runner._reduction_intents == {}
        assert runner.risk.paused is True
        assert runner.risk.pause_auto_resumable is False

    def test_clear_reduction_keeps_memory_latch_when_durable_clear_fails(
        self,
        monkeypatch,
    ) -> None:
        runner = AppRunner()
        intent = _ReductionIntent(
            action="SELL",
            cause="PRICE_STOP",
            reason="stop",
            trigger_price=98.0,
            started_at=datetime.now(timezone.utc),
        )
        runner._reduction_intents["NVDA.US"] = intent
        monkeypatch.setattr(
            runner._state_svc,
            "clear_reduction",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
        )

        assert runner._clear_reduction("NVDA.US", reason="flat") is False
        assert runner._reduction_intents["NVDA.US"] is intent
        assert runner.risk.paused is True

    def test_quote_cannot_clear_durable_reduction_from_local_flat_state(self) -> None:
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.FLAT
        intent = _ReductionIntent(
            action="SELL",
            cause="PRICE_STOP",
            reason="persisted stop",
            trigger_price=98.0,
            started_at=datetime.now(timezone.utc),
        )
        runner._reduction_intents["NVDA.US"] = intent

        result, newly_latched, should_clear = (
            runner._reduction_intent_for_quote_locked(
                Quote("NVDA.US", 99.0, 98.9, 99.1, _fresh_timestamp()),
                runner.engine,
                "US",
            )
        )

        assert result is intent
        assert newly_latched is False
        assert should_clear is False
        assert runner._reduction_intents["NVDA.US"] is intent

    def test_invalid_persisted_reduction_is_latched_instead_of_discarded(
        self,
        monkeypatch,
    ) -> None:
        from app.database import SessionLocal

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        monkeypatch.setattr(
            runner._state_svc,
            "load_reduction",
            lambda *_args, **_kwargs: {
                "action": "BROKEN",
                "cause": "UNKNOWN",
                "reason": "corrupt",
                "trigger_price": 98.0,
                "started_at": datetime.now(timezone.utc),
            },
        )
        monkeypatch.setattr(
            runner._state_svc,
            "clear_reduction",
            lambda *_args, **_kwargs: pytest.fail(
                "invalid durable reduction must not be cleared without broker proof"
            ),
        )

        with SessionLocal() as db:
            runner._restore_reduction(db)

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )
        assert runner._reduction_intents["NVDA.US"].action == "BROKEN"

    def test_reduction_recovery_pauses_when_broker_positions_are_unavailable(
        self,
    ) -> None:
        from app.database import SessionLocal

        class Broker:
            def get_positions(self):
                raise TimeoutError("positions unavailable")

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.FLAT
        runner.broker = Broker()
        runner._reduction_intents["NVDA.US"] = _ReductionIntent(
            action="SELL",
            cause="PRICE_STOP",
            reason="persisted stop",
            trigger_price=98.0,
            started_at=datetime.now(timezone.utc),
        )

        with SessionLocal() as db:
            completed = runner._reconcile_tracked_entries_with_broker(db)

        assert completed == []
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )
        assert "NVDA.US" in runner._reduction_intents

    def test_recent_unproven_fill_cannot_be_masked_by_old_expectation(self) -> None:
        from app.database import SessionLocal

        symbol = "AMBIG.US"
        now = datetime.now(timezone.utc)

        class Broker:
            def get_positions(self) -> list[Position]:
                return [
                    Position(symbol, "LONG", Decimal("5"), Decimal("100"))
                ]

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner._post_fill_expectations[symbol] = runner_module._PostFillExpectation(
            side="LONG",
            quantity=Decimal("5"),
            recorded_at=time.monotonic() - 30,
            cost=Decimal("500"),
            opened_at=now - timedelta(minutes=5),
        )

        with SessionLocal() as db:
            db.query(runner_module.TradeEvent).filter(
                runner_module.TradeEvent.symbol == symbol
            ).delete()
            db.query(runner_module.OrderRecord).filter(
                runner_module.OrderRecord.symbol == symbol
            ).delete()
            db.add(
                runner_module.OrderRecord(
                    broker_order_id="broker-only-newer-exit",
                    symbol=symbol,
                    side="SELL",
                    quantity=5.0,
                    price=101.0,
                    executed_quantity=5.0,
                    executed_price=101.0,
                    status="FILLED",
                    created_at=now - timedelta(seconds=5),
                    filled_at=now - timedelta(seconds=4),
                )
            )
            db.commit()
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_unproven_newer_fill",
            )

            assert runner.risk.paused is True
            assert runner.risk.pause_reason.startswith(
                "POSITION_RECONCILIATION_UNCERTAIN:"
            )
            assert symbol in runner._unsettled_position_symbols
            assert symbol in runner._post_fill_expectations

            db.query(runner_module.TradeEvent).filter(
                runner_module.TradeEvent.symbol == symbol
            ).delete()
            db.query(runner_module.OrderRecord).filter(
                runner_module.OrderRecord.symbol == symbol
            ).delete()
            db.commit()

    def test_durable_fill_query_failure_preserves_tracked_position(
        self,
        monkeypatch,
    ) -> None:
        from app.database import SessionLocal

        symbol = "DURABLEFAIL.US"

        class Broker:
            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol=symbol, market="US")
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner._trade_svc.load_tracked_entries(
            {
                symbol: (
                    Decimal("5"),
                    Decimal("500"),
                    "LONG",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            }
        )

        def fail_fill_query(*_args, **_kwargs):
            raise runner_module.DurableFillReconciliationError("db unavailable")

        monkeypatch.setattr(
            runner,
            "_latest_filled_orders_by_symbol",
            fail_fill_query,
        )

        with SessionLocal() as db:
            completed = runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_durable_fill_query_failure",
            )

        assert completed == []
        tracked = runner._trade_svc.tracked_position(symbol)
        assert tracked is not None and tracked.quantity == Decimal("5")
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )
        assert symbol in runner._unsettled_position_symbols

    def test_pending_buy_fill_survives_stale_flat_position_snapshots(self) -> None:
        from app.database import SessionLocal
        from app.services.trade_execution_service import _PendingOrder

        class Broker:
            positions: list[Position] = []

            def get_positions(self) -> list[Position]:
                return list(self.positions)

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        with SessionLocal() as db:
            db.query(runner_module.TrackedEntry).filter(
                runner_module.TrackedEntry.symbol == "NVDA.US"
            ).delete()
            db.commit()
        pending = _PendingOrder(
            broker=runner.broker,
            broker_order_id="entry-fill",
            symbol="NVDA.US",
            action="BUY",
            quantity=Decimal("5"),
            price=Decimal("100"),
            engine_snapshot=None,
            avg_price=None,
        )

        runner._trade_svc._finalize_pending_fill(
            pending,
            SimpleNamespace(
                executed_quantity=Decimal("5"),
                executed_price=Decimal("100"),
            ),
            fill_qty=Decimal("5"),
        )

        tracked = runner._trade_svc.tracked_position("NVDA.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("5")
        assert runner._post_fill_expectations["NVDA.US"].side == "LONG"

        for _ in range(2):
            with SessionLocal() as db:
                runner._reconcile_tracked_entries_with_broker(
                    db,
                    source="test_stale_entry_settlement",
                )
        assert runner._trade_svc.tracked_position("NVDA.US") is not None
        assert runner.engine.state == EngineState.LONG
        assert runner.risk.paused is True
        assert runner._sync_engine_state_with_positions(force=True) is False
        assert runner.engine.state == EngineState.LONG

        runner.broker.positions = [
            Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))
        ]
        with SessionLocal() as db:
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_confirmed_entry_settlement",
            )
        assert "NVDA.US" not in runner._post_fill_expectations
        assert runner._trade_svc.tracked_position("NVDA.US") is not None

    def test_pending_sell_fill_does_not_accept_stale_pre_exit_position(self) -> None:
        from app.database import SessionLocal
        from app.models import OrderRecord, TradeEvent
        from app.services.trade_execution_service import _PendingOrder

        class Broker:
            positions = [
                Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))
            ]

            def get_positions(self) -> list[Position]:
                return list(self.positions)

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.FLAT
        runner.broker = Broker()
        runner._trade_svc.load_tracked_entries(
            {
                "NVDA.US": (
                    Decimal("5"),
                    Decimal("500"),
                    "LONG",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            }
        )
        runner._persist_tracked_entry(
            "NVDA.US",
            Decimal("5"),
            Decimal("500"),
        )
        with SessionLocal() as db:
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == "exit-fill"
            ).delete()
            db.add(OrderRecord(
                broker_order_id="exit-fill",
                symbol="NVDA.US",
                side="SELL",
                quantity=5.0,
                price=101.0,
                status="SUBMITTED",
                pnl_source="TRACKED_ENTRY",
                cost_basis_price=100.0,
                cost_basis_quantity=5.0,
                position_quantity_before=5.0,
                pnl_fee_rate=0.0,
            ))
            db.add(TradeEvent(
                event_type="ORDER_SUBMITTED",
                symbol="NVDA.US",
                broker_order_id="exit-fill",
                side="SELL",
                status="SUBMITTED",
                message="locally submitted stale-position test exit",
            ))
            db.commit()
        pending = _PendingOrder(
            broker=runner.broker,
            broker_order_id="exit-fill",
            symbol="NVDA.US",
            action="SELL",
            quantity=Decimal("5"),
            price=Decimal("101"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )

        runner._trade_svc._finalize_pending_fill(
            pending,
            SimpleNamespace(
                executed_quantity=Decimal("5"),
                executed_price=Decimal("101"),
            ),
            fill_qty=Decimal("5"),
        )
        assert runner._trade_svc.tracked_position("NVDA.US") is None
        assert runner._post_fill_expectations["NVDA.US"].quantity == 0

        with SessionLocal() as db:
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_stale_exit_settlement",
            )
        assert runner._trade_svc.tracked_position("NVDA.US") is None
        assert "NVDA.US" in runner._post_fill_expectations
        assert runner.risk.paused is True

        runner.broker.positions = []
        with SessionLocal() as db:
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_confirmed_exit_settlement",
            )
        assert "NVDA.US" not in runner._post_fill_expectations
        assert runner._trade_svc.tracked_position("NVDA.US") is None
        with SessionLocal() as db:
            db.query(TradeEvent).filter(
                TradeEvent.broker_order_id == "exit-fill"
            ).delete()
            db.query(OrderRecord).filter(
                OrderRecord.broker_order_id == "exit-fill"
            ).delete()
            db.commit()

    def test_immediate_buy_fill_latches_expected_position_before_reconcile(self) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [
                    Quote(symbols[0], 100.0, 99.9, 100.1, _fresh_timestamp())
                ]

            def estimate_margin_max_quantity(self, *_args, **_kwargs) -> Decimal:
                return Decimal("5")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                return OrderResult(
                    "immediate-entry",
                    symbol,
                    side,
                    quantity,
                    price,
                    "FILLED",
                )

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.LONG
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        result = runner._trade_svc._execute_buy(
            "NVDA.US",
            Quote("NVDA.US", 100.0, 99.9, 100.1, _fresh_timestamp()),
            runner.broker,
            runner.risk,
            runner.notifier,
            "USD",
        )

        assert result is not None and result.status == "FILLED"
        expectation = runner._post_fill_expectations["NVDA.US"]
        assert expectation.side == "LONG"
        assert expectation.quantity == Decimal(str(result.executed_quantity))

    def test_immediate_sell_fill_latches_flat_before_stale_position_reconcile(
        self,
    ) -> None:
        class Broker:
            def get_positions(self) -> list[Position]:
                return [
                    Position(
                        "NVDA.US",
                        "LONG",
                        Decimal("5"),
                        Decimal("100"),
                    )
                ]

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [
                    Quote(symbols[0], 101.0, 100.9, 101.1, _fresh_timestamp())
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                return OrderResult(
                    "immediate-exit",
                    symbol,
                    side,
                    quantity,
                    price,
                    "FILLED",
                )

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner.engine.state = EngineState.FLAT
        runner.broker = Broker()
        runner.notifier = _NoopNotifier()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc.load_tracked_entries(
            {
                "NVDA.US": (
                    Decimal("5"),
                    Decimal("500"),
                    "LONG",
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
            }
        )
        result = runner._trade_svc._execute_sell(
            "NVDA.US",
            Quote("NVDA.US", 101.0, 100.9, 101.1, _fresh_timestamp()),
            runner.broker,
            runner.risk,
            runner.notifier,
            allow_loss_exit=True,
        )

        assert result is not None and result.status == "FILLED"
        assert runner._trade_svc.tracked_position("NVDA.US") is None
        assert runner._post_fill_expectations["NVDA.US"].quantity == 0

    @pytest.mark.parametrize("initial_status", ["SUBMITTED", "PARTIAL_FILLED"])
    def test_pending_protective_exit_fill_completes_reduction_and_pauses(
        self,
        monkeypatch,
        initial_status: str,
    ) -> None:
        class Broker:
            def get_order_status(self, order_id: str):
                return SimpleNamespace(
                    broker_order_id=order_id,
                    status="FILLED",
                    executed_quantity=Decimal("5"),
                    executed_price=Decimal("98"),
                )

            def get_positions(self) -> list[Position]:
                return []

        runner = AppRunner()
        runner._trade_svc._record_order = lambda *args: None
        runner._trade_svc._update_order_status = lambda *args, **kwargs: None
        runner._trade_svc._record_risk_event = lambda reason: None
        runner._trade_svc._record_order_skipped = lambda *args: None
        runner.notifier = _NoopNotifier()
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=95,
            sell_high=110,
        )
        runner.engine.state = EngineState.FLAT
        runner._trade_svc._persist_entry = None
        runner._trade_svc._on_fill = None
        runner._trade_svc._order_status_poll_interval_seconds = 0
        runner._trade_svc.load_tracked_entries({
            "NVDA.US": (
                Decimal("5"),
                Decimal("500"),
                "LONG",
                datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        })
        runner._reduction_intents["NVDA.US"] = _ReductionIntent(
            action="SELL",
            cause="PRICE_STOP",
            reason="async protective exit",
            trigger_price=98.0,
            started_at=datetime.now(timezone.utc),
        )
        broker = Broker()
        runner.broker = broker
        runner._trade_svc._track_pending_order(
            "SELL",
            OrderResult(
                "async-protective-exit",
                "NVDA.US",
                "SELL",
                Decimal("5"),
                Decimal("98"),
                initial_status,
            ),
            broker,
            runner.engine.snapshot(),
            avg_price=Decimal("100"),
        )
        completed: list[str] = []

        def clear_reduction(symbol: str, *, reason: str) -> bool:
            runner._reduction_intents.pop(symbol, None)
            completed.append(reason)
            return True

        monkeypatch.setattr(runner, "_clear_reduction", clear_reduction)
        monkeypatch.setattr(runner._state_svc, "persist", lambda *args, **kwargs: None)
        monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

        runner._trade_svc.reconcile(runner.risk, runner.notifier)

        assert runner._trade_svc.pending_order_for("NVDA.US") is None
        assert runner._trade_svc.tracked_position("NVDA.US") is None
        assert completed == ["broker fill completed reduction"]
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == "async protective exit"

    def test_llm_action_is_shadow_only_without_engine_transition(self, monkeypatch) -> None:
        class Broker:
            def __init__(self) -> None:
                self.submitted = []

            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(symbol, 100, 99.9, 100.1, _fresh_timestamp()) for symbol in symbols]

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=99, sell_high=101)
        runner.broker = Broker()
        runner._llm_order_execution_enabled = True
        monkeypatch.setattr(runner_module.settings, "llm_shadow_mode", True)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 100,
            "confidence_score": 0.9,
        })

        assert result["status"] == "SHADOW_ONLY"
        assert runner.engine.state == EngineState.FLAT
        assert runner.broker.submitted == []

    def test_existing_reduction_blocks_llm_order_before_state_transition(self, monkeypatch) -> None:
        class Broker:
            def get_quotes(self, symbols: list[str]) -> list[Quote]:
                return [Quote(symbol, 100, 99.9, 100.1, _fresh_timestamp()) for symbol in symbols]

        runner = AppRunner()
        runner._running = True
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=99, sell_high=101)
        runner.broker = Broker()
        runner._llm_order_execution_enabled = True
        runner._reduction_intents["NVDA.US"] = cast(Any, SimpleNamespace())
        monkeypatch.setattr(runner_module.settings, "llm_shadow_mode", False)

        result = runner.execute_llm_order_decision({
            "order_action": "BUY_NOW",
            "order_price": 100,
            "confidence_score": 0.9,
        })

        assert result["status"] == "REDUCING"
        assert runner.engine.state == EngineState.FLAT


class TestMarkFillProcessed:
    """_mark_fill_processed 现在要求 symbol 为必填参数,并按不同 symbol 分别记录时间戳。"""

    def test_requires_symbol_argument(self) -> None:
        runner = AppRunner()
        with pytest.raises(TypeError):
            runner._mark_fill_processed()  # type: ignore[call-arg]

    def test_stores_timestamp_per_symbol(self) -> None:
        runner = AppRunner()
        runner._mark_fill_processed(symbol="AAPL.US")
        assert "AAPL.US" in runner._last_fill_at
        assert "NVDA.US" not in runner._last_fill_at

        ts_aapl = runner._last_fill_at["AAPL.US"]
        runner._mark_fill_processed(symbol="NVDA.US")
        assert "NVDA.US" in runner._last_fill_at
        # Two different symbols have independent timestamps
        assert runner._last_fill_at["AAPL.US"] == ts_aapl
        assert runner._last_fill_at["NVDA.US"] >= ts_aapl

    def test_empty_symbol_falls_back_to_main_engine(self) -> None:
        """传递空字符串时仍回退到主引擎 symbol (防御性兼容)."""
        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
        runner._mark_fill_processed(symbol="")
        assert "NVDA.US" in runner._last_fill_at
