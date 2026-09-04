"""The two production sites that own a session must DECLARE that they do.

The 2026-09-03 pool deadlock was cross-module re-entrancy: a caller holding a
``Session`` reached a helper that opened a second one. Commits ``a9e53b8a..``
converted nine such sites to borrow the caller's session. Two did not convert,
and must not:

* ``AuditLogger.record`` opens its own session from an injected factory,
  commits, and swallows its own failures by design so an audit row survives the
  rollback of the transaction being audited. Borrowing would make the audit
  vanish together with the thing it audits.
* ``_record_control_trace`` opens ``SessionLocal()``, commits, and on failure
  rolls back **its own** session so a failed forensic trace cannot poison the
  control handler. A pause whose trace disappears when the pause errors is the
  gap you never want.

Both are still re-entrancy by the pool's definition, so with the process guard
flipped to ``strict=True`` they would raise inside ``env == "test"`` unless
they say so. ``independent_session(reason)`` is that declaration, and these
tests pin BOTH halves of it: the marker is active while the session is open
(so the guard waives the verdict) AND the site still writes durably.

Deliberately asserted at the pool, through a real queue-pooled engine and a
real strict guard, not by grepping for the marker: the contract is "no
violation is reported while this region holds two connections", not "the
string appears in the file".
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.config import settings
from app.core.audit import AuditLogger
from app.database import SessionReentrancyGuard
from app.models import AuditLog, Base, TradeEvent


def _queue_pooled_engine(tmp_path, name: str) -> Engine:
    """A file-backed SQLite engine, which SQLAlchemy serves with ``QueuePool``.

    ``sqlite://`` is served by ``SingletonThreadPool``, which hands the same
    connection back for every session on a thread and therefore emits no second
    checkout at all. Only a queue-pooled engine reproduces the incident, and
    only a queue-pooled engine is what production runs on.
    """
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=10,
        pool_timeout=10,
    )


def _touch(session: Session) -> None:
    """Force the session to actually check a connection out of the pool."""
    session.execute(text("SELECT 1"))


@pytest.fixture
def _test_env(monkeypatch: pytest.MonkeyPatch):
    """Pin ``settings.env`` to ``test`` so a strict guard is in raising mode."""
    monkeypatch.setattr(settings, "env", "test")
    return None


def test_audit_record_declares_its_independence(tmp_path, _test_env) -> None:
    """An audit write from inside a caller's transaction must not violate.

    Every mutating API handler holds the request ``db`` and then calls
    ``AuditLogger.record``, which opens its own session on the same thread.
    That is the incident shape and a strict guard would raise -- so the site
    has to declare why it owns a session instead of borrowing.
    """
    engine = _queue_pooled_engine(tmp_path, "audit_independent.db")
    Base.metadata.create_all(bind=engine)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    audit = AuditLogger(factory)

    caller = factory()
    try:
        _touch(caller)
        audit.record("STRATEGY_UPDATE", severity="INFO", actor_hash="abc")
    finally:
        caller.close()

    try:
        assert guard.violation_count == 0, (
            "AuditLogger.record checked out a second pooled connection without "
            "declaring independent_session; a strict guard raises here and "
            "takes the audited handler down with it"
        )
        with factory() as db:
            assert db.query(AuditLog).count() == 1
    finally:
        engine.dispose()


def test_audit_record_survives_the_audited_transactions_rollback(
    tmp_path,
    _test_env,
) -> None:
    """Independence is the POINT, not a workaround. Prove the row outlives.

    If this site were converted to borrow the caller's session, the audit row
    would be rolled back together with the transaction being audited -- the
    audit of a failed operation would be exactly the audit that disappears.

    The caller holds an open READ transaction across the audit write and only
    then attempts its own write, which is the real shape: SQLite has a single
    writer, so a handler that already held the write lock could not be audited
    by any independent session at all.
    """
    engine = _queue_pooled_engine(tmp_path, "audit_rollback.db")
    Base.metadata.create_all(bind=engine)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    audit = AuditLogger(factory)

    caller = factory()
    try:
        _touch(caller)
        audit.record("AUDITED_FAILURE", severity="WARNING", result="FAILED")
        caller.add(AuditLog(action="CALLER_WRITE", severity="INFO"))
        caller.flush()
        caller.rollback()
    finally:
        caller.close()

    try:
        assert guard.violation_count == 0
        with factory() as db:
            actions = [row.action for row in db.query(AuditLog).all()]
        assert actions == ["AUDITED_FAILURE"], (
            "the audit row must survive the rollback of the transaction it "
            "audits; borrowing the caller's session would delete both"
        )
    finally:
        engine.dispose()


def test_control_trace_declares_its_independence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _test_env,
) -> None:
    """``/api/trade/control/*`` writes its forensic trace on its own session.

    The control handlers hold the request ``db`` across
    ``_record_control_trace``, which opens ``SessionLocal()``. A strict guard
    would raise inside the handler that is trying to record why trading was
    paused.
    """
    from app.api import trade as trade_api

    engine = _queue_pooled_engine(tmp_path, "control_trace.db")
    Base.metadata.create_all(bind=engine)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(trade_api, "SessionLocal", factory)

    caller = factory()
    try:
        _touch(caller)
        trade_api._record_control_trace(
            event_type="CONTROL_PAUSE",
            status="REQUESTED",
            message="runner pause requested",
            payload={"primary_symbol": "AAPL.US"},
        )
    finally:
        caller.close()

    try:
        assert guard.violation_count == 0, (
            "_record_control_trace checked out a second pooled connection "
            "without declaring independent_session; a strict guard raises "
            "inside the control handler it is meant to be recording"
        )
        with factory() as db:
            rows = db.query(TradeEvent).all()
        assert [row.event_type for row in rows] == ["CONTROL_PAUSE"]
    finally:
        engine.dispose()


def test_control_trace_rolls_back_only_its_own_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    _test_env,
) -> None:
    """A failed trace must not poison the caller's in-flight transaction.

    This is why the site owns a session rather than borrowing: on failure it
    calls ``rollback()``. On a borrowed session that rollback would discard
    the control handler's own uncommitted work -- a failed trace turning a
    successful pause into a silent no-op.
    """
    from app.api import trade as trade_api

    engine = _queue_pooled_engine(tmp_path, "control_trace_rollback.db")
    Base.metadata.create_all(bind=engine)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(trade_api, "SessionLocal", factory)

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("trade event write failed")

    monkeypatch.setattr(trade_api, "record_trade_event", _explode)

    caller = factory()
    try:
        _touch(caller)
        caller.add(AuditLog(action="HANDLER_WRITE", severity="INFO"))
        caller.flush()
        trade_api._record_control_trace(
            event_type="CONTROL_PAUSE",
            status="REQUESTED",
            message="runner pause requested",
            payload={"primary_symbol": "AAPL.US"},
        )
        caller.commit()
    finally:
        caller.close()

    try:
        assert guard.violation_count == 0
        with factory() as db:
            actions = [row.action for row in db.query(AuditLog).all()]
        assert actions == ["HANDLER_WRITE"], (
            "the failed trace rolled back the caller's work; the trace must "
            "own its session so its failure stays contained"
        )
    finally:
        engine.dispose()


def test_the_marker_does_not_outlive_either_site(tmp_path, _test_env) -> None:
    """Neither site may leak the exemption onto the calling thread.

    A leaked marker is worse than no marker: every later session on that thread
    would be exempt, and the detector would go quiet exactly when the process
    is already in trouble. Both sites run on API request threads that go on to
    do more database work after the write.
    """
    from app.api import trade as trade_api

    engine = _queue_pooled_engine(tmp_path, "marker_leak.db")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    try:
        AuditLogger(factory).record("STRATEGY_UPDATE", severity="INFO")
        assert database.active_independent_session_reason() is None

        original = trade_api.SessionLocal
        trade_api.SessionLocal = factory  # type: ignore[assignment]
        try:
            trade_api._record_control_trace(
                event_type="CONTROL_STOP",
                status="REQUESTED",
                message="runner stop requested",
                payload={"primary_symbol": "AAPL.US"},
            )
        finally:
            trade_api.SessionLocal = original  # type: ignore[assignment]
        assert database.active_independent_session_reason() is None
    finally:
        engine.dispose()
