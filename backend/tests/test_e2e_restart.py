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
    """Reset DB tables for a fresh test state.

    We drop+recreate on entry so each test starts with empty tables, and
    intentionally do **not** drop on teardown — sibling test modules rely
    on the schema being present (conftest shares a single per-PID DB).
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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

        # Tracked entries must be restored in the trade service.
        snapshot = runner._trade_svc.snapshot_tracked_entries()
        assert "AAPL.US" in snapshot
        assert snapshot["AAPL.US"][0] == Decimal("100.0")
        assert snapshot["AAPL.US"][1] == Decimal("15000.0")

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
        finally:
            db.close()


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
            db.commit()
        finally:
            db.close()

        fake_broker = _FakeBroker(
            order_status_response=SimpleNamespace(
                broker_order_id="order-live-1",
                status="SUBMITTED",
                executed_quantity=Decimal("0"),
                executed_price=Decimal("0"),
            )
        )
        _install_fake_broker(monkeypatch, fake_broker)

        runner = get_runner()
        runner._initialize_runner()

        # The startup pause path should have activated.
        assert runner.risk.paused is True
        assert "unresolved live order" in runner.risk.pause_reason.lower()
        assert "order-live-1" in runner.risk.pause_reason

        # The risk state itself is the source of truth for the unresolved
        # live order guard: the guard only invokes ``risk.pause()`` and does
        # not emit a TradeEvent. The audit + TradeEvent trail is written by
        # the manual ``/api/control/start`` and ``/api/control/stop`` flows
        # which set the runtime_state row used by the dashboard.
        db = SessionLocal()
        try:
            from app.models import RuntimeState
            runtime = (
                db.query(RuntimeState).order_by(RuntimeState.id.desc()).first()
            )
            # Runtime state may be uninitialized until the next /api/control
            # call — that's expected for a fresh restart. The key assertion
            # is the in-memory risk state captured above.
            assert runner.risk.pause_reason
        finally:
            db.close()


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

            time.sleep(5.5)

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

            time.sleep(5.5)

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
        assert runner._trade_svc.pending_order_for("AAPL.US").broker_order_id == "order-pending-aapl"
        assert runner._trade_svc.pending_order_for("NVDA.US") is None
        assert set(runner._symbol_runtimes) == {"AAPL.US", "NVDA.US"}
        assert runner._symbol_runtimes["NVDA.US"].engine.state.value == "long"
        tracked = runner._trade_svc.snapshot_tracked_entries()
        assert tracked["NVDA.US"] == (Decimal("2.0"), Decimal("440.0"))
