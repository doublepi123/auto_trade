from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    StrategyV2ShadowDecision,
)


_NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _load_script() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "database_maintenance.py"
    )
    spec = importlib.util.spec_from_file_location(
        "database_maintenance", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def maintenance() -> ModuleType:
    return _load_script()


def _engine(db_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


def _old_decision(db: Session) -> None:
    old = _NOW - timedelta(days=120)
    db.add(StrategyV2ShadowDecision(
        idempotency_key="decision-old-gate",
        symbol="NVDA.US",
        market="US",
        config_version="version-a",
        session_date=old.date(),
        bar_at=old,
        observed_at=old,
        action="WAIT",
        reason="NO_BREACH",
        state_before="READY",
        state_after="READY",
        close_price=100.0,
        gate_passed=True,
        breach_armed=False,
        virtual_position="FLAT",
        quantity=0.0,
        exit_reason="",
        gate_reasons_json="[]",
        features_json="{}",
        created_at=old,
    ))
    db.commit()


def _seed_db(db_path: Path) -> None:
    engine = _engine(db_path)
    with Session(bind=engine) as session:
        _old_decision(session)
    engine.dispose()


def _backups(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        path = directory / name
        path.write_bytes(b"backup" * 16)
        older = _NOW - timedelta(days=len(names) - index)
        timestamp = older.timestamp()
        import os

        os.utime(path, (timestamp, timestamp))


def _counts(db_path: Path) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(bind=engine) as session:
        counts = {
            "decisions": session.query(StrategyV2ShadowDecision).count(),
        }
    engine.dispose()
    return counts


def _argv(db_path: Path, backups: Path, dest: Path, *extra: str) -> list[str]:
    return [
        "--database-url",
        f"sqlite:///{db_path}",
        "--backup-dir",
        str(backups),
        "--backup-dest",
        str(dest),
        *extra,
    ]


def test_preview_reports_plan_without_mutating(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "auto_trade.db"
    backups = tmp_path / "data" / "backups"
    dest = tmp_path / "offsite"
    _seed_db(db_path)
    _backups(backups, ["auto_trade-2026-08-05.db", "auto_trade-2026-08-22.db"])
    exit_code = maintenance.main(_argv(db_path, backups, dest))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "PREVIEW"
    assert "watchlist_quant_v6" not in payload["retention"]
    assert payload["retention"]["strategy_v2_diagnostic_wait"]["decisions"] == 1
    assert payload["applied"] is None
    assert payload["page_usage_available"] is True
    assert any(
        entry["name"] == "strategy_v2_shadow_decisions"
        for entry in payload["page_usage"]
    )
    assert payload["projection"]["current_bytes"] > 0
    assert payload["projection"]["projected_bytes"] <= (
        payload["projection"]["current_bytes"]
    )
    relocation = payload["backup_relocation"]
    assert relocation["applied"] is False
    assert len(relocation["move"]) == 2
    # Then: preview mutated nothing — rows and backup files are untouched.
    assert _counts(db_path) == {"decisions": 1}
    assert sorted(path.name for path in backups.iterdir()) == [
        "auto_trade-2026-08-05.db",
        "auto_trade-2026-08-22.db",
    ]
    assert not dest.exists()


def test_apply_prunes_expired_rows_and_keeps_provenance(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "auto_trade.db"
    backups = tmp_path / "data" / "backups"
    dest = tmp_path / "offsite"
    _seed_db(db_path)
    exit_code = maintenance.main(_argv(db_path, backups, dest, "--apply"))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "APPLY"
    assert "watchlist_quant_v6" not in payload["applied"]
    assert payload["applied"]["strategy_v2_diagnostic_wait"]["deleted"] == 1
    assert _counts(db_path) == {"decisions": 0}


def test_vacuum_refused_during_market_hours(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "auto_trade.db"
    backups = tmp_path / "data" / "backups"
    dest = tmp_path / "offsite"
    _seed_db(db_path)
    monkeypatch.setattr(
        maintenance, "is_trading_hours", lambda *_args, **_kwargs: True
    )

    exit_code = maintenance.main(
        _argv(db_path, backups, dest, "--apply", "--vacuum")
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "market" in captured.err.lower()
    # Then: the refusal is all-or-nothing — no retention was applied either.
    assert _counts(db_path)["decisions"] == 1


def test_vacuum_runs_outside_market_hours(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "auto_trade.db"
    backups = tmp_path / "data" / "backups"
    dest = tmp_path / "offsite"
    _seed_db(db_path)
    monkeypatch.setattr(
        maintenance, "is_trading_hours", lambda *_args, **_kwargs: False
    )

    exit_code = maintenance.main(
        _argv(db_path, backups, dest, "--apply", "--vacuum")
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["vacuum"]["applied"] is True


def test_backup_relocation_apply_moves_and_keeps_rolling_n(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "auto_trade.db"
    backups = tmp_path / "data" / "backups"
    dest = tmp_path / "offsite"
    _seed_db(db_path)
    _backups(
        backups,
        [
            "auto_trade-2026-08-01.db",
            "auto_trade-2026-08-05.db",
            "auto_trade-2026-08-22.db",
            "auto_trade-2026-08-29.db",
        ],
    )

    exit_code = maintenance.main(
        _argv(db_path, backups, dest, "--apply", "--backup-keep", "2")
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    relocation = payload["backup_relocation"]
    assert relocation["applied"] is True
    assert sorted(path.name for path in dest.iterdir()) == [
        "auto_trade-2026-08-22.db",
        "auto_trade-2026-08-29.db",
    ]
    assert list(backups.iterdir()) == []
    assert len(relocation["delete"]) == 2


def test_backup_relocation_refuses_live_db_directory(
    maintenance: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "auto_trade.db"
    _seed_db(db_path)

    exit_code = maintenance.main([
        "--database-url",
        f"sqlite:///{db_path}",
        "--backup-dir",
        str(tmp_path),
        "--apply",
    ])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "live" in captured.err.lower()
    assert db_path.exists()
