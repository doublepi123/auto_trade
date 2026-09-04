"""Thread identity is the wrong identity for a re-entrancy detector under FastAPI.

FastAPI runs a sync ``db = Depends(get_db)`` generator dependency through
``fastapi.concurrency.contextmanager_in_threadpool``, which deliberately runs
``__exit__`` on a SEPARATE limiter (``exit_limiter = CapacityLimiter(1)``) so a
teardown can never deadlock waiting for the pool token its own checkout holds.
Request A's ``db.close()`` is therefore dispatched as an independent task and
lands on whichever anyio worker is free -- measured here at 513 of 600 requests
on a thread other than the one that checked the connection out.

The owner stamp on the connection record correctly credits that decrement back
to A's original thread, and THAT correctness is what manufactures the false
positive: while A's teardown is still queued, the freed worker picks up request
B and checks out a second connection. Thread T now genuinely holds A's
not-yet-returned connection and B's, concurrently. ``depth == 2`` is
arithmetically true and semantically meaningless -- two independent requests
time-sharing one worker, not one caller nesting.

Measured against this exact tree before the fix, with ONE strictly non-nesting
endpoint::

    requests: 600
    violation_count: 387
    residual _held: {}          <- EMPTY, nothing leaked
    pool.checkedout(): 0
    positionally-mismatched checkout/checkin threads: 513/600

A balanced counter plus hundreds of reports is misattribution, not a leak. In
production this blamed seven unrelated single-query endpoints for over an hour
at ~22-34 suppressed warnings per five-minute window, with zero ``QueuePool
limit`` errors in the same 60 minutes. A detector that cries wolf is not an
alarm.

The fix keys ``_held`` on a per-request/per-task scope token taken from a
``ContextVar`` (set by ``SessionScopeMiddleware``), falling back to the thread
id for crons and the runner loop, which have no request scope and where thread
identity IS the right identity. These tests pin both halves: the false positive
is gone AND genuine nesting is still caught.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import anyio.to_thread
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import (
    SessionReentrancyGuard,
    SessionReentrancyViolation,
    SessionScopeMiddleware,
    current_session_scope,
    session_scope,
)

#: Enough concurrent requests to make the exit-limiter hand teardowns to other
#: workers hundreds of times. 600 is the volume the pre-fix measurement used.
REQUEST_COUNT = 600

#: Fewer anyio workers than client threads, so workers are genuinely reused
#: between requests -- the condition under which a freed worker picks up the
#: next request while the previous one's teardown is still queued.
WORKER_LIMIT = 4
CLIENT_THREADS = 16


def _queue_pooled_engine(tmp_path, name: str) -> Engine:
    """A file-backed SQLite engine, which SQLAlchemy serves with ``QueuePool``.

    Deliberately not ``sqlite://``: that is served by ``SingletonThreadPool``,
    which hands the same connection back per thread and emits no second
    checkout at all. Only a queue-pooled engine reproduces the incident, and
    only a queue-pooled engine is what production runs on.
    """
    return create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
        pool_size=20,
        max_overflow=30,
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


@asynccontextmanager
async def _pin_worker_limit(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Shrink the anyio threadpool so workers are reused across requests.

    The limiter is a per-event-loop object, so it can only be resized from
    inside the running loop. Without this the default 40 workers make reuse
    rare and the race is merely unlikely rather than reliably reproduced.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    original = limiter.total_tokens
    limiter.total_tokens = WORKER_LIMIT
    try:
        yield
    finally:
        limiter.total_tokens = original


def test_concurrent_innocent_requests_do_not_report(tmp_path) -> None:
    """A strictly non-nesting endpoint must never be blamed, at any concurrency.

    This is the false-positive storm itself. The endpoint takes ``db =
    Depends(get_db)`` and runs exactly one query; it opens no second session
    and calls nothing that could. Every report here is an innocent endpoint
    being blamed for the scheduler's choice of worker.

    Observing (``strict=False``) on purpose: production runs ``env == "prod"``
    where the guard cannot raise, so the live symptom was a warning storm and
    the thing to assert is the COUNT. Under thread identity this measured 387.
    """
    engine = _queue_pooled_engine(tmp_path, "false_positive.db")
    guard = SessionReentrancyGuard(strict=False)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    def get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI(lifespan=_pin_worker_limit)
    app.add_middleware(SessionScopeMiddleware)

    @app.get("/one")
    def one(db: Session = Depends(get_db)) -> dict[str, int]:
        return {"value": int(db.execute(text("SELECT 1")).scalar_one())}

    before = guard.violation_count
    try:
        with TestClient(app) as client:
            def hit(_index: int) -> int:
                return client.get("/one").status_code

            with ThreadPoolExecutor(max_workers=CLIENT_THREADS) as pool:
                codes = list(pool.map(hit, range(REQUEST_COUNT)))
    finally:
        engine.dispose()

    assert set(codes) == {200}
    assert guard.violation_count == before, (
        f"{guard.violation_count - before} innocent request(s) blamed out of "
        f"{REQUEST_COUNT}; the endpoint never opened a second session"
    )
    # Balanced accounting is the other half of the diagnosis: nothing leaked,
    # so every report was misattribution rather than a real held connection.
    assert guard.held_scopes == {}


def test_genuine_nesting_inside_one_request_still_reports(tmp_path, _test_env) -> None:
    """ANTI-BLUNTING. One request opening a second session must still be caught.

    A nested call inside a request inherits the same context and therefore the
    same scope token, so the depth is real re-entrancy and the verdict stands.
    If this ever goes quiet the fix has traded a false-positive storm for a
    blind detector, which is strictly worse than the storm.

    Strict + ``env == "test"``, so the violation must also RAISE.
    """
    engine = _queue_pooled_engine(tmp_path, "true_positive.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    def get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI(lifespan=_pin_worker_limit)
    app.add_middleware(SessionScopeMiddleware)

    @app.get("/nested")
    def nested(db: Session = Depends(get_db)) -> dict[str, int]:
        _touch(db)
        inner = factory()
        try:
            _touch(inner)
        finally:
            inner.close()
        return {"value": 1}

    before = guard.violation_count
    try:
        with TestClient(app) as client:
            with pytest.raises(SessionReentrancyViolation):
                client.get("/nested")
    finally:
        engine.dispose()

    assert guard.violation_count == before + 1
    assert guard.last_violation_stack is not None
    # The captured stack is the HANDLER's, taken inside the anyio worker, so
    # it names the endpoint frame rather than this test function.
    assert "in nested" in guard.last_violation_stack


def test_nested_session_on_a_plain_thread_still_reports(tmp_path, _test_env) -> None:
    """Cron / runner-loop fallback: no request scope, so thread identity rules.

    ``_alert_rules_cron`` and ``AppRunner._run_loop`` never pass through the
    middleware. They are single-threaded sequential workers, so a thread
    holding two connections there IS the 2026-09-03 shape and must still be
    detected. The fallback keeps that coverage.
    """
    engine = _queue_pooled_engine(tmp_path, "cron_fallback.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    failures: list[BaseException] = []
    caught: list[SessionReentrancyViolation] = []

    def cron_like() -> None:
        assert current_session_scope()[0] == "thread"
        outer = factory()
        try:
            _touch(outer)
            inner = factory()
            try:
                _touch(inner)
            except SessionReentrancyViolation as exc:
                caught.append(exc)
            finally:
                inner.close()
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            failures.append(exc)
        finally:
            outer.close()

    thread = threading.Thread(target=cron_like)
    thread.start()
    thread.join(timeout=15)
    engine.dispose()

    assert failures == []
    assert len(caught) == 1
    assert guard.violation_count == 1


def test_two_scopes_each_holding_one_do_not_report(tmp_path, _test_env) -> None:
    """Cross-scope isolation, on a SINGLE thread.

    This is the false positive reduced to its essentials: one worker thread
    holding one connection for scope A and one for scope B. Two independent
    callers time-sharing a worker is not nesting, and the guard must say so
    without any concurrency at all.
    """
    engine = _queue_pooled_engine(tmp_path, "cross_scope.db")
    guard = SessionReentrancyGuard(strict=True)
    guard.install(engine)
    factory = sessionmaker(bind=engine)

    with session_scope():
        first = factory()
        _touch(first)
        with session_scope():
            second = factory()
            try:
                _touch(second)
            finally:
                second.close()
        first.close()
    engine.dispose()

    assert guard.violation_count == 0


def test_scope_tokens_are_distinct_per_scope() -> None:
    """Two scopes must never collide, and the fallback must be namespaced."""
    fallback = current_session_scope()
    assert fallback[0] == "thread"
    assert fallback[1] == threading.get_ident()

    with session_scope():
        first = current_session_scope()
        with session_scope():
            second = current_session_scope()
        restored = current_session_scope()

    assert first[0] == "request"
    assert second[0] == "request"
    assert first != second
    assert restored == first
    assert current_session_scope() == fallback


def test_middleware_passes_through_non_request_scopes() -> None:
    """Lifespan and any other ASGI scope type must be forwarded untouched.

    The middleware sits in front of ``/ws`` and every streaming route, so it
    must be a pure ASGI pass-through rather than a ``BaseHTTPMiddleware``
    subclass, which buffers responses and spawns an extra task per request.
    """
    seen: list[str] = []

    async def inner_app(scope, receive, send) -> None:
        seen.append(scope["type"])

    middleware = SessionScopeMiddleware(inner_app)

    async def receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def send(_message: object) -> None:
        return None

    anyio.run(middleware, {"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
