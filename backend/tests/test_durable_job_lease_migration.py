from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.models import DurableJobLease


_REVISION = "20260801_durable_job_leases"
_PREDECESSOR = "20260801_watchlist_quant_v6"
_HEAD_REVISION = "20260802_opening_breakout_depth"


def _create_lease_table(engine: Engine) -> None:
    lease_table = DurableJobLease.__table__
    assert isinstance(lease_table, Table)
    lease_table.create(engine)


def _alembic_config(backend_root: Path, db_path: Path) -> Config:
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _run_entrypoint_legacy_stamp(
    backend_root: Path,
    db_path: Path,
) -> subprocess.CompletedProcess[str]:
    entrypoint = (backend_root / "docker-entrypoint.sh").read_text(
        encoding="utf-8",
    )
    marker = 'python -c "\n'
    suffix = '\n"\n\n# 覆盖 alembic.ini'
    stamp_code = entrypoint.split(marker, 1)[1].split(suffix, 1)[0]
    stamp_code = stamp_code.replace('\\"', '"')
    environment = os.environ.copy()
    environment.update(
        {
            "AUTO_TRADE_ENV": "test",
            "AUTO_TRADE_API_KEY": "test-key",
            "AUTO_TRADE_DATABASE_URL": f"sqlite:///{db_path}",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", stamp_code],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_durable_job_lease_revision_is_head_and_round_trips(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "lease-migration.db"
    config = _alembic_config(backend_root, db_path)
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == _HEAD_REVISION
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _PREDECESSOR

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert tuple(
        (
            str(column["name"]),
            str(column["type"]).upper(),
            bool(column["nullable"]),
        )
        for column in inspector.get_columns("durable_job_leases")
    ) == (
        ("lease_key", "VARCHAR(128)", False),
        ("holder_id", "VARCHAR(128)", False),
        ("fencing_token", "INTEGER", False),
        ("acquired_at_epoch_ms", "INTEGER", False),
        ("renewed_at_epoch_ms", "INTEGER", False),
        ("expires_at_epoch_ms", "INTEGER", False),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO durable_job_leases VALUES "
                "('job', 'holder', 1, 1, 1, 2)"
            )
        )
    with pytest.raises(IntegrityError, match="cannot be deleted"):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM durable_job_leases WHERE lease_key = 'job'")
            )

    command.downgrade(config, _PREDECESSOR)
    assert "durable_job_leases" not in inspect(engine).get_table_names()
    engine.dispose()


def test_entrypoint_stamps_complete_legacy_head_schema(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "legacy-head.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _HEAD_REVISION
    engine.dispose()


def test_entrypoint_advances_recorded_predecessor_for_prebuilt_lease_schema(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "prebuilt-lease.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    _create_lease_table(engine)

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _REVISION
    engine.dispose()


def test_entrypoint_rejects_partial_lease_schema(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "partial-lease.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE durable_job_leases "
                "(lease_key VARCHAR(128) NOT NULL PRIMARY KEY)"
            )
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert "partial durable-job-lease schema" in result.stderr
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _PREDECESSOR
    engine.dispose()


def test_entrypoint_rejects_nonblocking_lease_delete_trigger(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "bad-lease-trigger.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    _create_lease_table(engine)
    with engine.begin() as connection:
        connection.execute(
            text("DROP TRIGGER trg_durable_job_leases_no_delete")
        )
        connection.execute(
            text(
                "CREATE TRIGGER trg_durable_job_leases_no_delete "
                "BEFORE DELETE ON durable_job_leases "
                "BEGIN SELECT 1; END"
            )
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert "no-delete trigger does not match canonical DDL" in result.stderr
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _PREDECESSOR
    engine.dispose()
