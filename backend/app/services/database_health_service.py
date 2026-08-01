"""Read-only SQLite storage-health snapshot.

Returns a safe operational summary of the repository's SQLite database:
journal mode, page size, page/freelist counts, derived used/free space and
WAL file size. Never exposes filesystem/database paths, connection URLs,
table contents or secrets, and never mutates PRAGMA settings — every query
is a read-only ``PRAGMA``/``os.path.getsize``.

In-memory SQLite (``sqlite://``) is handled deterministically: there is no
file-backed main DB, so ``database_size_bytes`` / ``free_space_bytes`` are
``0`` and ``wal_size_bytes`` is ``None`` (documented as "not applicable").
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.schemas import DatabaseHealthSnapshot

logger = logging.getLogger(__name__)


class DatabaseHealthService:
    """Read-only SQLite storage-health snapshot.

    Constructed with the app's bound SQLAlchemy ``Engine`` (the same one the
    rest of the app uses). All PRAGMA probes run on a short-lived connection
    opened from that engine; nothing is committed and no PRAGMA is set.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def snapshot(self) -> DatabaseHealthSnapshot:
        dialect = self._engine.dialect.name
        # Only SQLite is meaningful for this probe; surface the dialect
        # regardless so callers can see what they hit, but the PRAGMA queries
        # below are SQLite-specific and only run for SQLite.
        if dialect != "sqlite":
            return DatabaseHealthSnapshot(
                checked_at=datetime.now(timezone.utc),
                dialect=dialect,
                journal_mode=None,
                page_size_bytes=None,
                page_count=None,
                freelist_count=None,
                used_page_count=None,
                database_size_bytes=None,
                free_space_bytes=None,
                wal_size_bytes=None,
            )

        journal_mode, page_size, page_count, freelist_count = self._pragmas()
        page_size = max(0, int(page_size or 0))
        page_count = max(0, int(page_count or 0))
        freelist_count = max(0, int(freelist_count or 0))
        used_page_count = max(0, page_count - freelist_count)
        database_size_bytes = page_size * page_count
        free_space_bytes = page_size * freelist_count

        wal_size_bytes = self._wal_size_bytes()

        return DatabaseHealthSnapshot(
            checked_at=datetime.now(timezone.utc),
            dialect=dialect,
            journal_mode=journal_mode,
            page_size_bytes=page_size,
            page_count=page_count,
            freelist_count=freelist_count,
            used_page_count=used_page_count,
            database_size_bytes=database_size_bytes,
            free_space_bytes=free_space_bytes,
            wal_size_bytes=wal_size_bytes,
        )

    # --- internals --------------------------------------------------------

    def _pragmas(self) -> tuple[str | None, int | None, int | None, int | None]:
        """Read journal_mode / page_size / page_count / freelist_count.

        Each PRAGMA is queried independently so one returning NULL (e.g.
        page_count on a fresh in-memory DB) does not abort the others. All
        queries are read-only — no PRAGMA is set and nothing is committed.
        """
        journal_mode: str | None = None
        page_size: int | None = None
        page_count: int | None = None
        freelist_count: int | None = None
        with self._engine.connect() as conn:
            row = conn.execute(text("PRAGMA journal_mode")).scalar()
            journal_mode = None if row is None else str(row)
            page_size = _to_int(conn.execute(text("PRAGMA page_size")).scalar())
            page_count = _to_int(conn.execute(text("PRAGMA page_count")).scalar())
            freelist_count = _to_int(conn.execute(text("PRAGMA freelist_count")).scalar())
        return journal_mode, page_size, page_count, freelist_count

    def _wal_size_bytes(self) -> int | None:
        """WAL file size in bytes for a file-backed main DB.

        In-memory SQLite (``sqlite://``) has no main DB file and therefore no
        WAL file — returns ``None`` (documented as "not applicable"). A
        file-backed DB whose ``-wal`` sidecar does not exist (e.g. journal
        mode is not WAL, or no writes have occurred) returns ``0``.
        """
        database = self._engine.url.database
        if not database:
            # In-memory or URL without a database path — no WAL file exists.
            return None
        wal_path = f"{database}-wal"
        try:
            return int(os.path.getsize(wal_path))
        except OSError:
            # File does not exist (no WAL yet) or is inaccessible — treat as
            # zero rather than surfacing a path/error to the caller.
            return 0


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# Module-level convenience kept for callers that already hold a Session (the
# API handler binds the engine from ``app.database``); the service itself is
# engine-based so it never needs a session.
def snapshot_from_session(session: Session) -> DatabaseHealthSnapshot:
    """Build a snapshot using the engine bound to ``session``.

    Convenience for code paths that only have a ``Session``; the PRAGMA probes
    still run on a fresh connection opened from the bound engine, not on the
    session's own connection, so the session transaction state is untouched.
    """
    bind = session.get_bind()
    if isinstance(bind, Engine):
        return DatabaseHealthService(bind).snapshot()
    # ``get_bind`` can return a Connection in some configurations; fall back to
    # the connection's engine so the PRAGMA probes still open a fresh connection.
    return DatabaseHealthService(bind.engine).snapshot()