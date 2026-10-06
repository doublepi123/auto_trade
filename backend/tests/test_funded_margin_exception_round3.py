# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Round-3 regression tests: gate-p3a review-2 findings 1 and 2.

Finding 1 (P0): no persisted or API-visible change on OFF / PAPER /
unbound paths — the ``execution_initiator`` marker (and any other new
internal marker) must never appear in persisted ORDER_SUBMITTED
payload_json, other trade events, orders rows, audit logs, event-list
API output, decision-funnel data or diagnostics for those paths. When
the exception IS effective, one small explicit evidence block
(``funded_margin: {"applied": true, ...}``) is persisted (needed by
finding 2).

Finding 2 (P1): the per-order exception flag must survive restart and
the submission-record-failure path — frozen at submit, persisted only
when true through the existing submission provenance, restored during
startup pending-order recovery, kept on the persistence-failure
recovery path.

The base-behaviour golden key-set below is captured from the pristine
432dc793 checkout through characterize/capture_payloads.py (see
characterize/golden-base.json; hash
10c884ac2d76d72ae08dc600044f6cd60ade6ca28936313f7c81027ae5d85d74).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
import time as _time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.broker import (
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
)
from app.core.risk import RiskController
from app.runner import AppRunner
from app.services import trade_execution_service as trade_svc_module
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
    _PendingOrder,
    _PreSubmitRiskRequest,
)

FP_A = "a" * 64
FP_B = "b" * 64

# Key-set of the persisted ORDER_SUBMITTED payload on 432dc793 (base),
# captured via characterize/capture_payloads.py for the OFF config —
# identical for PAPER and MISMATCH (diff exit 0, same sha256).
_BASE_ORDER_SUBMITTED_PAYLOAD_KEYS = frozenset(
    {
        "accounting_fee_model",
        "ack_latency_ms",
        "acknowledged_at",
        "config_version",
        "decision_ask",
        "decision_at",
        "decision_bid",
        "decision_spread",
        "decision_spread_bps",
        "downside_risk_amount",
        "edge_cost_ratio",
        "entry_cost_gate_version",
        "estimated_fee",
        "estimated_fees",
        "estimated_slippage_cost",
        "estimated_spread_cost",
        "estimated_stop_costs",
        "estimated_stop_gross_loss",
        "estimated_total_cost",
        "exit_cause",
        "exit_reason",
        "expected_exit_price",
        "expected_profit",
        "expected_stop_price",
        "fee_rate",
        "fee_source",
        "market",
        "minimum_edge_cost_ratio",
        "minimum_reward_risk_ratio",
        "net_expected_profit",
        "price",
        "quantity",
        "quote_age_ms",
        "required_profit",
        "reward_risk_ratio",
        "source",
        "stop_loss_pct",
        "submit_latency_ms",
        "submit_started_at",
    }
)


class _RealPersistHarness:
    """Real runner + REAL private SQLite + fake broker; durable output.

    Mirrors characterize/capture_payloads.py so the in-repo pin and the
    out-of-repo characterization prove the same boundary. Owns a PRIVATE
    engine (the module-level ``database.engine`` is process-shared by
    conftest, so rebinding is required for per-test isolation).
    """

    def __init__(self, tmpdir: str) -> None:
        from pathlib import Path

        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker

        from app import database

        self.database = database
        self.engine = create_engine(
            f"sqlite:///{Path(tmpdir) / 'char.db'}",
            connect_args={"check_same_thread": False, "timeout": 60},
            **database.queue_pool_kwargs(
                f"sqlite:///{Path(tmpdir) / 'char.db'}",
            ),
        )
        event.listen(self.engine, "connect", database._set_sqlite_pragmas)
        self._factory = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine,
        )
        import app.runner as runner_module
        import app.services.order_terminal_callback_service as otc

        self._patches = pytest.MonkeyPatch()
        self._patches.setattr(database, "engine", self.engine)
        for module in (database, runner_module, otc):
            self._patches.setattr(module, "SessionLocal", self._factory)
        database.init_db()

    def close(self) -> None:
        try:
            self._patches.undo()
        finally:
            self.engine.dispose()

    def recover_runner(self, *, broker) -> AppRunner:
        """A NEW runner performs the startup pending-order recovery.

        Uses the REAL ``_load_pending_orders`` against the same private
        DB (the same code path ``_initialize_runner`` drives at boot).
        """
        from app.core.engine import StrategyParams

        runner = AppRunner()
        runner.broker = broker
        runner._broker_identity_fingerprint = FP_A
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )
        with self._factory() as db:
            issues = runner._load_pending_orders(db)
        assert issues == []
        return runner

    # -- reusable pieces for the persistence-failure test -----------

    @staticmethod
    def runner_record_order():
        """The pristine bound _record_order of a fresh runner."""
        return AppRunner()._record_order

    @staticmethod
    def install_recording_broker() -> "_HarnessBroker":
        return _HarnessBroker()

    @staticmethod
    def build_armed_runner(broker: "_HarnessBroker") -> AppRunner:
        from app.core.engine import StrategyParams

        runner = AppRunner()
        runner.broker = broker
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )

        class _N:
            def notify_order(self, *a, **k) -> bool:
                return True

            def notify_risk_event(self, *a, **k) -> bool:
                return True

        runner.notifier = _N()
        runner._trade_svc._final_order_quote_check = (
            lambda _b, _s, _a, p: FinalOrderQuoteCheckResult(
                executable_price=p, bid=p, ask=p,
            )
        )
        runner._trade_svc._record_order_skipped = lambda *a, **k: None
        runner._trade_svc._record_risk_event = lambda *a: None
        svc = runner._trade_svc
        svc.funded_margin_enabled = True
        svc.paper_account_confirmed = False
        svc.funded_margin_account_fingerprint = FP_A
        svc.funded_margin_requested_quantity = 1000
        svc.funded_margin_requested_notional = 25000.0
        svc.funded_margin_requested_risk = 250.0
        svc.raw_strategy_max_position_quantity = 1000
        svc.raw_strategy_max_position_notional = 25000.0
        svc.raw_strategy_max_risk_per_trade = 250.0
        svc.funded_margin_fingerprint_provider = lambda: FP_A
        return runner

    @staticmethod
    def drive_quote(runner: AppRunner):
        """Drive the REAL quote-trigger submit with RTH forced open."""
        import app.runner as runner_module

        orig = (
            trade_svc_module.is_trading_hours,
            trade_svc_module.is_closing_window,
            trade_svc_module.is_opening_warmup,
            runner_module.is_trading_hours,
            runner_module.is_closing_window,
            runner_module.is_opening_warmup,
        )
        trade_svc_module.is_trading_hours = lambda _m: True
        trade_svc_module.is_closing_window = lambda *_a: False
        trade_svc_module.is_opening_warmup = lambda *_a: False
        runner_module.is_trading_hours = lambda _m: True
        runner_module.is_closing_window = lambda *_a: False
        runner_module.is_opening_warmup = lambda *_a: False
        try:
            quote = Quote(
                "NVDA.US", 99.0, 98.9, 99.1,
                datetime.now(timezone.utc).isoformat(),
            )
            runner._on_quote(quote)
        finally:
            (
                trade_svc_module.is_trading_hours,
                trade_svc_module.is_closing_window,
                trade_svc_module.is_opening_warmup,
                runner_module.is_trading_hours,
                runner_module.is_closing_window,
                runner_module.is_opening_warmup,
            ) = orig

    def run_submit(
        self,
        *,
        flag_on: bool,
        paper: bool,
        fp: str,
        bind_identity: bool = False,
    ) -> dict[str, Any]:
        from app.core.engine import StrategyParams

        broker = _HarnessBroker()
        runner = AppRunner()
        runner.broker = broker
        if bind_identity:
            runner._broker_identity_fingerprint = FP_A
        runner._running = True
        runner.engine.params = StrategyParams(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
        )

        class _N:
            def notify_order(self, *a, **k) -> bool:
                return True

            def notify_risk_event(self, *a, **k) -> bool:
                return True

        runner.notifier = _N()
        runner._trade_svc._final_order_quote_check = (
            lambda _b, _s, _a, p: FinalOrderQuoteCheckResult(
                executable_price=p, bid=p, ask=p,
            )
        )
        runner._trade_svc._record_order_skipped = lambda *a, **k: None
        runner._trade_svc._record_risk_event = lambda *a: None

        svc = runner._trade_svc
        svc.funded_margin_enabled = flag_on
        svc.paper_account_confirmed = paper
        svc.funded_margin_account_fingerprint = FP_A
        svc.funded_margin_requested_quantity = 1000
        svc.funded_margin_requested_notional = 25000.0
        svc.funded_margin_requested_risk = 250.0
        svc.raw_strategy_max_position_quantity = 1000
        svc.raw_strategy_max_position_notional = 25000.0
        svc.raw_strategy_max_risk_per_trade = 250.0
        svc.funded_margin_fingerprint_provider = lambda: fp

        import app.runner as runner_module

        orig = (
            trade_svc_module.is_trading_hours,
            trade_svc_module.is_closing_window,
            trade_svc_module.is_opening_warmup,
            runner_module.is_trading_hours,
            runner_module.is_closing_window,
            runner_module.is_opening_warmup,
        )
        trade_svc_module.is_trading_hours = lambda _m: True
        trade_svc_module.is_closing_window = lambda *_a: False
        trade_svc_module.is_opening_warmup = lambda *_a: False
        runner_module.is_trading_hours = lambda _m: True
        runner_module.is_closing_window = lambda *_a: False
        runner_module.is_opening_warmup = lambda *_a: False
        try:
            quote = Quote(
                "NVDA.US", 99.0, 98.9, 99.1,
                datetime.now(timezone.utc).isoformat(),
            )
            runner._on_quote(quote)
        finally:
            (
                trade_svc_module.is_trading_hours,
                trade_svc_module.is_closing_window,
                trade_svc_module.is_opening_warmup,
                runner_module.is_trading_hours,
                runner_module.is_closing_window,
                runner_module.is_opening_warmup,
            ) = orig

        from app.models import OrderRecord, TradeEvent
        from app.services.event_list_service import list_timeline_events

        with self._factory() as db:
            events = (
                db.query(TradeEvent)
                .filter(TradeEvent.event_type == "ORDER_SUBMITTED")
                .all()
            )
            payloads = [
                json.loads(str(e.payload_json or "{}")) for e in events
            ]
            orders = [
                {
                    "id": o.broker_order_id,
                    "status": o.status,
                    "side": o.side,
                    "symbol": o.symbol,
                    "quantity": float(o.quantity),
                    "price": float(o.price),
                }
                for o in db.query(OrderRecord).all()
            ]
            api_items, _ = list_timeline_events(
                db,
                source="trade",
                event_types=["ORDER_SUBMITTED"],
                symbol=None,
                page=1,
                page_size=10,
            )
            api_payloads = [dict(item.payload) for item in api_items]
        return {
            "submissions": [
                [s[0], s[1], str(s[2]), str(s[3])] for s in broker.submitted
            ],
            "payloads": payloads,
            "orders": orders,
            "api_payloads": api_payloads,
            "pending": runner._trade_svc.pending_order_for("NVDA.US"),
        }


class _HarnessBroker:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str, Decimal, Decimal]] = []
        self.status_results: dict[str, OrderStatusResult] = {}
        self.cancels: list[str] = []
        self._next = 1

    def register_disconnect_hook(self, _hook) -> None:
        pass

    def get_positions(self):
        return []

    def get_today_orders(self):
        return []

    def get_quotes(self, symbols):
        return [
            Quote(
                s, 99.0, 98.9, 99.1,
                datetime.now(timezone.utc).isoformat(),
            )
            for s in symbols
        ]

    def estimate_margin_max_quantity(
        self, _symbol, _side, _price, _currency=None
    ) -> Decimal:
        return Decimal("10")

    def submit_limit_order(self, symbol, side, quantity, price):
        self.submitted.append((symbol, side, quantity, price))
        result = OrderResult(
            f"fm-char-{self._next}", symbol, side, quantity, price,
            "SUBMITTED",
        )
        self._next += 1
        return result

    def get_order_status(self, order_id):
        return self.status_results[order_id]

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return OrderStatusResult(order_id, "CANCELLED")

    def close(self) -> None:
        pass


class TestOffPaperMismatchPersistenceUnchanged:
    """Finding 1: OFF / PAPER / MISMATCH persist exactly the base output."""

    @pytest.fixture(autouse=True)
    def _harness(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.harness = _RealPersistHarness(
            str(tmp_path_factory.mktemp("fm-r3-persist")),
        )
        yield
        self.harness.close()

    def test_off_payload_key_set_matches_base_golden(self) -> None:
        result = self.harness.run_submit(
            flag_on=False, paper=False, fp=FP_A,
        )
        assert result["submissions"], "the OFF path must still submit"
        assert len(result["payloads"]) == 1
        payload = result["payloads"][0]
        assert frozenset(payload) == _BASE_ORDER_SUBMITTED_PAYLOAD_KEYS

    def test_paper_payload_key_set_matches_base_golden(self) -> None:
        result = self.harness.run_submit(
            flag_on=True, paper=True, fp=FP_A,
        )
        assert result["submissions"]
        payload = result["payloads"][0]
        assert frozenset(payload) == _BASE_ORDER_SUBMITTED_PAYLOAD_KEYS

    def test_mismatch_payload_key_set_matches_base_golden(self) -> None:
        result = self.harness.run_submit(
            flag_on=True, paper=False, fp=FP_B,
        )
        assert result["submissions"]
        payload = result["payloads"][0]
        assert frozenset(payload) == _BASE_ORDER_SUBMITTED_PAYLOAD_KEYS

    def test_no_marker_in_any_durable_output_for_off_paper_mismatch(
        self,
    ) -> None:
        for flag_on, paper, fp in (
            (False, False, FP_A),
            (True, True, FP_A),
            (True, False, FP_B),
        ):
            result = self.harness.run_submit(
                flag_on=flag_on, paper=paper, fp=fp,
            )
            blobs = (
                [json.dumps(p) for p in result["payloads"]]
                + [json.dumps(p) for p in result["api_payloads"]]
                + [json.dumps(o) for o in result["orders"]]
            )
            for blob in blobs:
                assert "execution_initiator" not in blob
                assert "funded_margin" not in blob

    def test_effective_exception_persists_the_small_evidence_block_only(
        self,
    ) -> None:
        result = self.harness.run_submit(
            flag_on=True, paper=False, fp=FP_A,
        )
        assert result["submissions"]
        payload = result["payloads"][0]
        # The ONLY new key is the explicit evidence block; the initiator
        # marker still never appears.
        new_keys = (
            frozenset(payload) - _BASE_ORDER_SUBMITTED_PAYLOAD_KEYS
        )
        assert new_keys == {"funded_margin"}
        evidence = payload["funded_margin"]
        assert evidence["applied"] is True
        assert "execution_initiator" not in json.dumps(payload)
        # The pending order carries the frozen verdict.
        pending = result["pending"]
        assert pending is not None
        assert pending.funded_margin_entry is True


class _FakeMonotonicClock3:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestExceptionFlagSurvivesRestart:
    """Finding 2a: submit → NEW runner over the same DB recovers the flag.

    Real submit through the harness (effective exception) → a fresh
    AppRunner loads pending orders from the same DB → the recovered
    pending carries funded_margin_entry=True → the round-2 bounded
    cancel-retry path engages: first timeout cancel fails, later
    reconciles retry, cap exhaustion raises PENDING_ENTRY_UNCONFIRMED.
    """

    @pytest.fixture(autouse=True)
    def _harness(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.harness = _RealPersistHarness(
            str(tmp_path_factory.mktemp("fm-r3-restart")),
        )
        yield
        self.harness.close()

    def test_restart_recovers_flag_and_drives_bounded_retry(self) -> None:
        submitted = self.harness.run_submit(
            flag_on=True, paper=False, fp=FP_A, bind_identity=True,
        )
        assert submitted["submissions"]
        order_id = submitted["pending"].broker_order_id

        # A NEW runner over the same DB performs the startup recovery.
        recovered = self.harness.recover_runner(
            broker=_RecoveryBroker(order_id, cancel_succeeds=False),
        )
        pending = recovered._trade_svc.pending_order_for("NVDA.US")
        assert pending is not None
        assert pending.funded_margin_entry is True

        # Bounded retry: first timeout cancel fails, later reconciles
        # retry, cap exhaustion raises PENDING_ENTRY_UNCONFIRMED.
        clock = _FakeMonotonicClock3()
        notified: list[tuple[str, str]] = []
        risk_events: list[str] = []
        svc = recovered._trade_svc
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 1
        svc._record_risk_event = risk_events.append
        orig_monotonic = trade_svc_module.time.monotonic
        # Base the fake clock ABOVE the recovered submitted_at (which was
        # captured with the real monotonic during recovery) so the
        # timeout branch engages immediately.
        clock.t = orig_monotonic() + 120.0
        trade_svc_module.time.monotonic = clock
        try:
            svc.reconcile(
                risk=RiskController(),
                notify_risk_event=_record3(notified),
            )
            assert svc.pending_order_for("NVDA.US") is not None
            cancels_after_first = len(
                recovered_broker_cancels(recovered)
            )
            for _ in range(6):
                clock.advance(2)
                svc.reconcile(
                    risk=RiskController(),
                    notify_risk_event=_record3(notified),
                )
        finally:
            trade_svc_module.time.monotonic = orig_monotonic

        assert any(
            topic == "PENDING_ENTRY_UNCONFIRMED" for topic, _ in notified
        )
        assert any("manual" in r.lower() for r in risk_events)
        assert svc.pending_order_for("NVDA.US") is not None

    def test_off_order_recovers_without_the_flag(self) -> None:
        submitted = self.harness.run_submit(
            flag_on=False, paper=False, fp=FP_A, bind_identity=True,
        )
        assert submitted["submissions"]
        recovered = self.harness.recover_runner(
            broker=_RecoveryBroker(
                submitted["pending"].broker_order_id,
                cancel_succeeds=True,
            ),
        )
        pending = recovered._trade_svc.pending_order_for("NVDA.US")
        assert pending is not None
        # OFF orders recover exactly as today: no flag, no retry branch.
        assert pending.funded_margin_entry is False
        clock = _FakeMonotonicClock3()
        notified: list[tuple[str, str]] = []
        svc = recovered._trade_svc
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 1
        svc._record_risk_event = lambda *a: None
        orig_monotonic = trade_svc_module.time.monotonic
        clock.t = orig_monotonic() + 120.0
        trade_svc_module.time.monotonic = clock
        try:
            svc.reconcile(
                risk=RiskController(),
                notify_risk_event=_record3(notified),
            )
        finally:
            trade_svc_module.time.monotonic = orig_monotonic
        # Single cancel (the legacy one-shot timeout path); the broker
        # reported CANCELLED so the pending cleared, with the LEGACY
        # timeout notification only — never PENDING_ENTRY_UNCONFIRMED
        # and never a second cancel attempt.
        assert svc.pending_order_for("NVDA.US") is None
        assert len(runner_cancels(recovered)) == 1
        assert [
            topic for topic, _m in notified
        ] == ["ORDER_TIMEOUT"]


class _RecoveryBroker(_HarnessBroker):
    """Broker over the recovered order: configurable cancel behaviour."""

    def __init__(self, order_id: str, *, cancel_succeeds: bool) -> None:
        super().__init__()
        self.order_id = order_id
        self.cancel_succeeds = cancel_succeeds
        self.status_results[order_id] = OrderStatusResult(
            order_id, "SUBMITTED",
        )

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        if not self.cancel_succeeds:
            raise RuntimeError("cancel down")
        return OrderStatusResult(order_id, "CANCELLED")


def recovered_broker_cancels(runner: AppRunner) -> list[str]:
    return list(runner.broker.cancels)


def runner_cancels(runner: AppRunner) -> list[str]:
    return list(runner.broker.cancels)


class TestExceptionFlagSurvivesPersistenceFailure:
    """Finding 2b: the submission-record-failure path keeps the flag.

    The REAL submit path runs with the order-record persistence forced
    to fail (OrderPersistenceError) — the order goes through
    ``_recover_from_missing_order_record`` — and the recovered pending
    must still carry the frozen verdict so reconcile retries cancels
    instead of falling back to the one-shot latch.
    """

    @pytest.fixture(autouse=True)
    def _harness(
        self, tmp_path_factory: pytest.TempPathFactory,
    ) -> Generator[None, None, None]:
        self.harness = _RealPersistHarness(
            str(tmp_path_factory.mktemp("fm-r3-persistfail")),
        )
        yield
        self.harness.close()

    def test_persistence_failure_keeps_flag_and_retries(self) -> None:
        from app.services.trade_execution_service import (
            OrderPersistenceError,
        )

        captured: dict[str, object] = {}

        def failing_record_order(*args, **kwargs):
            captured["calls"] = int(captured.get("calls", 0)) + 1
            raise OrderPersistenceError("record failed (test)")

        # Keep the order LIVE after the recovery cancel attempt so the
        # pending survives for the reconcile-driven retry assertions.
        broker = self.harness.install_recording_broker()

        def live_cancel(order_id):
            broker.cancels.append(order_id)
            return OrderStatusResult(order_id, "SUBMITTED")

        broker.cancel_order = live_cancel
        broker.status_results = {}
        runner = self.harness.build_armed_runner(broker)
        # Force the persistence failure on the REAL submit path.
        runner._trade_svc._record_order = failing_record_order

        self.harness.drive_quote(runner)

        pending = runner._trade_svc.pending_order_for("NVDA.US")
        assert pending is not None
        assert pending.funded_margin_entry is True
        assert int(captured.get("calls", 0)) >= 1

        # Reconcile keeps the flag: the bounded retry branch engages.
        clock = _FakeMonotonicClock3()
        notified: list[tuple[str, str]] = []
        svc = runner._trade_svc
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 1
        svc._record_risk_event = lambda *a: None
        orig_monotonic = trade_svc_module.time.monotonic
        clock.t = orig_monotonic() + 120.0
        trade_svc_module.time.monotonic = clock
        try:
            svc.reconcile(
                risk=RiskController(),
                notify_risk_event=_record3(notified),
            )
            first = len(broker.cancels)
            assert first >= 1
            clock.advance(2)
            svc.reconcile(
                risk=RiskController(),
                notify_risk_event=_record3(notified),
            )
            assert len(broker.cancels) == first + 1
        finally:
            trade_svc_module.time.monotonic = orig_monotonic


def _record3(notified: list[tuple[str, str]]):
    def _notify(topic: str, message: str) -> None:
        notified.append((topic, message))

    return _notify
