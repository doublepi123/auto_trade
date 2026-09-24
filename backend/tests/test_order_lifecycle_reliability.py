from __future__ import annotations

import os
import tempfile
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

_DB_PATH = os.path.join(
    tempfile.gettempdir(),
    f"auto_trade_order_lifecycle_reliability_{os.getpid()}.db",
)
_DB_URL = f"sqlite:///{_DB_PATH}"
if os.environ.get("AUTO_TRADE_DATABASE_URL") != _DB_URL:
    for _path in (_DB_PATH, f"{_DB_PATH}-wal", f"{_DB_PATH}-shm"):
        if os.path.exists(_path):
            os.remove(_path)
    os.environ["AUTO_TRADE_DATABASE_URL"] = _DB_URL

from app import database
from app.api import strategy as strategy_api
from app.core.broker import BrokerGateway, OrderResult, OrderStatusResult
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.main import app
from app.models import OrderRecord, OrderTerminalCallback, TrackedEntry
from app.runner import AppRunner


class _FilledBroker(BrokerGateway):
    def __init__(self) -> None:
        pass

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(
            broker_order_id=order_id,
            status="FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("100"),
        )


class _RecordingNotifier(MultiChannelNotifier):
    def __init__(self) -> None:
        super().__init__([])
        self.order_ids: list[str] = []

    def notify_order(
        self,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> bool:
        del side, symbol, quantity, price
        self.order_ids.append(order_id)
        return True


def _replay_terminal_fill(
    runner: AppRunner,
    broker: _FilledBroker,
    notifier: _RecordingNotifier,
) -> None:
    runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
    runner._trade_svc._on_fill = lambda _symbol, _action: None
    runner._trade_svc._order_status_poll_interval_seconds = 0.0
    runner._trade_svc._track_pending_order(
        "BUY",
        OrderResult(
            broker_order_id="fill-replay-1",
            symbol="AAPL.US",
            side="BUY",
            quantity=Decimal("2"),
            price=Decimal("100"),
            status="SUBMITTED",
        ),
        broker,
        None,
    )
    runner._trade_svc.reconcile(risk=runner.risk, notifier=notifier)


def test_identical_terminal_fill_replay_persists_one_order_and_side_effect() -> None:
    # Given
    database.init_db()
    with database.SessionLocal() as db:
        db.query(OrderTerminalCallback).delete()
        db.query(TrackedEntry).delete()
        db.query(OrderRecord).delete()
        db.commit()
    broker = _FilledBroker()
    notifier = _RecordingNotifier()
    first_runner = AppRunner()
    first_runner._record_order(
        "fill-replay-1",
        "AAPL.US",
        "BUY",
        2.0,
        100.0,
    )

    # When
    _replay_terminal_fill(first_runner, broker, notifier)
    restarted_runner = AppRunner()
    with database.SessionLocal() as db:
        restarted_runner._load_tracked_entries(db)
    _replay_terminal_fill(restarted_runner, broker, notifier)
    with database.SessionLocal() as db:
        order_count = db.query(OrderRecord).filter_by(
            broker_order_id="fill-replay-1"
        ).count()
        tracked = db.query(TrackedEntry).filter_by(symbol="AAPL.US").one()

    # Then
    assert order_count == 1, "identical fill replay created a duplicate order row"
    assert tracked.quantity == 2.0, "identical fill replay duplicated fill side effects"
    assert notifier.order_ids == ["fill-replay-1"]


class _BlockingOrderBroker:
    """Holds get_today_orders open so a probe can observe an in-flight sync."""

    def __init__(self, *, fail: bool = False) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()
        self._fail = fail

    def get_today_orders(self) -> list[object]:
        self.entered.set()
        assert self.release.wait(5), "test never released the broker call"
        if self._fail:
            raise RuntimeError("snapshot unavailable")
        return []


def _run_sync_while_probing(
    runner: AppRunner,
    broker: _BlockingOrderBroker,
) -> tuple[bool, bool]:
    import threading

    errors: list[BaseException] = []

    def _sync() -> None:
        try:
            runner.sync_today_orders_from_broker(force=True)
        except BaseException as exc:  # surfaced to the test thread below
            errors.append(exc)

    worker = threading.Thread(target=_sync)
    worker.start()
    try:
        assert broker.entered.wait(5), "sync never reached the broker"
        in_flight_diagnostic = runner.diagnostics()["order_sync_succeeded"]
        in_flight_internal = runner._last_order_sync_succeeded
    finally:
        broker.release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == [], f"sync worker raised: {errors!r}"
    return in_flight_diagnostic, in_flight_internal


def test_diagnostics_keep_last_completed_order_sync_result_while_next_sync_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a previous sync that completed successfully
    runner = AppRunner()
    runner._last_order_sync_succeeded = True
    runner._last_completed_order_sync_succeeded = True
    broker = _BlockingOrderBroker()
    runner.broker = broker  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(runner, "_sync_risk_from_order_ledger", lambda: None)

    # When the readiness probe lands while the next sync is still in flight
    in_flight_diagnostic, in_flight_internal = _run_sync_while_probing(runner, broker)

    # Then readiness reports the last completed result, not a transient false
    assert in_flight_diagnostic is True, (
        "readiness flapped to false merely because a sync was in flight"
    )
    # And the trading-path flag keeps its fail-closed in-flight semantics
    assert in_flight_internal is False
    assert runner.diagnostics()["order_sync_succeeded"] is True


def test_diagnostics_report_failure_once_a_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a previous successful sync
    runner = AppRunner()
    runner._last_order_sync_succeeded = True
    runner._last_completed_order_sync_succeeded = True
    broker = _BlockingOrderBroker(fail=True)
    runner.broker = broker  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda db=None: None)
    monkeypatch.setattr(runner, "_record_risk_event", lambda _reason, db=None: None)
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

    # When the next sync fails
    _run_sync_while_probing(runner, broker)

    # Then both views report failure
    assert runner.diagnostics()["order_sync_succeeded"] is False
    assert runner._last_order_sync_succeeded is False


def test_order_sync_persistence_failure_is_reported_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a previous successful sync
    runner = AppRunner()
    runner._last_order_sync_succeeded = True
    runner._last_completed_order_sync_succeeded = True
    broker = _BlockingOrderBroker()
    broker.release.set()
    runner.broker = broker  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(runner, "_persist_risk_pause_best_effort", lambda db=None: None)
    monkeypatch.setattr(runner, "_record_risk_event", lambda _reason, db=None: None)
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)

    def _broken_session():  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(runner, "_db_session", _broken_session)

    # When the broker answers but the reconciliation cannot be persisted
    runner.sync_today_orders_from_broker(force=True)

    # Then neither view may claim success
    assert runner._last_order_sync_succeeded is False
    assert runner.diagnostics()["order_sync_succeeded"] is False


@pytest.mark.parametrize("failing_step", ["latch", "risk_ledger"])
def test_escaping_sync_exception_never_leaves_readiness_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    # Given a previous sync that completed successfully
    runner = AppRunner()
    runner._last_order_sync_succeeded = True
    runner._last_completed_order_sync_succeeded = True
    broker = _BlockingOrderBroker()
    broker.release.set()
    runner.broker = broker  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(runner, "_sync_risk_from_order_ledger", lambda: None)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"{failing_step} failed")

    target = (
        "_latch_live_order_reconciliation"
        if failing_step == "latch"
        else "_sync_risk_from_order_ledger"
    )
    monkeypatch.setattr(runner, target, _boom)

    # When a later step raises out of the sync after the broker answered
    with pytest.raises(RuntimeError, match=f"{failing_step} failed"):
        runner.sync_today_orders_from_broker(force=True)

    # Then readiness must not keep the stale success
    assert runner.diagnostics()["order_sync_succeeded"] is False


def test_throttled_sync_call_leaves_completed_result_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a sync that just completed successfully
    runner = AppRunner()
    runner._last_order_sync_succeeded = True
    runner._last_completed_order_sync_succeeded = True
    runner._last_order_sync_at = __import__("time").monotonic()

    class _MustNotBeCalled:
        def get_today_orders(self) -> list[object]:
            raise AssertionError("throttled sync reached the broker")

    runner.broker = _MustNotBeCalled()  # pyright: ignore[reportAttributeAccessIssue]

    # When a non-forced sync lands inside the interval
    assert runner.sync_today_orders_from_broker() == 0

    # Then neither flag changes
    assert runner._last_order_sync_succeeded is True
    assert runner.diagnostics()["order_sync_succeeded"] is True


def test_diagnostics_surfaces_manual_reconciliation_for_unresolved_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    runner = AppRunner()
    runner._unresolved_live_order_ids = ["unresolved-live-1"]
    runner._last_order_sync_succeeded = False
    monkeypatch.setattr(strategy_api, "get_runner", lambda: runner)

    # When
    response = TestClient(app).get("/api/diagnostics")

    # Then
    assert response.status_code == 200
    assert response.json().get("order_reconciliation_state") == (
        "MANUAL_RECONCILIATION"
    ), "diagnostics hid an unresolved live order from the operator"
