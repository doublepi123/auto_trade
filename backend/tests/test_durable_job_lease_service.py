from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Table, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import DurableJobLease
from app.services.durable_job_lease_service import (
    DurableJobLeaseService,
    LeaseBackendError,
    LeaseHandle,
    LeaseLostError,
)


_DB_NOW_EPOCH_MS = (
    "CAST(ROUND((julianday('now') - 2440587.5) * 86400000.0) AS INTEGER)"
)


@dataclass(frozen=True)
class _LeaseDatabase:
    engine: Engine
    sessions: sessionmaker[Session]


def _create_lease_database(
    path: Path,
    *,
    busy_timeout_ms: int = 1_000,
) -> _LeaseDatabase:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={
            "check_same_thread": False,
            "timeout": busy_timeout_ms / 1000,
        },
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    lease_table = DurableJobLease.__table__
    assert isinstance(lease_table, Table)
    lease_table.create(engine)
    sessions = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    return _LeaseDatabase(engine=engine, sessions=sessions)


@pytest.fixture
def lease_database(tmp_path: Path):
    database = _create_lease_database(tmp_path / "durable-leases.db")
    try:
        yield database
    finally:
        database.engine.dispose()


def _service(
    database: _LeaseDatabase,
    holder_id: str,
    *,
    ttl_seconds: int | float = 120,
) -> DurableJobLeaseService:
    return DurableJobLeaseService(
        database.sessions,
        holder_id=holder_id,
        default_ttl_seconds=ttl_seconds,
    )


def _expire(database: _LeaseDatabase, lease_key: str) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE durable_job_leases "
                "SET renewed_at_epoch_ms = 0, expires_at_epoch_ms = 0 "
                "WHERE lease_key = :lease_key"
            ),
            {"lease_key": lease_key},
        )


def _direct_takeover(
    database: _LeaseDatabase,
    lease_key: str,
    holder_id: str,
) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE durable_job_leases "
                "SET holder_id = :holder_id, "
                "fencing_token = fencing_token + 1, "
                f"acquired_at_epoch_ms = {_DB_NOW_EPOCH_MS}, "
                f"renewed_at_epoch_ms = {_DB_NOW_EPOCH_MS}, "
                f"expires_at_epoch_ms = {_DB_NOW_EPOCH_MS} + 1000 "
                "WHERE lease_key = :lease_key"
            ),
            {"holder_id": holder_id, "lease_key": lease_key},
        )


def _row_count(database: _LeaseDatabase, lease_key: str) -> int:
    with database.engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM durable_job_leases "
                    "WHERE lease_key = :lease_key"
                ),
                {"lease_key": lease_key},
            ).scalar_one()
        )


def _spawn_context() -> Any:
    import multiprocessing

    return multiprocessing.get_context("spawn")


def _spawn_acquire_worker(
    database_path: str,
    holder_id: str,
    start_event,
    result: Connection,
) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )
    sessions = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    service = DurableJobLeaseService(
        sessions,
        holder_id=holder_id,
        default_ttl_seconds=1,
    )
    try:
        if not start_event.wait(3):
            raise RuntimeError("spawn acquire start timed out")
        handle = service.try_acquire("spawn-race", ttl_seconds=1)
        result.send(
            None
            if handle is None
            else (handle.holder_id, handle.fencing_token)
        )
    finally:
        result.close()
        engine.dispose()


def _spawn_keepalive_holder(
    database_path: str,
    ready: Connection,
    finish_event,
) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )
    sessions = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    service = DurableJobLeaseService(
        sessions,
        holder_id="spawn-heartbeat-holder",
        default_ttl_seconds=0.4,
    )
    try:
        handle = service.try_acquire("spawn-heartbeat", ttl_seconds=0.4)
        if handle is None:
            raise RuntimeError("spawn heartbeat lease was unexpectedly held")
        with service.keepalive(
            handle,
            interval_seconds=0.1,
            ttl_seconds=0.4,
        ) as guard:
            ready.send((handle.holder_id, handle.fencing_token))
            if not finish_event.wait(3):
                raise RuntimeError("spawn heartbeat finish timed out")
            guard.checkpoint()
    finally:
        ready.close()
        engine.dispose()


def _spawn_acquire_without_release(
    database_path: str,
    result: Connection,
) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 2},
    )
    sessions = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    service = DurableJobLeaseService(
        sessions,
        holder_id="exiting-holder",
        default_ttl_seconds=0.3,
    )
    try:
        handle = service.try_acquire("spawn-takeover", ttl_seconds=0.3)
        if handle is None:
            raise RuntimeError("spawn takeover lease was unexpectedly held")
        result.send(handle.fencing_token)
        # Returning without release simulates a worker disappearing between
        # acquire and its finally/keepalive setup.
    finally:
        result.close()
        engine.dispose()


def test_acquire_uses_database_epoch_ms_and_active_same_holder_is_idempotent(
    lease_database: _LeaseDatabase,
) -> None:
    service = _service(lease_database, "holder-a")

    first = service.try_acquire("quant-v6")
    second = service.try_acquire("quant-v6")

    assert first is not None
    assert second is not None
    assert first.fencing_token == second.fencing_token == 1
    assert first.acquired_at_epoch_ms == second.acquired_at_epoch_ms
    assert first.renewed_at_epoch_ms <= first.expires_at_epoch_ms
    assert first.expires_at_epoch_ms - first.renewed_at_epoch_ms == 120_000
    with lease_database.engine.connect() as connection:
        database_now = int(
            connection.execute(text(f"SELECT {_DB_NOW_EPOCH_MS}")).scalar_one()
        )
    assert abs(database_now - second.renewed_at_epoch_ms) < 2_000


def test_active_other_holder_is_deferred_and_release_advances_next_token(
    lease_database: _LeaseDatabase,
) -> None:
    first_service = _service(lease_database, "holder-a")
    second_service = _service(lease_database, "holder-b")
    first = first_service.try_acquire("universe-selection")
    assert first is not None

    assert second_service.try_acquire("universe-selection") is None
    assert second_service.release(first) is False
    assert first_service.release(first) is True
    assert _row_count(lease_database, first.lease_key) == 1

    second = second_service.try_acquire("universe-selection")
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    assert first_service.release(first) is False
    with pytest.raises(LeaseLostError):
        first_service.heartbeat(first)


def test_expired_owner_cannot_heartbeat_release_or_fence(
    lease_database: _LeaseDatabase,
) -> None:
    service = _service(lease_database, "holder-a")
    handle = service.try_acquire("storage-maintenance")
    assert handle is not None
    _expire(lease_database, handle.lease_key)

    with pytest.raises(LeaseLostError):
        service.heartbeat(handle)
    assert service.release(handle) is False
    session = lease_database.sessions()
    try:
        with pytest.raises(LeaseLostError):
            service.fence_in_transaction(session, handle)
        session.rollback()
    finally:
        session.close()
    assert _row_count(lease_database, handle.lease_key) == 1
    reacquired = service.try_acquire(handle.lease_key)
    assert reacquired is not None
    assert reacquired.fencing_token == handle.fencing_token + 1


def test_expiry_takeover_increments_token_and_old_handle_never_revives(
    lease_database: _LeaseDatabase,
) -> None:
    first_service = _service(lease_database, "holder-a")
    second_service = _service(lease_database, "holder-b")
    first = first_service.try_acquire("quant-v6")
    assert first is not None
    _expire(lease_database, first.lease_key)

    second = second_service.try_acquire(first.lease_key)

    assert second is not None
    assert second.fencing_token == 2
    assert second.holder_id == "holder-b"
    with pytest.raises(LeaseLostError):
        first_service.heartbeat(first)
    assert first_service.release(first) is False


def test_release_never_deletes_and_database_trigger_blocks_delete(
    lease_database: _LeaseDatabase,
) -> None:
    service = _service(lease_database, "holder-a")
    handle = service.try_acquire("quant-v6")
    assert handle is not None
    assert service.release(handle) is True
    assert _row_count(lease_database, handle.lease_key) == 1

    with pytest.raises(IntegrityError, match="cannot be deleted"):
        with lease_database.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM durable_job_leases "
                    "WHERE lease_key = :lease_key"
                ),
                {"lease_key": handle.lease_key},
            )
    assert _row_count(lease_database, handle.lease_key) == 1


def test_two_connections_racing_to_acquire_have_one_winner(
    lease_database: _LeaseDatabase,
) -> None:
    services = (
        _service(lease_database, "holder-a"),
        _service(lease_database, "holder-b"),
    )
    barrier = threading.Barrier(2)

    def _attempt(service: DurableJobLeaseService) -> LeaseHandle | None:
        barrier.wait(timeout=2)
        return service.try_acquire("concurrent-job")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(_attempt, services))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert {result is None for result in results} == {False, True}
    assert winners[0].fencing_token == 1


def test_spawned_processes_racing_on_file_database_have_one_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "spawn-race.db"
    database = _create_lease_database(database_path)
    database.engine.dispose()
    context = _spawn_context()
    start_event = context.Event()
    parent_connections: list[Any] = []
    processes = []
    for holder_id in ("spawn-holder-a", "spawn-holder-b"):
        parent_connection, child_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_spawn_acquire_worker,
            args=(
                str(database_path),
                holder_id,
                start_event,
                child_connection,
            ),
        )
        process.start()
        child_connection.close()
        parent_connections.append(parent_connection)
        processes.append(process)

    start_event.set()
    results = [connection.recv() for connection in parent_connections]
    for connection in parent_connections:
        connection.close()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert winner[1] == 1


def test_spawned_healthy_heartbeat_prevents_takeover_across_ttl(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "spawn-heartbeat.db"
    database = _create_lease_database(database_path)
    context = _spawn_context()
    finish_event = context.Event()
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_spawn_keepalive_holder,
        args=(str(database_path), child_connection, finish_event),
    )
    process.start()
    child_connection.close()
    assert parent_connection.recv() == ("spawn-heartbeat-holder", 1)
    parent_connection.close()

    assert threading.Event().wait(1.0) is False
    contender = _service(database, "parent-contender", ttl_seconds=0.4)
    assert contender.try_acquire("spawn-heartbeat", ttl_seconds=0.4) is None

    finish_event.set()
    process.join(timeout=5)
    assert process.exitcode == 0
    takeover = contender.try_acquire("spawn-heartbeat", ttl_seconds=0.4)
    assert takeover is not None
    assert takeover.fencing_token == 2
    database.engine.dispose()


def test_process_exit_without_release_allows_expiry_takeover(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "spawn-expiry-takeover.db"
    database = _create_lease_database(database_path)
    context = _spawn_context()
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_spawn_acquire_without_release,
        args=(str(database_path), child_connection),
    )
    process.start()
    child_connection.close()
    assert parent_connection.recv() == 1
    parent_connection.close()
    process.join(timeout=5)
    assert process.exitcode == 0

    assert threading.Event().wait(0.5) is False
    successor = _service(database, "successor", ttl_seconds=0.3)
    takeover = successor.try_acquire("spawn-takeover", ttl_seconds=0.3)
    assert takeover is not None
    assert takeover.fencing_token == 2
    database.engine.dispose()


def test_fence_is_part_of_caller_transaction_and_does_not_commit(
    lease_database: _LeaseDatabase,
) -> None:
    with lease_database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE protected_writes "
                "(value VARCHAR(20) NOT NULL)"
            )
        )
    service = _service(lease_database, "holder-a")
    original = service.try_acquire("fenced-writer")
    assert original is not None
    renewed = service.heartbeat(original)

    session = lease_database.sessions()
    try:
        fenced = service.fence_in_transaction(session, original)
        session.execute(
            text("INSERT INTO protected_writes (value) VALUES ('pending')")
        )
        assert session.in_transaction() is True
        assert fenced.fencing_token == renewed.fencing_token
        session.rollback()
    finally:
        session.close()

    with lease_database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM protected_writes")
        ).scalar_one() == 0


def test_fence_writer_lock_turns_sqlite_busy_into_backend_error(
    tmp_path: Path,
) -> None:
    database = _create_lease_database(
        tmp_path / "busy-fence.db",
        busy_timeout_ms=25,
    )
    try:
        first_service = _service(database, "holder-a")
        second_service = _service(database, "holder-b")
        handle = first_service.try_acquire("fenced-writer")
        assert handle is not None
        session = database.sessions()
        try:
            first_service.fence_in_transaction(session, handle)
            with pytest.raises(LeaseBackendError):
                second_service.try_acquire(handle.lease_key)
            session.rollback()
        finally:
            session.close()
        assert second_service.try_acquire(handle.lease_key) is None
    finally:
        database.engine.dispose()


def test_keepalive_lost_callback_runs_once_and_normal_body_fails_closed(
    lease_database: _LeaseDatabase,
) -> None:
    service = _service(lease_database, "holder-a", ttl_seconds=1)
    handle = service.try_acquire("keepalive-lost")
    assert handle is not None
    callback_event = threading.Event()
    callbacks: list[Exception] = []

    def _on_lost(error: Exception) -> None:
        callbacks.append(error)
        callback_event.set()

    with pytest.raises(LeaseLostError) as captured:
        with service.keepalive(
            handle,
            interval_seconds=0.02,
            ttl_seconds=1,
            on_lost=_on_lost,
        ):
            _direct_takeover(
                lease_database,
                handle.lease_key,
                "holder-b",
            )
            assert callback_event.wait(1)

    assert callbacks == [captured.value]


def test_keepalive_backend_busy_callback_runs_once_and_fails_closed(
    tmp_path: Path,
) -> None:
    database = _create_lease_database(
        tmp_path / "busy-keepalive.db",
        busy_timeout_ms=25,
    )
    try:
        service = _service(database, "holder-a", ttl_seconds=1)
        handle = service.try_acquire("keepalive-busy")
        assert handle is not None
        callback_event = threading.Event()
        callbacks: list[Exception] = []

        def _on_lost(error: Exception) -> None:
            callbacks.append(error)
            callback_event.set()

        blocker = database.engine.connect()
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            with pytest.raises(LeaseBackendError) as captured:
                with service.keepalive(
                    handle,
                    interval_seconds=0.02,
                    ttl_seconds=1,
                    on_lost=_on_lost,
                ):
                    assert callback_event.wait(1)
        finally:
            blocker.rollback()
            blocker.close()

        assert callbacks == [captured.value]
    finally:
        database.engine.dispose()


def test_successful_keepalive_exit_expires_but_retains_row(
    lease_database: _LeaseDatabase,
) -> None:
    service = _service(lease_database, "holder-a", ttl_seconds=1)
    handle = service.try_acquire("keepalive-success")
    assert handle is not None

    with service.keepalive(
        handle,
        interval_seconds=0.5,
        ttl_seconds=1,
    ) as guard:
        assert guard.checkpoint().fencing_token == handle.fencing_token

    assert _row_count(lease_database, handle.lease_key) == 1
    assert service.release(handle) is False


def test_lease_sql_never_reads_python_wall_clock(
    lease_database: _LeaseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(lease_database, "holder-a")

    def _forbid_python_wall_clock() -> float:
        raise AssertionError("Python wall clock must not decide lease ownership")

    monkeypatch.setattr(time, "time", _forbid_python_wall_clock)
    handle = service.try_acquire("database-clock")
    assert handle is not None
    renewed = service.heartbeat(handle)
    assert renewed.fencing_token == handle.fencing_token
    assert service.release(renewed) is True


def test_body_exception_is_not_masked_when_release_backend_fails(
    tmp_path: Path,
) -> None:
    database = _create_lease_database(
        tmp_path / "body-exception.db",
        busy_timeout_ms=25,
    )
    try:
        service = _service(database, "holder-a", ttl_seconds=1)
        handle = service.try_acquire("body-exception")
        assert handle is not None
        blocker = database.engine.connect()
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            with pytest.raises(ValueError, match="original body failure"):
                with service.keepalive(
                    handle,
                    interval_seconds=0.5,
                    ttl_seconds=1,
                ):
                    raise ValueError("original body failure")
        finally:
            blocker.rollback()
            blocker.close()
    finally:
        database.engine.dispose()
