from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app import database, models


_REVISION = "20260921_fill_settlements"
_PREDECESSOR = "20260802_opening_breakout_depth"
_TRIGGER = "trg_fill_settlements_no_delete"
_INSERT = (
    "INSERT INTO fill_settlements (broker_order_id, symbol, action, "
    "booked_quantity, booked_price, quantity_source, price_source, "
    "tracked_quantity_after, tracked_cost_after, first_terminal_status, created_at) "
    "VALUES ('order-1', 'AAPL.US', 'OPEN_LONG', 1, 100, 'BROKER', 'BROKER', "
    "1, 100, ?, CURRENT_TIMESTAMP)"
)


def _alembic_config(db_path: Path) -> Config:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _run_entrypoint_legacy_stamp(db_path: Path) -> subprocess.CompletedProcess[str]:
    backend_root = Path(__file__).resolve().parents[1]
    entrypoint = (backend_root / "docker-entrypoint.sh").read_text(encoding="utf-8")
    stamp_code = entrypoint.split('python -c "\n', 1)[1].split(
        '\n"\n\n# 覆盖 alembic.ini', 1,
    )[0].replace('\\"', '"')
    environment = os.environ.copy()
    environment.update({
        "AUTO_TRADE_ENV": "test",
        "AUTO_TRADE_API_KEY": "test-key",
        "AUTO_TRADE_DATABASE_URL": f"sqlite:///{db_path}",
    })
    return subprocess.run(
        [sys.executable, "-c", stamp_code], cwd=backend_root, env=environment,
        capture_output=True, check=False, text=True,
    )


def test_alembic_head_is_fill_settlements(tmp_path: Path) -> None:
    # Given
    script = ScriptDirectory.from_config(_alembic_config(tmp_path / "head.db"))
    # When / Then
    assert script.get_current_head() == _REVISION
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _PREDECESSOR


def test_upgrade_creates_table_and_trigger_and_downgrade_drops_them(
    tmp_path: Path,
) -> None:
    # Given
    db_path = tmp_path / "migration.db"
    config = _alembic_config(db_path)
    command.upgrade(config, _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        # When
        command.upgrade(config, "head")
        # Then
        assert "fill_settlements" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (_TRIGGER,),
            ).scalar_one() == _TRIGGER
        command.downgrade(config, _PREDECESSOR)
        assert "fill_settlements" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (_TRIGGER,),
            ).scalar_one_or_none() is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("recorded_revision", [_PREDECESSOR, _REVISION])
def test_entrypoint_stamps_existing_table_to_head(
    tmp_path: Path, recorded_revision: str,
) -> None:
    # Given
    db_path = tmp_path / "stamp.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = ?", (recorded_revision,),
            )
        # When
        result = _run_entrypoint_legacy_stamp(db_path)
        # Then
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version",
            ).scalar_one() == _REVISION
    finally:
        engine.dispose()


def test_entrypoint_refuses_partial_schema(tmp_path: Path) -> None:
    # Given
    db_path = tmp_path / "partial.db"
    command.upgrade(_alembic_config(db_path), _PREDECESSOR)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE fill_settlements (broker_order_id TEXT NOT NULL PRIMARY KEY)"
            )
        # When
        result = _run_entrypoint_legacy_stamp(db_path)
        # Then
        assert result.returncode != 0
        assert "partial fill-settlements schema" in result.stderr
    finally:
        engine.dispose()


def test_entrypoint_refuses_unexpected_lineage(tmp_path: Path) -> None:
    # Given
    db_path = tmp_path / "lineage.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = '20260801_watchlist_quant_v6'"
            )
        # When
        result = _run_entrypoint_legacy_stamp(db_path)
        # Then
        assert result.returncode != 0
        assert "fill-settlements" in result.stderr
        assert "lineage" in result.stderr
    finally:
        engine.dispose()


@pytest.mark.parametrize("creation", ["alembic", "runtime", "orm"])
def test_receipt_schema_preserves_identity_and_allows_risk_marking(
    tmp_path: Path, creation: str,
) -> None:
    # Given
    db_path = tmp_path / f"receipt-{creation}.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        if creation == "alembic":
            command.upgrade(_alembic_config(db_path), "head")
        elif creation == "runtime":
            database._ensure_fill_settlements_table(engine)
        else:
            models.Base.metadata.create_all(engine)
        # When
        with engine.begin() as connection:
            connection.exec_driver_sql(_INSERT, ("FILLED",))
            connection.exec_driver_sql(
                "UPDATE fill_settlements SET risk_applied_at = CURRENT_TIMESTAMP, "
                "risk_applied_via = 'LIVE' WHERE broker_order_id = 'order-1'"
            )
        # Then
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT risk_applied_at IS NOT NULL, risk_applied_via, cost_basis_price, "
                "consumed_quantity, gross_pnl, net_pnl, pnl_source, tracked_side "
                "FROM fill_settlements"
            ).one() == (1, "LIVE", None, None, None, None, None, None)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(_INSERT, ("CANCELED",))
        with pytest.raises(IntegrityError, match="cannot be deleted"):
            with engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM fill_settlements")
    finally:
        engine.dispose()
