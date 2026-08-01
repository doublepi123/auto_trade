"""Private SQLite read-snapshot helper — fail-closed physical isolation.

Both the audit-stats and intervention-evidence read paths need to run several
queries against ONE consistent SQLite read snapshot WITHOUT touching the
caller's Session/Connection/transaction. This tiny helper centralizes that
logic so the two services stay consistent.

Safety contract (fail closed):
* The caller Session must be bound to an ``Engine`` (the ordinary request path)
  whose pool can guarantee a DISTINCT physical connection. The helper opens its
  OWN physical connection off that engine, begins one read snapshot, runs the
  caller's query function, and releases the connection.
* Before calling ``engine.connect()`` or issuing ANY transaction command, the
  helper rejects pool modes and states that CANNOT guarantee a distinct
  physical SQLite connection when another owner may be active:
    - connection-bound sessions (``Session(bind=Connection(...))``);
    - sessions with an active/checked-out caller transaction;
    - ``StaticPool`` and ``SingletonThreadPool`` (unconditionally — they can
      hand out the SAME underlying DBAPI connection to a second caller);
    - an exhausted single-slot / full ``QueuePool`` (detected via public pool
      state APIs without a blocking connect).
  In all these cases the helper raises ``SnapshotUnavailable`` BEFORE any
  transaction command. The caller transaction/row state is never altered.
* If opening the owned connection or the explicit ``BEGIN`` fails for any
  reason, the helper immediately closes/releases ONLY the owned resource and
  raises ``SnapshotUnavailable``. It never swallows a BEGIN failure and
  continues queries; it never issues rollback/close against a possibly aliased
  caller resource.
* This is SQLite-specific (WAL read snapshot via ``BEGIN``) and is NOT a
  general transaction framework.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from sqlalchemy import Engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool, SingletonThreadPool, StaticPool

__all__ = ["SnapshotUnavailable", "open_read_snapshot"]

_T = TypeVar("_T")

_UNAVAILABLE_MESSAGE = (
    "read snapshot unavailable: caller session cannot be guaranteed a "
    "distinct physical connection"
)


class SnapshotUnavailable(RuntimeError):
    """The caller Session cannot be given a distinct physical read snapshot.

    Raised BEFORE any transaction command when the caller Session is
    connection-bound, already mid-transaction, or backed by a pool mode that
    cannot guarantee a distinct physical SQLite connection (StaticPool /
    SingletonThreadPool / exhausted single-slot QueuePool). Map to HTTP 503 at
    the API edge.
    """


def _resolve_engine(session: Session) -> Engine:
    """Resolve the engine for a snapshot, rejecting unsafe binds/pools up front.

    Rejects (raising ``SnapshotUnavailable``) BEFORE any transaction command
    when a distinct physical connection cannot be guaranteed:
    * the session bind is a ``Connection`` (connection-bound session);
    * the session already has an active/checked-out transaction;
    * the engine's pool is ``StaticPool`` or ``SingletonThreadPool``;
    * the engine's ``QueuePool`` is exhausted (all slots checked out).

    Returns the ``Engine`` for the ordinary request path.
    """
    bind = session.get_bind()
    if isinstance(bind, Connection):
        raise SnapshotUnavailable(
            "read snapshot requires an Engine-bound session; "
            "connection-bound sessions cannot be given a distinct physical "
            "read snapshot"
        )
    if not isinstance(bind, Engine):  # pragma: no cover - defensive
        raise SnapshotUnavailable(
            "read snapshot requires an Engine-bound session"
        )

    # An already-active caller transaction means a connection is checked out;
    # under single-slot pools a second engine.connect() can alias it.
    if session.in_transaction():
        raise SnapshotUnavailable(
            "read snapshot requires a session without an active transaction; "
            "an in-flight caller transaction may alias the snapshot connection"
        )

    _reject_unsafe_pool(bind)
    return bind


def _reject_unsafe_pool(engine: Engine) -> None:
    """Reject pool modes that cannot guarantee a distinct physical connection.

    ``StaticPool`` and ``SingletonThreadPool`` are unconditionally rejected:
    they can hand the SAME underlying DBAPI connection to a second caller,
    which would alias any active owner. An exhausted ``QueuePool`` (all finite
    slots checked out) is rejected via public pool state APIs without a
    blocking connect.

    ``QueuePool.size()`` returns only the BASE pool size; finite total capacity
    is base size + configured ``max_overflow``. Overflow is unlimited when
    ``max_overflow`` is negative (SQLAlchemy convention, typically -1), in
    which case we never pre-reject for exhaustion (``engine.connect()`` can
    always obtain an overflow connection). If the configured max-overflow
    cannot be determined reliably, the early exhaustion optimization is skipped
    rather than returning a false ``SnapshotUnavailable``.
    """
    pool = engine.pool
    if isinstance(pool, (StaticPool, SingletonThreadPool)):
        raise SnapshotUnavailable(
            f"read snapshot requires a multi-connection pool; "
            f"{type(pool).__name__} may alias the caller connection"
        )
    if isinstance(pool, QueuePool):
        try:
            base_size = int(pool.size())
            checked_out = int(pool.checkedout())
        except Exception:
            # Pool state APIs changed or unavailable: skip the early exhaustion
            # optimization rather than risk a false rejection.
            return
        # ``_max_overflow`` is the configured max_overflow attribute on
        # QueuePool. There is no public getter in the installed SQLAlchemy, so
        # a narrow guarded attribute read is used. If it is missing/unreadable,
        # skip the optimization (never a false rejection).
        max_overflow = getattr(pool, "_max_overflow", None)
        try:
            max_overflow_int = int(max_overflow) if max_overflow is not None else None
        except (TypeError, ValueError):
            max_overflow_int = None
        if max_overflow_int is None:
            # Cannot determine overflow capacity: skip early rejection.
            return
        if max_overflow_int < 0:
            # Unlimited overflow: engine.connect() can always obtain an
            # overflow connection; never pre-reject for exhaustion.
            return
        capacity = base_size + max_overflow_int
        if checked_out >= capacity:
            raise SnapshotUnavailable(
                "read snapshot unavailable: connection pool is exhausted"
            )


def open_read_snapshot(
    session: Session,
    query_fn: Callable[[Connection], _T],
) -> _T:
    """Run ``query_fn`` against one owned SQLite read snapshot connection.

    Opens a fresh connection off the session's engine, issues ``BEGIN`` to
    establish a WAL read snapshot, runs every query in ``query_fn`` against
    that one snapshot, then rolls back and closes the owned connection. The
    caller Session/Connection is never begun/committed/rolled back.

    Raises ``SnapshotUnavailable`` (before any transaction command) when a
    distinct physical connection cannot be guaranteed, or if opening the owned
    connection / explicit BEGIN fails for any reason.
    """
    engine = _resolve_engine(session)
    # Open the owned connection. If this fails for any reason, raise
    # SnapshotUnavailable immediately — never continue to BEGIN/queries.
    try:
        connection = engine.connect()
    except Exception as exc:
        raise SnapshotUnavailable(
            "read snapshot unavailable: could not open an owned connection"
        ) from exc
    try:
        driver_connection = connection.connection
        # Establish one explicit read snapshot. In SQLite WAL, BEGIN defers the
        # write lock and fixes the read view; all SELECTs in query_fn observe
        # the same committed state. We never COMMIT, so nothing is mutated.
        # If BEGIN fails, do NOT swallow and continue — raise immediately so no
        # query proceeds without a guaranteed snapshot.
        try:
            driver_connection.execute("BEGIN")
        except Exception as exc:
            raise SnapshotUnavailable(
                "read snapshot unavailable: could not establish a read snapshot"
            ) from exc
        try:
            return query_fn(connection)
        finally:
            try:
                driver_connection.rollback()
            except Exception:
                pass
    finally:
        # Always close ONLY the owned connection; never touch the caller's.
        connection.close()
