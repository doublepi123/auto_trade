"""Database storage-health API — read-only, authenticated.

The API route depends on ``get_db``/Session (not the module-level engine) so
FastAPI dependency overrides are reliable. File-backed, in-memory, memory-URI,
relative-file, and stat-error behaviors are all covered.
"""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_database_health_{os.getpid()}.db"
)

from datetime import timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base
from app.services.database_health_service import DatabaseHealthService, snapshot_from_session


class TestDatabaseHealthServiceInMemory:
    """Direct service tests against in-memory SQLite engines (memory variants)."""

    @classmethod
    def setup_class(cls) -> None:
        cls.engine: Engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()

    def test_in_memory_snapshot_is_deterministic(self) -> None:
        snap = DatabaseHealthService(self.engine).snapshot()
        assert snap.dialect == "sqlite"
        assert snap.journal_mode == "memory"
        assert snap.page_size_bytes is not None and snap.page_size_bytes > 0
        # in-memory: no WAL file -> None (documented "not applicable")
        assert snap.wal_size_bytes is None
        # page_count may be 0 on a fresh in-memory DB; size must be non-negative
        assert snap.database_size_bytes is not None
        assert snap.database_size_bytes >= 0
        assert snap.free_space_bytes is not None
        assert snap.free_space_bytes >= 0

    def test_in_memory_metrics_internally_consistent(self) -> None:
        snap = DatabaseHealthService(self.engine).snapshot()
        assert snap.page_count is not None
        assert snap.freelist_count is not None
        assert snap.page_size_bytes is not None
        assert snap.used_page_count is not None
        assert snap.used_page_count == snap.page_count - snap.freelist_count
        assert snap.used_page_count >= 0
        assert snap.database_size_bytes == snap.page_size_bytes * snap.page_count
        assert snap.free_space_bytes == snap.page_size_bytes * snap.freelist_count
        # checked_at is timezone-aware UTC
        assert snap.checked_at.tzinfo is not None
        assert snap.checked_at.utcoffset() == timezone.utc.utcoffset(None)


class TestDatabaseHealthServiceMemoryUri:
    """``:memory:`` and shared-memory URI variants are treated as in-memory
    (no WAL applicability -> wal_size_bytes=None)."""

    def test_memory_uri_variant(self) -> None:
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        try:
            snap = DatabaseHealthService(engine).snapshot()
            assert snap.dialect == "sqlite"
            assert snap.wal_size_bytes is None  # :memory: -> no WAL applicable
        finally:
            engine.dispose()

    def test_shared_memory_uri_variant(self) -> None:
        engine = create_engine(
            "sqlite:///file:dh_shared_mem?mode=memory&cache=shared&uri=true",
            connect_args={"check_same_thread": False, "uri": True},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        try:
            snap = DatabaseHealthService(engine).snapshot()
            assert snap.dialect == "sqlite"
            assert snap.wal_size_bytes is None  # shared-memory URI -> no WAL
        finally:
            engine.dispose()


class TestDatabaseHealthServiceFileBacked:
    """Direct service tests against a file-backed SQLite engine with WAL."""

    @classmethod
    def setup_class(cls) -> None:
        cls.path = os.path.join(
            tempfile.gettempdir(), f"auto_trade_dh_service_{os.getpid()}.db"
        )
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)
        cls.engine: Engine = create_engine(
            f"sqlite:///{cls.path}", connect_args={"check_same_thread": False}
        )
        # Enable WAL so the -wal sidecar is created on write.

        @event.listens_for(cls.engine, "connect")
        def _set_wal(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

        Base.metadata.create_all(bind=cls.engine)
        # Force a write so a WAL file materializes.
        with cls.engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS dh_probe (x INTEGER)"))
            conn.execute(text("INSERT INTO dh_probe VALUES (1),(2),(3)"))
            conn.commit()

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)

    def test_file_backed_snapshot_has_wal_size(self) -> None:
        snap = DatabaseHealthService(self.engine).snapshot()
        assert snap.dialect == "sqlite"
        assert snap.journal_mode == "wal"
        assert snap.page_size_bytes is not None and snap.page_size_bytes > 0
        assert snap.page_count is not None and snap.page_count > 0
        # WAL sidecar exists after a write -> non-negative integer size.
        assert snap.wal_size_bytes is not None
        assert snap.wal_size_bytes >= 0
        assert snap.database_size_bytes == snap.page_size_bytes * snap.page_count
        assert snap.used_page_count == snap.page_count - (snap.freelist_count or 0)

    def test_file_backed_metrics_non_negative_and_consistent(self) -> None:
        snap = DatabaseHealthService(self.engine).snapshot()
        assert snap.freelist_count is not None and snap.freelist_count >= 0
        assert snap.used_page_count is not None and snap.used_page_count >= 0
        assert snap.database_size_bytes is not None and snap.database_size_bytes >= 0
        assert snap.free_space_bytes is not None and snap.free_space_bytes >= 0
        assert snap.page_size_bytes is not None
        assert snap.free_space_bytes == snap.page_size_bytes * snap.freelist_count


class TestDatabaseHealthServiceRelativeFile:
    """Relative file resolution: the resolved main filename from
    ``PRAGMA database_list`` (absolute) is used to stat ``<resolved>-wal``,
    not the (possibly relative) URL database."""

    @classmethod
    def setup_class(cls) -> None:
        cls.cwd = os.getcwd()
        cls.path = os.path.join(tempfile.gettempdir(), f"auto_trade_dh_rel_{os.getpid()}.db")
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)
        # Use a relative URL by changing cwd; the engine URL database will be
        # relative, but database_list resolves to the absolute path.
        os.chdir(tempfile.gettempdir())
        rel_name = os.path.basename(cls.path)
        cls.engine: Engine = create_engine(
            f"sqlite:///{rel_name}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(cls.engine, "connect")
        def _set_wal(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            finally:
                cur.close()

        Base.metadata.create_all(bind=cls.engine)
        with cls.engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS dh_rel (x INTEGER)"))
            conn.execute(text("INSERT INTO dh_rel VALUES (1)"))
            conn.commit()

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        os.chdir(cls.cwd)
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)

    def test_relative_file_resolves_wal(self) -> None:
        snap = DatabaseHealthService(self.engine).snapshot()
        assert snap.dialect == "sqlite"
        assert snap.journal_mode == "wal"
        # The resolved absolute path was used to stat the -wal sidecar.
        assert snap.wal_size_bytes is not None
        assert snap.wal_size_bytes >= 0


class TestDatabaseHealthServiceStatError:
    """Non-FileNotFoundError stat errors return None (unavailable), not 0."""

    def test_permission_error_returns_none(self) -> None:
        # Build a file-backed engine, then monkeypatch os.path.getsize to raise
        # PermissionError (a non-FileNotFoundError OSError) to prove the WAL
        # size is reported as None (unavailable) rather than 0 (absent).
        path = os.path.join(tempfile.gettempdir(), f"auto_trade_dh_stat_{os.getpid()}.db")
        for p in (path, f"{path}-wal", f"{path}-shm"):
            if os.path.exists(p):
                os.remove(p)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _set_wal(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            finally:
                cur.close()

        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS dh_stat (x INTEGER)"))
            conn.execute(text("INSERT INTO dh_stat VALUES (1)"))
            conn.commit()
        engine.dispose()
        try:
            # Reopen read-only-ish; the -wal sidecar now exists.
            engine2 = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
            import app.services.database_health_service as dh_mod

            original_getsize = dh_mod.os.path.getsize

            def _raise_permission(p: str) -> int:
                if p.endswith("-wal"):
                    raise PermissionError("simulated permission denied")
                return original_getsize(p)

            dh_mod.os.path.getsize = _raise_permission  # type: ignore[assignment]
            try:
                snap = DatabaseHealthService(engine2).snapshot()
            finally:
                dh_mod.os.path.getsize = original_getsize  # type: ignore[assignment]
            # Permission error -> None (unavailable), not 0 (absent).
            assert snap.wal_size_bytes is None
            engine2.dispose()
        finally:
            for p in (path, f"{path}-wal", f"{path}-shm"):
                if os.path.exists(p):
                    os.remove(p)


class TestDatabaseHealthSnapshotFromSession:
    def test_snapshot_from_session_uses_bound_engine(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        try:
            with Session(bind=engine) as db:
                snap = snapshot_from_session(db)
            assert snap.dialect == "sqlite"
            assert snap.wal_size_bytes is None  # in-memory
        finally:
            engine.dispose()


class TestDatabaseHealthAPI:
    """API tests via get_db dependency override (file-backed engine)."""

    @classmethod
    def setup_class(cls) -> None:
        cls.path = os.path.join(
            tempfile.gettempdir(), f"auto_trade_dh_api_{os.getpid()}.db"
        )
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)
        cls.engine: Engine = create_engine(
            f"sqlite:///{cls.path}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(cls.engine, "connect")
        def _set_wal(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            finally:
                cur.close()

        Base.metadata.create_all(bind=cls.engine)
        with cls.engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS dh_api (x INTEGER)"))
            conn.execute(text("INSERT INTO dh_api VALUES (1)"))
            conn.commit()

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()
        for p in (cls.path, f"{cls.path}-wal", f"{cls.path}-shm"):
            if os.path.exists(p):
                os.remove(p)

    def setup_method(self) -> None:
        settings.api_key = ""

    def test_endpoint_returns_snapshot_fields(self) -> None:
        resp = self.client.get("/api/database-health")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # All required fields present.
        for key in (
            "checked_at",
            "dialect",
            "journal_mode",
            "page_size_bytes",
            "page_count",
            "freelist_count",
            "used_page_count",
            "database_size_bytes",
            "free_space_bytes",
            "wal_size_bytes",
        ):
            assert key in data, f"missing field {key}"
        assert data["dialect"] == "sqlite"
        # File-backed engine -> WAL size is an integer (0 if no -wal sidecar).
        assert isinstance(data["wal_size_bytes"], int)
        assert data["page_size_bytes"] > 0
        assert data["page_count"] >= 0

    def test_endpoint_metrics_consistent(self) -> None:
        resp = self.client.get("/api/database-health")
        data = resp.json()
        ps = data["page_size_bytes"]
        pc = data["page_count"]
        fc = data["freelist_count"]
        assert data["database_size_bytes"] == ps * pc
        assert data["free_space_bytes"] == ps * fc
        assert data["used_page_count"] == pc - fc
        assert data["used_page_count"] >= 0

    def test_endpoint_does_not_leak_paths(self) -> None:
        resp = self.client.get("/api/database-health")
        body = resp.text
        # No filesystem path, db filename, or connection URL leaked. The JSON
        # response (keys + ISO timestamp values) contains no path separators.
        assert "auto_trade" not in body
        assert "tmp" not in body
        assert ".db" not in body
        assert "sqlite:///" not in body
        assert "/" not in body

    def test_auth_enforced_when_api_key_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "dh-secret")
        assert self.client.get("/api/database-health").status_code == 401
        resp = self.client.get("/api/database-health", headers={"X-API-Key": "dh-secret"})
        assert resp.status_code == 200

    def test_auth_disabled_in_dev_without_key(self) -> None:
        settings.api_key = ""
        assert self.client.get("/api/database-health").status_code == 200