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
