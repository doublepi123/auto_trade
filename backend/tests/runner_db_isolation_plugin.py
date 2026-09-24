from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


@contextmanager
def runner_database_for(test_path: Path) -> Iterator[None]:
    """Own one database for a legacy runner test, leaving other paths untouched."""
    if test_path.resolve() != Path(__file__).with_name("test_runner.py").resolve():
        yield
        return

    # Import only after matching: non-target tests retain their import lifecycle.
    from app import database, runner
    from app.api import deps
    from app.services import order_terminal_callback_service

    with TemporaryDirectory(
        prefix=f"runner_db_{os.getpid()}_",
        # A runner daemon thread may briefly reopen the file after dispose and
        # recreate -wal/-shm; the explicit rmtree below still removes them.
        ignore_cleanup_errors=True,
    ) as directory:
        filename = Path(directory) / f"runner_{os.getpid()}_{uuid4().hex}.db"
        url = f"sqlite:///{filename}"
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 60},
            **database.queue_pool_kwargs(url),
        )
        try:
            event.listen(engine, "connect", database._set_sqlite_pragmas)
            database.SessionReentrancyGuard(strict=True).install(engine)
            factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(database, "engine", engine)
                for module in (database, runner, order_terminal_callback_service):
                    patch.setattr(module, "SessionLocal", factory)
                # A lazily created audit singleton would otherwise stay bound
                # to this private database after it is deleted.
                patch.setattr(deps, "_audit_logger_singleton", deps._audit_logger_singleton)
                database.init_db()
                yield
        finally:
            engine.dispose()
            shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_runner_database(request: pytest.FixtureRequest) -> Iterator[None]:
    with runner_database_for(request.node.path):
        yield
