# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""P3b review 2: disabling automatic exits must alert, not only write a row."""

from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.broker import OrderResult, OrderStatusResult, Position, Quote
from app.core.risk import RiskController
from app.services import trade_execution_service as execution
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    TradeExecutionService,
)
from tests.test_trade_execution_service_extended_hours_trading import _pin_clock

_POST = datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("America/New_York"))


class _Alert:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify_risk_event(
        self,
        event_type: str,
        reason: str,
        *,
        severity: str | None = None,
    ) -> bool:
        self.calls.append((event_type, reason, severity))
        return True


class _Broker:
    def __init__(self, status: str = "REJECTED") -> None:
        self.status = status
        self.submissions = 0

    def get_positions(self) -> list[Position]:
        return [Position("TSLA.US", "LONG", Decimal("4"), Decimal("200"))]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [Quote("TSLA.US", 210, 209.9, 210.1, _POST.isoformat())]

    def estimate_margin_max_quantity(self, *args, **kwargs) -> Decimal:
        return Decimal("0")

    def submit_limit_order(self, symbol, side, quantity, price, *, outside_rth=None):
        self.submissions += 1
        return OrderResult("rej", symbol, side, quantity, price, self.status)

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, self.status)


def _service(alerts: _Alert, *, record_raises: bool = False) -> TradeExecutionService:
    def record(*_args, **_kwargs) -> None:
        if record_raises:
            raise RuntimeError("db write failed")
        return None

    svc = TradeExecutionService(
        record_order=lambda *_a, **_k: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=record,
        final_order_quote_check=lambda *_a: FinalOrderQuoteCheckResult(
            executable_price=Decimal("209.9"),
            bid=Decimal("209.9"),
            ask=Decimal("210.1"),
            price_floor=Decimal("200"),
        ),
    )
    svc.extended_hours_trading_enabled = True
    svc.extended_hours_protective_exits_enabled = True
    svc.load_tracked_entries({
        "TSLA.US": (Decimal("4"), Decimal("800"), "LONG", _POST),
    })
    return svc


def test_third_exit_rejection_sends_one_takeover_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_clock(monkeypatch, _POST)
    alerts = _Alert()
    svc = _service(alerts)
    broker = _Broker()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock["now"])

    for _ in range(3):
        status = svc.execute(
            "SELL",
            "TSLA.US",
            Quote("TSLA.US", 210, 209.9, 210.1, _POST.isoformat()),
            broker,
            RiskController(),
            alerts,
            "USD",
            market="US",
            trading_session_mode="ANY",
            reduce_only=True,
            allow_loss_exit=True,
            notify_risk_event=alerts.notify_risk_event,
        )
        clock["now"] += 61
        if status is None or getattr(status, "status", None) != "REJECTED":
            raise AssertionError(repr(status))

    assert status is not None
    deadline = threading.Event()
    for _ in range(40):
        takeover = [
            call for call in alerts.calls
            if "manual takeover required" in call[1]
        ]
        if takeover:
            break
        deadline.wait(0.05)
    assert len(takeover) == 1
    _event, reason, severity = takeover[0]
    assert "TSLA.US" in reason
    assert "POST" in reason
    assert "4" in reason
    assert severity == "CRITICAL"


def test_record_risk_event_raising_still_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_clock(monkeypatch, _POST)
    alerts = _Alert()
    svc = _service(alerts, record_raises=True)
    clock = {"now": 5_000.0}
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock["now"])
    for _ in range(3):
        svc.execute(
        "SELL",
        "TSLA.US",
        Quote("TSLA.US", 210, 209.9, 210.1, _POST.isoformat()),
        _Broker(),
        RiskController(),
        alerts,
        "USD",
        market="US",
        trading_session_mode="ANY",
        reduce_only=True,
        allow_loss_exit=True,
        notify_risk_event=alerts.notify_risk_event,
        )
        clock["now"] += 61
    deadline = threading.Event()
    for _ in range(40):
        if any("manual takeover required" in call[1] for call in alerts.calls):
            break
        deadline.wait(0.05)
    assert any("manual takeover required" in call[1] for call in alerts.calls)


def test_pending_rejected_exit_sends_takeover_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.trade_execution_service import _PendingOrder

    _pin_clock(monkeypatch, _POST)
    alerts = _Alert()
    svc = _service(alerts)
    clock = {"now": 8_000.0}
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock["now"])
    now = _POST.date()
    for _ in range(3):
        pending = _PendingOrder(
            broker=_Broker(),
            broker_order_id=f"pending-{clock['now']}",
            symbol="TSLA.US",
            action="SELL",
            quantity=Decimal("4"),
            price=Decimal("210"),
            engine_snapshot=None,
            extended_hours=True,
            extended_hours_key=("TSLA.US", "POST", now, "EXIT"),
        )
        svc._pending_orders_by_id[pending.broker_order_id] = pending
        svc.reconcile(notify_risk_event=alerts.notify_risk_event)
        clock["now"] += 61
    deadline = threading.Event()
    for _ in range(40):
        if any("manual takeover required" in call[1] for call in alerts.calls):
            break
        deadline.wait(0.05)
    assert any("manual takeover required" in call[1] for call in alerts.calls)


def test_blocked_notify_does_not_hold_submission_lock() -> None:
    svc = TradeExecutionService(
        record_order=lambda *_a, **_k: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked(_event: str, _reason: str, **_kwargs: object) -> bool:
        entered.set()
        release.wait(timeout=5)
        return True

    returned = threading.Event()

    def caller() -> None:
        with svc._submission_lock:
            svc._extended_hours_terminal_outcome(
                ("TSLA.US", "POST", _POST.date(), "EXIT"),
                unsupported=True,
                notify_risk_event=blocked,
            )
        returned.set()

    worker = threading.Thread(target=caller)
    worker.start()
    assert entered.wait(timeout=2), "blocked notify never started"
    assert returned.wait(timeout=1), (
        "caller holding the submission lock did not return before the "
        "blocked notification was released"
    )
    # The notify is still inside release.wait. If the caller had waited
    # for it, returned could not be set yet.
    assert not release.is_set()
    held = threading.Event()

    def take_submission_lock() -> None:
        with svc._submission_lock:
            held.set()

    other = threading.Thread(target=take_submission_lock)
    other.start()
    assert held.wait(timeout=1), "submission lock stayed held after the caller returned"
    other.join(timeout=1)
    release.set()
    worker.join(timeout=2)


def test_blocked_alert_does_not_hold_state_lock() -> None:
    svc = TradeExecutionService(
        record_order=lambda *_a, **_k: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked(_event: str, _reason: str, **_kwargs: object) -> bool:
        entered.set()
        release.wait(timeout=5)
        return True

    key = ("TSLA.US", "POST", _POST.date(), "EXIT")
    worker = threading.Thread(
        target=svc._extended_hours_terminal_outcome,
        args=(key,),
        kwargs={"unsupported": True, "notify_risk_event": blocked},
    )
    worker.start()
    assert entered.wait(timeout=2)
    acquired = threading.Event()

    def take_lock() -> None:
        with svc._state_lock:
            svc.tracked_position("TSLA.US")
            acquired.set()

    other = threading.Thread(target=take_lock)
    other.start()
    assert acquired.wait(timeout=1), "state lock stayed held during the alert"
    release.set()
    worker.join(timeout=2)
    other.join(timeout=2)


def test_missing_notifier_does_not_consume_the_dedup_key() -> None:
    svc = TradeExecutionService(
        record_order=lambda *_a, **_k: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
    )
    alerts = _Alert()
    key = ("TSLA.US", "POST", _POST.date(), "EXIT")
    svc._extended_hours_terminal_outcome(key, unsupported=True, notify_risk_event=None)
    svc._extended_hours_terminal_outcome(
        key, unsupported=True, notify_risk_event=alerts.notify_risk_event,
    )
    delivered = threading.Event()
    for _ in range(40):
        if alerts.calls:
            delivered.set()
            break
        if delivered.wait(0.05):
            break
    assert delivered.wait(timeout=2), "async takeover alert was not delivered"
    assert len(alerts.calls) == 1
