from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app import database
from app.models import (
    Base,
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
)


_ASSESSMENT_KIND = "WATCHLIST_QUANT_V6_ASSESSMENT"
_EVENT_KIND = "WATCHLIST_QUANT_V6_EVENT"
_SESSION_INPUT_KIND = "WATCHLIST_QUANT_V6_SESSION_INPUT"
_TABLES = set(database.WATCHLIST_QUANT_V6_TABLE_NAMES)


def _sqlite_engine(url: str) -> Engine:
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()

    return engine


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


def _registration(**overrides: object) -> WatchlistQuantV6Registration:
    cutoff_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    observed_at = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "identity_sha256": "1" * 64,
        "schema_version": 1,
        "contract_version": "watchlist-quant-v6-registration-v1",
        "selection_rule_version": "rotation-research-catalog-pit-v1",
        "algorithm_version": "watchlist-quant-v6-v1",
        "semantic_digest_sha256": "2" * 64,
        "evaluator_digest_sha256": "3" * 64,
        "acquisition_spec_sha256": "4" * 64,
        "cohort_source": "ROTATION_RESEARCH_CATALOG_PIT",
        "market": "US",
        "source_snapshot_sha256": "5" * 64,
        "cohort_manifest_sha256": "6" * 64,
        "cohort_member_count": 1,
        "schedule_sha256": "7" * 64,
        "training_session_count": 10,
        "target_session_count": 30,
        "first_training_session_date": date(2026, 5, 1),
        "first_target_session_date": date(2026, 5, 15),
        "last_target_session_date": date(2026, 6, 26),
        "data_cutoff_at": cutoff_at,
        "bar_period": "MIN_5",
        "adjustment_mode": "NO_ADJUST",
        "registration_json": (
            '{"cohort":{"member_count":1,"members":['
            '{"market":"US","ordinal":0,"symbol":"AAPL.US"}]}}'
        ),
        "server_generated": True,
        "short_entry_allowed": False,
        "position_add_on_allowed": False,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "cohort_observed_at": observed_at,
        "registered_at": observed_at,
    }
    values.update(overrides)
    return WatchlistQuantV6Registration(**values)


def _artifact(
    digest: str,
    kind: str,
) -> WatchlistQuantV6Artifact:
    return WatchlistQuantV6Artifact(
        digest_sha256=digest,
        schema_version=1,
        kind=kind,
        codec="zlib",
        compression_level=9,
        raw_size=1,
        compressed_size=1,
        payload=b"x",
        created_at=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )


def _publication(
    registration_id: int,
    **overrides: object,
) -> WatchlistQuantV6Publication:
    values: dict[str, object] = {
        "registration_id": registration_id,
        "registration_identity_sha256": "1" * 64,
        "identity_sha256": "8" * 64,
        "schema_version": 1,
        "contract_version": "watchlist-quant-v6-publication-v1",
        "status": "PUBLISHED",
        "manifest_sha256": "9" * 64,
        "publication_json": "{}",
        "registered_member_count": 1,
        "assessment_artifact_count": 1,
        "session_input_artifact_count": 0,
        "event_artifact_count": 2,
        "binding_count": 3,
        "promotion_eligible": False,
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "short_entry_allowed": False,
        "position_add_on_allowed": False,
        "published_at": datetime(
            2026,
            7,
            31,
            20,
            0,
            tzinfo=timezone.utc,
        ),
    }
    values.update(overrides)
    return WatchlistQuantV6Publication(**values)


def _binding(
    publication_id: int,
    *,
    role: str,
    artifact_ordinal: int,
    artifact_sha256: str,
    artifact_kind: str,
    binding_sha256: str,
    session_date: date | None,
    member_ordinal: int = 0,
    symbol: str = "AAPL.US",
    market: str = "US",
) -> WatchlistQuantV6PublicationArtifact:
    return WatchlistQuantV6PublicationArtifact(
        publication_id=publication_id,
        member_ordinal=member_ordinal,
        symbol=symbol,
        market=market,
        role=role,
        artifact_ordinal=artifact_ordinal,
        session_date=session_date,
        artifact_sha256=artifact_sha256,
        artifact_kind=artifact_kind,
        binding_sha256=binding_sha256,
        created_at=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )


def _assert_reference_rejected_with_foreign_keys_off(
    engine: Engine,
    value: object,
) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        assert connection.exec_driver_sql(
            "PRAGMA foreign_keys"
        ).scalar_one() == 0
        connection.commit()
        with Session(bind=connection) as session:
            session.add(value)
            with pytest.raises(IntegrityError, match="invalid reference"):
                session.commit()
            session.rollback()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


def _published_engine(tmp_path: Path) -> tuple[Engine, dict[str, object]]:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'quant-v6.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    with Session(engine) as session:
        registration = _registration()
        session.add(registration)
        session.flush()
        assessment = _artifact("a" * 64, _ASSESSMENT_KIND)
        event_artifact = _artifact("b" * 64, _EVENT_KIND)
        session.add_all((assessment, event_artifact))
        publication = _publication(registration.id)
        session.add(publication)
        session.flush()
        session.add_all((
            _binding(
                publication.id,
                role="ASSESSMENT",
                artifact_ordinal=0,
                artifact_sha256=assessment.digest_sha256,
                artifact_kind=assessment.kind,
                binding_sha256="c" * 64,
                session_date=None,
            ),
            _binding(
                publication.id,
                role="EVENT",
                artifact_ordinal=0,
                artifact_sha256=event_artifact.digest_sha256,
                artifact_kind=event_artifact.kind,
                binding_sha256="d" * 64,
                session_date=date(2026, 5, 15),
            ),
            _binding(
                publication.id,
                role="EVENT",
                artifact_ordinal=1,
                artifact_sha256=event_artifact.digest_sha256,
                artifact_kind=event_artifact.kind,
                binding_sha256="e" * 64,
                session_date=date(2026, 5, 16),
            ),
        ))
        session.commit()
        identities: dict[str, object] = {
            "watchlist_quant_v6_registrations": registration.id,
            "watchlist_quant_v6_artifacts": assessment.digest_sha256,
            "watchlist_quant_v6_publications": publication.id,
            "watchlist_quant_v6_publication_artifacts": (
                publication.id,
                0,
                "ASSESSMENT",
                0,
            ),
        }
    return engine, identities


def test_create_all_parity_signature_is_complete_and_idempotent(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'parity.db'}")

    database._ensure_watchlist_quant_v6_tables(engine)
    database._ensure_watchlist_quant_v6_tables(engine)

    inspector = inspect(engine)
    assert _TABLES <= set(inspector.get_table_names())
    assert database._watchlist_quant_v6_schema_issues(engine) == ()
    with engine.connect() as connection:
        triggers = {
            str(row[0])
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert len(database.WATCHLIST_QUANT_V6_TRIGGER_NAMES) == 14
    assert set(database.WATCHLIST_QUANT_V6_TRIGGER_NAMES) <= triggers


def test_publication_allows_content_reuse_but_all_tables_are_insert_only(
    tmp_path: Path,
) -> None:
    engine, identities = _published_engine(tmp_path)

    attempts = (
        (
            "watchlist_quant_v6_registrations",
            "id = :identity",
            identities["watchlist_quant_v6_registrations"],
        ),
        (
            "watchlist_quant_v6_artifacts",
            "digest_sha256 = :identity",
            identities["watchlist_quant_v6_artifacts"],
        ),
        (
            "watchlist_quant_v6_publications",
            "id = :identity",
            identities["watchlist_quant_v6_publications"],
        ),
    )
    with engine.connect() as connection:
        reused = connection.execute(text(
            "SELECT count(*) FROM watchlist_quant_v6_publication_artifacts "
            "WHERE artifact_sha256 = :digest"
        ), {"digest": "b" * 64}).scalar_one()
    assert reused == 2

    with pytest.raises(IntegrityError, match="duplicate key"):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT OR REPLACE INTO watchlist_quant_v6_artifacts "
                "SELECT * FROM watchlist_quant_v6_artifacts "
                "WHERE digest_sha256 = :digest"
            ), {"digest": "a" * 64})

    for table_name, predicate, identity in attempts:
        for operation in ("UPDATE", "DELETE"):
            statement = (
                f"UPDATE {table_name} SET {predicate.split(' = ')[0]} = "
                f"{predicate.split(' = ')[0]} WHERE {predicate}"
                if operation == "UPDATE"
                else f"DELETE FROM {table_name} WHERE {predicate}"
            )
            with pytest.raises(IntegrityError, match="append-only"):
                with engine.begin() as connection:
                    connection.execute(text(statement), {"identity": identity})

    binding_identity = identities[
        "watchlist_quant_v6_publication_artifacts"
    ]
    assert isinstance(binding_identity, tuple)
    binding_predicate = (
        "publication_id = :publication_id "
        "AND member_ordinal = :member_ordinal "
        "AND role = :role AND artifact_ordinal = :artifact_ordinal"
    )
    binding_params = {
        "publication_id": binding_identity[0],
        "member_ordinal": binding_identity[1],
        "role": binding_identity[2],
        "artifact_ordinal": binding_identity[3],
    }
    for statement in (
        "UPDATE watchlist_quant_v6_publication_artifacts "
        f"SET symbol = symbol WHERE {binding_predicate}",
        "DELETE FROM watchlist_quant_v6_publication_artifacts "
        f"WHERE {binding_predicate}",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(text(statement), binding_params)


def test_duplicate_guards_cover_every_conflict_key_with_recursion_off(
    tmp_path: Path,
) -> None:
    engine, _ = _published_engine(tmp_path)
    registration_table = Base.metadata.tables[
        "watchlist_quant_v6_registrations"
    ]
    artifact_table = Base.metadata.tables["watchlist_quant_v6_artifacts"]
    publication_table = Base.metadata.tables[
        "watchlist_quant_v6_publications"
    ]
    binding_table = Base.metadata.tables[
        "watchlist_quant_v6_publication_artifacts"
    ]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA recursive_triggers=OFF")
        assert connection.exec_driver_sql(
            "PRAGMA recursive_triggers"
        ).scalar_one() == 0
        registration = dict(connection.execute(
            select(registration_table)
        ).mappings().one())
        assessment_artifact = dict(connection.execute(
            select(artifact_table).where(
                artifact_table.c.kind == _ASSESSMENT_KIND
            )
        ).mappings().one())
        publication = dict(connection.execute(
            select(publication_table)
        ).mappings().one())
        assessment_binding = dict(connection.execute(
            select(binding_table).where(
                binding_table.c.role == "ASSESSMENT"
            )
        ).mappings().one())
        connection.commit()

        extra_registration = registration | {
            "id": 8000,
            "identity_sha256": "e" * 64,
        }
        connection.execute(
            registration_table.insert().values(**extra_registration)
        )
        connection.commit()

        registration_pk = registration | {
            "identity_sha256": "f" * 64,
        }
        registration_identity = registration | {"id": 9001}
        artifact_pk = assessment_artifact | {"kind": _EVENT_KIND}
        publication_pk = publication | {
            "registration_id": 8000,
            "registration_identity_sha256": "e" * 64,
            "identity_sha256": "0" * 64,
        }
        publication_registration = publication | {
            "id": 9001,
            "identity_sha256": "0" * 64,
        }
        publication_identity = publication | {
            "id": 9002,
            "registration_id": 8000,
            "registration_identity_sha256": "e" * 64,
        }
        binding_pk = assessment_binding | {
            "binding_sha256": "f" * 64,
        }
        binding_identity = assessment_binding | {
            "role": "EVENT",
            "artifact_ordinal": 999,
            "session_date": date(2026, 5, 15),
            "artifact_sha256": "b" * 64,
            "artifact_kind": _EVENT_KIND,
        }
        attempts = (
            ("registration PK", registration_table, registration_pk),
            (
                "registration identity UQ",
                registration_table,
                registration_identity,
            ),
            (
                "registration composite UQ",
                registration_table,
                registration,
            ),
            ("artifact PK", artifact_table, artifact_pk),
            (
                "artifact digest/kind UQ",
                artifact_table,
                assessment_artifact,
            ),
            ("publication PK", publication_table, publication_pk),
            (
                "publication registration UQ",
                publication_table,
                publication_registration,
            ),
            (
                "publication identity UQ",
                publication_table,
                publication_identity,
            ),
            ("binding composite PK", binding_table, binding_pk),
            ("binding SHA UQ", binding_table, binding_identity),
        )
        counts_before = {
            table.name: connection.execute(
                select(text("count(*)")).select_from(table)
            ).scalar_one()
            for table in (
                registration_table,
                artifact_table,
                publication_table,
                binding_table,
            )
        }
        connection.commit()
        for label, table, values in attempts:
            with pytest.raises(IntegrityError, match="duplicate key") as exc:
                connection.execute(
                    table.insert().prefix_with("OR REPLACE").values(**values)
                )
            assert f"{table.name} duplicate key" in str(exc.value), label
            connection.rollback()
        counts_after = {
            table.name: connection.execute(
                select(text("count(*)")).select_from(table)
            ).scalar_one()
            for table in (
                registration_table,
                artifact_table,
                publication_table,
                binding_table,
            )
        }
        assert counts_after == counts_before


@pytest.mark.parametrize(
    ("model_factory", "expected_constraint"),
    (
        (
            lambda registration_id: _registration(
                identity_sha256="A" * 64,
            ),
            "identity_sha",
        ),
        (
            lambda registration_id: _registration(
                order_submission_allowed=True,
                identity_sha256="f" * 64,
            ),
            "registration_p0",
        ),
        (
            lambda registration_id: _publication(
                registration_id,
                identity_sha256="0" * 64,
                assessment_artifact_count=0,
            ),
            "publication_counts",
        ),
        (
            lambda registration_id: _publication(
                registration_id,
                identity_sha256="0" * 64,
                order_submission_allowed=True,
            ),
            "publication_p0",
        ),
    ),
)
def test_fixed_sha_count_and_p0_constraints_fail_closed(
    tmp_path: Path,
    model_factory: Callable[[int], object],
    expected_constraint: str,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'checks.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    with Session(engine) as session:
        registration_id = 0
        if "publication" in expected_constraint:
            registration = _registration()
            session.add(registration)
            session.flush()
            registration_id = registration.id
        session.add(model_factory(registration_id))
        with pytest.raises(IntegrityError, match=expected_constraint):
            session.commit()


def test_registration_identity_and_artifact_kind_foreign_keys_fail_closed(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'foreign-keys.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    with Session(engine) as session:
        registration = _registration()
        session.add(registration)
        session.commit()
        session.add(_publication(
            registration.id,
            registration_identity_sha256="f" * 64,
        ))
        with pytest.raises(
            IntegrityError,
            match="invalid reference|FOREIGN KEY",
        ):
            session.commit()
        session.rollback()

        publication = _publication(registration.id)
        artifact = _artifact("a" * 64, _ASSESSMENT_KIND)
        session.add_all((publication, artifact))
        session.flush()
        session.add(_binding(
            publication.id,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256=artifact.digest_sha256,
            artifact_kind=_EVENT_KIND,
            binding_sha256="b" * 64,
            session_date=None,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_reference_triggers_fail_closed_with_foreign_keys_disabled(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'foreign-keys-off.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    with Session(engine) as session:
        registration = _registration()
        session.add(registration)
        session.flush()
        registration_id = registration.id
        session.commit()

    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _publication(
            registration_id + 999,
            identity_sha256="a" * 64,
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _publication(
            registration_id,
            registration_identity_sha256="f" * 64,
            identity_sha256="b" * 64,
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _publication(
            registration_id,
            identity_sha256="c" * 64,
            registered_member_count=2,
            assessment_artifact_count=2,
            event_artifact_count=0,
            binding_count=2,
        ),
    )

    with Session(engine) as session:
        artifact = _artifact("d" * 64, _ASSESSMENT_KIND)
        publication = _publication(
            registration_id,
            identity_sha256="e" * 64,
        )
        session.add_all((artifact, publication))
        session.flush()
        publication_id = publication.id
        session.commit()

    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _binding(
            publication_id + 999,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256="d" * 64,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256="1" * 64,
            session_date=None,
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _binding(
            publication_id,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256="f" * 64,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256="2" * 64,
            session_date=None,
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _binding(
            publication_id,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256="d" * 64,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256="3" * 64,
            session_date=None,
            member_ordinal=1,
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _binding(
            publication_id,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256="d" * 64,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256="4" * 64,
            session_date=None,
            symbol="MSFT.US",
        ),
    )
    _assert_reference_rejected_with_foreign_keys_off(
        engine,
        _binding(
            publication_id,
            role="ASSESSMENT",
            artifact_ordinal=0,
            artifact_sha256="d" * 64,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256="5" * 64,
            session_date=None,
            symbol="0700.HK",
            market="HK",
        ),
    )


def test_session_input_binding_ordinal_is_bounded_by_target_denominator(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'session-ordinal.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    with Session(engine) as session:
        registration = _registration()
        session.add(registration)
        session.flush()
        publication = _publication(
            registration.id,
            session_input_artifact_count=1,
            event_artifact_count=0,
            binding_count=2,
        )
        artifact = _artifact("a" * 64, _SESSION_INPUT_KIND)
        session.add_all((publication, artifact))
        session.flush()
        session.add(_binding(
            publication.id,
            role="SESSION_INPUT",
            artifact_ordinal=30,
            artifact_sha256=artifact.digest_sha256,
            artifact_kind=artifact.kind,
            binding_sha256="b" * 64,
            session_date=date(2026, 5, 15),
        ))
        with pytest.raises(IntegrityError, match="role_kind_session"):
            session.commit()


def test_same_name_trigger_with_pseudo_abort_body_is_rejected(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'bad-trigger.db'}")
    database._ensure_watchlist_quant_v6_tables(engine)
    bad_trigger = database.WATCHLIST_QUANT_V6_TRIGGER_NAMES[0]
    definitions = database._watchlist_quant_v6_trigger_definitions()
    table_name = definitions[bad_trigger][0]
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TRIGGER {bad_trigger}")
        connection.exec_driver_sql(
            f"CREATE TRIGGER {bad_trigger} BEFORE UPDATE ON {table_name} "
            "BEGIN SELECT CASE WHEN 0 THEN "
            f"RAISE(ABORT, '{table_name} is append-only') ELSE 1 END; END"
        )

    issues = database._watchlist_quant_v6_schema_issues(engine)
    assert (
        f"trigger {bad_trigger} does not match canonical DDL"
        in issues
    )
    with pytest.raises(RuntimeError, match="canonical DDL"):
        database._ensure_watchlist_quant_v6_tables(engine)


def test_partial_same_name_table_is_rejected(tmp_path: Path) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE watchlist_quant_v6_registrations "
            "(id INTEGER PRIMARY KEY)"
        )

    with pytest.raises(RuntimeError, match="columns differ"):
        database._ensure_watchlist_quant_v6_tables(engine)


def test_schema_signature_rejects_wrong_column_type_and_nullability(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'malformed-columns.db'}")
    table = Base.metadata.tables["watchlist_quant_v6_registrations"]
    canonical_ddl = str(CreateTable(table).compile(engine))
    malformed_ddl = canonical_ddl.replace(
        "id INTEGER NOT NULL",
        "id TEXT NOT NULL",
        1,
    ).replace(
        "identity_sha256 VARCHAR(64) NOT NULL",
        "identity_sha256 VARCHAR(64)",
        1,
    )
    assert malformed_ddl != canonical_ddl
    with engine.begin() as connection:
        connection.exec_driver_sql(malformed_ddl)

    issues = database._watchlist_quant_v6_schema_issues(
        engine,
        require_triggers=False,
    )

    assert any("column id type differs" in issue for issue in issues)
    assert any(
        "column identity_sha256 nullability differs" in issue
        for issue in issues
    )


def test_alembic_quant_v6_revision_upgrades_and_downgrades_safely(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "alembic.db"
    config = _alembic_config(backend_root, db_path)
    existing_application_logger = logging.getLogger(
        "auto_trade.notify.webhook"
    )
    existing_application_logger.disabled = False
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "20260801_durable_job_leases"
    revision = script.get_revision("20260801_watchlist_quant_v6")
    assert revision is not None
    assert revision.down_revision == "20260727_opening_execution"

    command.upgrade(config, "head")
    assert existing_application_logger.disabled is False
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    assert database._watchlist_quant_v6_schema_issues(engine) == ()

    command.downgrade(config, "20260727_opening_execution")
    assert not (_TABLES & set(inspect(engine).get_table_names()))


@pytest.mark.parametrize(
    "revision",
    (
        "20260726_opening_stop",
        "20260727_opening_context",
        "20260727_opening_execution",
        "20260801_watchlist_quant_v6",
        "20260801_durable_job_leases",
    ),
)
def test_entrypoint_legacy_stamp_follows_every_frozen_revision(
    tmp_path: Path,
    revision: str,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / f"legacy-{revision}.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, revision)
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        stamped = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert stamped == revision
    command.upgrade(config, "head")


def test_entrypoint_rejects_partial_context_before_stamping(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "partial-context.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, "20260726_opening_stop")
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
        connection.exec_driver_sql(
            "ALTER TABLE opening_momentum_shadow_runs ADD COLUMN "
            "candidate_overnight_gap_bps FLOAT"
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert "partial opening-context schema" in result.stderr
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_entrypoint_rejects_nonblocking_quant_trigger_before_stamping(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "bad-quant-trigger.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, "20260801_watchlist_quant_v6")
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    bad_trigger = database.WATCHLIST_QUANT_V6_TRIGGER_NAMES[0]
    table_name = database._watchlist_quant_v6_trigger_definitions()[
        bad_trigger
    ][0]
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version")
        connection.exec_driver_sql(f"DROP TRIGGER {bad_trigger}")
        connection.exec_driver_sql(
            f"CREATE TRIGGER {bad_trigger} BEFORE UPDATE ON {table_name} "
            "BEGIN SELECT CASE WHEN 0 THEN "
            f"RAISE(ABORT, '{table_name} is append-only') ELSE 1 END; END"
        )

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert (
        f"trigger {bad_trigger} does not match canonical DDL"
        in result.stderr
    )
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_entrypoint_advances_recorded_execution_for_prebuilt_quant_schema(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "prebuilt-quant.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, "20260727_opening_execution")
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    database._ensure_watchlist_quant_v6_tables(engine)

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode == 0, result.stderr
    with engine.connect() as connection:
        recorded_revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert recorded_revision == "20260801_watchlist_quant_v6"
    command.upgrade(config, "head")


def test_entrypoint_rejects_prebuilt_quant_schema_on_wrong_recorded_lineage(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "wrong-quant-lineage.db"
    config = _alembic_config(backend_root, db_path)
    command.upgrade(config, "20260727_opening_context")
    engine = _sqlite_engine(f"sqlite:///{db_path}")
    database._ensure_watchlist_quant_v6_tables(engine)

    result = _run_entrypoint_legacy_stamp(backend_root, db_path)

    assert result.returncode != 0
    assert "outside the expected recorded lineage" in result.stderr
    with engine.connect() as connection:
        recorded_revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert recorded_revision == "20260727_opening_context"
