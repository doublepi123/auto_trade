"""A thread must never hold two pooled connections at once.

P0 incident 2026-09-03: the backend went unresponsive for ~65 minutes behind
1768 x ``sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
reached``. A py-spy dump taken during the outage showed all 15 pooled
connections checked out while only three threads were inside SQL -- and all
three were parked in ``queue.py:201`` waiting for a connection that could
never arrive. Nothing was executing a query: a deadlock, not saturation.

The shape that produces it is re-entrancy: a caller holding an outer
``Session`` reaches a helper that opens a SECOND one while the first is still
checked out. On the runner's 5-second loop, during a failure storm, that
consumes the pool. Commit ``fa983919`` fixed the two helpers that did it
(``_record_risk_event`` / ``_persist_risk_pause_best_effort`` now take an
optional ``db`` and borrow through ``AppRunner._db_session_or``).

A static audit can prove today's tree is clean, but it cannot stop a runner
method that holds a session from calling into a *service object* that opens
its own ``SessionLocal()`` tomorrow -- a cross-module, dynamic-dispatch shape
whose failure mode is a 65-minute production hang rather than a red test.
:class:`app.database.SessionReentrancyGuard` closes that gap at runtime, from
the pool's own ``checkout`` / ``checkin`` events, where every session is
visible regardless of which module opened it.

Its own safety rule: in ``env == "test"`` it raises so this suite catches the
regression; in prod/dev it may only log a throttled warning. A guard that took
a live trading system down would be strictly worse than the bug it guards.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import (
    SessionReentrancyGuard,
    SessionReentrancyViolation,
)
from app.runner import AppRunner


def _queue_pooled_engine(tmp_path, name: str = "guard.db") -> Engine:
    """A file-backed SQLite engine, which SQLAlchemy serves with ``QueuePool``.

    Deliberately not the in-memory spelling: ``sqlite://`` is served by
    ``SingletonThreadPool``, which hands the SAME connection back for every
    session on a thread and therefore emits no second ``checkout`` at all. Only
    a queue-pooled engine can reproduce the incident, and only a queue-pooled
    engine is what production runs on.
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
    """Pin ``settings.env`` to ``test`` so the guard is in raising mode."""
    monkeypatch.setattr(settings, "env", "test")
    return None


def test_nested_sessions_on_one_thread_raise_in_test_env(tmp_path, _test_env) -> None:
    """The incident shape itself: a second session while the first is held."""
    engine = _queue_pooled_engine(tmp_path)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    try:
        _touch(outer)
        inner = factory()
        try:
            with pytest.raises(SessionReentrancyViolation):
                _touch(inner)
        finally:
            inner.close()
    finally:
        outer.close()
        engine.dispose()

    assert guard.violation_count == 1
    assert guard.last_violation_stack is not None
    assert "test_nested_sessions_on_one_thread_raise_in_test_env" in (
        guard.last_violation_stack
    )


def test_borrowed_session_does_not_trip_guard(tmp_path, _test_env) -> None:
    """``AppRunner._db_session_or`` borrows and opens nothing, so it is clean.

    This is the legitimate exception the guard must never punish: given a
    caller's ``db`` it yields that same object, checking out no second
    connection. It is the fix commit ``fa983919`` shipped, and the guard
    exists to keep it in place.
    """
    engine = _queue_pooled_engine(tmp_path, "borrow.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    try:
        _touch(outer)
        with AppRunner._db_session_or(outer) as borrowed:
            assert borrowed is outer
            _touch(borrowed)
    finally:
        outer.close()
        engine.dispose()

    assert guard.violation_count == 0


def test_sequential_sessions_do_not_trip_guard(tmp_path, _test_env) -> None:
    """Open, close, open again is the normal pattern and must stay silent."""
    engine = _queue_pooled_engine(tmp_path, "sequential.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    for _ in range(5):
        session = factory()
        try:
            _touch(session)
        finally:
            session.close()
    engine.dispose()

    assert guard.violation_count == 0


def test_two_threads_each_holding_one_do_not_trip_each_other(tmp_path, _test_env) -> None:
    """Accounting is per thread; two threads holding one connection each is fine."""
    engine = _queue_pooled_engine(tmp_path, "threads.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    both_checked_out = threading.Barrier(2, timeout=10)
    failures: list[BaseException] = []

    def hold_one() -> None:
        session = factory()
        try:
            _touch(session)
            both_checked_out.wait()
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            failures.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=hold_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    engine.dispose()

    assert failures == []
    assert guard.violation_count == 0


def test_connection_returned_on_another_thread_is_credited_to_its_owner(
    tmp_path,
    _test_env,
) -> None:
    """Cross-thread checkin must not corrupt either thread's count.

    SQLAlchemy fires ``checkin`` on whichever thread closes the session, which
    is not always the one that checked it out. Decrementing the closing thread
    would leak +1 on the owner forever -- every later session on that thread
    would then false-positive -- and drive the closing thread negative, masking
    a real violation there. The owning thread is stamped at checkout and
    credited at checkin, so both counts stay exact and this thread may still
    open a fresh session afterwards without tripping.
    """
    engine = _queue_pooled_engine(tmp_path, "crossthread.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    session = factory()
    _touch(session)
    closer = threading.Thread(target=session.close)
    closer.start()
    closer.join(timeout=10)

    reopened = factory()
    try:
        _touch(reopened)
    finally:
        reopened.close()
        engine.dispose()

    assert guard.violation_count == 0


def test_in_memory_engine_still_works_with_guard_installed(_test_env) -> None:
    """``sqlite://`` uses ``SingletonThreadPool``; installing must not break it.

    Many tests build in-memory engines, and
    ``tests/test_watchlist_quant_v6_reader_import_isolation.py`` boots a whole
    fresh interpreter with ``AUTO_TRADE_DATABASE_URL=sqlite://``. That pool
    hands one connection per thread back repeatedly, so nesting emits no second
    checkout and the guard is simply inert there rather than wrong.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    inner = factory()
    try:
        _touch(outer)
        _touch(inner)
    finally:
        inner.close()
        outer.close()
        engine.dispose()

    assert guard.violation_count == 0


@pytest.mark.parametrize("env", ["prod", "dev"])
def test_guard_never_raises_outside_test_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
) -> None:
    """In prod/dev the guard observes and warns; it must never raise.

    This is the whole reason the guard is allowed near a live trading system.
    The violation is still counted and its stack still captured, so the
    offending path stays identifiable from the logs.
    """
    monkeypatch.setattr(settings, "env", env)
    engine = _queue_pooled_engine(tmp_path, f"{env}.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    inner = factory()
    try:
        _touch(outer)
        _touch(inner)
    finally:
        inner.close()
        outer.close()
        engine.dispose()

    assert guard.violation_count == 1
    assert guard.last_violation_stack is not None


def test_observing_guard_never_raises_even_in_test_env(tmp_path, _test_env) -> None:
    """``strict=False`` downgrades the test-env raise to a counted warning.

    The process engine shipped this way while the audit's thirteen sites were
    being resolved: detection had to be able to land ahead of the fixes, and
    an observing guard still counts and still names the path. Those sites are
    resolved now and the process guard is strict, but the observing mode
    remains part of the class's contract -- a caller installing a guard on a
    secondary engine may legitimately want diagnosis without a verdict.
    """
    engine = _queue_pooled_engine(tmp_path, "observing.db")
    guard = SessionReentrancyGuard(strict=False)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    inner = factory()
    try:
        _touch(outer)
        _touch(inner)
    finally:
        inner.close()
        outer.close()
        engine.dispose()

    assert guard.violation_count == 1
    assert guard.last_violation_stack is not None


def test_process_engine_guard_is_installed_and_strict() -> None:
    """The shipped guard must be attached, and strict.

    Attached, because a detector nobody installed detects nothing. Strict,
    because the thirteen sites the first audit found are now resolved --
    eleven threaded onto the caller's session, two declared through
    ``independent_session`` at the site that owns them. With none left, a new
    unannotated re-entrancy is a regression and must fail CI rather than add
    one more line to a warning log nobody reads.

    Strict changes only what a violation COSTS, and only under
    ``env == "test"``. In prod/dev it is still counted, its stack still
    recorded, and a throttled warning still emitted -- see
    ``test_guard_never_raises_outside_test_env`` and
    ``test_process_engine_guard_cannot_raise_in_prod_or_dev``, which pin that
    a guard near a live trading system can never halt it.
    """
    from app import database

    assert isinstance(database.session_reentrancy_guard, SessionReentrancyGuard)
    assert database.session_reentrancy_guard.strict is True


@pytest.mark.parametrize("env", ["prod", "dev"])
def test_process_engine_guard_cannot_raise_in_prod_or_dev(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
) -> None:
    """The SHIPPED guard object, strict, against a real nested session.

    ``test_guard_never_raises_outside_test_env`` proves the rule for a guard
    this test constructs. This proves it for the one production actually runs:
    the same object, in strict mode, driven through a genuine second checkout
    on one thread with ``settings.env`` pinned to prod and to dev.

    A guard that raised inside a live trading system would be strictly worse
    than the bug it detects -- it would abort an order path mid-flight over a
    diagnostic. Raising stays confined to ``env == "test"``, whatever
    ``strict`` says.
    """
    from app import database

    guard = database.session_reentrancy_guard
    assert guard.strict is True, "this test is only meaningful on a strict guard"

    monkeypatch.setattr(settings, "env", env)
    engine = _queue_pooled_engine(tmp_path, f"shipped_{env}.db")
    guard.install(engine)
    factory = sessionmaker(bind=engine)
    before = guard.violation_count

    outer = factory()
    inner = factory()
    try:
        _touch(outer)
        # Must NOT raise: no pytest.raises, no try/except. If the shipped
        # guard ever raises here, this line fails the test directly.
        _touch(inner)
    finally:
        inner.close()
        outer.close()
        engine.dispose()

    assert guard.violation_count == before + 1, (
        "prod/dev must still COUNT the violation -- observing is not ignoring"
    )
    assert guard.last_violation_stack is not None


@pytest.mark.parametrize(
    "migration_name",
    [
        "_ensure_order_execution_ledger_columns",
        "_ensure_drawdown_columns",
        "_ensure_runtime_state_symbol_columns",
        "_ensure_report_query_indexes",
    ],
)
def test_runtime_migrations_hold_one_connection_at_a_time(
    tmp_path,
    _test_env,
    migration_name: str,
) -> None:
    """Runtime migrations must not query an engine-bound Inspector in ``begin()``.

    These four are a real nesting site the guard found on its first run, not a
    hypothetical. ``inspect(db_engine)`` binds the Inspector to the *engine*,
    so each ``get_columns`` / ``get_table_names`` / ``get_indexes`` call on it
    checks out its own connection; issued from inside ``with
    db_engine.begin() as connection:`` that is two connections held at once by
    one thread -- precisely the 2026-09-03 shape.

    Startup is single-threaded and brief, so this never caused the outage. It
    still has to go: it sits in the one function every process runs before
    serving traffic, and leaving a known instance in place would make the
    guard's own baseline a lie. Reading the schema before opening the
    transaction costs nothing and removes it.
    """
    from app import database
    from app.models import Base

    engine = _queue_pooled_engine(tmp_path, f"{migration_name}.db")
    Base.metadata.create_all(bind=engine)
    guard = SessionReentrancyGuard()
    guard.install(engine)
    migration = getattr(database, migration_name)
    try:
        migration(engine)
        # Second call exercises the already-migrated branches too.
        migration(engine)
    finally:
        engine.dispose()

    assert guard.violation_count == 0


def test_repeated_violations_are_log_throttled(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prod logging goes through ``RepeatedLogThrottle``, not free-form lines.

    The incident produced 1768 identical errors in ~65 minutes. An unthrottled
    warning on a 5-second loop reproduces exactly that flood, so repeats within
    the window are suppressed and counted, and the next emission reports how
    many were held back.
    """
    monkeypatch.setattr(settings, "env", "prod")
    engine = _queue_pooled_engine(tmp_path, "throttle.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    with caplog.at_level("WARNING", logger="auto_trade.database"):
        outer = factory()
        try:
            _touch(outer)
            for _ in range(4):
                inner = factory()
                try:
                    _touch(inner)
                finally:
                    inner.close()
        finally:
            outer.close()
            engine.dispose()

    assert guard.violation_count == 4
    warnings = [
        record for record in caplog.records if "re-entrant" in record.getMessage()
    ]
    assert len(warnings) == 1
