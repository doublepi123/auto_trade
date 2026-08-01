from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import database
from app.domain.strategy_v2 import forward_replay_artifact as artifact_codec
from app.domain.strategy_v2.forward_replay_artifact import (
    FORWARD_REPLAY_ARTIFACT_CODEC,
    FORWARD_REPLAY_ARTIFACT_KIND,
    FORWARD_REPLAY_ARTIFACT_ROLE,
    FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION,
    EncodedForwardReplayArtifact,
    ForwardReplayArtifactError,
    decode_forward_replay_artifact,
    encode_forward_replay_artifact,
    forward_replay_artifact_binding_sha256,
)
from app.models import (
    Base,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardRegistration,
    StrategyV2ForwardReplayArtifact,
)


def _decode_args(encoded: EncodedForwardReplayArtifact) -> dict[str, Any]:
    return asdict(encoded)


def _sqlite_engine(url: str = "sqlite://") -> Engine:
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def test_forward_replay_artifact_codec_is_canonical_and_round_trips() -> None:
    left = {
        "schema": 1,
        "symbol": "AAPL.US",
        "bars": [
            {
                "timestamp": "2026-07-06T13:30:00+00:00",
                "open": 100.0,
                "high": 100.2,
                "low": 99.9,
                "close": 100.1,
                "volume": 1_000,
            }
        ],
        "label": "前向证据",
    }
    right = {
        "label": "前向证据",
        "bars": left["bars"],
        "symbol": "AAPL.US",
        "schema": 1,
    }

    encoded_left = encode_forward_replay_artifact(left)
    encoded_right = encode_forward_replay_artifact(right)

    assert encoded_left == encoded_right
    assert encoded_left.schema_version == FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION
    assert encoded_left.kind == FORWARD_REPLAY_ARTIFACT_KIND
    assert encoded_left.codec == FORWARD_REPLAY_ARTIFACT_CODEC
    assert decode_forward_replay_artifact(**_decode_args(encoded_left)) == left
    canonical = artifact_codec.canonical_forward_replay_json(left)
    assert encoded_left.digest_sha256 == hashlib.sha256(canonical).hexdigest()
    assert zlib.decompress(encoded_left.payload) == canonical


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_forward_replay_artifact_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ForwardReplayArtifactError, match="finite"):
        encode_forward_replay_artifact({"nested": [{"value": value}]})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"value": (1, 2)}, "unsupported JSON value"),
        ({"value": {1: "not-a-string-key"}}, "non-string key"),
    ],
)
def test_forward_replay_artifact_rejects_non_json_types(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ForwardReplayArtifactError, match=message):
        encode_forward_replay_artifact(payload)


def test_forward_replay_artifact_enforces_raw_and_compressed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        artifact_codec,
        "MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES",
        32,
    )
    with pytest.raises(ForwardReplayArtifactError, match="raw size limit"):
        encode_forward_replay_artifact({"value": "x" * 64})

    monkeypatch.setattr(
        artifact_codec,
        "MAX_FORWARD_REPLAY_ARTIFACT_RAW_BYTES",
        1024,
    )
    monkeypatch.setattr(
        artifact_codec,
        "MAX_FORWARD_REPLAY_ARTIFACT_COMPRESSED_BYTES",
        8,
    )
    with pytest.raises(ForwardReplayArtifactError, match="compressed size limit"):
        encode_forward_replay_artifact({"value": "abcdefghijklmnopqrstuvwxyz"})


def test_forward_replay_artifact_rejects_excessive_json_nesting() -> None:
    nested: object = "leaf"
    for _ in range(70):
        nested = [nested]

    with pytest.raises(ForwardReplayArtifactError, match="nesting limit"):
        encode_forward_replay_artifact({"value": nested})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema"),
        ("kind", "OTHER", "kind"),
        ("codec", "gzip", "codec"),
        ("digest_sha256", "A" * 64, "lowercase SHA-256"),
        ("raw_size", 0, "raw size"),
        ("compressed_size", 0, "compressed size"),
    ],
)
def test_forward_replay_artifact_rejects_invalid_metadata(
    field: str,
    value: object,
    message: str,
) -> None:
    encoded = encode_forward_replay_artifact({"value": 1})
    values = _decode_args(encoded)
    values[field] = value

    with pytest.raises(ForwardReplayArtifactError, match=message):
        decode_forward_replay_artifact(**values)


def test_forward_replay_artifact_rejects_trailing_and_truncated_bytes() -> None:
    encoded = encode_forward_replay_artifact({"value": [1, 2, 3]})
    trailing = _decode_args(encoded)
    trailing_payload = encoded.payload + b"trailing"
    trailing["payload"] = trailing_payload
    trailing["compressed_size"] = len(trailing_payload)
    with pytest.raises(ForwardReplayArtifactError, match="trailing bytes"):
        decode_forward_replay_artifact(**trailing)

    truncated = _decode_args(encoded)
    truncated_payload = encoded.payload[:-1]
    truncated["payload"] = truncated_payload
    truncated["compressed_size"] = len(truncated_payload)
    with pytest.raises(ForwardReplayArtifactError, match="incomplete|decompression"):
        decode_forward_replay_artifact(**truncated)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"b":1, "a":2}',
        b'{"a":1,"a":2}',
        b'{"value":NaN}',
    ],
)
def test_forward_replay_artifact_rejects_noncanonical_json(raw: bytes) -> None:
    compressed = zlib.compress(raw, level=9)
    with pytest.raises(ForwardReplayArtifactError):
        decode_forward_replay_artifact(
            digest_sha256=hashlib.sha256(raw).hexdigest(),
            schema_version=FORWARD_REPLAY_ARTIFACT_SCHEMA_VERSION,
            kind=FORWARD_REPLAY_ARTIFACT_KIND,
            codec=FORWARD_REPLAY_ARTIFACT_CODEC,
            raw_size=len(raw),
            compressed_size=len(compressed),
            payload=compressed,
        )


def test_forward_replay_artifact_binding_is_canonical_and_strict() -> None:
    evidence_digest = "a" * 64
    artifact_digest = "b" * 64
    first = forward_replay_artifact_binding_sha256(
        evidence_id=7,
        evidence_digest_sha256=evidence_digest,
        artifact_digest_sha256=artifact_digest,
    )
    second = forward_replay_artifact_binding_sha256(
        artifact_digest_sha256=artifact_digest,
        evidence_digest_sha256=evidence_digest,
        evidence_id=7,
    )

    assert first == second
    assert len(first) == 64
    assert first != forward_replay_artifact_binding_sha256(
        evidence_id=8,
        evidence_digest_sha256=evidence_digest,
        artifact_digest_sha256=artifact_digest,
    )
    with pytest.raises(ForwardReplayArtifactError, match="positive integer"):
        forward_replay_artifact_binding_sha256(
            evidence_id=0,
            evidence_digest_sha256=evidence_digest,
            artifact_digest_sha256=artifact_digest,
        )
    with pytest.raises(ForwardReplayArtifactError, match="role"):
        forward_replay_artifact_binding_sha256(
            evidence_id=7,
            evidence_digest_sha256=evidence_digest,
            artifact_digest_sha256=artifact_digest,
            role="OTHER",
        )


def test_forward_replay_artifact_models_enforce_fk_lifecycle() -> None:
    engine = _sqlite_engine()
    Base.metadata.create_all(bind=engine)
    encoded = encode_forward_replay_artifact({"symbol": "AAPL.US", "bars": []})

    with Session(bind=engine) as db:
        evidence = StrategyV2ForwardEvidence(
            registration_id=1,
            target_session_date=date(2026, 7, 6),
            target_open_at=datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc),
            evaluated_at=datetime(2026, 7, 6, 20, 10, tzinfo=timezone.utc),
            disposition="INCLUDED",
            evidence_digest_sha256="a" * 64,
        )
        db.add(evidence)
        db.flush()
        db.add(StrategyV2ForwardReplayArtifact(**asdict(encoded)))
        db.flush()
        db.add(StrategyV2ForwardEvidenceArtifact(
            evidence_id=evidence.id,
            role=FORWARD_REPLAY_ARTIFACT_ROLE,
            artifact_sha256=encoded.digest_sha256,
            binding_sha256=forward_replay_artifact_binding_sha256(
                evidence_id=evidence.id,
                evidence_digest_sha256=evidence.evidence_digest_sha256,
                artifact_digest_sha256=encoded.digest_sha256,
            ),
        ))
        db.commit()
        evidence_id = evidence.id

        with pytest.raises(IntegrityError):
            db.execute(delete(StrategyV2ForwardReplayArtifact).where(
                StrategyV2ForwardReplayArtifact.digest_sha256
                == encoded.digest_sha256
            ))
            db.commit()
        db.rollback()

        db.execute(delete(StrategyV2ForwardEvidence).where(
            StrategyV2ForwardEvidence.id == evidence_id
        ))
        db.commit()
        assert db.get(
            StrategyV2ForwardEvidenceArtifact,
            (evidence_id, FORWARD_REPLAY_ARTIFACT_ROLE),
        ) is None
        assert db.get(
            StrategyV2ForwardReplayArtifact,
            encoded.digest_sha256,
        ) is not None


def test_forward_replay_artifact_models_reject_invalid_fixed_metadata() -> None:
    engine = _sqlite_engine()
    Base.metadata.create_all(bind=engine)
    encoded = encode_forward_replay_artifact({"symbol": "AAPL.US", "bars": []})

    with Session(bind=engine) as db:
        invalid_artifact = asdict(encoded)
        invalid_artifact["kind"] = "OTHER"
        db.add(StrategyV2ForwardReplayArtifact(**invalid_artifact))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        evidence = StrategyV2ForwardEvidence(
            registration_id=1,
            target_session_date=date(2026, 7, 6),
            target_open_at=datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc),
            evaluated_at=datetime(2026, 7, 6, 20, 10, tzinfo=timezone.utc),
            disposition="INCLUDED",
            evidence_digest_sha256="a" * 64,
        )
        db.add(evidence)
        db.add(StrategyV2ForwardReplayArtifact(**asdict(encoded)))
        db.commit()
        evidence_id = evidence.id

        db.add(StrategyV2ForwardEvidenceArtifact(
            evidence_id=evidence_id,
            role="OTHER",
            artifact_sha256=encoded.digest_sha256,
            binding_sha256="b" * 64,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(StrategyV2ForwardEvidenceArtifact(
            evidence_id=evidence_id,
            role=FORWARD_REPLAY_ARTIFACT_ROLE,
            artifact_sha256=encoded.digest_sha256,
            binding_sha256="B" * 64,
        ))
        with pytest.raises(IntegrityError):
            db.commit()


def test_forward_replay_artifact_migration_is_idempotent_and_preserves_legacy(
    tmp_path: Path,
) -> None:
    engine = _sqlite_engine(f"sqlite:///{tmp_path / 'forward-artifact.db'}")
    Base.metadata.tables["strategy_v2_forward_registrations"].create(engine)
    Base.metadata.tables["strategy_v2_forward_evidence"].create(engine)
    registered_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with Session(bind=engine) as db:
        registration = StrategyV2ForwardRegistration(
            symbol="AAPL.US",
            market="US",
            candidate_algorithm_version="candidate-v1",
            source_config_version="a" * 64,
            evaluator_digest="b" * 64,
            candidate_spec_json="{}",
            registered_at=registered_at,
            eligible_after=registered_at,
        )
        db.add(registration)
        db.flush()
        evidence = StrategyV2ForwardEvidence(
            registration_id=registration.id,
            target_session_date=date(2026, 7, 2),
            target_open_at=registered_at,
            evaluated_at=registered_at,
            disposition="EXCLUDED",
            exclusion_reason="FINALIZATION_WINDOW_MISSED",
            evidence_digest_sha256="c" * 64,
        )
        db.add(evidence)
        db.commit()
        evidence_id = evidence.id

    database._ensure_strategy_v2_shadow_tables(engine)
    database._ensure_strategy_v2_shadow_tables(engine)

    inspector = inspect(engine)
    assert {
        "strategy_v2_forward_replay_artifacts",
        "strategy_v2_forward_evidence_artifacts",
    } <= set(inspector.get_table_names())
    assert {
        "digest_sha256",
        "schema_version",
        "kind",
        "codec",
        "raw_size",
        "compressed_size",
        "payload",
        "created_at",
    } == {
        column["name"]
        for column in inspector.get_columns(
            "strategy_v2_forward_replay_artifacts"
        )
    }
    assert {
        "evidence_id",
        "role",
        "artifact_sha256",
        "binding_sha256",
        "created_at",
    } == {
        column["name"]
        for column in inspector.get_columns(
            "strategy_v2_forward_evidence_artifacts"
        )
    }
    foreign_keys = inspector.get_foreign_keys(
        "strategy_v2_forward_evidence_artifacts"
    )
    assert {
        (
            item["referred_table"],
            (item.get("options") or {}).get("ondelete"),
        )
        for item in foreign_keys
    } == {
        ("strategy_v2_forward_evidence", "CASCADE"),
        ("strategy_v2_forward_replay_artifacts", "RESTRICT"),
    }
    with Session(bind=engine) as db:
        preserved = db.scalar(select(StrategyV2ForwardEvidence).where(
            StrategyV2ForwardEvidence.id == evidence_id
        ))
        assert preserved is not None
        assert preserved.evidence_digest_sha256 == "c" * 64
