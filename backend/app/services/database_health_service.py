"""Read-only SQLite storage-health snapshot.

Returns a safe operational summary of the repository's SQLite database:
journal mode, page size, page/freelist counts, derived used/free space and
WAL file size. Never exposes filesystem/database paths, connection URLs,
table contents or secrets, and never mutates PRAGMA settings — every query
is a read-only ``PRAGMA`` or ``os.path.getsize``.

``database_size_bytes`` and ``free_space_bytes`` are logical SQLite page
metrics (``page_size_bytes * page_count`` and ``page_size_bytes *
freelist_count``) for both in-memory and file-backed databases — they reflect
SQLite's internal page accounting, not the on-disk file size.

``wal_size_bytes`` is ``None`` for in-memory SQLite (no WAL file is
applicable, including ``:memory:`` and shared-memory URI variants), ``0`` for
a file-backed DB whose ``-wal`` sidecar is absent, and ``None`` when the WAL
file size cannot be determined (e.g. permission or I/O error) — never
misreported as merely absent. The resolved main DB filename is obtained from
``PRAGMA database_list`` on the same connection as the page/journal PRAGMAs
and is never exposed in the response.
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

# SQLite main-DB filename values that indicate an in-memory database (no WAL
# file is applicable). ``PRAGMA database_list`` returns ``''`` for in-memory
# and shared-memory URI databases; ``:memory:`` is the legacy memory URL form.
_IN_MEMORY_MAIN_FILES = {"", ":memory:"}


class DatabaseHealthService:
    """Read-only SQLite storage-health snapshot.

    Constructed with the app's bound SQLAlchemy ``Engine`` (the same one the
    rest of the app uses). All PRAGMA probes run on a single short-lived
    connection opened from that engine; nothing is committed and no PRAGMA
    is set.
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

        journal_mode, page_size, page_count, freelist_count, main_file = self._pragmas()
        page_size = max(0, int(page_size or 0))
        page_count = max(0, int(page_count or 0))
        freelist_count = max(0, int(freelist_count or 0))
        used_page_count = max(0, page_count - freelist_count)
        # Logical SQLite page metrics for both memory and file-backed DBs.
        database_size_bytes = page_size * page_count
        free_space_bytes = page_size * freelist_count

        wal_size_bytes = self._wal_size_bytes(main_file)

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

    def _pragmas(self) -> tuple[str | None, int | None, int | None, int | None, str]:
        """Read journal_mode / page_size / page_count / freelist_count and the
        resolved main DB filename from ``PRAGMA database_list``.

        All queries run on the SAME connection so the ``database_list`` filename
        is consistent with the page/journal PRAGMAs. Each PRAGMA is queried
        independently so one returning NULL (e.g. page_count on a fresh
        in-memory DB) does not abort the others. All queries are read-only —
        no PRAGMA is set and nothing is committed. The resolved main filename
        is returned for internal WAL stat use only; it is never exposed in the
        response.
        """
        journal_mode: str | None = None
        page_size: int | None = None
        page_count: int | None = None
        freelist_count: int | None = None
        main_file = ""
        with self._engine.connect() as conn:
            row = conn.execute(text("PRAGMA journal_mode")).scalar()
            journal_mode = None if row is None else str(row)
            page_size = _to_int(conn.execute(text("PRAGMA page_size")).scalar())
            page_count = _to_int(conn.execute(text("PRAGMA page_count")).scalar())
            freelist_count = _to_int(conn.execute(text("PRAGMA freelist_count")).scalar())
            # database_list returns (seq, name, file); main DB is name == 'main'.
            for seq, name, file_path in conn.execute(text("PRAGMA database_list")):
                if name == "main":
                    main_file = "" if file_path is None else str(file_path)
                    break
        return journal_mode, page_size, page_count, freelist_count, main_file

    def _wal_size_bytes(self, main_file: str) -> int | None:
        """WAL file size in bytes for a file-backed main DB.

        In-memory SQLite (empty main filename, ``:memory:``, or shared-memory
        URI variants) has no WAL file — returns ``None`` (documented as "not
        applicable"). A file-backed DB whose ``-wal`` sidecar does not exist
        (e.g. journal mode is not WAL, or no writes have occurred) returns
        ``0``. For other ``OSError``/permission/I/O failures, returns ``None``
        (unavailable) and logs without path leakage — never misreports as
        merely absent.
        """
        if main_file in _IN_MEMORY_MAIN_FILES:
            # In-memory or shared-memory URI — no WAL file is applicable.
            return None
        wal_path = f"{main_file}-wal"
        try:
            return int(os.path.getsize(wal_path))
        except FileNotFoundError:
            # WAL sidecar does not exist (no WAL yet) — genuinely absent.
            return 0
        except OSError:
            # Permission denied, I/O error, or other stat failure — the WAL
            # size is unavailable, not absent. Log without the path and
            # surface None so callers can distinguish "not applicable /
            # unavailable" from "absent (0)".
            logger.warning("database-health WAL size unavailable (stat failed)", exc_info=True)
            return None


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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