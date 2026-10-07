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
)
from app.services.research_artifact_retention_service import (
    ResearchArtifactRetentionService,
)


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
