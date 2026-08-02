from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


_REVISION = "20260802_opening_breakout_depth"
_PREDECESSOR = "20260801_durable_job_leases"
_WATCHLIST_REVISION = "20260801_watchlist_quant_v6"
_TABLE = "opening_momentum_shadow_runs"
_COLUMN = "candidate_breakout_depth_bps"


def _alembic_config(backend_root: Path, db_path: Path) -> Config:
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _column_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {
            str(column["name"])
            for column in inspect(engine).get_columns(_TABLE)
        }
    finally:
        engine.dispose()


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
    environment.update({
        "AUTO_TRADE_ENV": "test",
        "AUTO_TRADE_API_KEY": "test-key",
        "AUTO_TRADE_DATABASE_URL": f"sqlite:///{db_path}",
    })
    return subprocess.run(
        [sys.executable, "-c", stamp_code],
        cwd=backend_root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_opening_breakout_depth_revision_is_head_and_preserves_rows(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "opening-breakout-depth.db"
    config = _alembic_config(backend_root, db_path)
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == _REVISION
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _PREDECESSOR

    command.upgrade(config, _PREDECESSOR)
    assert _COLUMN not in _column_names(db_path)

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO opening_momentum_shadow_runs ("
            "session_date, algorithm_version, config_version, status, "
            "signal_at, observed_at, estimated_cost_bps, created_at, "
            "updated_at) VALUES ("
            "'2026-07-31', 'legacy', 'legacy-config', 'SKIPPED', "
            "'2026-07-31 13:35:00', '2026-07-31 13:36:00', 30.0, "
            "'2026-07-31 13:36:00', '2026-07-31 13:36:00')"
        ))
    engine.dispose()

    command.upgrade(config, "head")
    assert _COLUMN in _column_names(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT candidate_breakout_depth_bps "
            "FROM opening_momentum_shadow_runs "
            "WHERE config_version = 'legacy-config'"
        )).scalar_one() is None
    engine.dispose()

    command.downgrade(config, _PREDECESSOR)
    assert _COLUMN not in _column_names(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM opening_momentum_shadow_runs "
            "WHERE config_version = 'legacy-config'"
        )).scalar_one() == 1
    engine.dispose()

    command.upgrade(config, "head")
    assert _COLUMN in _column_names(db_path)


def test_entrypoint_advances_recorded_predecessor_for_prebuilt_column(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "prebuilt-opening-breakout-depth.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} FLOAT"
        ))

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == _REVISION
    engine.dispose()


def test_entrypoint_advances_combined_direct_start_schema_to_head(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "combined-direct-start-schema.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _REVISION)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": _WATCHLIST_REVISION},
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == _REVISION
    engine.dispose()


def test_entrypoint_rejects_recorded_head_without_breakout_depth_column(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "missing-opening-breakout-depth.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": _REVISION},
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert (
        "alembic_version is opening-breakout-depth but its schema or "
        "predecessor schema is incomplete"
    ) in result.stderr
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT version_num FROM alembic_version"
        )).scalar_one() == _REVISION
    engine.dispose()
