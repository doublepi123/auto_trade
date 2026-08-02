from __future__ import annotations

import logging
import math
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal

logger = logging.getLogger("auto_trade.durable_job_lease")

# ``julianday('now')`` is evaluated by SQLite and is available on the older
# SQLite builds that do not yet support unixepoch(..., 'subsec').  SQLite keeps
# the value of ``'now'`` stable for the duration of one statement.
_DB_NOW_EPOCH_MS = (
    "CAST(ROUND((julianday('now') - 2440587.5) * 86400000.0) AS INTEGER)"
)
_RETURNING_COLUMNS = (
    "lease_key, holder_id, fencing_token, acquired_at_epoch_ms, "
    "renewed_at_epoch_ms, expires_at_epoch_ms"
)

_ACQUIRE_SQL = text(
    f"""
    INSERT INTO durable_job_leases (
        lease_key,
        holder_id,
        fencing_token,
        acquired_at_epoch_ms,
        renewed_at_epoch_ms,
        expires_at_epoch_ms
    ) VALUES (
        :lease_key,
        :holder_id,
        1,
        {_DB_NOW_EPOCH_MS},
        {_DB_NOW_EPOCH_MS},
        {_DB_NOW_EPOCH_MS} + :ttl_ms
    )
    ON CONFLICT(lease_key) DO UPDATE SET
        holder_id = excluded.holder_id,
        fencing_token = CASE
            WHEN durable_job_leases.holder_id = excluded.holder_id
             AND durable_job_leases.expires_at_epoch_ms > {_DB_NOW_EPOCH_MS}
            THEN durable_job_leases.fencing_token
            ELSE durable_job_leases.fencing_token + 1
        END,
        acquired_at_epoch_ms = CASE
            WHEN durable_job_leases.holder_id = excluded.holder_id
             AND durable_job_leases.expires_at_epoch_ms > {_DB_NOW_EPOCH_MS}
            THEN durable_job_leases.acquired_at_epoch_ms
            ELSE {_DB_NOW_EPOCH_MS}
        END,
        renewed_at_epoch_ms = {_DB_NOW_EPOCH_MS},
        expires_at_epoch_ms = {_DB_NOW_EPOCH_MS} + :ttl_ms
    WHERE durable_job_leases.expires_at_epoch_ms <= {_DB_NOW_EPOCH_MS}
       OR durable_job_leases.holder_id = excluded.holder_id
    RETURNING {_RETURNING_COLUMNS}
    """
)

_RENEW_SQL = text(
    f"""
    UPDATE durable_job_leases
    SET renewed_at_epoch_ms = {_DB_NOW_EPOCH_MS},
        expires_at_epoch_ms = {_DB_NOW_EPOCH_MS} + :ttl_ms
    WHERE lease_key = :lease_key
      AND holder_id = :holder_id
      AND fencing_token = :fencing_token
      AND expires_at_epoch_ms > {_DB_NOW_EPOCH_MS}
    RETURNING {_RETURNING_COLUMNS}
    """
)

_RELEASE_SQL = text(
    f"""
    UPDATE durable_job_leases
    SET renewed_at_epoch_ms = {_DB_NOW_EPOCH_MS},
        expires_at_epoch_ms = {_DB_NOW_EPOCH_MS}
    WHERE lease_key = :lease_key
      AND holder_id = :holder_id
      AND fencing_token = :fencing_token
      AND expires_at_epoch_ms > {_DB_NOW_EPOCH_MS}
    RETURNING fencing_token
    """
)


class DurableJobLeaseError(RuntimeError):
    """Base error for durable lease operations."""


class LeaseLostError(DurableJobLeaseError):
    """The supplied holder/token no longer owns an active lease."""


class LeaseBackendError(DurableJobLeaseError):
    """Lease ownership could not be proved because SQLite failed."""


@dataclass(frozen=True)
class LeaseHandle:
    lease_key: str
    holder_id: str
    fencing_token: int
    acquired_at_epoch_ms: int
    renewed_at_epoch_ms: int
    expires_at_epoch_ms: int


SessionFactory = Callable[[], Session]
LeaseLostCallback = Callable[[DurableJobLeaseError], None]


def _new_holder_id() -> str:
    hostname = socket.gethostname().strip()[:48] or "unknown-host"
    return f"{hostname}:{os.getpid()}:{uuid4().hex}"


def _validate_identifier(value: str, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _ttl_milliseconds(value: int | float) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("ttl_seconds must be finite and positive")
    return max(1, math.ceil(float(value) * 1000))


def _handle_from_row(row: RowMapping) -> LeaseHandle:
    return LeaseHandle(
        lease_key=str(row["lease_key"]),
        holder_id=str(row["holder_id"]),
        fencing_token=int(row["fencing_token"]),
        acquired_at_epoch_ms=int(row["acquired_at_epoch_ms"]),
        renewed_at_epoch_ms=int(row["renewed_at_epoch_ms"]),
        expires_at_epoch_ms=int(row["expires_at_epoch_ms"]),
    )


class DurableJobLeaseService:
    """SQLite-backed cross-process lease with monotonic fencing tokens."""

    def __init__(
        self,
        session_factory: SessionFactory = SessionLocal,
        *,
        holder_id: str | None = None,
        default_ttl_seconds: int | float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.holder_id = _validate_identifier(
            holder_id if holder_id is not None else _new_holder_id(),
            name="holder_id",
            maximum=128,
        )
        configured_ttl = (
            settings.job_lease_ttl_seconds
            if default_ttl_seconds is None
            else default_ttl_seconds
        )
        _ttl_milliseconds(configured_ttl)
        self.default_ttl_seconds = configured_ttl

    def try_acquire(
        self,
        lease_key: str,
        *,
        ttl_seconds: int | float | None = None,
    ) -> LeaseHandle | None:
        """Acquire or renew this holder's lease with one SQLite statement.

        Another active holder is normal scheduler contention and returns
        ``None``.  An expired lease, including one previously released by this
        holder, advances the persistent fencing token.
        """

        validated_key = _validate_identifier(
            lease_key,
            name="lease_key",
            maximum=128,
        )
        ttl_ms = self._ttl_ms(ttl_seconds)
        try:
            with self._session_factory() as session:
                with session.begin():
                    row = session.execute(
                        _ACQUIRE_SQL,
                        {
                            "lease_key": validated_key,
                            "holder_id": self.holder_id,
                            "ttl_ms": ttl_ms,
                        },
                    ).mappings().one_or_none()
            acquired = None if row is None else _handle_from_row(row)
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise LeaseBackendError(
                f"failed to acquire durable lease {validated_key!r}"
            ) from exc
        return acquired

    def heartbeat(
        self,
        handle: LeaseHandle,
        *,
        ttl_seconds: int | float | None = None,
    ) -> LeaseHandle:
        """Renew an active exact holder/token lease using SQLite's clock."""

        ttl_ms = self._ttl_ms(ttl_seconds)
        try:
            with self._session_factory() as session:
                with session.begin():
                    row = session.execute(
                        _RENEW_SQL,
                        self._owned_parameters(handle, ttl_ms=ttl_ms),
                    ).mappings().one_or_none()
            renewed = None if row is None else _handle_from_row(row)
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise LeaseBackendError(
                f"failed to heartbeat durable lease {handle.lease_key!r}"
            ) from exc
        if renewed is None:
            raise LeaseLostError(
                "durable lease heartbeat rejected for "
                f"{handle.lease_key!r} at fencing token "
                f"{handle.fencing_token}"
            )
        return renewed

    def fence_in_transaction(
        self,
        session: Session,
        handle: LeaseHandle,
        *,
        ttl_seconds: int | float | None = None,
    ) -> LeaseHandle:
        """Fence and renew immediately before protected business writes.

        This method deliberately performs no commit or rollback.  Its UPDATE
        must be the first DML in the caller's fresh SQLite transaction; the
        resulting writer lock then prevents a takeover until that same
        transaction commits or rolls back.  The expiry recorded in ``handle``
        is informational and is never compared, so a concurrent keepalive may
        safely have renewed it already.
        """

        ttl_ms = self._ttl_ms(ttl_seconds)
        try:
            row = session.execute(
                _RENEW_SQL,
                self._owned_parameters(handle, ttl_ms=ttl_ms),
            ).mappings().one_or_none()
            renewed = None if row is None else _handle_from_row(row)
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise LeaseBackendError(
                f"failed to fence durable lease {handle.lease_key!r}"
            ) from exc
        if renewed is None:
            raise LeaseLostError(
                "durable lease fence rejected for "
                f"{handle.lease_key!r} at fencing token "
                f"{handle.fencing_token}"
            )
        return renewed

    def release(self, handle: LeaseHandle) -> bool:
        """Expire an exact holder/token lease without deleting its row."""

        try:
            with self._session_factory() as session:
                with session.begin():
                    row = session.execute(
                        _RELEASE_SQL,
                        self._owned_parameters(handle),
                    ).one_or_none()
        except SQLAlchemyError as exc:
            raise LeaseBackendError(
                f"failed to release durable lease {handle.lease_key!r}"
            ) from exc
        return row is not None

    def keepalive(
        self,
        handle: LeaseHandle,
        *,
        interval_seconds: int | float | None = None,
        ttl_seconds: int | float | None = None,
        on_lost: LeaseLostCallback | None = None,
    ) -> LeaseKeepalive:
        """Build a context manager that heartbeats on an independent session."""

        configured_ttl = (
            self.default_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        configured_interval = (
            settings.job_lease_heartbeat_seconds
            if interval_seconds is None
            else interval_seconds
        )
        return LeaseKeepalive(
            self,
            handle,
            interval_seconds=configured_interval,
            ttl_seconds=configured_ttl,
            on_lost=on_lost,
        )

    def _ttl_ms(self, ttl_seconds: int | float | None) -> int:
        value = (
            self.default_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        return _ttl_milliseconds(value)

    def _owned_parameters(
        self,
        handle: LeaseHandle,
        *,
        ttl_ms: int | None = None,
    ) -> dict[str, str | int]:
        parameters: dict[str, str | int] = {
            "lease_key": handle.lease_key,
            "holder_id": self.holder_id,
            "fencing_token": handle.fencing_token,
        }
        if ttl_ms is not None:
            parameters["ttl_ms"] = ttl_ms
        return parameters


class LeaseKeepalive:
    """Background heartbeat guard for one already-acquired lease."""

    def __init__(
        self,
        service: DurableJobLeaseService,
        handle: LeaseHandle,
        *,
        interval_seconds: int | float,
        ttl_seconds: int | float,
        on_lost: LeaseLostCallback | None,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(float(interval_seconds))
            or float(interval_seconds) <= 0
        ):
            raise ValueError("interval_seconds must be finite and positive")
        _ttl_milliseconds(ttl_seconds)
        if float(interval_seconds) >= float(ttl_seconds):
            raise ValueError(
                "interval_seconds must be shorter than ttl_seconds"
            )
        self._service = service
        self._interval_seconds = float(interval_seconds)
        self._ttl_seconds = ttl_seconds
        self._on_lost = on_lost
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._handle = handle
        self._failure: DurableJobLeaseError | None = None
        self._started = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"lease-heartbeat-{handle.lease_key[:40]}",
            daemon=True,
        )

    @property
    def handle(self) -> LeaseHandle:
        with self._state_lock:
            return self._handle

    def __enter__(self) -> Self:
        with self._state_lock:
            if self._started or self._closed:
                raise RuntimeError("lease keepalive contexts are single-use")
            self._started = True
        self._thread.start()
        return self

    def checkpoint(self) -> LeaseHandle:
        """Raise the original asynchronous lease failure, if one occurred."""

        with self._state_lock:
            failure = self._failure
            handle = self._handle
        if failure is not None:
            raise failure
        return handle

    def fence_in_transaction(self, session: Session) -> LeaseHandle:
        """Fence with the current handle and retain the renewed handle."""

        handle = self.checkpoint()
        try:
            renewed = self._service.fence_in_transaction(
                session,
                handle,
                ttl_seconds=self._ttl_seconds,
            )
            self._replace_handle(renewed)
        except (LeaseLostError, LeaseBackendError) as exc:
            self._record_failure(exc)
            raise
        return renewed

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self._stop_event.set()
        if self._started:
            self._thread.join()
        with self._state_lock:
            self._closed = True
            heartbeat_failure = self._failure
            handle = self._handle

        release_failure: DurableJobLeaseError | None = None
        try:
            released = self._service.release(handle)
            if not released:
                release_failure = LeaseLostError(
                    "durable lease release rejected for "
                    f"{handle.lease_key!r} at fencing token "
                    f"{handle.fencing_token}"
                )
                self._record_failure(release_failure)
        except LeaseBackendError as release_exc:
            release_failure = release_exc
            self._record_failure(release_exc)

        if exc_type is not None:
            if release_failure is not None:
                logger.warning(
                    "Lease release failed while preserving an existing "
                    "exception for %r: %s",
                    handle.lease_key,
                    release_failure,
                )
            return False
        if heartbeat_failure is not None:
            if release_failure is not None:
                logger.warning(
                    "Lease release also failed after keepalive failure for "
                    "%r: %s",
                    handle.lease_key,
                    release_failure,
                )
            raise heartbeat_failure
        if release_failure is not None:
            raise release_failure
        return False

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                renewed = self._service.heartbeat(
                    self.handle,
                    ttl_seconds=self._ttl_seconds,
                )
                self._replace_handle(renewed)
            except (LeaseLostError, LeaseBackendError) as exc:
                self._record_failure(exc)
                return

    def _replace_handle(self, handle: LeaseHandle) -> None:
        with self._state_lock:
            current = self._handle
            if (
                handle.lease_key != current.lease_key
                or handle.holder_id != current.holder_id
                or handle.fencing_token != current.fencing_token
            ):
                raise LeaseLostError(
                    "keepalive received a different durable lease identity"
                )
            if handle.renewed_at_epoch_ms >= current.renewed_at_epoch_ms:
                self._handle = handle

    def _record_failure(self, failure: DurableJobLeaseError) -> None:
        callback: LeaseLostCallback | None = None
        with self._state_lock:
            if self._failure is not None:
                return
            self._failure = failure
            self._stop_event.set()
            callback = self._on_lost
        if callback is not None:
            try:
                callback(failure)
            except Exception:
                logger.exception(
                    "Durable lease on_lost callback failed for %r",
                    self.handle.lease_key,
                )


__all__ = [
    "DurableJobLeaseError",
    "DurableJobLeaseService",
    "LeaseBackendError",
    "LeaseHandle",
    "LeaseKeepalive",
    "LeaseLostError",
]
