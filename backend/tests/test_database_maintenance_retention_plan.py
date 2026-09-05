from __future__ import annotations

import importlib.util
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    StrategyV2ForwardReplayArtifact,
    StrategyV2ShadowDecision,
    WatchlistQuantV6Artifact,
)


_NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
_PLAN_CASES = (
    ("_quant_v6_plan", "artifacts"),
    ("_forward_replay_plan", "artifacts"),
    ("_diagnostic_wait_plan", "decisions"),
)


def _load_script() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "database_maintenance.py"
    )
    spec = importlib.util.spec_from_file_location(
        "database_maintenance_retention_plan", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def maintenance() -> ModuleType:
    return _load_script()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Session, Path, Engine]]:
    db_path = tmp_path / "maintenance.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    with Session(bind=engine) as session:
        yield session, db_path, engine
    engine.dispose()


def _seed_rows(session: Session, *ages: timedelta) -> None:
    for index, age in enumerate(ages, start=1):
        created_at = _NOW - age
        session.add(WatchlistQuantV6Artifact(
            digest_sha256=f"{index:064x}",
            schema_version=1,
            kind="WATCHLIST_QUANT_V6_ASSESSMENT",
            codec="zlib",
            compression_level=9,
            raw_size=1,
            compressed_size=1,
            payload=b"x",
            created_at=created_at,
        ))
        session.add(StrategyV2ForwardReplayArtifact(
            digest_sha256=f"{index + 100:064x}",
            schema_version=1,
            kind="STRATEGY_V2_FORWARD_REPLAY",
            codec="zlib",
            raw_size=1,
            compressed_size=1,
            payload=b"x",
            created_at=created_at,
        ))
        session.add(StrategyV2ShadowDecision(
            idempotency_key=f"maintenance-plan-{index}",
            symbol="NVDA.US",
            market="US",
            config_version="version-a",
            session_date=created_at.date(),
            bar_at=created_at,
            observed_at=created_at,
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
            created_at=created_at,
        ))
    session.commit()


@pytest.mark.parametrize(("plan_name", "count_key"), _PLAN_CASES)
def test_plan_reports_zero_when_retention_window_is_disabled(
    maintenance: ModuleType,
    database: tuple[Session, Path, Engine],
    plan_name: str,
    count_key: str,
) -> None:
    session, _, _ = database
    _seed_rows(session, timedelta(days=60))

    plan = getattr(maintenance, plan_name)(
        session,
        retention_days=0,
        now=_NOW,
    )

    assert plan[count_key] == 0


@pytest.mark.parametrize(("plan_name", "count_key"), _PLAN_CASES)
def test_plan_counts_rows_older_than_enabled_window(
    maintenance: ModuleType,
    database: tuple[Session, Path, Engine],
    plan_name: str,
    count_key: str,
) -> None:
    session, _, _ = database
    _seed_rows(session, timedelta(days=60))

    plan = getattr(maintenance, plan_name)(
        session,
        retention_days=30,
        now=_NOW,
    )

    assert plan[count_key] == 1


@pytest.mark.parametrize(("plan_name", "count_key"), _PLAN_CASES)
def test_plan_applies_retention_cutoff_boundary(
    maintenance: ModuleType,
    database: tuple[Session, Path, Engine],
    plan_name: str,
    count_key: str,
) -> None:
    session, _, _ = database
    _seed_rows(session, timedelta(days=2), timedelta(hours=2))

    plan = getattr(maintenance, plan_name)(
        session,
        retention_days=1,
        now=_NOW,
    )

    assert plan[count_key] == 1


@pytest.mark.parametrize(("plan_name", "count_key"), _PLAN_CASES)
def test_plan_rejects_negative_retention_window(
    maintenance: ModuleType,
    database: tuple[Session, Path, Engine],
    plan_name: str,
    count_key: str,
) -> None:
    session, _, _ = database
    _seed_rows(session, timedelta(days=60))

    with pytest.raises(ValueError, match="retention_days must be non-negative"):
        getattr(maintenance, plan_name)(
            session,
            retention_days=-1,
            now=_NOW,
        )


def test_preview_projects_no_reduction_when_all_windows_are_disabled(
    maintenance: ModuleType,
    database: tuple[Session, Path, Engine],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, db_path, _ = database
    _seed_rows(session, timedelta(days=60))
    monkeypatch.setattr(
        maintenance.settings,
        "watchlist_quant_v6_artifact_retention_days",
        0,
    )
    monkeypatch.setattr(
        maintenance.settings,
        "strategy_v2_forward_replay_artifact_retention_days",
        0,
    )
    monkeypatch.setattr(
        maintenance.settings,
        "strategy_v2_diagnostic_wait_retention_days",
        0,
    )

    exit_code = maintenance.main([
        "--database-url",
        f"sqlite:///{db_path}",
        "--backup-dir",
        str(tmp_path / "source-backups"),
        "--backup-dest",
        str(tmp_path / "destination-backups"),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["retention"]["watchlist_quant_v6"]["artifacts"] == 0
    assert payload["retention"]["strategy_v2_forward_replay"]["artifacts"] == 0
    assert payload["retention"]["strategy_v2_diagnostic_wait"]["decisions"] == 0
    assert payload["projection"]["est_freed_bytes"] == 0
    assert payload["projection"]["projected_bytes"] == payload["projection"][
        "current_bytes"
    ]
