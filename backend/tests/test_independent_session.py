"""A deliberately independent nested session must be declared, not silent.

``SessionReentrancyGuard`` counts a second pooled checkout on one thread as
the 2026-09-03 deadlock shape, and it is right to: with ``QueuePool`` every
``Session`` holds its connection until closed, so a thread needs a second one
only when it has re-entered.

A few sites nest on purpose. ``AuditLogger.record`` owns its session so an
audit row survives the rollback of the thing being audited; coupling audit
durability to the audited transaction would be a product change, not a
refactor. Those sites still trip the detector, and a permanently-warning
detector is one nobody reads.

:func:`app.database.independent_session` is the declaration. It is NOT an
allowlist: there is no path pattern, no module name, no config file. A site
opts itself out for the duration of one ``with`` block and must say why in
code -- a marker with no written justification is exactly the "silence it"
move that lets the next outage through, so a missing reason is a ``TypeError``
and a blank one is a ``ValueError``.

The marker is thread-local by construction. Marking on the runner loop must
never mask a genuine violation on an API request thread, because the two share
the process engine and its single guard.
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
    active_independent_session_reason,
    independent_session,
)


def _queue_pooled_engine(tmp_path, name: str = "independent.db") -> Engine:
    """A file-backed SQLite engine, which SQLAlchemy serves with ``QueuePool``.

    ``sqlite://`` is served by ``SingletonThreadPool``, which hands the same
    connection back for every session on a thread and so emits no second
    ``checkout`` at all. Only a queue-pooled engine reproduces the incident,
    and only a queue-pooled engine is what production runs on.
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


def test_marker_requires_a_reason() -> None:
    """No reason, no exemption. The justification IS the review record."""
    with pytest.raises(TypeError):
        independent_session()  # type: ignore[call-arg]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_reason_is_rejected_eagerly(blank: str) -> None:
    """A whitespace reason is the silence-it move wearing the marker's coat.

    Rejected at the call, not at ``__enter__``: a validation that only fires
    on entry lets ``independent_session("")`` sit in a code path that is never
    exercised by a test and read as justified.
    """
    with pytest.raises(ValueError):
        independent_session(blank)


def test_reason_is_readable_while_the_marker_is_active() -> None:
    """The active reason is observable, so a warning can name it."""
    assert active_independent_session_reason() is None
    with independent_session("audit rows must survive the caller's rollback"):
        assert active_independent_session_reason() == (
            "audit rows must survive the caller's rollback"
        )
    assert active_independent_session_reason() is None


def test_marked_nested_checkout_is_not_counted_as_a_violation(
    tmp_path, _test_env
) -> None:
    """The whole point: a declared independent session does not trip the guard."""
    engine = _queue_pooled_engine(tmp_path, "marked.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    try:
        _touch(outer)
        with independent_session("audit durability must outlive the caller"):
            inner = factory()
            try:
                _touch(inner)
            finally:
                inner.close()
    finally:
        outer.close()
        engine.dispose()

    assert guard.violation_count == 0
    assert guard.last_violation_stack is None


def test_unmarked_nested_checkout_still_violates(tmp_path, _test_env) -> None:
    """Control: the marker must exempt only what it wraps."""
    engine = _queue_pooled_engine(tmp_path, "unmarked.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    try:
        _touch(outer)
        with independent_session("declared for this block only"):
            pass
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


def test_marker_is_restored_when_the_body_raises(tmp_path, _test_env) -> None:
    """An exception must not leave the exemption latched on this thread.

    A leaked marker is worse than no marker: every later session on the runner
    loop would be exempt, and the detector would go quiet exactly when the
    process is already in trouble.
    """
    engine = _queue_pooled_engine(tmp_path, "restore.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    with pytest.raises(RuntimeError):
        with independent_session("audit write"):
            raise RuntimeError("boom")

    assert active_independent_session_reason() is None

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


def test_marker_nests_and_restores_the_outer_reason(tmp_path, _test_env) -> None:
    """Nesting is real: an audit write inside another marked block still works."""
    engine = _queue_pooled_engine(tmp_path, "nested.db")
    guard = SessionReentrancyGuard()
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    outer = factory()
    try:
        _touch(outer)
        with independent_session("outer independent write"):
            assert active_independent_session_reason() == "outer independent write"
            with independent_session("inner independent write"):
                assert (
                    active_independent_session_reason() == "inner independent write"
                )
                inner = factory()
                try:
                    _touch(inner)
                finally:
                    inner.close()
            assert active_independent_session_reason() == "outer independent write"
        assert active_independent_session_reason() is None
    finally:
        outer.close()
        engine.dispose()

    assert guard.violation_count == 0


def test_marker_on_one_thread_does_not_mask_another_thread(tmp_path) -> None:
    """Thread isolation, not a process-wide switch.

    The runner loop, the post-fill-persist thread and every API request thread
    share one process engine and therefore one guard. A marker that was global
    would let a deliberate audit write on the runner loop blind the detector to
    a genuine nested session on a request thread -- the exact shape that hung
    production for 65 minutes.
    """
    engine = _queue_pooled_engine(tmp_path, "threads.db")
    guard = SessionReentrancyGuard(strict=False)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    marked = threading.Event()
    release = threading.Event()

    def hold_marker() -> None:
        with independent_session("marked on the other thread only"):
            marked.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold_marker)
    worker.start()
    try:
        assert marked.wait(timeout=5)
        assert active_independent_session_reason() is None
        outer = factory()
        try:
            _touch(outer)
            inner = factory()
            try:
                _touch(inner)
            finally:
                inner.close()
        finally:
            outer.close()
    finally:
        release.set()
        worker.join(timeout=5)
        engine.dispose()

    assert guard.violation_count == 1
