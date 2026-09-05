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
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
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


def _old_quant_v6_rows(db: Session) -> None:
    old = _NOW - timedelta(days=60)
    registration = WatchlistQuantV6Registration(
        identity_sha256="1" * 64,
        schema_version=1,
        contract_version="watchlist-quant-v6-registration-v1",
        selection_rule_version="rotation-research-catalog-pit-v1",
        algorithm_version="watchlist-quant-v6-v1",
        semantic_digest_sha256="2" * 64,
        evaluator_digest_sha256="3" * 64,
        acquisition_spec_sha256="4" * 64,
        cohort_source="ROTATION_RESEARCH_CATALOG_PIT",
        market="US",
        source_snapshot_sha256="5" * 64,
        cohort_manifest_sha256="6" * 64,
        cohort_member_count=1,
        schedule_sha256="7" * 64,
        training_session_count=10,
        target_session_count=30,
        first_training_session_date=(old - timedelta(days=45)).date(),
        first_target_session_date=(old - timedelta(days=30)).date(),
        last_target_session_date=old.date(),
        data_cutoff_at=old,
        bar_period="MIN_5",
        adjustment_mode="NO_ADJUST",
        registration_json="{}",
        server_generated=True,
        short_entry_allowed=False,
        position_add_on_allowed=False,
        order_submission_allowed=False,
        automatic_promotion_allowed=False,
        cohort_observed_at=old,
        registered_at=old,
    )
    db.add(registration)
    db.flush()
    publication = WatchlistQuantV6Publication(
        registration_id=registration.id,
        registration_identity_sha256=registration.identity_sha256,
        identity_sha256="8" * 64,
        schema_version=1,
        contract_version="watchlist-quant-v6-publication-v1",
        status="PUBLISHED",
        manifest_sha256="9" * 64,
        publication_json="{}",
        registered_member_count=1,
        assessment_artifact_count=1,
        session_input_artifact_count=0,
        event_artifact_count=0,
        binding_count=1,
        promotion_eligible=False,
        automatic_promotion_allowed=False,
        order_submission_allowed=False,
        short_entry_allowed=False,
        position_add_on_allowed=False,
        published_at=old,
    )
    db.add(publication)
    db.flush()
    db.add(WatchlistQuantV6Artifact(
        digest_sha256="a" * 64,
        schema_version=1,
        kind="WATCHLIST_QUANT_V6_ASSESSMENT",
        codec="zlib",
        compression_level=9,
        raw_size=1,
        compressed_size=1,
        payload=b"x",
        created_at=old,
    ))
    db.add(WatchlistQuantV6PublicationArtifact(
        publication_id=publication.id,
        member_ordinal=0,
        symbol="AAPL.US",
        market="US",
        role="ASSESSMENT",
        artifact_ordinal=0,
        session_date=None,
        artifact_sha256="a" * 64,
        artifact_kind="WATCHLIST_QUANT_V6_ASSESSMENT",
        binding_sha256="b" * 64,
        created_at=old,
    ))
    db.commit()


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
        _old_quant_v6_rows(session)
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
            "bindings": session.query(WatchlistQuantV6PublicationArtifact).count(),
            "artifacts": session.query(WatchlistQuantV6Artifact).count(),
            "publications": session.query(WatchlistQuantV6Publication).count(),
            "registrations": session.query(WatchlistQuantV6Registration).count(),
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
    monkeypatch.setattr(
        maintenance.settings, "watchlist_quant_v6_artifact_retention_days", 30
    )

    exit_code = maintenance.main(_argv(db_path, backups, dest))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "PREVIEW"
    assert payload["retention"]["watchlist_quant_v6"]["bindings"] == 1
    assert payload["retention"]["watchlist_quant_v6"]["artifacts"] == 1
    assert payload["retention"]["strategy_v2_diagnostic_wait"]["decisions"] == 1
    assert payload["applied"] is None
    assert payload["page_usage_available"] is True
    assert any(
        entry["name"] == "watchlist_quant_v6_artifacts"
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
    assert _counts(db_path) == {
        "bindings": 1,
        "artifacts": 1,
        "publications": 1,
        "registrations": 1,
        "decisions": 1,
    }
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
    # The shipped default is 0 because the bindings are append-only in a
    # trigger-equipped database; enable a window explicitly so this still
    # exercises the prune path rather than the disabled short-circuit.
    monkeypatch.setattr(
        maintenance.settings, "watchlist_quant_v6_artifact_retention_days", 30
    )

    exit_code = maintenance.main(_argv(db_path, backups, dest, "--apply"))
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "APPLY"
    assert payload["applied"]["watchlist_quant_v6"]["bindings_deleted"] == 1
    assert payload["applied"]["watchlist_quant_v6"]["artifacts_deleted"] == 1
    assert payload["applied"]["strategy_v2_diagnostic_wait"]["deleted"] == 1
    # Then: bulk payload rows are gone but provenance rows survive.
    assert _counts(db_path) == {
        "bindings": 0,
        "artifacts": 0,
        "publications": 1,
        "registrations": 1,
        "decisions": 0,
    }


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
    assert _counts(db_path)["bindings"] == 1


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
