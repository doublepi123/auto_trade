"""Re-entrant nested sessions must not be opened while a caller holds one.

P0 incident 2026-09-03: the backend went UNHEALTHY and its API unresponsive
for ~65 minutes behind 1768 x ``sqlalchemy.exc.TimeoutError: QueuePool limit
of size 5 overflow 10 reached``. A py-spy dump taken during the outage showed
all 15 pooled connections checked out while only three threads were inside
SQL -- and all three were parked in ``queue.py:201`` waiting for a connection
that could never arrive. Nothing was executing a query: it was a deadlock, not
saturation.

The shape that produces it is a caller holding an outer ``Session`` and then
opening a SECOND one while still holding the first. ``_reconcile_runtime_positions``
holds ``with self._db_session() as db`` and reaches
``_latch_durable_fill_reconciliation_failure``, which receives that open ``db``
and then called two helpers that each opened their own session --
``_record_risk_event`` and ``_persist_risk_pause_best_effort``. That is +2
nested connections per iteration of a 5-second loop, precisely during the
failure storm when every other thread is also retrying.

Both helpers therefore take an optional ``db``: given one they reuse it and
open nothing; omitted they behave exactly as before. The five guarded operator
surfaces added in commit ``fe81e79b`` -- risk event, notification, runtime
persist, last action, broadcast -- must all still fire, because a halt nobody
is told about is the original incident.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app import database
from app import runner as runner_module
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.runner import AppRunner

database.init_db()


SYMBOL = "NESTEDSESSION.US"
EVENT_TYPE = "DURABLE_FILL_RECONCILIATION_FAILED"


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

    The caller's own session is created directly from ``database.SessionLocal``
    and is deliberately NOT counted: the whole point is that the runner must
    reuse it instead of checking out a second connection.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened = 0
        real_factory = runner_module.SessionLocal

        def counting_factory(*args: object, **kwargs: object) -> Session:
            self.opened += 1
            return real_factory(*args, **kwargs)

        monkeypatch.setattr(runner_module, "SessionLocal", counting_factory)


def _clean_rows() -> None:
    from app.models import RiskEvent, RuntimeState, RuntimeStateSnapshot, TradeEvent

    with database.SessionLocal() as db:
        db.query(TradeEvent).filter(
            TradeEvent.event_type.in_([EVENT_TYPE, "RISK_PAUSED"]),
            TradeEvent.message.like("%NESTEDSESSION%"),
        ).delete(synchronize_session=False)
        db.query(RiskEvent).filter(
            RiskEvent.reason.like("%NESTEDSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeStateSnapshot).filter(
            RuntimeStateSnapshot.symbol.like("NESTEDSESSION%")
        ).delete(synchronize_session=False)
        db.query(RuntimeState).filter(
            RuntimeState.symbol.like("NESTEDSESSION%")
        ).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def _isolate_rows():
    _clean_rows()
    yield
    _clean_rows()


def _runner(notifier: _RecordingNotifier) -> AppRunner:
    runner = AppRunner()
    runner.engine.params = StrategyParams(
        symbol=SYMBOL,
        market="US",
        buy_low=100.0,
        sell_high=110.0,
    )
    runner.engine.last_price = 105.0
    # The runner only ever calls ``notify_risk_event`` here; the fake
    # implements exactly that surface, per the house style of inline ``_Fake``
    # doubles over MagicMock.
    runner.notifier = cast(MultiChannelNotifier, notifier)
    return runner


def _risk_event_types(reason_fragment: str, *, after_id: int = 0) -> list[str]:
    """Risk-event types matching ``reason_fragment``, newer than ``after_id``.

    The latch builds a reason that names no symbol, and other modules sharing
    this database write their own ``POSITION_RECONCILIATION_UNCERTAIN`` rows,
    so the id watermark is what keeps the assertion about THIS call.
    """
    from app.models import RiskEvent

    with database.SessionLocal() as db:
        return [
            str(row.event_type)
            for row in db.query(RiskEvent)
            .filter(
                RiskEvent.reason.like(f"%{reason_fragment}%"),
                RiskEvent.id > after_id,
            )
            .order_by(RiskEvent.id)
            .all()
        ]


def _max_risk_event_id() -> int:
    from app.models import RiskEvent

    with database.SessionLocal() as db:
        latest = db.query(RiskEvent).order_by(RiskEvent.id.desc()).first()
        return 0 if latest is None else int(latest.id)


def _runtime_state_paused() -> bool:
    from app.models import RuntimeState

    with database.SessionLocal() as db:
        row = (
            db.query(RuntimeState)
            .filter(RuntimeState.symbol == SYMBOL)
            .first()
        )
        return bool(row is not None and row.paused)


def test_record_risk_event_reuses_a_supplied_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given an open session, no second connection may be checked out."""
    runner = _runner(_RecordingNotifier())
    counter = _SessionCounter(monkeypatch)
    reason = f"NESTEDSESSION reuse probe for {SYMBOL}"

    with database.SessionLocal() as db:
        runner._record_risk_event(
            reason,
            event_type=EVENT_TYPE,
            db=db,
        )

    assert counter.opened == 0
    assert _risk_event_types("NESTEDSESSION reuse probe") == [EVENT_TYPE]


def test_record_risk_event_still_owns_a_session_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other call site passes no session and must keep working."""
    runner = _runner(_RecordingNotifier())
    counter = _SessionCounter(monkeypatch)
    reason = f"NESTEDSESSION owned-session probe for {SYMBOL}"

    runner._record_risk_event(reason, event_type=EVENT_TYPE)

    assert counter.opened == 1
    assert _risk_event_types("NESTEDSESSION owned-session probe") == [EVENT_TYPE]


def test_persist_risk_pause_reuses_a_supplied_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(_RecordingNotifier())
    runner.risk.pause(
        f"POSITION_RECONCILIATION_UNCERTAIN: NESTEDSESSION persist probe",
        auto_resumable=False,
    )
    counter = _SessionCounter(monkeypatch)

    with database.SessionLocal() as db:
        runner._persist_risk_pause_best_effort(db=db)

    assert counter.opened == 0
    assert _runtime_state_paused() is True


def test_persist_risk_pause_still_owns_a_session_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner(_RecordingNotifier())
    runner.risk.pause(
        "POSITION_RECONCILIATION_UNCERTAIN: NESTEDSESSION owned persist probe",
        auto_resumable=False,
    )
    counter = _SessionCounter(monkeypatch)

    runner._persist_risk_pause_best_effort()

    assert counter.opened == 1
    assert _runtime_state_paused() is True


def test_latch_opens_no_nested_session_and_still_fires_every_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 5-second reconcile loop must not add connections to latch a halt.

    This exercises the REAL helpers -- no stubbing of the surfaces under test
    -- so it proves both halves at once: zero additional sessions, and all
    five operator signals from ``fe81e79b`` still land.
    """
    notifier = _RecordingNotifier()
    runner = _runner(notifier)
    broadcasts: list[bool] = []
    monkeypatch.setattr(
        runner,
        "_broadcast_status",
        lambda: broadcasts.append(True),
    )
    counter = _SessionCounter(monkeypatch)
    watermark = _max_risk_event_id()

    with database.SessionLocal() as db:
        result = runner._latch_durable_fill_reconciliation_failure(
            db,
            {SYMBOL},
            source="test_nested_session_latch",
            error=RuntimeError("NESTEDSESSION durable fills unprovable"),
        )

    effective_reason = runner.risk.pause_reason
    assert counter.opened == 0, (
        f"latch opened {counter.opened} nested session(s) while the caller "
        "already held one"
    )
    assert result == []
    assert runner.risk.paused is True
    # 1. risk event
    assert _risk_event_types(
        "POSITION_RECONCILIATION_UNCERTAIN",
        after_id=watermark,
    ) == [EVENT_TYPE]
    # 2. notification
    assert notifier.events == [(EVENT_TYPE, effective_reason)]
    # 3. runtime state persisted
    assert _runtime_state_paused() is True
    # 4. last action
    assert runner.last_action_message == effective_reason
    # 5. broadcast
    assert broadcasts == [True]


def test_reused_session_stays_usable_after_a_failed_risk_event_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write must not hand the caller a poisoned transaction.

    ``_reconcile_runtime_positions`` keeps using its session after the latch
    returns (``_state_svc.persist``, ``_apply_reconciliation_outcome``). If a
    reused-session write left the transaction in a failed state, the recovery
    path would fail too -- turning one lost row into a second silent halt.
    """
    runner = _runner(_RecordingNotifier())

    def exploding_record_trade_event(_db: Any, **_kwargs: object) -> Any:
        raise RuntimeError("(sqlite3.OperationalError) database is locked")

    monkeypatch.setattr(
        runner_module,
        "record_trade_event",
        exploding_record_trade_event,
    )

    with database.SessionLocal() as db:
        with pytest.raises(RuntimeError):
            runner._record_risk_event(
                f"NESTEDSESSION poisoned-session probe for {SYMBOL}",
                event_type=EVENT_TYPE,
                db=db,
            )
        monkeypatch.undo()
        # The caller must still be able to write through the same session.
        runner_module.record_trade_event(
            db,
            event_type=EVENT_TYPE,
            status="ERROR",
            message=f"NESTEDSESSION recovery write for {SYMBOL}",
            payload={"source": "test"},
        )
        db.commit()

    from app.models import TradeEvent

    with database.SessionLocal() as db:
        messages = [
            str(row.message)
            for row in db.query(TradeEvent)
            .filter(TradeEvent.message.like("%NESTEDSESSION recovery write%"))
            .all()
        ]
    assert messages == [f"NESTEDSESSION recovery write for {SYMBOL}"]


def test_latch_records_the_true_risk_event_type_in_the_trade_event_payload() -> None:
    """FIX C: the durable-fill cause must be recoverable from ``trade_events``.

    Live evidence from the 2026-09-03 incident: ``risk_events`` holds
    ``DURABLE_FILL_RECONCILIATION_FAILED`` rows at 11:07 and 11:15 while
    ``trade_events`` holds ZERO -- it shows ``RISK_PAUSED`` instead, because
    ``_record_risk_event`` hardcoded that type for everything except
    ``DRAWDOWN_LIMIT``.

    ``RISK_PAUSED`` is load-bearing and stays: ``review_service.has_risk_pause``,
    the ``event_list_service`` severity map, ``labels.ts``, Review.vue,
    DecisionTimeline.vue and Dashboard.vue all key on it. So the true type is
    recorded inside the existing payload instead, which makes the cause
    queryable without moving a single consumer.
    """
    from app.models import TradeEvent
    from app.services.trade_event_service import decode_event_payload

    runner = _runner(_RecordingNotifier())
    reason = f"NESTEDSESSION payload probe for {SYMBOL}"

    with database.SessionLocal() as db:
        runner._record_risk_event(reason, event_type=EVENT_TYPE, db=db)

    with database.SessionLocal() as db:
        rows = (
            db.query(TradeEvent)
            .filter(TradeEvent.message == reason)
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert str(row.event_type) == "RISK_PAUSED"
    payload = decode_event_payload(row.payload_json)
    assert payload["source"] == "risk_controller"
    assert payload["risk_event_type"] == EVENT_TYPE


def test_drawdown_limit_keeps_its_own_trade_event_type() -> None:
    """``DRAWDOWN_LIMIT`` already had a dedicated type; it must not regress."""
    from app.models import TradeEvent
    from app.services.trade_event_service import decode_event_payload

    runner = _runner(_RecordingNotifier())
    reason = f"NESTEDSESSION drawdown probe for {SYMBOL}"

    with database.SessionLocal() as db:
        runner._record_risk_event(reason, event_type="DRAWDOWN_LIMIT", db=db)

    with database.SessionLocal() as db:
        row = (
            db.query(TradeEvent)
            .filter(TradeEvent.message == reason)
            .one()
        )
        payload = decode_event_payload(row.payload_json)
        assert str(row.event_type) == "DRAWDOWN_LIMIT"
        assert payload["risk_event_type"] == "DRAWDOWN_LIMIT"


def test_generic_risk_rejection_still_reports_risk_paused() -> None:
    """The default path is unchanged for every existing caller."""
    from app.models import TradeEvent
    from app.services.trade_event_service import decode_event_payload

    runner = _runner(_RecordingNotifier())
    reason = f"NESTEDSESSION default probe for {SYMBOL}"

    with database.SessionLocal() as db:
        runner._record_risk_event(reason, db=db)

    with database.SessionLocal() as db:
        row = (
            db.query(TradeEvent)
            .filter(TradeEvent.message == reason)
            .one()
        )
        payload = decode_event_payload(row.payload_json)
        assert str(row.event_type) == "RISK_PAUSED"
        assert payload["risk_event_type"] == "RISK_REJECTION"


def test_helper_signatures_expose_an_optional_session() -> None:
    """Pin the contract: ``db`` is optional, so no existing caller changes."""
    import inspect

    for method in (
        AppRunner._record_risk_event,
        AppRunner._persist_risk_pause_best_effort,
    ):
        parameter = inspect.signature(method).parameters["db"]
        assert parameter.default is None
        assert parameter.annotation == "Session | None"

    latch_source = inspect.getsource(
        AppRunner._latch_durable_fill_reconciliation_failure
    )
    assert "db=db" in latch_source
