from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardRegistration,
    StrategyV2ForwardReplayArtifact,
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
)
from app.services.research_artifact_retention_service import (
    ResearchArtifactRetentionService,
)


_ASSESSMENT_KIND = "WATCHLIST_QUANT_V6_ASSESSMENT"
_EVENT_KIND = "WATCHLIST_QUANT_V6_EVENT"
_NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


def _session() -> Session:
    return Session(bind=_engine())


def _registration(
    db: Session,
    *,
    identity: str = "1" * 64,
    registration_json: str = "{}",
) -> WatchlistQuantV6Registration:
    observed_at = _NOW - timedelta(days=60)
    registration = WatchlistQuantV6Registration(
        identity_sha256=identity,
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
        first_training_session_date=date(2026, 5, 1),
        first_target_session_date=date(2026, 5, 15),
        last_target_session_date=date(2026, 6, 26),
        data_cutoff_at=observed_at,
        bar_period="MIN_5",
        adjustment_mode="NO_ADJUST",
        registration_json=registration_json,
        server_generated=True,
        short_entry_allowed=False,
        position_add_on_allowed=False,
        order_submission_allowed=False,
        automatic_promotion_allowed=False,
        cohort_observed_at=observed_at,
        registered_at=observed_at,
    )
    db.add(registration)
    db.flush()
    return registration


def _publication(
    db: Session,
    registration: WatchlistQuantV6Registration,
    *,
    identity: str,
    published_at: datetime,
) -> WatchlistQuantV6Publication:
    publication = WatchlistQuantV6Publication(
        registration_id=registration.id,
        registration_identity_sha256=registration.identity_sha256,
        identity_sha256=identity,
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
        published_at=published_at,
    )
    db.add(publication)
    db.flush()
    return publication


def _v6_artifact(
    db: Session,
    digest: str,
    *,
    kind: str = _ASSESSMENT_KIND,
    created_at: datetime,
) -> WatchlistQuantV6Artifact:
    artifact = WatchlistQuantV6Artifact(
        digest_sha256=digest,
        schema_version=1,
        kind=kind,
        codec="zlib",
        compression_level=9,
        raw_size=1,
        compressed_size=1,
        payload=b"x",
        created_at=created_at,
    )
    db.add(artifact)
    db.flush()
    return artifact


def _v6_binding(
    db: Session,
    publication_id: int,
    *,
    artifact_sha256: str,
    artifact_kind: str,
    binding_sha256: str,
    created_at: datetime,
) -> WatchlistQuantV6PublicationArtifact:
    binding = WatchlistQuantV6PublicationArtifact(
        publication_id=publication_id,
        member_ordinal=0,
        symbol="AAPL.US",
        market="US",
        role="ASSESSMENT",
        artifact_ordinal=0,
        session_date=None,
        artifact_sha256=artifact_sha256,
        artifact_kind=artifact_kind,
        binding_sha256=binding_sha256,
        created_at=created_at,
    )
    db.add(binding)
    db.flush()
    return binding


def _forward_registration(db: Session) -> StrategyV2ForwardRegistration:
    registration = StrategyV2ForwardRegistration(
        symbol="NVDA.US",
        market="US",
        candidate_algorithm_version="strategy-v2-causal-trend-prewarm-v1",
        source_config_version="version-a",
        evaluator_digest="a" * 64,
        candidate_spec_json="{}",
        registered_at=_NOW - timedelta(days=90),
        eligible_after=_NOW - timedelta(days=89),
    )
    db.add(registration)
    db.flush()
    return registration


def _forward_evidence(
    db: Session,
    registration_id: int,
    *,
    evaluated_at: datetime,
    target_session_date: date | None = None,
) -> StrategyV2ForwardEvidence:
    evidence = StrategyV2ForwardEvidence(
        registration_id=registration_id,
        target_session_date=target_session_date or evaluated_at.date(),
        seed_session_date=None,
        target_open_at=evaluated_at,
        evaluated_at=evaluated_at,
        disposition="INCLUDED",
        exclusion_reason="",
        structural_failure=False,
        target_bars=0,
        target_bars_sha256="",
        seed_bars_sha256="",
        baseline_input_sha256="",
        candidate_input_sha256="",
        same_target_bars=False,
        baseline_replay_match=None,
        session_local_invariant=None,
        baseline_result_json="{}",
        candidate_result_json="{}",
        baseline_result_sha256="",
        candidate_result_sha256="",
        evidence_digest_sha256="",
    )
    db.add(evidence)
    db.flush()
    return evidence


def _replay_artifact(
    db: Session,
    digest: str,
    *,
    created_at: datetime,
) -> StrategyV2ForwardReplayArtifact:
    artifact = StrategyV2ForwardReplayArtifact(
        digest_sha256=digest,
        schema_version=1,
        kind="STRATEGY_V2_FORWARD_REPLAY",
        codec="zlib",
        raw_size=1,
        compressed_size=1,
        payload=b"x",
        created_at=created_at,
    )
    db.add(artifact)
    db.flush()
    return artifact


def _replay_binding(
    db: Session,
    evidence_id: int,
    *,
    artifact_sha256: str,
    binding_sha256: str,
    created_at: datetime,
) -> StrategyV2ForwardEvidenceArtifact:
    binding = StrategyV2ForwardEvidenceArtifact(
        evidence_id=evidence_id,
        role="REPLAY_BUNDLE",
        artifact_sha256=artifact_sha256,
        binding_sha256=binding_sha256,
        created_at=created_at,
    )
    db.add(binding)
    db.flush()
    return binding


def test_prune_quant_v6_payloads_deletes_old_payloads_and_keeps_recent() -> None:
    db = _session()
    old = _NOW - timedelta(days=60)
    recent = _NOW - timedelta(days=5)
    registration = _registration(db)
    old_publication = _publication(
        db, registration, identity="8" * 64, published_at=old
    )
    old_artifact = _v6_artifact(db, "a" * 64, created_at=old)
    _v6_binding(
        db,
        old_publication.id,
        artifact_sha256=old_artifact.digest_sha256,
        artifact_kind=old_artifact.kind,
        binding_sha256="b" * 64,
        created_at=old,
    )
    recent_registration = _registration(db, identity="f" * 64)
    recent_publication = _publication(
        db, recent_registration, identity="c" * 64, published_at=recent
    )
    recent_artifact = _v6_artifact(db, "d" * 64, created_at=recent)
    _v6_binding(
        db,
        recent_publication.id,
        artifact_sha256=recent_artifact.digest_sha256,
        artifact_kind=recent_artifact.kind,
        binding_sha256="e" * 64,
        created_at=recent,
    )
    db.commit()

    result = ResearchArtifactRetentionService(
        db
    ).prune_expired_quant_v6_publication_payloads(
        retention_days=30,
        batch_size=10,
        now=_NOW,
    )

    # When: the window expires the old publication's payloads
    # Then: the old binding and artifact are deleted...
    assert result.bindings_deleted == 1
    assert result.artifacts_deleted == 1
    assert db.query(WatchlistQuantV6PublicationArtifact).filter_by(
        publication_id=old_publication.id
    ).count() == 0
    assert db.get(WatchlistQuantV6Artifact, "a" * 64) is None
    # ...and the recent publication keeps its binding and artifact.
    assert db.query(WatchlistQuantV6PublicationArtifact).filter_by(
        publication_id=recent_publication.id
    ).count() == 1
    assert db.get(WatchlistQuantV6Artifact, "d" * 64) is not None
    # Then: provenance rows (registrations + both publications) survive.
    assert db.get(WatchlistQuantV6Registration, registration.id) is not None
    assert db.query(WatchlistQuantV6Publication).count() == 2


def test_prune_quant_v6_payloads_keeps_artifact_still_referenced_by_recent_publication() -> None:
    db = _session()
    old = _NOW - timedelta(days=60)
    recent = _NOW - timedelta(days=5)
    registration = _registration(db)
    shared_artifact = _v6_artifact(db, "a" * 64, created_at=old)
    old_publication = _publication(
        db, registration, identity="8" * 64, published_at=old
    )
    _v6_binding(
        db,
        old_publication.id,
        artifact_sha256=shared_artifact.digest_sha256,
        artifact_kind=shared_artifact.kind,
        binding_sha256="b" * 64,
        created_at=old,
    )
    recent_registration = _registration(db, identity="f" * 64)
    recent_publication = _publication(
        db, recent_registration, identity="c" * 64, published_at=recent
    )
    _v6_binding(
        db,
        recent_publication.id,
        artifact_sha256=shared_artifact.digest_sha256,
        artifact_kind=shared_artifact.kind,
        binding_sha256="d" * 64,
        created_at=recent,
    )
    db.commit()

    result = ResearchArtifactRetentionService(
        db
    ).prune_expired_quant_v6_publication_payloads(
        retention_days=30,
        batch_size=10,
        now=_NOW,
    )

    # Then: the expired binding is gone but the shared artifact survives.
    assert result.bindings_deleted == 1
    assert result.artifacts_deleted == 0
    assert db.get(WatchlistQuantV6Artifact, "a" * 64) is not None
    assert db.query(WatchlistQuantV6PublicationArtifact).filter_by(
        publication_id=recent_publication.id
    ).count() == 1


def test_prune_quant_v6_payloads_respects_window_and_zero_disables() -> None:
    db = _session()
    inside_window = _NOW - timedelta(days=20)
    registration = _registration(db)
    publication = _publication(
        db, registration, identity="8" * 64, published_at=inside_window
    )
    _v6_artifact(db, "a" * 64, created_at=inside_window)
    _v6_binding(
        db,
        publication.id,
        artifact_sha256="a" * 64,
        artifact_kind=_ASSESSMENT_KIND,
        binding_sha256="b" * 64,
        created_at=inside_window,
    )
    db.commit()

    service = ResearchArtifactRetentionService(db)
    inside = service.prune_expired_quant_v6_publication_payloads(
        retention_days=30,
        batch_size=10,
        now=_NOW,
    )
    disabled = service.prune_expired_quant_v6_publication_payloads(
        retention_days=0,
        batch_size=10,
        now=_NOW,
    )

    # Then: rows inside the window are kept, and 0 disables pruning entirely.
    assert inside.bindings_deleted == 0
    assert inside.artifacts_deleted == 0
    assert disabled.bindings_deleted == 0
    assert disabled.artifacts_deleted == 0
    assert db.query(WatchlistQuantV6PublicationArtifact).count() == 1
    assert db.get(WatchlistQuantV6Artifact, "a" * 64) is not None


def test_prune_quant_v6_payloads_bounds_batches() -> None:
    db = _session()
    old = _NOW - timedelta(days=60)
    for index in range(6):
        registration = _registration(db, identity=f"{index + 48:064x}")
        publication = _publication(
            db,
            registration,
            identity=f"{index:064x}",
            published_at=old,
        )
        digest = f"{index + 16:064x}"
        _v6_artifact(db, digest, created_at=old)
        _v6_binding(
            db,
            publication.id,
            artifact_sha256=digest,
            artifact_kind=_ASSESSMENT_KIND,
            binding_sha256=f"{index + 32:064x}",
            created_at=old,
        )
    db.commit()

    result = ResearchArtifactRetentionService(
        db
    ).prune_expired_quant_v6_publication_payloads(
        retention_days=30,
        batch_size=2,
        max_batches=1,
        now=_NOW,
    )

    # Then: the run is bounded; unprocessed rows wait for the next tick.
    assert result.batches == 1
    assert result.bindings_deleted <= 2
    assert db.query(WatchlistQuantV6PublicationArtifact).count() >= 4


def test_prune_forward_replay_artifacts_deletes_old_and_keeps_recent() -> None:
    db = _session()
    old = _NOW - timedelta(days=60)
    recent = _NOW - timedelta(days=5)
    registration = _forward_registration(db)
    old_evidence = _forward_evidence(db, registration.id, evaluated_at=old)
    _replay_artifact(db, "a" * 64, created_at=old)
    _replay_binding(
        db,
        old_evidence.id,
        artifact_sha256="a" * 64,
        binding_sha256="b" * 64,
        created_at=old,
    )
    recent_evidence = _forward_evidence(db, registration.id, evaluated_at=recent)
    _replay_artifact(db, "c" * 64, created_at=recent)
    _replay_binding(
        db,
        recent_evidence.id,
        artifact_sha256="c" * 64,
        binding_sha256="d" * 64,
        created_at=recent,
    )
    db.commit()

    result = ResearchArtifactRetentionService(
        db
    ).prune_expired_forward_replay_artifacts(
        retention_days=30,
        batch_size=10,
        now=_NOW,
    )

    # Then: old replay bytes and bindings are deleted...
    assert result.bindings_deleted == 1
    assert result.artifacts_deleted == 1
    assert db.query(StrategyV2ForwardEvidenceArtifact).filter_by(
        evidence_id=old_evidence.id
    ).count() == 0
    assert db.get(StrategyV2ForwardReplayArtifact, "a" * 64) is None
    # ...recent ones are kept...
    assert db.query(StrategyV2ForwardEvidenceArtifact).filter_by(
        evidence_id=recent_evidence.id
    ).count() == 1
    assert db.get(StrategyV2ForwardReplayArtifact, "c" * 64) is not None
    # ...and the evidence/registration proof rows survive.
    assert db.query(StrategyV2ForwardEvidence).count() == 2
    assert db.get(StrategyV2ForwardRegistration, registration.id) is not None


def test_prune_forward_replay_artifacts_keeps_shared_artifact_and_window() -> None:
    db = _session()
    old = _NOW - timedelta(days=60)
    recent = _NOW - timedelta(days=20)
    registration = _forward_registration(db)
    _replay_artifact(db, "a" * 64, created_at=old)
    old_evidence = _forward_evidence(db, registration.id, evaluated_at=old)
    _replay_binding(
        db,
        old_evidence.id,
        artifact_sha256="a" * 64,
        binding_sha256="b" * 64,
        created_at=old,
    )
    recent_evidence = _forward_evidence(db, registration.id, evaluated_at=recent)
    _replay_binding(
        db,
        recent_evidence.id,
        artifact_sha256="a" * 64,
        binding_sha256="c" * 64,
        created_at=recent,
    )
    inside_evidence = _forward_evidence(
        db,
        registration.id,
        evaluated_at=recent,
        target_session_date=recent.date() - timedelta(days=1),
    )
    _replay_artifact(db, "d" * 64, created_at=recent)
    _replay_binding(
        db,
        inside_evidence.id,
        artifact_sha256="d" * 64,
        binding_sha256="e" * 64,
        created_at=recent,
    )
    db.commit()

    service = ResearchArtifactRetentionService(db)
    result = service.prune_expired_forward_replay_artifacts(
        retention_days=30,
        batch_size=10,
        now=_NOW,
    )
    disabled = service.prune_expired_forward_replay_artifacts(
        retention_days=0,
        batch_size=10,
        now=_NOW,
    )

    # Then: the old binding is deleted but the shared artifact survives,
    # rows inside the window are untouched, and 0 disables pruning.
    assert result.bindings_deleted == 1
    assert result.artifacts_deleted == 0
    assert disabled.bindings_deleted == 0
    assert disabled.artifacts_deleted == 0
    assert db.get(StrategyV2ForwardReplayArtifact, "a" * 64) is not None
    assert db.get(StrategyV2ForwardReplayArtifact, "d" * 64) is not None
    assert db.query(StrategyV2ForwardEvidenceArtifact).count() == 2


def _engine_with_production_triggers() -> Engine:
    from app.database import _ensure_watchlist_quant_v6_tables

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA recursive_triggers=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(bind=engine)
    _ensure_watchlist_quant_v6_tables(engine)
    return engine


def _seed_expired_publication(db: Session) -> None:
    """Seed one expired publication that satisfies the reference triggers.

    The publication and binding INSERT triggers re-derive cohort identity from
    ``registration_json``, so the seed must carry a real member array whose
    ordinal/symbol/market match the binding rather than the ``{}`` the
    trigger-less fixtures get away with.
    """
    import json as _json

    old = _NOW - timedelta(days=60)
    registration = _registration(
        db,
        registration_json=_json.dumps(
            {"cohort": {"member_count": 1, "members": [
                {"ordinal": 0, "symbol": "AAPL.US", "market": "US"}
            ]}}
        ),
    )
    publication = _publication(db, registration, identity="8" * 64, published_at=old)
    artifact = _v6_artifact(db, "a" * 64, created_at=old)
    _v6_binding(
        db,
        publication.id,
        artifact_sha256=artifact.digest_sha256,
        artifact_kind=artifact.kind,
        binding_sha256="b" * 64,
        created_at=old,
    )
    db.commit()


def test_production_triggers_block_delete_on_every_quant_v6_table() -> None:
    from sqlalchemy import text

    db = Session(bind=_engine_with_production_triggers())
    _seed_expired_publication(db)

    for table in (
        "watchlist_quant_v6_publication_artifacts",
        "watchlist_quant_v6_artifacts",
        "watchlist_quant_v6_publications",
        "watchlist_quant_v6_registrations",
    ):
        try:
            db.execute(text(f"DELETE FROM {table}"))
            db.commit()
            raise AssertionError(f"DELETE FROM {table} must abort as append-only")
        except AssertionError:
            raise
        except Exception as exc:
            assert "append-only" in str(exc), f"{table} -> {exc}"
            db.rollback()
    db.close()


def test_configured_quant_v6_retention_default_survives_production_triggers() -> None:
    from app.config import settings

    db = Session(bind=_engine_with_production_triggers())
    _seed_expired_publication(db)

    result = ResearchArtifactRetentionService(db).prune_expired_quant_v6_publication_payloads(
        retention_days=settings.watchlist_quant_v6_artifact_retention_days,
        batch_size=settings.watchlist_quant_v6_artifact_maintenance_batch_size,
        max_batches=8,
        now=_NOW,
    )
    assert result.bindings_deleted == 0
    assert result.artifacts_deleted == 0
    db.close()
