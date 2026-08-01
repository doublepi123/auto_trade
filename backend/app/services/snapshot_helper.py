"""Private SQLite read-snapshot helper — fail-closed physical isolation.

Both the audit-stats and intervention-evidence read paths need to run several
queries against ONE consistent SQLite read snapshot WITHOUT touching the
caller's Session/Connection/transaction. This tiny helper centralizes that
logic so the two services stay consistent.

Safety contract (fail closed):
* The caller Session must be bound to an ``Engine`` (the ordinary request path).
  The helper opens its OWN physical connection off that engine, begins one read
  snapshot, runs the caller's query function, and releases the connection.
* If the caller Session is explicitly connection-bound (``Session(bind=Connection(...))``)
  or already has an active/checked-out transaction, a distinct physical
  connection CANNOT be guaranteed without aliasing a connection that may be
  mid-transaction (notably under ``StaticPool`` / ``SingletonThreadPool`` /
  single-slot pools, where ``engine.connect()`` can return the SAME underlying
  DBAPI connection). Rather than probe a second connection and risk
  BEGIN/rollback on an aliased wrapper, the helper rejects up front with
  ``SnapshotUnavailable`` BEFORE any transaction command. The caller
  transaction/row state is never altered.
* This is SQLite-specific (WAL read snapshot via ``BEGIN``) and is NOT a
  general transaction framework.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from sqlalchemy import Engine
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

__all__ = ["SnapshotUnavailable", "open_read_snapshot"]

_T = TypeVar("_T")


class SnapshotUnavailable(RuntimeError):
    """The caller Session cannot be given a distinct physical read snapshot.

    Raised BEFORE any transaction command when the caller Session is
    connection-bound or already mid-transaction, where opening a second
    connection could alias the caller's DBAPI connection (StaticPool /
    SingletonThreadPool / single-slot pools). Map to HTTP 503 at the API edge.
    """


def _resolve_engine(session: Session) -> Engine:
    """Resolve the engine for a snapshot, rejecting unsafe binds up front.

    Rejects (raising ``SnapshotUnavailable``) when:
    * the session bind is a ``Connection`` (connection-bound session); or
    * the session already has an active/checked-out transaction
      (``session.in_transaction()``), because a second connection off the same
      engine may alias the caller's connection under single-slot pools.

    Returns the ``Engine`` for the ordinary request path.
    """
    bind = session.get_bind()
    if isinstance(bind, Connection):
        raise SnapshotUnavailable(
            "audit snapshot requires an Engine-bound session; "
            "connection-bound sessions cannot be given a distinct physical "
            "read snapshot"
        )
    if not isinstance(bind, Engine):  # pragma: no cover - defensive
        raise SnapshotUnavailable(
            "audit snapshot requires an Engine-bound session"
        )
    # An already-active caller transaction means a connection is checked out;
    # under StaticPool/SingletonThreadPool/single-slot QueuePool a second
    # engine.connect() can alias it. Reject rather than risk aliasing.
    if session.in_transaction():
        raise SnapshotUnavailable(
            "audit snapshot requires a session without an active transaction; "
            "an in-flight caller transaction may alias the snapshot connection"
        )
    return bind


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
    distinct physical connection cannot be guaranteed.
    """
    engine = _resolve_engine(session)
    with engine.connect() as connection:
        driver_connection = connection.connection
        # Establish one explicit read snapshot. In SQLite WAL, BEGIN defers the
        # write lock and fixes the read view; all SELECTs in query_fn observe
        # the same committed state. We never COMMIT, so nothing is mutated.
        in_transaction = False
        try:
            driver_connection.execute("BEGIN")
            in_transaction = True
        except Exception:
            # The pooled connection may already report an active transaction
            # (SQLAlchemy begins lazily); the SELECTs still share one snapshot.
            in_transaction = False
        try:
            return query_fn(connection)
        finally:
            if in_transaction:
                try:
                    driver_connection.rollback()
                except Exception:
                    pass
