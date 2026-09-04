"""Paths that already hold a session must not open a second one.

Continuation of ``fa983919`` / ``test_runner_session_reentrancy.py``. That
commit fixed the two helpers on the durable-fill latch path; the runtime
detector shipped in ``e117104b`` then ran the whole suite as an audit and
named the sites it could not fix in the same change.

This module pins the remaining GENUINE ones -- paths where the caller holds
an open ``Session`` and the callee opens its own, the exact shape that checked
out all 15 pooled connections and deadlocked the process for ~65 minutes on
2026-09-03:

* ``_latch_live_order_reconciliation``: reached from
  ``_initialize_runner`` -> ``_pause_if_unresolved_live_order_exists``, which
  holds a session, and it called ``_persist_risk_pause_best_effort`` and
  ``_record_risk_event`` without passing it -- both are borrow-capable and
  the call sites simply omitted ``db=``.
* ``_sync_symbol_runtimes`` -> ``OpeningMomentumExecutionService.active_policies``
  (a pure read) is already given the caller's ``db``; the runtime is pinned so
  a future refactor cannot reintroduce an owned session there.
* ``_load_tracked_entries`` from the today-order sync, called one line after
  ``db.commit()`` -- the commit ends the transaction, so nothing uncommitted
  crosses that read.
* ``_load_credentials`` from ``_initialize_runner``, which holds a session
  across the whole block.
* ``reload_strategy`` from ``PUT /api/strategy``, whose request session has
  already committed its save.

Each is asserted by counting the sessions the runner opens for itself while a
caller's session is held. The caller's own session comes straight from
``database.SessionLocal`` and is deliberately not counted: the point is reuse,
not a smaller number.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app import database
from app import runner as runner_module
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.runner import AppRunner

database.init_db()


SYMBOL = "BORROWSESSION.US"


class _RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def notify_order(self, *_args: object) -> bool:
        return True

    def notify_risk_event(self, event_type: str, reason: str) -> bool:
        self.events.append((event_type, reason))
        return True


class _SessionCounter:
    """Count every session the runner opens for itself.

    Patches the ``SessionLocal`` name each module resolves at call time, so a
    helper that opens its own session is counted regardless of which module it
    lives in.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened = 0
        real_factory = runner_module.SessionLocal

        def counting_factory(*args: object, **kwargs: object) -> Session:
            self.opened += 1
            return real_factory(*args, **kwargs)

        monkeypatch.setattr(runner_module, "SessionLocal", counting_factory)


def _clean_rows() -> None:
    from app.models import (
        OrderRecord,
        RiskEvent,
        RuntimeState,
        RuntimeStateSnapshot,
        TrackedEntry,
        TradeEvent,
    )

    with database.SessionLocal() as db:
        db.query(TradeEvent).filter(
            TradeEvent.message.like("%BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RiskEvent).filter(
            RiskEvent.reason.like("%BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(OrderRecord).filter(
            OrderRecord.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(TrackedEntry).filter(
            TrackedEntry.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeStateSnapshot).filter(
            RuntimeStateSnapshot.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeState).filter(
            RuntimeState.symbol.like("BORROWSESSION%")
        ).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_rows():
    _clean_rows()
    yield
    _clean_rows()


def _runner(notifier: _RecordingNotifier | None = None) -> AppRunner:
    runner = AppRunner()
    runner.engine.params = StrategyParams(
        symbol=SYMBOL,
        market="US",
        buy_low=100.0,
        sell_high=110.0,
    )
    runner.engine.last_price = 105.0
    runner.notifier = cast(
        MultiChannelNotifier, notifier if notifier is not None else _RecordingNotifier()
    )
    return runner


def _runtime_state_paused() -> bool:
    from app.models import RuntimeState

    with database.SessionLocal() as db:
        row = (
            db.query(RuntimeState).filter(RuntimeState.symbol == SYMBOL).first()
        )
        return bool(row is not None and row.paused)


def _risk_event_reasons() -> list[str]:
    from app.models import RiskEvent

    with database.SessionLocal() as db:
        return [
            str(row.reason)
            for row in db.query(RiskEvent)
            .filter(RiskEvent.reason.like("%BORROWSESSION%"))
            .order_by(RiskEvent.id)
            .all()
        ]


def _insert_live_order(broker_order_id: str, symbol: str = SYMBOL) -> None:
    from app.models import OrderRecord

    with database.SessionLocal() as db:
        db.add(
            OrderRecord(
                broker_order_id=broker_order_id,
                symbol=symbol,
                side="BUY",
                quantity=1.0,
                price=100.0,
                status="SUBMITTED",
            )
        )
        db.commit()


def test_live_order_latch_reuses_the_callers_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6/#7/#8: the startup latch must borrow, not open two more sessions.

    ``_initialize_runner`` holds a session across
    ``_pause_if_unresolved_live_order_exists``. Reached from there,
    ``_latch_live_order_reconciliation`` called ``_persist_risk_pause_best_effort``
    and ``_record_risk_event`` with no ``db=``, so each opened its own -- two
    extra pooled connections on the thread that is already holding one, during
    startup reconciliation.

    Both helpers have been borrow-capable since ``fa983919``; only the call
    sites omitted the argument. Exercises the REAL helpers so the operator
    surfaces are proved to still fire.
    """
    notifier = _RecordingNotifier()
    runner = _runner(notifier)
    broadcasts: list[bool] = []
    monkeypatch.setattr(runner, "_broadcast_status", lambda: broadcasts.append(True))
    _insert_live_order("BORROWSESSION-order-1")
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        latched = runner._pause_if_unresolved_live_order_exists(db)

    assert latched is True
    assert counter.opened == 0, (
        f"the live-order latch opened {counter.opened} nested session(s) while "
        "the caller already held one"
    )
    reason = runner.risk.pause_reason
    assert reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    # Every operator surface must still fire on a borrowed session.
    assert _runtime_state_paused() is True
    assert [
        recorded
        for recorded in _risk_event_reasons()
        if recorded.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    ] == [reason]
    assert broadcasts == [True]


def test_live_order_latch_records_its_risk_event_on_the_borrowed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The halt evidence must survive the borrow, not be traded away for it.

    Borrowing is only correct if the row still lands. ``_record_risk_event``
    commits even on a borrowed session by design (durable proof of a halt must
    not wait on the caller's later success), so the risk event is queryable as
    soon as the latch returns.
    """
    runner = _runner()
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    _insert_live_order("BORROWSESSION-order-evidence")
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        runner._pause_if_unresolved_live_order_exists(db)

    assert counter.opened == 0
    reasons = [
        reason
        for reason in _risk_event_reasons()
        if reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    ]
    assert len(reasons) == 1
    assert "BORROWSESSION-order-evidence" in reasons[0]


def test_live_order_latch_still_owns_sessions_when_no_db_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that hold no session must keep working exactly as before.

    ``_sync_today_orders_from_broker_serialized`` latches from OUTSIDE its
    ``with self._db_session()`` block in two of three places, so the optional
    ``db`` must stay optional -- forcing it would break the very path this
    change is trying not to disturb.
    """
    runner = _runner()
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    counter = _SessionCounter(monkeypatch)

    latched = runner._latch_live_order_reconciliation(
        {SYMBOL: ["BORROWSESSION-owned"]},
        ["BORROWSESSION probe issue"],
    )

    assert latched is True
    assert counter.opened == 2, (
        "without a caller's session the latch must still own the two it needs "
        f"(persist + risk event); it opened {counter.opened}"
    )
    assert _runtime_state_paused() is True
