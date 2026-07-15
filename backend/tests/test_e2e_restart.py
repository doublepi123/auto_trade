# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""E2E integration tests for runner startup with persisted state (Roadmap #17).

Each scenario exercises the **full** ``AppRunner._initialize_runner`` flow
(plus, where relevant, the ``/api/orders`` endpoint) against the per-PID
SQLite file configured by ``conftest.py`` with a fake broker in place of the
real Longbridge gateway. The goal is to lock the end-to-end behavior of:

* ``_load_tracked_entries`` + ``_reconcile_tracked_entries_with_broker``
* ``_pause_if_unresolved_live_order_exists``
* ``sync_today_orders_from_broker`` (including the ``refresh=true`` path)
* ``reconcile_pending_order`` / ``_handle_pending_order_timeout``
* the public ``start`` / ``stop`` control surface
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App imports (after env setup)
# ---------------------------------------------------------------------------
from app.core.broker import AccountInfo, OrderResult, Position, Quote
from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    AuditLog,
    Base,
    OrderRecord,
    RuntimeState,
    StrategyConfig,
    TrackedEntry,
    TradeEvent,
    WatchlistItem,
)
from app.runner import AppRunner, get_runner
from app.services.trade_execution_service import _PendingOrder


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _RecordingNotifier:
    """Captures risk/order notifications so tests can assert on them."""

    def __init__(self) -> None:
        self.risk_events: list[tuple[str, str, dict[str, object]]] = []
        self.order_notifications: list[tuple[object, ...]] = []

    def notify_order(self, *args: object) -> bool:
        self.order_notifications.append(args)
        return True

    def notify_risk_event(
        self, event_type: str, reason: str, **kwargs: object
    ) -> bool:
        self.risk_events.append((event_type, reason, kwargs))
        return True


class _FakeBroker:
    """In-memory fake of the Longbridge broker gateway surface used by the runner."""

    def __init__(
        self,
        *,
        positions: list[Position] | None = None,
        today_orders: list[object] | None = None,
        order_status_response: object | None = None,
        account_info: AccountInfo | None = None,
        get_positions_exc: BaseException | None = None,
        get_today_orders_exc: BaseException | None = None,
    ) -> None:
        self.positions = list(positions or [])
        self.today_orders = list(today_orders or [])
        self._order_status_response = order_status_response
        self._account_info = account_info
        self._get_positions_exc = get_positions_exc
        self._get_today_orders_exc = get_today_orders_exc
        self.subscribed_to: list[str] = []
        self.cancelled: list[str] = []
        self.closed = False
        self.get_today_orders_calls = 0
        self.get_positions_calls = 0
        self.disconnect_hooks: list[Callable[[str], None]] = []

    def register_disconnect_hook(self, hook: Callable[[str], None]) -> None:
        if hook not in self.disconnect_hooks:
            self.disconnect_hooks.append(hook)

    def simulate_disconnect(self, reason: str) -> None:
        for hook in list(self.disconnect_hooks):
            hook(reason)

    def subscribe_quotes(self, symbol: str, _callback: object) -> None:
        self.subscribed_to.append(symbol)

    def unsubscribe_quotes(self) -> None:
        self.subscribed_to = []

    def close(self) -> None:
        self.closed = True

    def get_positions(self) -> list[Position]:
        self.get_positions_calls += 1
        if self._get_positions_exc is not None:
            raise self._get_positions_exc
        return list(self.positions)

    def get_today_orders(self) -> list[object]:
        self.get_today_orders_calls += 1
        if self._get_today_orders_exc is not None:
            raise self._get_today_orders_exc
        return list(self.today_orders)

    def get_order_status(self, order_id: str) -> object:
        if self._order_status_response is not None:
            return self._order_status_response
        return SimpleNamespace(
            broker_order_id=order_id,
            status="FILLED",
            executed_quantity=Decimal("0"),
            executed_price=Decimal("0"),
        )

    def cancel_order(self, order_id: str) -> object:
        self.cancelled.append(order_id)
        return SimpleNamespace(
            broker_order_id=order_id,
            status="CANCELLED",
            executed_quantity=Decimal("0"),
            executed_price=Decimal("0"),
        )

    def get_account(self) -> AccountInfo:
        if self._account_info is not None:
            return self._account_info
        return AccountInfo(total_assets=Decimal("10000"), cash_balances=[], net_assets=[])

    def get_quote(self, symbol: str) -> Quote:
        # Return a price that sits *inside* the default 150-200 strategy
        # range so the engine never triggers an unsolicited BUY or SELL
        # when the runner loop polls for active refresh.
        return Quote(symbol, 175.0, 174.5, 175.5, "")

    def get_quotes(self, _symbols: list[str]) -> list[Quote]:
        return []

    def estimate_margin_max_quantity(
        self, _symbol: str, _side: str, _price: Decimal, _currency: str | None = None
    ) -> Decimal:
        return Decimal("10")

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal
    ) -> OrderResult:
        return OrderResult("order-1", symbol, side, quantity, price, "FILLED")


def _make_broker_order(
    order_id: str,
    *,
    symbol: str = "AAPL.US",
    side: str = "BUY",
    quantity: float = 10.0,
    price: float = 150.0,
    status: str = "SUBMITTED",
    created_at: datetime | None = None,
    filled_at: datetime | None = None,
    executed_quantity: float | None = None,
    executed_price: float | None = None,
) -> object:
    return SimpleNamespace(
        broker_order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
        filled_at=filled_at,
        executed_quantity=executed_quantity,
        executed_price=executed_price,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _setup_db():
    """Truncate all tables for a fresh test state.

    We truncate on entry so each test starts with empty tables, and
    intentionally do **not** drop on teardown — sibling test modules rely
    on the schema being present (conftest shares a single per-PID DB).

    We also ensure the schema exists via ``create_all``: tests that use the
    ``client`` fixture trigger the FastAPI lifespan (which calls ``init_db``),
    but tests that exercise the runner directly skip the lifespan and would
    otherwise hit "no such table" on the first insert.
    """
    from sqlalchemy import inspect, text

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        existing = set(inspect(conn).get_table_names())
        for table in reversed(Base.metadata.sorted_tables):
            if table.name not in existing:
                continue
            conn.execute(text(f"DELETE FROM {table.name}"))
    yield


@pytest.fixture
def fresh_runner():
    """Provide a fresh AppRunner singleton (and clean up after each test)."""
    import app.api.trade as trade_api
    import app.runner as runner_mod

    old_runner = runner_mod._runner
    old_refresh_lock = trade_api._account_refresh_lock
    old_cache = trade_api._account_snapshot_cache
    runner_mod._runner = None
    trade_api._account_snapshot_cache = None
    trade_api._account_refresh_lock = threading.Lock()
    try:
        yield
    finally:
        current = runner_mod._runner
        if current is not None:
            if current._thread is not None and current._thread.is_alive():
                current.stop()
            elif current._running:
                current._running = False
        runner_mod._runner = old_runner
        trade_api._account_snapshot_cache = old_cache
        trade_api._account_refresh_lock = old_refresh_lock


@pytest.fixture
def client(fresh_runner):
    """TestClient with runner singleton reset."""
    with TestClient(app) as c:
        yield c


def _seed_strategy(**overrides: object) -> StrategyConfig:
    """Create a StrategyConfig row with sensible defaults."""
    db = SessionLocal()
    try:
        config = StrategyConfig(
            symbol="AAPL.US",
            market="US",
            buy_low=150.0,
            sell_high=200.0,
            short_selling=False,
            min_profit_amount=0.0,
            max_daily_loss=5000.0,
            max_consecutive_losses=3,
            fee_rate_us=0.0005,
            fee_rate_hk=0.003,
            min_repricing_pct=0.003,
            llm_action_cooldown_seconds=60,
            trading_session_mode="ANY",
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        db.add(config)
        db.commit()
        db.refresh(config)
        return config
    finally:
        db.close()


def _install_fake_broker(monkeypatch: pytest.MonkeyPatch, broker: _FakeBroker) -> None:
    """Patch ``AppRunner._build_broker`` so credential reload + initial
    construction both install the supplied fake. The signature keeps
    ``(self, audit)`` to satisfy the original ``@staticmethod`` call sites.
    """
    monkeypatch.setattr(
        AppRunner,
        "_build_broker",
        lambda self, audit: broker,
        raising=True,
    )


# ---------------------------------------------------------------------------
# Scenario 1: startup restores tracked entries and records drift
# ---------------------------------------------------------------------------
class TestE2EStartupRestoresTrackedEntries:
    def test_e2e_startup_restores_tracked_entries_and_records_drift(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")

        # Pre-seed a tracked entry that disagrees with broker positions
        # (tracked=100 shares, broker=80 shares -> 20% drift).
        db = SessionLocal()
        try:
            db.add(TrackedEntry(symbol="AAPL.US", quantity=100.0, cost=15000.0))
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            positions=[
                Position(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=Decimal("80"),
                    avg_price=Decimal("150"),
                )
            ]
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        # Broker position is startup truth: the stale local quantity is repaired
        # while its weighted average entry price is preserved.
        snapshot = runner._trade_svc.snapshot_tracked_entries()
        assert "AAPL.US" in snapshot
        assert snapshot["AAPL.US"][0] == Decimal("80.0")
        assert snapshot["AAPL.US"][1] == Decimal("12000.0")

        # Broker should have been queried for positions and the symbol subscribed.
        assert fake_broker.get_positions_calls >= 1
        assert "AAPL.US" in fake_broker.subscribed_to

        # A TRACKED_ENTRY_DRIFT event must have been written to the DB.
        db = SessionLocal()
        try:
            drift_events = (
                db.query(TradeEvent)
                .filter(TradeEvent.event_type == "TRACKED_ENTRY_DRIFT")
                .all()
            )
            assert len(drift_events) == 1
            payload = json.loads(drift_events[0].payload_json)
            assert payload["symbol"] == "AAPL.US"
            assert payload["tracked_quantity"] == 100.0
            assert payload["broker_quantity"] == 80.0
            assert payload["source"] == "startup_tracked_entry_reconcile"
            assert payload["repaired"] is True
        finally:
            db.close()

    def test_recent_terminal_fill_with_missing_symbol_latches_reconciliation(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        malformed_fill = _make_broker_order(
            "terminal-missing-symbol",
            symbol="",
            side="BUY",
            quantity=5.0,
            price=100.0,
            status="FILLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        fake_broker = _FakeBroker(today_orders=[malformed_fill], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:"
        )
        assert "missing symbol" in runner.risk.pause_reason

        pause_reason = runner.risk.pause_reason
        fake_broker.today_orders = []
        runner.risk.pause(
            pause_reason,
            auto_resumable=False,
            paused_at=datetime.now(timezone.utc) - timedelta(seconds=61),
        )
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "first coherent empty broker proof" in error

        runner._unknown_submission_proof_at -= 6
        safe, error = runner.verify_operational_resume()
        assert safe is True
        assert error == ""

    def test_recent_terminal_fill_with_unknown_side_latches_reconciliation(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        malformed_fill = _make_broker_order(
            "terminal-unknown-side",
            symbol="AAPL.US",
            side="UNKNOWN",
            quantity=5.0,
            price=100.0,
            status="FILLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        fake_broker = _FakeBroker(today_orders=[malformed_fill], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:"
        )
        assert "invalid side" in runner.risk.pause_reason

    def test_unknown_submission_resume_requires_grace_and_two_empty_proofs(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        fake_broker = _FakeBroker(today_orders=[], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)
        runner = get_runner()
        runner._initialize_runner()
        pause_reason = (
            "ORDER_SUBMISSION_UNCERTAIN: symbol=AAPL.US action=BUY "
            "broker response was lost"
        )
        runner.risk.pause(pause_reason, auto_resumable=False)

        safe, error = runner.permit_protective_exits_after_verification()
        assert safe is False
        assert "grace period" in error
        assert runner.risk.protective_exit_permitted is False

        runner.risk.pause(
            pause_reason,
            auto_resumable=False,
            paused_at=datetime.now(timezone.utc) - timedelta(seconds=61),
        )
        safe, error = runner.permit_protective_exits_after_verification()
        assert safe is False
        assert "first coherent empty broker proof" in error
        assert runner.risk.protective_exit_permitted is False

        runner._unknown_submission_proof_at -= 6
        safe, error = runner.permit_protective_exits_after_verification()
        assert safe is True
        assert error == ""
        assert runner.risk.paused is True
        assert runner.risk.protective_exit_permitted is True


class TestE2EOfflineFillRecovery:
    def test_restart_rebuilds_tracked_entry_for_fill_completed_while_down(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        submitted_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        filled_at = submitted_at + timedelta(seconds=20)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="offline-entry",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=150.0,
                    status="SUBMITTED",
                    created_at=submitted_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="offline-entry",
                    side="BUY",
                    status="SUBMITTED",
                    message="locally submitted entry",
                    created_at=submitted_at,
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            today_orders=[
                _make_broker_order(
                    "offline-entry",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=150.0,
                    status="FILLED",
                    executed_quantity=10.0,
                    executed_price=151.0,
                    created_at=submitted_at,
                    filled_at=filled_at,
                )
            ],
            positions=[
                Position(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=Decimal("10"),
                    avg_price=Decimal("151"),
                )
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.side == "LONG"
        assert tracked.quantity == Decimal("10.0")
        assert tracked.cost == Decimal("1510.0")
        assert tracked.opened_at is not None
        restored_opened_at = tracked.opened_at
        if restored_opened_at.tzinfo is None:
            restored_opened_at = restored_opened_at.replace(tzinfo=timezone.utc)
        assert restored_opened_at == filled_at
        assert runner.engine.state.value == "long"

        db = SessionLocal()
        try:
            rows = db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").all()
            assert len(rows) == 1
            assert rows[0].quantity == 10.0
            assert rows[0].cost == 1510.0
        finally:
            db.close()

    def test_restart_clears_stale_tracked_and_completes_offline_reduction_fill(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        submitted_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        filled_at = submitted_at + timedelta(seconds=20)
        db = SessionLocal()
        try:
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=10.0,
                    cost=1500.0,
                    opened_at=submitted_at - timedelta(minutes=20),
                    updated_at=submitted_at - timedelta(seconds=1),
                )
            )
            db.add(
                RuntimeState(
                    symbol="AAPL.US",
                    engine_state="flat",
                    execution_state="REDUCING",
                    reduction_action="SELL",
                    reduction_cause="PRICE_STOP",
                    reduction_reason="offline protective exit",
                    reduction_started_at=submitted_at,
                    reduction_trigger_price=148.0,
                )
            )
            db.add(
                OrderRecord(
                    broker_order_id="offline-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10.0,
                    price=148.0,
                    status="SUBMITTED",
                    created_at=submitted_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="offline-exit",
                    side="SELL",
                    status="SUBMITTED",
                    message="locally submitted exit",
                    created_at=submitted_at,
                )
            )
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            today_orders=[
                _make_broker_order(
                    "offline-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10.0,
                    price=148.0,
                    status="FILLED",
                    executed_quantity=10.0,
                    executed_price=147.5,
                    created_at=submitted_at,
                    filled_at=filled_at,
                )
            ],
            positions=[],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner._trade_svc.tracked_position("AAPL.US") is None
        assert runner.execution_state()[0] == "IDLE"
        assert runner.engine.state.value == "flat"
        assert runner.risk.paused is True
        assert runner.risk.pause_reason == "offline protective exit"

        db = SessionLocal()
        try:
            assert db.query(TrackedEntry).filter(TrackedEntry.symbol == "AAPL.US").first() is None
            state = db.query(RuntimeState).filter(RuntimeState.symbol == "AAPL.US").one()
            assert state.execution_state == "IDLE"
        finally:
            db.close()

    def test_restart_does_not_resurrect_position_while_filled_exit_settles(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="settling-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10.0,
                    price=151.0,
                    executed_quantity=10.0,
                    executed_price=151.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="settling-exit",
                    side="SELL",
                    status="FILLED",
                    message="locally submitted exit",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        filled_order = _make_broker_order(
            "settling-exit",
            symbol="AAPL.US",
            side="SELL",
            quantity=10.0,
            price=151.0,
            status="FILLED",
            executed_quantity=10.0,
            executed_price=151.0,
            filled_at=filled_at,
        )
        fake_broker = _FakeBroker(
            today_orders=[filled_order],
            positions=[
                Position(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=Decimal("10"),
                    avg_price=Decimal("150"),
                )
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner._trade_svc.tracked_position("AAPL.US") is None
        assert runner.engine.state.value == "flat"
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )
        expectation = runner._post_fill_expectations["AAPL.US"]
        assert expectation.side == ""
        assert expectation.quantity == Decimal("0")

        fake_broker.positions = []
        db = SessionLocal()
        try:
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="test_exit_settlement_confirmed",
            )
        finally:
            db.close()
        assert "AAPL.US" not in runner._post_fill_expectations
        assert runner._trade_svc.tracked_position("AAPL.US") is None

    def test_restart_blocks_resume_before_filled_exit_consumes_tracked_entry(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        opened_at = filled_at - timedelta(minutes=30)
        db = SessionLocal()
        try:
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=10.0,
                    cost=1500.0,
                    opened_at=opened_at,
                    updated_at=filled_at - timedelta(seconds=1),
                )
            )
            db.add(
                OrderRecord(
                    broker_order_id="pre-cleanup-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=10.0,
                    price=151.0,
                    executed_quantity=10.0,
                    executed_price=151.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="pre-cleanup-exit",
                    side="SELL",
                    status="FILLED",
                    message="locally submitted exit",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        filled_order = _make_broker_order(
            "pre-cleanup-exit",
            symbol="AAPL.US",
            side="SELL",
            quantity=10.0,
            price=151.0,
            status="FILLED",
            executed_quantity=10.0,
            executed_price=151.0,
            filled_at=filled_at,
        )
        fake_broker = _FakeBroker(
            today_orders=[filled_order],
            positions=[
                Position("AAPL.US", "LONG", Decimal("10"), Decimal("150"))
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.engine.state.value == "flat"
        assert runner._post_fill_expectations["AAPL.US"].quantity == Decimal("0")
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "settlement" in error

        fake_broker.positions = []
        safe, error = runner.verify_operational_resume()
        assert safe is True
        assert error == ""
        assert runner._trade_svc.tracked_position("AAPL.US") is None
        assert "AAPL.US" not in runner._post_fill_expectations

    def test_restart_rebuilds_expected_entry_before_tracked_entry_is_written(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="pre-tracked-entry",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=5.0,
                    price=100.0,
                    executed_quantity=5.0,
                    executed_price=100.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="pre-tracked-entry",
                    side="BUY",
                    status="FILLED",
                    message="locally submitted entry",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        filled_order = _make_broker_order(
            "pre-tracked-entry",
            symbol="AAPL.US",
            side="BUY",
            quantity=5.0,
            price=100.0,
            status="FILLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=filled_at,
        )
        fake_broker = _FakeBroker(today_orders=[filled_order], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        expectation = runner._post_fill_expectations["AAPL.US"]
        assert expectation.side == "LONG"
        assert expectation.quantity == Decimal("5")
        assert runner.engine.state.value == "long"
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "settlement" in error

        fake_broker.positions = [
            Position("AAPL.US", "LONG", Decimal("5"), Decimal("100"))
        ]
        safe, error = runner.verify_operational_resume()
        assert safe is True
        assert error == ""
        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("5.0")
        assert tracked.cost == Decimal("500.0")
        assert runner.engine.state.value == "long"

    def test_restart_pauses_recent_fill_when_first_position_lookup_fails(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="entry-before-position-timeout",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=5.0,
                    price=100.0,
                    executed_quantity=5.0,
                    executed_price=100.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="entry-before-position-timeout",
                    side="BUY",
                    status="FILLED",
                    message="locally submitted entry",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        filled_order = _make_broker_order(
            "entry-before-position-timeout",
            symbol="AAPL.US",
            side="BUY",
            quantity=5.0,
            price=100.0,
            status="FILLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=filled_at,
        )

        class FailFirstPositionBroker(_FakeBroker):
            def __init__(self) -> None:
                super().__init__(today_orders=[filled_order], positions=[])
                self.remaining_failures = 1

            def get_positions(self) -> list[Position]:
                if self.remaining_failures > 0:
                    self.remaining_failures -= 1
                    raise RuntimeError("temporary position timeout")
                return super().get_positions()

        fake_broker = FailFirstPositionBroker()
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )
        assert runner._unsettled_position_symbols == {"AAPL.US"}
        assert runner.engine.state.value == "flat"
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "settlement" in error

    def test_restart_recovers_cancelled_partial_entry_before_tracked_write(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        executed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="cancelled-partial-entry",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=100.0,
                    executed_quantity=5.0,
                    executed_price=100.0,
                    status="CANCELLED",
                    created_at=executed_at - timedelta(seconds=2),
                    filled_at=executed_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="cancelled-partial-entry",
                    side="BUY",
                    status="CANCELLED",
                    message="locally submitted partial entry",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="flat"))
            db.commit()
        finally:
            db.close()

        cancelled_order = _make_broker_order(
            "cancelled-partial-entry",
            symbol="AAPL.US",
            side="BUY",
            quantity=10.0,
            price=100.0,
            status="CANCELLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=executed_at,
        )
        fake_broker = _FakeBroker(today_orders=[cancelled_order], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        expectation = runner._post_fill_expectations["AAPL.US"]
        assert expectation.side == "LONG"
        assert expectation.quantity == Decimal("5")
        assert runner.engine.state.value == "long"
        assert runner.risk.paused is True
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "settlement" in error

    def test_old_exit_fill_does_not_mask_new_broker_exposure(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="old-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=5.0,
                    price=110.0,
                    executed_quantity=5.0,
                    executed_price=110.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="old-exit",
                    side="SELL",
                    status="FILLED",
                    message="old local exit",
                )
            )
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            positions=[
                Position("AAPL.US", "LONG", Decimal("4"), Decimal("120"))
            ]
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("4.0")
        assert tracked.cost == Decimal("480.0")
        assert runner.engine.state.value == "long"
        assert "AAPL.US" not in runner._post_fill_expectations
        assert "AAPL.US" not in runner._unsettled_position_symbols

    def test_recent_terminal_transition_recovers_old_partial_fill(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        partial_filled_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        terminal_observed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="old-partial-recent-terminal",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=100.0,
                    executed_quantity=5.0,
                    executed_price=100.0,
                    status="CANCELLED",
                    created_at=partial_filled_at - timedelta(seconds=2),
                    filled_at=partial_filled_at,
                )
            )
            db.add_all(
                [
                    TradeEvent(
                        event_type="ORDER_SUBMITTED",
                        symbol="AAPL.US",
                        broker_order_id="old-partial-recent-terminal",
                        side="BUY",
                        status="SUBMITTED",
                        message="locally submitted entry",
                        created_at=partial_filled_at - timedelta(seconds=2),
                    ),
                    TradeEvent(
                        event_type="ORDER_CANCELLED",
                        symbol="AAPL.US",
                        broker_order_id="old-partial-recent-terminal",
                        side="BUY",
                        status="CANCELLED",
                        message="partial entry became terminal",
                        created_at=terminal_observed_at,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        terminal_order = _make_broker_order(
            "old-partial-recent-terminal",
            symbol="AAPL.US",
            side="BUY",
            quantity=10.0,
            price=100.0,
            status="CANCELLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=partial_filled_at,
        )
        fake_broker = _FakeBroker(today_orders=[terminal_order], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        expectation = runner._post_fill_expectations["AAPL.US"]
        assert expectation.side == "LONG"
        assert expectation.quantity == Decimal("5")
        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "POSITION_RECONCILIATION_UNCERTAIN:"
        )

    def test_recent_broker_discovered_fill_blocks_direction_guessing(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        raw_fill = _make_broker_order(
            "broker-only-fill",
            symbol="AAPL.US",
            side="BUY",
            quantity=5.0,
            price=100.0,
            status="FILLED",
            executed_quantity=5.0,
            executed_price=100.0,
            filled_at=filled_at,
        )
        fake_broker = _FakeBroker(today_orders=[raw_fill], positions=[])
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:"
        )
        assert runner.engine.state.value == "flat"
        assert runner._trade_svc.tracked_position("AAPL.US") is None
        assert runner._unsettled_position_symbols == {"AAPL.US"}
        safe, error = runner.verify_operational_resume()
        assert safe is False
        assert "grace period" in error

    def test_settled_partial_exit_keeps_remaining_tracked_position(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        filled_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db = SessionLocal()
        try:
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=3.0,
                    cost=300.0,
                    opened_at=filled_at - timedelta(minutes=30),
                    updated_at=filled_at + timedelta(seconds=1),
                )
            )
            db.add(
                OrderRecord(
                    broker_order_id="partial-exit",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=2.0,
                    price=101.0,
                    executed_quantity=2.0,
                    executed_price=101.0,
                    status="FILLED",
                    created_at=filled_at - timedelta(seconds=2),
                    filled_at=filled_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="partial-exit",
                    side="SELL",
                    status="FILLED",
                    message="partial local exit",
                )
            )
            db.add(RuntimeState(symbol="AAPL.US", engine_state="long"))
            db.commit()
        finally:
            db.close()

        partial_fill = _make_broker_order(
            "partial-exit",
            symbol="AAPL.US",
            side="SELL",
            quantity=2.0,
            price=101.0,
            status="FILLED",
            executed_quantity=2.0,
            executed_price=101.0,
            filled_at=filled_at,
        )
        fake_broker = _FakeBroker(
            today_orders=[partial_fill],
            positions=[
                Position("AAPL.US", "LONG", Decimal("3"), Decimal("100"))
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3.0")
        assert tracked.cost == Decimal("300.0")
        assert runner.engine.state.value == "long"
        assert "AAPL.US" not in runner._post_fill_expectations
        assert "AAPL.US" not in runner._unsettled_position_symbols


# ---------------------------------------------------------------------------
# Scenario 2: restart pauses when an unresolved live order exists
# ---------------------------------------------------------------------------
class TestE2ERestartPausesOnUnresolvedOrder:
    def test_e2e_restart_pauses_when_unresolved_live_order_exists(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")

        # Pre-seed a SUBMITTED order that the broker still reports as live.
        old_created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="order-live-1",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=150.0,
                    status="SUBMITTED",
                    created_at=old_created_at,
                )
            )
            db.add(
                TradeEvent(
                    event_type="ORDER_SUBMITTED",
                    symbol="AAPL.US",
                    broker_order_id="order-live-1",
                    side="BUY",
                    status="SUBMITTED",
                    message="locally submitted live order",
                    created_at=old_created_at,
                )
            )
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            today_orders=[
                _make_broker_order(
                    "order-live-1",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=150.0,
                    status="SUBMITTED",
                    created_at=old_created_at,
                )
            ],
            order_status_response=SimpleNamespace(
                broker_order_id="order-live-1",
                status="SUBMITTED",
                executed_quantity=Decimal("0"),
                executed_price=Decimal("0"),
            ),
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert "unresolved live order" in runner.risk.pause_reason.lower()
        assert "order-live-1" in runner.risk.pause_reason

        db = SessionLocal()
        try:
            runtime = (
                db.query(RuntimeState).order_by(RuntimeState.id.desc()).first()
            )
            assert runner.risk.pause_reason
        finally:
            db.close()

    def test_broker_only_live_order_never_becomes_semantic_pending(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db = SessionLocal()
        try:
            db.add(
                TrackedEntry(
                    symbol="AAPL.US",
                    side="SHORT",
                    quantity=5.0,
                    cost=500.0,
                    opened_at=opened_at,
                    updated_at=opened_at,
                )
            )
            db.commit()
        finally:
            db.close()
        partial_order = _make_broker_order(
            "broker-only-live",
            symbol="AAPL.US",
            side="BUY",
            quantity=5.0,
            price=100.0,
            status="PARTIAL_FILLED",
            executed_quantity=2.0,
            executed_price=100.0,
        )
        fake_broker = _FakeBroker(
            today_orders=[partial_order],
            positions=[
                Position("AAPL.US", "SHORT", Decimal("5"), Decimal("100"))
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith(
            "ORDER_RECONCILIATION_UNCERTAIN:"
        )
        assert runner._trade_svc.pending_order_for("AAPL.US") is None
        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None and tracked.side == "SHORT"
        assert tracked.quantity == Decimal("5.0")
        assert runner.risk.daily_pnl == 0

        fake_broker.today_orders = [
            _make_broker_order(
                "broker-only-live",
                symbol="AAPL.US",
                side="BUY",
                quantity=5.0,
                price=100.0,
                status="FILLED",
                executed_quantity=5.0,
                executed_price=100.0,
            )
        ]
        runner.sync_today_orders_from_broker(force=True)
        runner._trade_svc.reconcile(runner.risk, runner.notifier)

        assert runner._trade_svc.pending_order_for("AAPL.US") is None
        tracked = runner._trade_svc.tracked_position("AAPL.US")
        assert tracked is not None and tracked.side == "SHORT"
        assert tracked.quantity == Decimal("5.0")
        assert runner.risk.daily_pnl == 0

# ---------------------------------------------------------------------------
# Scenario 3: pending order timeout pauses and emits ORDER_TIMEOUT
# ---------------------------------------------------------------------------
class TestE2EPendingOrderTimeout:
    def test_e2e_pending_order_timeout_pauses_and_records_event(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")

        # Pre-seed a SUBMITTED order in the DB whose created_at is far in the
        # past (the runner treats any live order as "unresolved" at startup,
        # but the timeout path is what we want to exercise here).
        old_created_at = datetime.now(timezone.utc) - timedelta(seconds=99999)
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="order-old-pending",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10.0,
                    price=150.0,
                    status="SUBMITTED",
                    created_at=old_created_at,
                )
            )
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            order_status_response=SimpleNamespace(
                broker_order_id="order-old-pending",
                status="SUBMITTED",
                executed_quantity=Decimal("0"),
                executed_price=Decimal("0"),
            )
        )
        _install_fake_broker(monkeypatch, fake_broker)

        recording_notifier = _RecordingNotifier()
        runner = get_runner()
        # Install the recording notifier so we can capture ORDER_TIMEOUT.
        runner.notifier = recording_notifier

        # Run the full startup path (this will pause the runner because the
        # SUBMITTED OrderRecord is unresolved — we accept that side effect).
        runner._initialize_runner()

        # Inject a pending order in the trade service that has been stuck
        # longer than the configured timeout, so the next reconcile fires
        # the timeout path.
        pending = _PendingOrder(
            broker=fake_broker,
            broker_order_id="order-old-pending",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("10"),
            price=Decimal("150"),
            engine_snapshot=None,
            avg_price=None,
            next_status_check_at=0.0,
            submitted_at=time.monotonic() - 99999,
        )
        with runner._trade_svc._state_lock:
            runner._trade_svc._pending_order = pending
            runner._trade_svc._order_status_timeout_seconds = 1
            runner._trade_svc._order_status_poll_interval_seconds = 0

        # Drive the reconcile pass manually so the test doesn't need to wait
        # for the runner's 5s background loop.
        runner._trade_svc.reconcile(
            runner.risk,
            recording_notifier,
            runner.engine.restore,
            recording_notifier.notify_risk_event,
        )

        # The timeout path must have paused trading and cleared the pending
        # order from in-memory state.
        assert runner.risk.paused is True
        assert "timed out" in runner.risk.pause_reason.lower()
        assert runner._trade_svc.has_pending_order is False

        # The notifier should have received an ORDER_TIMEOUT event.
        timeout_events = [
            e for e in recording_notifier.risk_events if e[0] == "ORDER_TIMEOUT"
        ]
        assert timeout_events, "expected notifier to receive ORDER_TIMEOUT"
        assert "timed out" in timeout_events[0][1].lower()
        assert "order-old-pending" in timeout_events[0][1]

        # The DB should contain a RISK_PAUSED event whose message references
        # the timeout (the "unresolved" message from the startup pause is
        # overwritten by the timeout pause).
        db = SessionLocal()
        try:
            timeout_paused = [
                event
                for event in db.query(TradeEvent)
                .filter(TradeEvent.event_type == "RISK_PAUSED")
                .all()
                if "timed out" in event.message.lower()
            ]
            assert timeout_paused, "expected a RISK_PAUSED event with timeout message"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Scenario 4: GET /api/orders?refresh=true forces broker sync
# ---------------------------------------------------------------------------
class TestE2EOrdersRefreshTriggersBrokerSync:
    def test_e2e_orders_refresh_triggers_broker_sync(
        self, fresh_runner, client, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")

        broker_orders = [
            _make_broker_order(
                "order-r-1",
                status="FILLED",
                quantity=10,
                price=150,
                executed_quantity=10,
                executed_price=150,
            ),
            _make_broker_order(
                "order-r-2", status="SUBMITTED", quantity=5, price=200
            ),
        ]
        fake_broker = _FakeBroker(today_orders=broker_orders)
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        # Note: the runner is *not* started — we want the API's refresh path
        # to be the trigger for the first sync. (Starting would also work
        # because ``_initialize_runner`` calls sync with ``force=True``.)
        runner._initialize_runner()

        # ``_initialize_runner`` already syncs once with ``force=True``, so
        # both broker orders are now in the DB. The refresh API call must
        # then invoke the broker a *second* time (force=True bypasses the
        # order-sync interval cooldown).
        pre_call_count = fake_broker.get_today_orders_calls
        assert pre_call_count >= 1

        resp = client.get(
            "/api/orders",
            params={
                "scope": "today",
                "refresh": "true",
                "page": "1",
                "page_size": "50",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "today"
        assert data["page"] == 1
        assert data["page_size"] == 50

        # The API refresh must have called the broker at least one more time.
        assert fake_broker.get_today_orders_calls > pre_call_count

        # The API response must include both orders.
        order_ids = {item["broker_order_id"] for item in data["items"]}
        assert {"order-r-1", "order-r-2"} <= order_ids

        # The DB must contain both orders.
        db = SessionLocal()
        try:
            rows = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id.in_(["order-r-1", "order-r-2"]))
                .all()
            )
            db_ids = {row.broker_order_id for row in rows}
            assert db_ids == {"order-r-1", "order-r-2"}
            for row in rows:
                assert row.symbol == "AAPL.US"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Scenario 5: control start/stop cycles with no state leak
# ---------------------------------------------------------------------------
class TestE2EControlStartStopNoStateLeak:
    def test_e2e_control_start_stop_no_state_leak(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")

        fake_broker = _FakeBroker()
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        # The runner starts in a stopped state.
        assert runner.is_running is False
        assert runner._thread is None

        for cycle in range(3):
            # Start the runner.
            started = runner.start()
            assert started is True, f"start() returned False on cycle {cycle}"
            assert runner.is_running is True, f"is_running False on cycle {cycle}"
            assert runner._thread is not None
            assert runner._thread.is_alive()
            # Each start re-initializes the runner, which subscribes to the
            # strategy symbol. The cumulative subscription count must be >0.
            assert "AAPL.US" in fake_broker.subscribed_to

            # Stop the runner and verify it transitions to a clean stopped
            # state with the background thread joined.
            runner.stop()
            assert runner.is_running is False, f"is_running True after stop on cycle {cycle}"
            assert runner._thread is None or not runner._thread.is_alive()

        # The fake broker should have been closed at least once (the first
        # _initialize_runner -> _apply_credentials closes the prior broker).
        assert fake_broker.closed is True

        # The trade-svc should not retain any state across cycles — the
        # runner's own state was reloaded from the DB each time and we never
        # seeded tracked entries or pending orders.
        assert runner._trade_svc.snapshot_tracked_entries() == {}
        assert runner._trade_svc.has_pending_order is False


# ---------------------------------------------------------------------------
# Scenario 6: broker disconnect hook triggers audit and resubscribe
# ---------------------------------------------------------------------------
class TestE2EBrokerDisconnectResubscribe:
    def test_broker_disconnect_triggers_resubscribe_within_5s(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        fake_broker = _FakeBroker()
        _install_fake_broker(monkeypatch, fake_broker)

        with TestClient(app) as client:
            response = client.post("/api/control/start")
            assert response.status_code == 200
            runner = get_runner()
            assert runner._quotes_subscribed is True

            fake_broker.simulate_disconnect("test_network_drop")
            assert runner._quotes_subscribed is False

            # Poll for resubscribe instead of fixed sleep to avoid flakiness.
            deadline = time.monotonic() + 10.0
            while not runner._quotes_subscribed and time.monotonic() < deadline:
                time.sleep(0.25)
            assert runner._quotes_subscribed is True

            db = SessionLocal()
            try:
                actions = [row.action for row in db.query(AuditLog).all()]
            finally:
                db.close()
            assert "BROKER_DISCONNECT" in actions

            status = client.get("/api/status").json()
            assert status["paused"] is False
            assert runner._quotes_subscribed is True
            assert fake_broker.subscribed_to == ["AAPL.US"]


class TestE2EMultiSymbolSubscriptions:
    def test_e2e_start_and_disconnect_resubscribe_all_runtime_symbols(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        db = SessionLocal()
        try:
            db.add(WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=False))
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker()
        _install_fake_broker(monkeypatch, fake_broker)

        with TestClient(app) as client:
            response = client.post("/api/control/start")
            assert response.status_code == 200
            runner = get_runner()
            assert runner._quotes_subscribed is True
            assert fake_broker.subscribed_to == ["AAPL.US", "NVDA.US"]

            fake_broker.simulate_disconnect("multi_symbol_drop")
            assert runner._quotes_subscribed is False

            deadline = time.monotonic() + 10.0
            while not runner._quotes_subscribed and time.monotonic() < deadline:
                time.sleep(0.25)

            assert runner._quotes_subscribed is True
            assert fake_broker.subscribed_to == ["AAPL.US", "NVDA.US"]


class TestE2EMultiSymbolPendingRestart:
    def test_e2e_restart_restores_only_live_pending_orders_per_symbol(
        self, fresh_runner, monkeypatch
    ) -> None:
        _seed_strategy(symbol="AAPL.US")
        db = SessionLocal()
        try:
            db.add(WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=False))
            db.add(TrackedEntry(symbol="NVDA.US", quantity=2.0, cost=440.0))
            db.add(
                RuntimeState(
                    symbol="NVDA.US",
                    engine_state="long",
                    last_price=221.0,
                    last_trigger_price=220.5,
                )
            )
            db.add(
                OrderRecord(
                    broker_order_id="order-pending-aapl",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=3.0,
                    price=199.0,
                    status="SUBMITTED",
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
                )
            )
            db.add(
                OrderRecord(
                    broker_order_id="order-filled-nvda",
                    symbol="NVDA.US",
                    side="BUY",
                    quantity=2.0,
                    price=220.0,
                    executed_quantity=2.0,
                    executed_price=220.0,
                    status="FILLED",
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=20),
                    filled_at=datetime.now(timezone.utc) - timedelta(seconds=19),
                )
            )
            db.add_all(
                [
                    TradeEvent(
                        event_type="ORDER_SUBMITTED",
                        symbol="AAPL.US",
                        broker_order_id="order-pending-aapl",
                        side="BUY",
                        status="SUBMITTED",
                        message="locally submitted pending order",
                    ),
                    TradeEvent(
                        event_type="ORDER_SUBMITTED",
                        symbol="NVDA.US",
                        broker_order_id="order-filled-nvda",
                        side="BUY",
                        status="SUBMITTED",
                        message="locally submitted filled order",
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            today_orders=[
                _make_broker_order(
                    "order-pending-aapl",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=3.0,
                    price=199.0,
                    status="SUBMITTED",
                ),
                _make_broker_order(
                    "order-filled-nvda",
                    symbol="NVDA.US",
                    side="BUY",
                    quantity=2.0,
                    price=220.0,
                    status="FILLED",
                    executed_quantity=2.0,
                    executed_price=220.0,
                    filled_at=datetime.now(timezone.utc),
                ),
            ],
            positions=[
                Position(
                    symbol="NVDA.US",
                    side="LONG",
                    quantity=Decimal("2"),
                    avg_price=Decimal("220"),
                )
            ],
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        assert runner.risk.paused is True
        assert "unresolved live order" in runner.risk.pause_reason.lower()
        assert runner._trade_svc.pending_order_for("AAPL.US") is not None
        aapl_pending = runner._trade_svc.pending_order_for("AAPL.US")
        assert aapl_pending is not None
        assert aapl_pending.broker_order_id == "order-pending-aapl"
        assert runner._trade_svc.pending_order_for("NVDA.US") is None
        assert set(runner._symbol_runtimes) == {"AAPL.US", "NVDA.US"}
        assert runner._symbol_runtimes["NVDA.US"].engine.state.value == "long"
        tracked = runner._trade_svc.snapshot_tracked_entries()
        assert tracked["NVDA.US"] == (Decimal("2.0"), Decimal("440.0"))
