from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.watchlist_quant_v6 import (
    BAR_NEXT_OPEN_STRESSED,
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    QUANT_V6_ACQUISITION_SPEC,
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_EVENT_ARTIFACT_KIND,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QUANT_V6_SEMANTIC_DIGEST,
    QUANT_V6_SESSION_INPUT_CONTRACT,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    SESSION_COVERED,
    SESSION_MISSING,
    EncodedQuantV6Artifact,
    QuantV6AssessmentError,
    QuantV6Bar,
    QuantV6SemanticError,
    QuantV6SessionLeaf,
    QuantV6ThresholdEvidence,
    QuantV6TrainingSession,
    assess_bar_next_open_stressed_window,
    build_bar_next_open_stressed_session_events,
    build_quant_v6_threshold_evidence,
    canonical_decimal,
    canonical_quant_v6_json,
    canonical_utc_timestamp,
    decode_quant_v6_artifact,
    encode_quant_v6_artifact,
    quant_v6_expected_rth_bar_starts,
    quant_v6_fee_rate,
    quant_v6_payload_sha256,
    quant_v6_previous_trading_session_dates,
)
from app.models import (
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
)
from app.services.watchlist_quant_v6_evaluation_service import (
    ASSESSMENT_ROLE,
    EVENT_ROLE,
    QUANT_V6_BINDING_CONTRACT,
    QUANT_V6_COHORT_SOURCE,
    QUANT_V6_REGISTRATION_CONTRACT,
    QUANT_V6_REGISTRATION_SCHEMA_VERSION,
    QUANT_V6_SELECTION_RULE_VERSION,
    SESSION_INPUT_ROLE,
    QuantV6CandidateEvaluation,
    QuantV6HistoricalProvider,
    QuantV6PendingArtifactBinding,
    QuantV6RegistrationPlan,
    evaluate_quant_v6_registration,
    validate_quant_v6_registration_plan,
)
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationDeadline,
)


QUANT_V6_PUBLICATION_SCHEMA_VERSION = 1
QUANT_V6_PUBLICATION_CONTRACT = "watchlist-quant-v6-publication-v1"
QUANT_V6_BINDING_MANIFEST_CONTRACT = (
    "watchlist-quant-v6-binding-manifest-v1"
)
QUANT_V6_ACCEPTED_BAR_STARTS_DIGEST_CONTRACT = (
    "watchlist-quant-v6-accepted-bar-starts-digest-v1"
)
QUANT_V6_SCHEDULED_GRID_STARTS_DIGEST_CONTRACT = (
    "watchlist-quant-v6-scheduled-grid-starts-digest-v1"
)
_ARTIFACT_QUERY_CHUNK_SIZE = 400

_ROLE_RANK: Mapping[str, int] = {
    ASSESSMENT_ROLE: 0,
    SESSION_INPUT_ROLE: 1,
    EVENT_ROLE: 2,
}
_KIND_BY_ROLE: Mapping[str, str] = {
    ASSESSMENT_ROLE: QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    SESSION_INPUT_ROLE: QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    EVENT_ROLE: QUANT_V6_EVENT_ARTIFACT_KIND,
}
_PUBLICATION_POLICY: Mapping[str, object] = {
    "automatic_promotion_allowed": False,
    "order_submission_allowed": False,
    "position_add_on_allowed": False,
    "promotion_eligible": False,
    "short_entry_allowed": False,
}


def _evaluation_checkpoint(
    evaluation_deadline: QuantV6EvaluationDeadline | None,
) -> None:
    if evaluation_deadline is not None:
        evaluation_deadline.checkpoint()


def _noop_evaluation_checkpoint() -> None:
    """Allow generic evaluators to cooperate without requiring a deadline."""
    return None


class QuantV6PublicationError(RuntimeError):
    """Raised when a quant-v6 cohort cannot be published safely."""


class QuantV6PublicationConflictError(QuantV6PublicationError):
    """Raised when immutable persisted evidence differs from its preimage."""


@dataclass(frozen=True)
class QuantV6RegistrationReceipt:
    registration_id: int
    identity_sha256: str
    created: bool


@dataclass(frozen=True)
class QuantV6PublicationReceipt:
    publication_id: int
    registration_id: int
    registration_identity_sha256: str
    identity_sha256: str
    manifest_sha256: str
    binding_count: int
    created: bool


@dataclass(frozen=True)
class _PreparedPublication:
    bindings: tuple[QuantV6PendingArtifactBinding, ...]
    artifacts: tuple[EncodedQuantV6Artifact, ...]
    manifest_sha256: str
    publication_json: str
    identity_sha256: str
    assessment_count: int
    session_input_count: int
    event_count: int
    acquisition_outcomes: tuple[dict[str, object], ...]
    request_start_at: datetime
    request_end_at: datetime

    @property
    def binding_count(self) -> int:
        return len(self.bindings)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise QuantV6PublicationError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _persisted_utc(value: datetime, *, label: str) -> datetime:
    if type(value) is not datetime:
        raise QuantV6PublicationConflictError(
            f"persisted {label} is not a timestamp"
        )
    # SQLite discards the offset from DateTime(timezone=True). The application
    # writes only UTC, so a naive value read back from SQLite is UTC storage.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_bytes(left: bytes, right: bytes) -> bool:
    return len(left) == len(right) and hmac.compare_digest(left, right)


def _same_digest(left: object, right: object) -> bool:
    return (
        type(left) is str
        and type(right) is str
        and len(left) == len(right)
        and hmac.compare_digest(left, right)
    )


def _registration_fields(
    plan: QuantV6RegistrationPlan,
) -> dict[str, object]:
    return {
        "identity_sha256": plan.identity_sha256,
        "schema_version": QUANT_V6_REGISTRATION_SCHEMA_VERSION,
        "contract_version": QUANT_V6_REGISTRATION_CONTRACT,
        "selection_rule_version": QUANT_V6_SELECTION_RULE_VERSION,
        "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
        "semantic_digest_sha256": QUANT_V6_SEMANTIC_DIGEST,
        "evaluator_digest_sha256": plan.evaluator_digest_sha256,
        "acquisition_spec_sha256": plan.acquisition_spec_sha256,
        "cohort_source": QUANT_V6_COHORT_SOURCE,
        "market": plan.market,
        "source_snapshot_sha256": plan.source_snapshot_sha256,
        "cohort_manifest_sha256": plan.cohort_manifest_sha256,
        "cohort_member_count": len(plan.members),
        "schedule_sha256": plan.schedule_sha256,
        "training_session_count": len(plan.training_session_dates),
        "target_session_count": len(plan.target_session_dates),
        "first_training_session_date": plan.training_session_dates[0],
        "first_target_session_date": plan.target_session_dates[0],
        "last_target_session_date": plan.target_session_dates[-1],
        "data_cutoff_at": plan.data_cutoff_at,
        "bar_period": "MIN_5",
        "adjustment_mode": "NO_ADJUST",
        "registration_json": plan.registration_json,
        "server_generated": True,
        "short_entry_allowed": False,
        "position_add_on_allowed": False,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "cohort_observed_at": plan.cohort_observed_at,
    }


def _verify_registration_row(
    row: WatchlistQuantV6Registration,
    plan: QuantV6RegistrationPlan,
) -> None:
    expected = _registration_fields(plan)
    datetime_fields = {"data_cutoff_at", "cohort_observed_at"}
    for field, expected_value in expected.items():
        actual_value = getattr(row, field)
        if field in datetime_fields:
            if type(expected_value) is not datetime:
                raise QuantV6PublicationError(
                    f"registration plan {field} is not a timestamp"
                )
            if _persisted_utc(actual_value, label=field) != _aware_utc(
                expected_value,
                label=field,
            ):
                raise QuantV6PublicationConflictError(
                    f"persisted registration {field} conflicts with the plan"
                )
        elif actual_value != expected_value or type(actual_value) is not type(
            expected_value
        ):
            raise QuantV6PublicationConflictError(
                f"persisted registration {field} conflicts with the plan"
            )
    registered_at = _persisted_utc(
        row.registered_at,
        label="registered_at",
    )
    observed_at = _persisted_utc(
        row.cohort_observed_at,
        label="cohort_observed_at",
    )
    if registered_at < observed_at:
        raise QuantV6PublicationConflictError(
            "persisted registration timestamp predates observation"
        )
    if not _same_digest(row.identity_sha256, plan.identity_sha256):
        raise QuantV6PublicationConflictError(
            "persisted registration identity conflicts with the plan"
        )
    if row.registration_json.encode("utf-8") != plan.registration_json.encode(
        "utf-8"
    ):
        raise QuantV6PublicationConflictError(
            "persisted registration canonical JSON conflicts with the plan"
        )


def _binding_payload(
    binding: QuantV6PendingArtifactBinding,
) -> dict[str, object]:
    return {
        "artifact_kind": binding.artifact.kind,
        "artifact_ordinal": binding.artifact_ordinal,
        "artifact_sha256": binding.artifact.digest_sha256,
        "binding_sha256": binding.binding_sha256,
        "market": binding.market,
        "member_ordinal": binding.member_ordinal,
        "role": binding.role,
        "session_date": (
            binding.session_date.isoformat()
            if binding.session_date is not None
            else None
        ),
        "symbol": binding.symbol,
    }


def _persisted_binding_payload(
    row: WatchlistQuantV6PublicationArtifact,
) -> dict[str, object]:
    return {
        "artifact_kind": row.artifact_kind,
        "artifact_ordinal": row.artifact_ordinal,
        "artifact_sha256": row.artifact_sha256,
        "binding_sha256": row.binding_sha256,
        "market": row.market,
        "member_ordinal": row.member_ordinal,
        "role": row.role,
        "session_date": (
            row.session_date.isoformat()
            if row.session_date is not None
            else None
        ),
        "symbol": row.symbol,
    }


def _binding_preimage(
    *,
    registration_identity_sha256: str,
    binding: QuantV6PendingArtifactBinding,
) -> dict[str, object]:
    payload = _binding_payload(binding)
    payload.update({
        "contract": QUANT_V6_BINDING_CONTRACT,
        "registration_identity_sha256": registration_identity_sha256,
        "schema_version": 1,
    })
    payload.pop("binding_sha256")
    return payload


def _manifest_sha256(
    *,
    registration_identity_sha256: str,
    binding_payloads: Sequence[Mapping[str, object]],
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> str:
    """Hash the canonical manifest incrementally without a cohort size cap."""
    digest = hashlib.sha256()
    digest.update(b'{"bindings":[')
    for index, payload in enumerate(binding_payloads):
        _evaluation_checkpoint(evaluation_deadline)
        if index:
            digest.update(b",")
        digest.update(canonical_quant_v6_json(payload))
    digest.update(b'],"contract":"')
    digest.update(QUANT_V6_BINDING_MANIFEST_CONTRACT.encode("ascii"))
    digest.update(b'","registration_identity_sha256":"')
    digest.update(registration_identity_sha256.encode("ascii"))
    digest.update(b'","schema_version":1}')
    return digest.hexdigest()


def _publication_payload(
    *,
    registration_identity_sha256: str,
    registered_member_count: int,
    manifest_sha256: str,
    assessment_count: int,
    session_input_count: int,
    event_count: int,
    acquisition_outcomes: Sequence[Mapping[str, object]],
    request_start_at: datetime,
    request_end_at: datetime,
) -> dict[str, object]:
    binding_count = assessment_count + session_input_count + event_count
    return {
        "acquisition_outcome": {
            "members": [dict(value) for value in acquisition_outcomes],
            "request_end_at": canonical_utc_timestamp(request_end_at),
            "request_start_at": canonical_utc_timestamp(request_start_at),
        },
        "artifact_counts": {
            "assessment": assessment_count,
            "binding": binding_count,
            "event": event_count,
            "session_input": session_input_count,
        },
        "contract": QUANT_V6_PUBLICATION_CONTRACT,
        "manifest_contract": QUANT_V6_BINDING_MANIFEST_CONTRACT,
        "manifest_sha256": manifest_sha256,
        "policy": dict(_PUBLICATION_POLICY),
        "registered_member_count": registered_member_count,
        "registration_identity_sha256": registration_identity_sha256,
        "schema_version": QUANT_V6_PUBLICATION_SCHEMA_VERSION,
        "status": "PUBLISHED",
    }


def _compare_encoded_artifacts(
    left: EncodedQuantV6Artifact,
    right: EncodedQuantV6Artifact,
    *,
    label: str,
) -> None:
    for field in (
        "digest_sha256",
        "schema_version",
        "kind",
        "codec",
        "raw_size",
        "compressed_size",
    ):
        if getattr(left, field) != getattr(right, field) or type(
            getattr(left, field)
        ) is not type(getattr(right, field)):
            raise QuantV6PublicationConflictError(
                f"{label} artifact {field} conflicts with canonical bytes"
            )
    if not _same_bytes(left.payload, right.payload):
        raise QuantV6PublicationConflictError(
            f"{label} artifact payload conflicts with canonical bytes"
        )


def _decode_and_verify_artifact(
    artifact: EncodedQuantV6Artifact,
    *,
    label: str,
) -> dict[str, Any]:
    if type(artifact) is not EncodedQuantV6Artifact:
        raise QuantV6PublicationError(
            f"{label} artifact has an unsupported type"
        )
    if (
        type(artifact.digest_sha256) is not str
        or type(artifact.schema_version) is not int
        or type(artifact.kind) is not str
        or type(artifact.codec) is not str
        or type(artifact.raw_size) is not int
        or type(artifact.compressed_size) is not int
        or type(artifact.payload) is not bytes
    ):
        raise QuantV6PublicationError(
            f"{label} artifact envelope has unsupported field types"
        )
    try:
        decoded = decode_quant_v6_artifact(
            digest_sha256=artifact.digest_sha256,
            schema_version=artifact.schema_version,
            kind=artifact.kind,
            codec=artifact.codec,
            raw_size=artifact.raw_size,
            compressed_size=artifact.compressed_size,
            payload=artifact.payload,
        )
        canonical = encode_quant_v6_artifact(decoded, kind=artifact.kind)
    except Exception as exc:
        raise QuantV6PublicationError(
            f"{label} artifact failed bounded canonical decode"
        ) from exc
    _compare_encoded_artifacts(
        artifact,
        canonical,
        label=label,
    )
    return decoded


def _require_dict(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise QuantV6PublicationError(f"{label} must be an object")
    return value


def _require_list(value: object, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise QuantV6PublicationError(f"{label} must be an array")
    return value


_BAR_KEYS = frozenset({"close", "high", "low", "open", "start_at", "volume"})
_TRAINING_SESSION_KEYS = frozenset({
    "absolute_log_returns_bps",
    "bar_input_sha256",
    "bars",
    "return_count",
    "session_date",
})
_THRESHOLD_EVIDENCE_KEYS = frozenset({
    "market",
    "minimum_returns",
    "percentile",
    "preimage_digest_sha256",
    "shock_threshold_bps",
    "symbol",
    "target_session_date",
    "training_sessions",
    "training_sessions_required",
})
_SESSION_INPUT_KEYS = frozenset({
    "acquisition",
    "algorithm_version",
    "bar_input_sha256",
    "bars",
    "capture_mode",
    "contract",
    "fee_rate",
    "identity",
    "p0",
    "schema_version",
    "semantic_digest",
    "threshold_evidence",
})
_ASSESSMENT_KEYS = frozenset({
    "aggregates",
    "algorithm_version",
    "blockers",
    "capture_mode",
    "contract",
    "event_set_digest_sha256",
    "identity",
    "leaves",
    "policy",
    "schema_version",
    "semantic_digest",
    "session_cluster_methodology",
    "thresholds",
})
_ASSESSMENT_LEAF_KEYS = frozenset({
    "bar_input_sha256",
    "blockers",
    "event_artifact_sha256",
    "event_count",
    "fee_rate",
    "replay_input_artifact_sha256",
    "session_date",
    "status",
    "threshold_preimage_sha256",
})
_PUBLICATION_KEYS = frozenset({
    "acquisition_outcome",
    "artifact_counts",
    "contract",
    "manifest_contract",
    "manifest_sha256",
    "policy",
    "registered_member_count",
    "registration_identity_sha256",
    "schema_version",
    "status",
})
_ACQUISITION_OUTCOME_KEYS = frozenset({
    "members",
    "request_end_at",
    "request_start_at",
})
_MEMBER_ACQUISITION_OUTCOME_KEYS = frozenset({
    "accepted_bars",
    "accepted_bar_starts_sha256",
    "complete_session_count",
    "market",
    "member_ordinal",
    "off_grid_accepted_bars",
    "pages",
    "raw_rows",
    "rejected_rows",
    "scheduled_grid_coverage_bitset_hex",
    "scheduled_grid_present_bars",
    "scheduled_grid_present_starts_sha256",
    "symbol",
})
_ASSESSMENT_AGGREGATE_KEYS = frozenset({
    "candidate_thresholds_met",
    "covered_sessions",
    "event_count",
    "event_sessions",
    "gross_edge_to_cost_ratio",
    "median_cost_bps",
    "median_gross_edge_bps",
    "median_net_return_bps",
    "session_cluster_lcb_90_bps",
    "session_denominator",
})
_ASSESSMENT_POLICY_KEYS = frozenset({
    "automatic_promotion_allowed",
    "order_submission_allowed",
    "position_add_on_allowed",
    "promotion_eligible",
    "recommended_action",
    "short_entry_allowed",
})


@dataclass(frozen=True)
class _ReplayedSessionInput:
    session_bars: tuple[QuantV6Bar, ...]
    threshold_evidence: QuantV6ThresholdEvidence
    fee_rate: Decimal


@dataclass(frozen=True)
class _AssessmentLeafDeclaration:
    session_date: date
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class _AcquisitionProjection:
    outcome: dict[str, object]
    present_grid_starts: frozenset[datetime]


def _require_exact_dict(
    value: object,
    *,
    label: str,
    keys: frozenset[str],
) -> dict[str, Any]:
    result = _require_dict(value, label=label)
    actual_keys = frozenset(result)
    if actual_keys != keys:
        raise QuantV6PublicationError(
            f"{label} fields differ: expected {sorted(keys)}, "
            f"found {sorted(actual_keys)}"
        )
    return result


def _require_text(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise QuantV6PublicationError(f"{label} must be text")
    return value


def _require_nonnegative_integer(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise QuantV6PublicationError(
            f"{label} must be a non-negative integer"
        )
    return value


def _require_sha256(value: object, *, label: str) -> str:
    result = _require_text(value, label=label)
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise QuantV6PublicationError(
            f"{label} must be lowercase SHA-256"
        )
    return result


def _optional_sha256(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, label=label)


def _parse_canonical_date(value: object, *, label: str) -> date:
    text_value = _require_text(value, label=label)
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError as exc:
        raise QuantV6PublicationError(
            f"{label} must be a canonical ISO date"
        ) from exc
    if parsed.isoformat() != text_value:
        raise QuantV6PublicationError(
            f"{label} must be a canonical ISO date"
        )
    return parsed


def _parse_canonical_timestamp(value: object, *, label: str) -> datetime:
    text_value = _require_text(value, label=label)
    try:
        parsed = datetime.strptime(
            text_value,
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise QuantV6PublicationError(
            f"{label} must be a canonical UTC timestamp"
        ) from exc
    if canonical_utc_timestamp(parsed) != text_value:
        raise QuantV6PublicationError(
            f"{label} must be a canonical UTC timestamp"
        )
    return parsed


def _parse_canonical_decimal(value: object, *, label: str) -> Decimal:
    text_value = _require_text(value, label=label)
    try:
        parsed = Decimal(text_value)
        rendered = canonical_decimal(parsed)
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise QuantV6PublicationError(
            f"{label} must be a finite canonical decimal"
        ) from exc
    if rendered != text_value:
        raise QuantV6PublicationError(
            f"{label} must be a finite canonical decimal"
        )
    return parsed


def _optional_canonical_decimal(
    value: object,
    *,
    label: str,
) -> Decimal | None:
    if value is None:
        return None
    return _parse_canonical_decimal(value, label=label)


def _parse_canonical_text_list(
    value: object,
    *,
    label: str,
) -> tuple[str, ...]:
    items = _require_list(value, label=label)
    result = tuple(
        _require_text(item, label=f"{label} item") for item in items
    )
    if any(not item or item != item.strip() for item in result):
        raise QuantV6PublicationError(
            f"{label} items must be non-empty canonical text"
        )
    if len(set(result)) != len(result):
        raise QuantV6PublicationError(f"{label} items must be unique")
    return result


def _parse_quant_v6_bar(value: object, *, label: str) -> QuantV6Bar:
    payload = _require_exact_dict(value, label=label, keys=_BAR_KEYS)
    try:
        bar = QuantV6Bar(
            start_at=_parse_canonical_timestamp(
                payload["start_at"],
                label=f"{label} start_at",
            ),
            open=_parse_canonical_decimal(
                payload["open"],
                label=f"{label} open",
            ),
            high=_parse_canonical_decimal(
                payload["high"],
                label=f"{label} high",
            ),
            low=_parse_canonical_decimal(
                payload["low"],
                label=f"{label} low",
            ),
            close=_parse_canonical_decimal(
                payload["close"],
                label=f"{label} close",
            ),
            volume=_parse_canonical_decimal(
                payload["volume"],
                label=f"{label} volume",
            ),
        )
    except QuantV6SemanticError as exc:
        raise QuantV6PublicationError(
            f"{label} failed typed bar validation"
        ) from exc
    if QuantV6Bar.canonical_payload(bar) != payload:
        raise QuantV6PublicationError(
            f"{label} failed canonical bar replay"
        )
    return bar


def _parse_training_session(
    value: object,
    *,
    label: str,
) -> QuantV6TrainingSession:
    payload = _require_exact_dict(
        value,
        label=label,
        keys=_TRAINING_SESSION_KEYS,
    )
    returns = _require_list(
        payload["absolute_log_returns_bps"],
        label=f"{label} absolute returns",
    )
    for index, item in enumerate(returns):
        _parse_canonical_decimal(
            item,
            label=f"{label} absolute return {index}",
        )
    _require_sha256(
        payload["bar_input_sha256"],
        label=f"{label} bar input digest",
    )
    return_count = _require_nonnegative_integer(
        payload["return_count"],
        label=f"{label} return_count",
    )
    if return_count != len(returns):
        raise QuantV6PublicationError(
            f"{label} return count does not match its values"
        )
    bars = tuple(
        _parse_quant_v6_bar(item, label=f"{label} bar {index}")
        for index, item in enumerate(
            _require_list(payload["bars"], label=f"{label} bars")
        )
    )
    try:
        return QuantV6TrainingSession(
            session_date=_parse_canonical_date(
                payload["session_date"],
                label=f"{label} session_date",
            ),
            bars=bars,
        )
    except QuantV6SemanticError as exc:
        raise QuantV6PublicationError(
            f"{label} failed typed training-session validation"
        ) from exc


def _parse_threshold_evidence(
    value: object,
    *,
    label: str,
) -> QuantV6ThresholdEvidence:
    payload = _require_exact_dict(
        value,
        label=label,
        keys=_THRESHOLD_EVIDENCE_KEYS,
    )
    symbol = _require_text(payload["symbol"], label=f"{label} symbol")
    market = _require_text(payload["market"], label=f"{label} market")
    target_session_date = _parse_canonical_date(
        payload["target_session_date"],
        label=f"{label} target_session_date",
    )
    _require_nonnegative_integer(
        payload["minimum_returns"],
        label=f"{label} minimum_returns",
    )
    _parse_canonical_decimal(
        payload["percentile"],
        label=f"{label} percentile",
    )
    _require_sha256(
        payload["preimage_digest_sha256"],
        label=f"{label} preimage digest",
    )
    _parse_canonical_decimal(
        payload["shock_threshold_bps"],
        label=f"{label} shock threshold",
    )
    _require_nonnegative_integer(
        payload["training_sessions_required"],
        label=f"{label} training_sessions_required",
    )
    training_sessions = tuple(
        _parse_training_session(item, label=f"{label} training {index}")
        for index, item in enumerate(_require_list(
            payload["training_sessions"],
            label=f"{label} training sessions",
        ))
    )
    try:
        rebuilt = build_quant_v6_threshold_evidence(
            symbol=symbol,
            market=market,
            target_session_date=target_session_date,
            training_sessions=training_sessions,
        )
    except QuantV6SemanticError as exc:
        raise QuantV6PublicationError(
            f"{label} failed typed threshold replay"
        ) from exc
    if canonical_quant_v6_json(
        QuantV6ThresholdEvidence.canonical_payload(rebuilt)
    ) != canonical_quant_v6_json(payload):
        raise QuantV6PublicationError(
            f"{label} conflicts with canonical threshold replay"
        )
    return rebuilt


def _replay_session_input(
    *,
    binding: QuantV6PendingArtifactBinding,
    decoded: dict[str, Any],
    symbol: str,
    market: str,
    session_date: date,
) -> _ReplayedSessionInput:
    label = f"{symbol} session input {binding.artifact_ordinal}"
    payload = _require_exact_dict(
        decoded,
        label=label,
        keys=_SESSION_INPUT_KEYS,
    )
    acquisition = _require_exact_dict(
        payload["acquisition"],
        label=f"{label} acquisition",
        keys=frozenset((*QUANT_V6_ACQUISITION_SPEC.keys(), "spec_digest_sha256")),
    )
    expected_acquisition = {
        **dict(QUANT_V6_ACQUISITION_SPEC),
        "spec_digest_sha256": QUANT_V6_ACQUISITION_SPEC_DIGEST,
    }
    if canonical_quant_v6_json(acquisition) != canonical_quant_v6_json(
        expected_acquisition
    ):
        raise QuantV6PublicationError(
            f"{label} acquisition contract conflicts with frozen semantics"
        )
    identity = _require_exact_dict(
        payload["identity"],
        label=f"{label} identity",
        keys=frozenset({"market", "session_date", "symbol"}),
    )
    identity_symbol = _require_text(
        identity["symbol"],
        label=f"{label} identity symbol",
    )
    identity_market = _require_text(
        identity["market"],
        label=f"{label} identity market",
    )
    identity_date = _parse_canonical_date(
        identity["session_date"],
        label=f"{label} identity session_date",
    )
    p0 = _require_exact_dict(
        payload["p0"],
        label=f"{label} p0",
        keys=frozenset({
            "automatic_promotion_allowed",
            "order_submission_allowed",
            "position_add_on_allowed",
            "short_entry_allowed",
        }),
    )
    if canonical_quant_v6_json(p0) != canonical_quant_v6_json({
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "position_add_on_allowed": False,
        "short_entry_allowed": False,
    }):
        raise QuantV6PublicationError(f"{label} violates P0 policy")
    if (
        _require_text(
            payload["algorithm_version"],
            label=f"{label} algorithm_version",
        )
        != QUANT_V6_ALGORITHM_VERSION
        or _require_text(
            payload["capture_mode"],
            label=f"{label} capture_mode",
        )
        != BAR_NEXT_OPEN_STRESSED
        or _require_text(
            payload["contract"],
            label=f"{label} contract",
        )
        != QUANT_V6_SESSION_INPUT_CONTRACT
        or _require_nonnegative_integer(
            payload["schema_version"],
            label=f"{label} schema_version",
        )
        != QUANT_V6_PAYLOAD_SCHEMA_VERSION
        or _require_text(
            payload["semantic_digest"],
            label=f"{label} semantic_digest",
        )
        != QUANT_V6_SEMANTIC_DIGEST
        or identity_symbol != symbol
        or identity_market != market
        or identity_date != session_date
    ):
        raise QuantV6PublicationError(
            f"{label} identity or semantic contract conflicts"
        )
    _require_sha256(
        payload["bar_input_sha256"],
        label=f"{label} bar input digest",
    )
    bars = tuple(
        _parse_quant_v6_bar(item, label=f"{label} bar {index}")
        for index, item in enumerate(
            _require_list(payload["bars"], label=f"{label} bars")
        )
    )
    threshold = _parse_threshold_evidence(
        payload["threshold_evidence"],
        label=f"{label} threshold evidence",
    )
    fee_rate = _parse_canonical_decimal(
        payload["fee_rate"],
        label=f"{label} fee_rate",
    )
    if (
        threshold.symbol != symbol
        or threshold.market != market
        or threshold.target_session_date != session_date
        or fee_rate != quant_v6_fee_rate(market)
    ):
        raise QuantV6PublicationError(
            f"{label} typed threshold identity or fee conflicts"
        )
    try:
        leaf = QuantV6SessionLeaf(
            session_date=session_date,
            status=SESSION_COVERED,
            session_bars=bars,
            threshold_evidence=threshold,
            fee_rate=fee_rate,
        )
        rebuilt_artifact = leaf.encoded_replay_input(
            symbol=symbol,
            market=market,
        )
    except (QuantV6AssessmentError, QuantV6SemanticError) as exc:
        raise QuantV6PublicationError(
            f"{label} failed canonical session-input replay"
        ) from exc
    _compare_encoded_artifacts(
        binding.artifact,
        rebuilt_artifact,
        label=label,
    )
    return _ReplayedSessionInput(
        session_bars=bars,
        threshold_evidence=threshold,
        fee_rate=fee_rate,
    )


def _parse_assessment_leaf_declaration(
    value: object,
    *,
    label: str,
) -> _AssessmentLeafDeclaration:
    payload = _require_exact_dict(
        value,
        label=label,
        keys=_ASSESSMENT_LEAF_KEYS,
    )
    session_date = _parse_canonical_date(
        payload["session_date"],
        label=f"{label} session_date",
    )
    status = _require_text(payload["status"], label=f"{label} status")
    blockers = _parse_canonical_text_list(
        payload["blockers"],
        label=f"{label} blockers",
    )
    bar_digest = _optional_sha256(
        payload["bar_input_sha256"],
        label=f"{label} bar input digest",
    )
    replay_digest = _optional_sha256(
        payload["replay_input_artifact_sha256"],
        label=f"{label} replay input digest",
    )
    threshold_digest = _optional_sha256(
        payload["threshold_preimage_sha256"],
        label=f"{label} threshold preimage digest",
    )
    fee_rate = _optional_canonical_decimal(
        payload["fee_rate"],
        label=f"{label} fee_rate",
    )
    event_digests = tuple(
        _require_sha256(item, label=f"{label} event digest {index}")
        for index, item in enumerate(_require_list(
            payload["event_artifact_sha256"],
            label=f"{label} event digests",
        ))
    )
    event_count = _require_nonnegative_integer(
        payload["event_count"],
        label=f"{label} event_count",
    )
    if event_count != len(event_digests):
        raise QuantV6PublicationError(
            f"{label} event count does not match its digest list"
        )
    if status == SESSION_COVERED:
        if (
            blockers
            or bar_digest is None
            or replay_digest is None
            or threshold_digest is None
            or fee_rate is None
        ):
            raise QuantV6PublicationError(
                f"{label} covered declaration is incomplete"
            )
    elif status == SESSION_MISSING:
        if (
            not blockers
            or bar_digest is not None
            or replay_digest is not None
            or threshold_digest is not None
            or fee_rate is not None
            or event_digests
            or event_count != 0
        ):
            raise QuantV6PublicationError(
                f"{label} missing declaration contains replay evidence"
            )
    else:
        raise QuantV6PublicationError(
            f"{label} has an unsupported status"
        )
    return _AssessmentLeafDeclaration(
        session_date=session_date,
        status=status,
        blockers=blockers,
    )


def _parse_assessment_leaf_declarations(
    assessment: dict[str, Any],
    *,
    symbol: str,
    market: str,
) -> tuple[_AssessmentLeafDeclaration, ...]:
    payload = _require_exact_dict(
        assessment,
        label=f"{symbol} assessment",
        keys=_ASSESSMENT_KEYS,
    )
    identity = _require_exact_dict(
        payload["identity"],
        label=f"{symbol} assessment identity",
        keys=frozenset({"market", "symbol", "window_digest_sha256"}),
    )
    if (
        _require_text(
            identity["symbol"],
            label=f"{symbol} assessment identity symbol",
        )
        != symbol
        or _require_text(
            identity["market"],
            label=f"{symbol} assessment identity market",
        )
        != market
    ):
        raise QuantV6PublicationError(
            f"assessment identity conflicts for {symbol}"
        )
    _require_sha256(
        identity["window_digest_sha256"],
        label=f"{symbol} assessment window digest",
    )
    _require_dict(
        payload["aggregates"],
        label=f"{symbol} assessment aggregates",
    )
    _parse_canonical_text_list(
        payload["blockers"],
        label=f"{symbol} assessment blockers",
    )
    _require_sha256(
        payload["event_set_digest_sha256"],
        label=f"{symbol} assessment event-set digest",
    )
    _require_dict(payload["policy"], label=f"{symbol} assessment policy")
    _require_dict(
        payload["session_cluster_methodology"],
        label=f"{symbol} assessment cluster methodology",
    )
    _require_dict(
        payload["thresholds"],
        label=f"{symbol} assessment thresholds",
    )
    return tuple(
        _parse_assessment_leaf_declaration(
            item,
            label=f"{symbol} assessment leaf {index}",
        )
        for index, item in enumerate(
            _require_list(payload["leaves"], label=f"{symbol} assessment leaves")
        )
    )


def _scheduled_session_grids(
    plan: QuantV6RegistrationPlan,
) -> tuple[tuple[date, tuple[datetime, ...]], ...]:
    session_dates = (
        *plan.training_session_dates,
        *plan.target_session_dates,
    )
    grids = tuple(
        (
            session_date,
            quant_v6_expected_rth_bar_starts(plan.market, session_date),
        )
        for session_date in session_dates
    )
    flattened = tuple(
        start_at
        for _session_date, grid in grids
        for start_at in grid
    )
    if (
        any(not grid for _session_date, grid in grids)
        or len(flattened) != len(set(flattened))
        or any(
            current <= previous
            for previous, current in zip(flattened, flattened[1:])
        )
    ):
        raise QuantV6PublicationError(
            "registration scheduled grid is not canonical"
        )
    return grids


def _encode_grid_coverage_bitset(flags: Sequence[bool]) -> str:
    encoded = bytearray((len(flags) + 7) // 8)
    for index, present in enumerate(flags):
        if present:
            encoded[index // 8] |= 1 << (7 - (index % 8))
    return bytes(encoded).hex()


def _decode_grid_coverage_bitset(
    value: object,
    *,
    expected_bits: int,
    label: str,
) -> tuple[bool, ...]:
    text_value = _require_text(value, label=label)
    expected_bytes = (expected_bits + 7) // 8
    if (
        len(text_value) != expected_bytes * 2
        or any(character not in "0123456789abcdef" for character in text_value)
    ):
        raise QuantV6PublicationError(
            f"{label} must be a canonical fixed-width lowercase hex bitset"
        )
    encoded = bytes.fromhex(text_value)
    unused_bits = expected_bytes * 8 - expected_bits
    if unused_bits and encoded and encoded[-1] & ((1 << unused_bits) - 1):
        raise QuantV6PublicationError(
            f"{label} has non-zero padding bits"
        )
    return tuple(
        bool(encoded[index // 8] & (1 << (7 - (index % 8))))
        for index in range(expected_bits)
    )


def _accepted_bar_starts_sha256(
    *,
    plan: QuantV6RegistrationPlan,
    evaluation: QuantV6CandidateEvaluation,
    starts: Sequence[datetime],
) -> str:
    # Stream the preimage because a real cohort member can have more than the
    # canonical JSON container item limit. Newline is outside every canonical
    # field alphabet used here, so the framing is unambiguous.
    digest = hashlib.sha256()
    for value in (
        QUANT_V6_ACCEPTED_BAR_STARTS_DIGEST_CONTRACT,
        plan.identity_sha256,
        evaluation.member.market,
        evaluation.member.symbol,
        *(canonical_utc_timestamp(start_at) for start_at in starts),
    ):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _scheduled_grid_present_starts_sha256(
    *,
    plan: QuantV6RegistrationPlan,
    starts: Sequence[datetime],
) -> str:
    digest = hashlib.sha256()
    for value in (
        QUANT_V6_SCHEDULED_GRID_STARTS_DIGEST_CONTRACT,
        plan.identity_sha256,
        *(canonical_utc_timestamp(start_at) for start_at in starts),
    ):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _projection_from_outcome(
    outcome: Mapping[str, object],
    *,
    plan: QuantV6RegistrationPlan,
    grids: Sequence[tuple[date, tuple[datetime, ...]]],
    label: str,
) -> _AcquisitionProjection:
    exact = _require_exact_dict(
        outcome,
        label=label,
        keys=_MEMBER_ACQUISITION_OUTCOME_KEYS,
    )
    accepted_bars = _require_nonnegative_integer(
        exact["accepted_bars"],
        label=f"{label} accepted bars",
    )
    _require_sha256(
        exact["accepted_bar_starts_sha256"],
        label=f"{label} accepted bar starts digest",
    )
    complete_session_count = _require_nonnegative_integer(
        exact["complete_session_count"],
        label=f"{label} complete session count",
    )
    scheduled_grid_present_bars = _require_nonnegative_integer(
        exact["scheduled_grid_present_bars"],
        label=f"{label} scheduled grid present bars",
    )
    off_grid_accepted_bars = _require_nonnegative_integer(
        exact["off_grid_accepted_bars"],
        label=f"{label} off-grid accepted bars",
    )
    flat_grid = tuple(
        start_at for _session_date, grid in grids for start_at in grid
    )
    flags = _decode_grid_coverage_bitset(
        exact["scheduled_grid_coverage_bitset_hex"],
        expected_bits=len(flat_grid),
        label=f"{label} scheduled grid coverage",
    )
    present_grid_starts = frozenset(
        start_at
        for start_at, present in zip(flat_grid, flags, strict=True)
        if present
    )
    complete_session_dates = frozenset(
        session_date
        for session_date, grid in grids
        if all(start_at in present_grid_starts for start_at in grid)
    )
    expected_scheduled_digest = _scheduled_grid_present_starts_sha256(
        plan=plan,
        starts=tuple(sorted(present_grid_starts)),
    )
    if (
        scheduled_grid_present_bars != len(present_grid_starts)
        or scheduled_grid_present_bars > accepted_bars
        or off_grid_accepted_bars
        != accepted_bars - scheduled_grid_present_bars
        or complete_session_count != len(complete_session_dates)
        or not _same_digest(
            _require_sha256(
                exact["scheduled_grid_present_starts_sha256"],
                label=f"{label} scheduled grid present starts digest",
            ),
            expected_scheduled_digest,
        )
    ):
        raise QuantV6PublicationError(
            f"{label} scheduled grid projection conflicts with counts"
        )
    return _AcquisitionProjection(
        outcome=dict(exact),
        present_grid_starts=present_grid_starts,
    )


def _fresh_acquisition_projection(
    *,
    plan: QuantV6RegistrationPlan,
    evaluation: QuantV6CandidateEvaluation,
    grids: Sequence[tuple[date, tuple[datetime, ...]]],
    request_start_at: datetime,
    request_end_at: datetime,
) -> _AcquisitionProjection:
    if type(evaluation.fetched_bar_starts) is not tuple:
        raise QuantV6PublicationError(
            "candidate fetched bar starts must be an immutable tuple"
        )
    normalized: list[datetime] = []
    for index, value in enumerate(evaluation.fetched_bar_starts):
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise QuantV6PublicationError(
                f"candidate fetched bar start {index} must be timezone-aware"
            )
        start_at = value.astimezone(timezone.utc)
        if (
            start_at < request_start_at
            or start_at >= request_end_at
            or (normalized and start_at <= normalized[-1])
        ):
            raise QuantV6PublicationError(
                "candidate fetched bar starts violate the acquisition window "
                "or strict ordering"
            )
        normalized.append(start_at)
    if len(normalized) != evaluation.fetched_accepted_bars:
        raise QuantV6PublicationError(
            "candidate accepted bar count conflicts with its timestamp witness"
        )
    accepted_starts = frozenset(normalized)
    flat_grid = tuple(
        start_at for _session_date, grid in grids for start_at in grid
    )
    flags = tuple(start_at in accepted_starts for start_at in flat_grid)
    present_grid_starts = frozenset(
        start_at
        for start_at, present in zip(flat_grid, flags, strict=True)
        if present
    )
    complete_session_count = sum(
        all(start_at in present_grid_starts for start_at in grid)
        for _session_date, grid in grids
    )
    outcome: dict[str, object] = {
        "accepted_bars": evaluation.fetched_accepted_bars,
        "accepted_bar_starts_sha256": _accepted_bar_starts_sha256(
            plan=plan,
            evaluation=evaluation,
            starts=normalized,
        ),
        "complete_session_count": complete_session_count,
        "market": evaluation.member.market,
        "member_ordinal": evaluation.member.ordinal,
        "off_grid_accepted_bars": (
            evaluation.fetched_accepted_bars - len(present_grid_starts)
        ),
        "pages": evaluation.fetched_pages,
        "raw_rows": evaluation.fetched_raw_rows,
        "rejected_rows": evaluation.rejected_rows,
        "scheduled_grid_coverage_bitset_hex": (
            _encode_grid_coverage_bitset(flags)
        ),
        "scheduled_grid_present_bars": len(present_grid_starts),
        "scheduled_grid_present_starts_sha256": (
            _scheduled_grid_present_starts_sha256(
                plan=plan,
                starts=tuple(sorted(present_grid_starts)),
            )
        ),
        "symbol": evaluation.member.symbol,
    }
    return _projection_from_outcome(
        outcome,
        plan=plan,
        grids=grids,
        label=f"candidate {evaluation.member.symbol} acquisition projection",
    )


def _validate_candidate_closure(
    *,
    plan: QuantV6RegistrationPlan,
    evaluation: QuantV6CandidateEvaluation,
    decoded_by_key: Mapping[tuple[str, int], dict[str, Any]],
    binding_by_key: Mapping[
        tuple[str, int],
        QuantV6PendingArtifactBinding,
    ],
    present_grid_starts: frozenset[datetime],
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> None:
    _evaluation_checkpoint(evaluation_deadline)
    assessment_binding = binding_by_key[(ASSESSMENT_ROLE, 0)]
    assessment = decoded_by_key[(ASSESSMENT_ROLE, 0)]
    declarations = _parse_assessment_leaf_declarations(
        assessment,
        symbol=evaluation.member.symbol,
        market=evaluation.member.market,
    )
    if len(declarations) != len(plan.target_session_dates):
        raise QuantV6PublicationError(
            f"assessment leaf count conflicts for {evaluation.member.symbol}"
        )

    session_keys = {
        key for key in binding_by_key if key[0] == SESSION_INPUT_ROLE
    }
    event_keys = {key for key in binding_by_key if key[0] == EVENT_ROLE}
    consumed_session_keys: set[tuple[str, int]] = set()
    consumed_event_keys: set[tuple[str, int]] = set()
    replayed_leaves: list[QuantV6SessionLeaf] = []
    next_event_ordinal = 0
    for ordinal, (target_date, declaration) in enumerate(zip(
        plan.target_session_dates,
        declarations,
        strict=True,
    )):
        _evaluation_checkpoint(evaluation_deadline)
        if declaration.session_date != target_date:
            raise QuantV6PublicationError(
                f"assessment leaf date conflicts for {evaluation.member.symbol}"
            )
        prior_dates = quant_v6_previous_trading_session_dates(
            plan.market,
            target_date,
            count=len(plan.training_session_dates),
        )
        target_grid = quant_v6_expected_rth_bar_starts(
            plan.market,
            target_date,
        )
        training_grids = tuple(
            quant_v6_expected_rth_bar_starts(plan.market, session_date)
            for session_date in prior_dates
        )
        expected_blockers: list[str] = []
        if not all(
            start_at in present_grid_starts for start_at in target_grid
        ):
            expected_blockers.append("MISSING_COMPLETE_TARGET_BAR_INPUT")
        if any(
            not all(
                start_at in present_grid_starts for start_at in training_grid
            )
            for training_grid in training_grids
        ):
            expected_blockers.append("MISSING_COMPLETE_TRAINING_BAR_INPUT")
        if expected_blockers:
            if (
                declaration.status != SESSION_MISSING
                or declaration.blockers != tuple(expected_blockers)
            ):
                raise QuantV6PublicationError(
                    "assessment status or blockers conflict with acquisition grid "
                    f"evidence for {evaluation.member.symbol}"
                )
        elif declaration.status != SESSION_COVERED:
            raise QuantV6PublicationError(
                "assessment status or blockers conflict with acquisition grid "
                f"evidence for {evaluation.member.symbol}"
            )
        if declaration.status == SESSION_MISSING:
            try:
                replayed_leaves.append(QuantV6SessionLeaf(
                    session_date=target_date,
                    status=SESSION_MISSING,
                    blockers=declaration.blockers,
                ))
            except QuantV6AssessmentError as exc:
                raise QuantV6PublicationError(
                    f"missing leaf failed typed replay for {evaluation.member.symbol}"
                ) from exc
            continue

        session_key = (SESSION_INPUT_ROLE, ordinal)
        session_binding = binding_by_key.get(session_key)
        decoded_session = decoded_by_key.get(session_key)
        if session_binding is None or decoded_session is None:
            raise QuantV6PublicationError(
                f"session input closure failed for {evaluation.member.symbol}"
            )
        if session_binding.session_date != target_date:
            raise QuantV6PublicationError(
                f"session input date conflicts for {evaluation.member.symbol}"
            )
        replayed_input = _replay_session_input(
            binding=session_binding,
            decoded=decoded_session,
            symbol=evaluation.member.symbol,
            market=evaluation.member.market,
            session_date=target_date,
        )
        consumed_session_keys.add(session_key)
        try:
            replayed_events = build_bar_next_open_stressed_session_events(
                symbol=evaluation.member.symbol,
                market=evaluation.member.market,
                session_date=target_date,
                bars=replayed_input.session_bars,
                threshold_evidence=replayed_input.threshold_evidence,
                fee_rate=replayed_input.fee_rate,
            )
        except QuantV6SemanticError as exc:
            raise QuantV6PublicationError(
                f"session event replay failed for {evaluation.member.symbol}"
            ) from exc
        for event in replayed_events:
            _evaluation_checkpoint(evaluation_deadline)
            event_key = (EVENT_ROLE, next_event_ordinal)
            event_binding = binding_by_key.get(event_key)
            if event_binding is None:
                raise QuantV6PublicationError(
                    f"event binding closure failed for {evaluation.member.symbol}"
                )
            if event_binding.session_date != target_date:
                raise QuantV6PublicationError(
                    f"event binding date conflicts for {evaluation.member.symbol}"
                )
            try:
                rebuilt_event_artifact = event.encoded_artifact()
            except QuantV6SemanticError as exc:
                raise QuantV6PublicationError(
                    f"event failed canonical replay for {evaluation.member.symbol}"
                ) from exc
            _compare_encoded_artifacts(
                event_binding.artifact,
                rebuilt_event_artifact,
                label=(
                    f"{evaluation.member.symbol} event {next_event_ordinal}"
                ),
            )
            consumed_event_keys.add(event_key)
            next_event_ordinal += 1
        try:
            replayed_leaves.append(QuantV6SessionLeaf(
                session_date=target_date,
                status=SESSION_COVERED,
                session_bars=replayed_input.session_bars,
                threshold_evidence=replayed_input.threshold_evidence,
                fee_rate=replayed_input.fee_rate,
                events=replayed_events,
            ))
        except QuantV6AssessmentError as exc:
            raise QuantV6PublicationError(
                f"covered leaf failed typed replay for {evaluation.member.symbol}"
            ) from exc

    if session_keys != consumed_session_keys:
        raise QuantV6PublicationError(
            f"session input closure failed for {evaluation.member.symbol}"
        )
    if event_keys != consumed_event_keys:
        raise QuantV6PublicationError(
            f"event binding closure failed for {evaluation.member.symbol}"
        )
    try:
        _evaluation_checkpoint(evaluation_deadline)
        rebuilt_assessment = assess_bar_next_open_stressed_window(
            symbol=evaluation.member.symbol,
            market=evaluation.member.market,
            leaves=replayed_leaves,
            checkpoint=(
                evaluation_deadline.checkpoint
                if evaluation_deadline is not None
                else None
            ),
        )
        rebuilt_assessment_artifact = rebuilt_assessment.encoded_artifact(
            checkpoint=(
                evaluation_deadline.checkpoint
                if evaluation_deadline is not None
                else None
            )
        )
    except QuantV6AssessmentError as exc:
        raise QuantV6PublicationError(
            f"assessment typed replay failed for {evaluation.member.symbol}"
        ) from exc
    _compare_encoded_artifacts(
        assessment_binding.artifact,
        rebuilt_assessment_artifact,
        label=f"{evaluation.member.symbol} assessment",
    )
    if (
        evaluation.recommended_action != rebuilt_assessment.recommended_action
        or evaluation.covered_sessions != rebuilt_assessment.covered_sessions
        or evaluation.event_count != rebuilt_assessment.event_count
        or evaluation.event_sessions != rebuilt_assessment.event_sessions
        or evaluation.blockers != rebuilt_assessment.blockers
        or not _same_digest(
            evaluation.assessment_artifact_sha256,
            rebuilt_assessment_artifact.digest_sha256,
        )
    ):
        raise QuantV6PublicationError(
            f"assessment summary conflicts for {evaluation.member.symbol}"
        )
    _evaluation_checkpoint(evaluation_deadline)


def _prepare_publication(
    *,
    plan: QuantV6RegistrationPlan,
    evaluations: Sequence[QuantV6CandidateEvaluation],
    persisted_acquisition_outcomes: (
        Sequence[Mapping[str, object]] | None
    ) = None,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> _PreparedPublication:
    _evaluation_checkpoint(evaluation_deadline)
    normalized = tuple(evaluations)
    if len(normalized) != len(plan.members):
        raise QuantV6PublicationError(
            "publication requires exactly every registered member"
        )
    if any(type(value) is not QuantV6CandidateEvaluation for value in normalized):
        raise QuantV6PublicationError(
            "publication contains an unsupported evaluation type"
        )
    ordered_evaluations = tuple(
        sorted(normalized, key=lambda value: value.member.ordinal)
    )
    if [value.member.ordinal for value in ordered_evaluations] != list(
        range(len(plan.members))
    ):
        raise QuantV6PublicationError(
            "publication member ordinals are incomplete or duplicated"
        )
    if len({value.member.symbol for value in ordered_evaluations}) != len(
        ordered_evaluations
    ):
        raise QuantV6PublicationError(
            "publication member symbols are duplicated"
        )

    grids = _scheduled_session_grids(plan)
    request_start_at = grids[0][1][0]
    request_end_at = plan.data_cutoff_at.astimezone(timezone.utc)
    persisted_outcomes = (
        None
        if persisted_acquisition_outcomes is None
        else tuple(persisted_acquisition_outcomes)
    )
    if (
        persisted_outcomes is not None
        and len(persisted_outcomes) != len(ordered_evaluations)
    ):
        raise QuantV6PublicationError(
            "persisted acquisition projections are incomplete"
        )

    all_bindings: list[QuantV6PendingArtifactBinding] = []
    artifacts: dict[str, EncodedQuantV6Artifact] = {}
    seen_binding_digests: set[str] = set()
    acquisition_outcomes: list[dict[str, object]] = []
    for expected_member, evaluation in zip(
        plan.members,
        ordered_evaluations,
        strict=True,
    ):
        _evaluation_checkpoint(evaluation_deadline)
        if (
            type(evaluation.member) is not type(expected_member)
            or evaluation.member.canonical_payload()
            != expected_member.canonical_payload()
            or type(evaluation.recommended_action) is not str
            or evaluation.recommended_action not in {"AVOID", "WATCH"}
            or type(evaluation.blockers) is not tuple
            or any(type(value) is not str for value in evaluation.blockers)
            or type(evaluation.assessment_artifact_sha256) is not str
        ):
            raise QuantV6PublicationError(
                "candidate evaluation conflicts with the registration"
            )
        for field in (
            "covered_sessions",
            "event_count",
            "event_sessions",
            "fetched_pages",
            "fetched_raw_rows",
            "fetched_accepted_bars",
            "rejected_rows",
        ):
            value = getattr(evaluation, field)
            if type(value) is not int or value < 0:
                raise QuantV6PublicationError(
                    f"candidate {field} must be a non-negative integer"
                )
        if evaluation.fetched_pages < 1:
            raise QuantV6PublicationError(
                "candidate fetched page count must be positive"
            )
        if (
            evaluation.fetched_accepted_bars + evaluation.rejected_rows
            > evaluation.fetched_raw_rows
        ):
            raise QuantV6PublicationError(
                "candidate accepted and rejected rows exceed fetched rows"
            )
        if persisted_outcomes is None:
            acquisition_projection = _fresh_acquisition_projection(
                plan=plan,
                evaluation=evaluation,
                grids=grids,
                request_start_at=request_start_at,
                request_end_at=request_end_at,
            )
        else:
            persisted_outcome = persisted_outcomes[expected_member.ordinal]
            acquisition_projection = _projection_from_outcome(
                persisted_outcome,
                plan=plan,
                grids=grids,
                label=(
                    f"persisted {evaluation.member.symbol} acquisition "
                    "projection"
                ),
            )
            expected_telemetry = {
                "accepted_bars": evaluation.fetched_accepted_bars,
                "market": evaluation.member.market,
                "member_ordinal": evaluation.member.ordinal,
                "pages": evaluation.fetched_pages,
                "raw_rows": evaluation.fetched_raw_rows,
                "rejected_rows": evaluation.rejected_rows,
                "symbol": evaluation.member.symbol,
            }
            if any(
                acquisition_projection.outcome[key] != value
                or type(acquisition_projection.outcome[key]) is not type(value)
                for key, value in expected_telemetry.items()
            ):
                raise QuantV6PublicationError(
                    "persisted acquisition projection conflicts with its member"
                )
        acquisition_outcomes.append(acquisition_projection.outcome)
        if type(evaluation.bindings) is not tuple or not evaluation.bindings:
            raise QuantV6PublicationError(
                "candidate evaluation requires immutable artifact bindings"
            )

        binding_by_key: dict[
            tuple[str, int], QuantV6PendingArtifactBinding
        ] = {}
        decoded_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        for binding in evaluation.bindings:
            _evaluation_checkpoint(evaluation_deadline)
            if type(binding) is not QuantV6PendingArtifactBinding:
                raise QuantV6PublicationError(
                    "candidate contains an unsupported binding type"
                )
            if (
                type(binding.member_ordinal) is not int
                or type(binding.symbol) is not str
                or type(binding.market) is not str
                or type(binding.role) is not str
                or type(binding.artifact_ordinal) is not int
                or (
                    binding.session_date is not None
                    and type(binding.session_date) is not date
                )
                or type(binding.binding_sha256) is not str
            ):
                raise QuantV6PublicationError(
                    "binding envelope has unsupported field types"
                )
            expected_kind = _KIND_BY_ROLE.get(binding.role)
            if (
                binding.member_ordinal != expected_member.ordinal
                or binding.symbol != expected_member.symbol
                or binding.market != expected_member.market
                or binding.artifact_ordinal < 0
                or expected_kind is None
                or binding.artifact.kind != expected_kind
            ):
                raise QuantV6PublicationError(
                    "binding identity conflicts with its registered member"
                )
            if binding.role == ASSESSMENT_ROLE and (
                binding.artifact_ordinal != 0
                or binding.session_date is not None
            ):
                raise QuantV6PublicationError(
                    "assessment binding identity is invalid"
                )
            if binding.role == SESSION_INPUT_ROLE and (
                binding.artifact_ordinal >= len(plan.target_session_dates)
                or binding.session_date
                != plan.target_session_dates[binding.artifact_ordinal]
            ):
                raise QuantV6PublicationError(
                    "session input binding is outside the frozen schedule"
                )
            if binding.role == EVENT_ROLE and (
                binding.session_date not in plan.target_session_dates
            ):
                raise QuantV6PublicationError(
                    "event binding is outside the frozen schedule"
                )
            key = (binding.role, binding.artifact_ordinal)
            if key in binding_by_key:
                raise QuantV6PublicationError(
                    "candidate contains a duplicate binding ordinal"
                )
            if binding.binding_sha256 in seen_binding_digests:
                raise QuantV6PublicationError(
                    "cohort contains a duplicate binding identity"
                )
            expected_binding_digest = quant_v6_payload_sha256(
                _binding_preimage(
                    registration_identity_sha256=plan.identity_sha256,
                    binding=binding,
                )
            )
            if not _same_digest(
                binding.binding_sha256,
                expected_binding_digest,
            ):
                raise QuantV6PublicationError(
                    "binding digest failed canonical replay"
                )
            decoded = _decode_and_verify_artifact(
                binding.artifact,
                label=f"{expected_member.symbol} {binding.role}",
            )
            existing_artifact = artifacts.get(binding.artifact.digest_sha256)
            if existing_artifact is not None:
                _compare_encoded_artifacts(
                    existing_artifact,
                    binding.artifact,
                    label="reused in-memory",
                )
            else:
                artifacts[binding.artifact.digest_sha256] = binding.artifact
            binding_by_key[key] = binding
            # Event payloads are compared against regenerated canonical
            # artifacts below, so retaining every decoded event would multiply
            # peak cohort memory without adding validation strength.
            if binding.role != EVENT_ROLE:
                decoded_by_key[key] = decoded
            seen_binding_digests.add(binding.binding_sha256)
            all_bindings.append(binding)

        if set(key for key in binding_by_key if key[0] == ASSESSMENT_ROLE) != {
            (ASSESSMENT_ROLE, 0)
        }:
            raise QuantV6PublicationError(
                "candidate requires exactly one assessment binding"
            )
        _validate_candidate_closure(
            plan=plan,
            evaluation=evaluation,
            decoded_by_key=decoded_by_key,
            binding_by_key=binding_by_key,
            present_grid_starts=(
                acquisition_projection.present_grid_starts
            ),
            evaluation_deadline=evaluation_deadline,
        )
        _evaluation_checkpoint(evaluation_deadline)

    ordered_bindings = tuple(sorted(
        all_bindings,
        key=lambda value: (
            value.member_ordinal,
            _ROLE_RANK[value.role],
            value.artifact_ordinal,
        ),
    ))
    payload_list: list[dict[str, object]] = []
    assessment_count = 0
    session_input_count = 0
    event_count = 0
    for binding in ordered_bindings:
        _evaluation_checkpoint(evaluation_deadline)
        payload_list.append(_binding_payload(binding))
        if binding.role == ASSESSMENT_ROLE:
            assessment_count += 1
        elif binding.role == SESSION_INPUT_ROLE:
            session_input_count += 1
        elif binding.role == EVENT_ROLE:
            event_count += 1
    payloads = tuple(payload_list)
    manifest_sha256 = _manifest_sha256(
        registration_identity_sha256=plan.identity_sha256,
        binding_payloads=payloads,
        evaluation_deadline=evaluation_deadline,
    )
    if assessment_count != len(plan.members):
        raise QuantV6PublicationError(
            "publication assessment count does not match registration"
        )
    publication_payload = _publication_payload(
        registration_identity_sha256=plan.identity_sha256,
        registered_member_count=len(plan.members),
        manifest_sha256=manifest_sha256,
        assessment_count=assessment_count,
        session_input_count=session_input_count,
        event_count=event_count,
        acquisition_outcomes=acquisition_outcomes,
        request_start_at=request_start_at,
        request_end_at=request_end_at,
    )
    publication_bytes = canonical_quant_v6_json(publication_payload)
    if len(publication_bytes) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
        raise QuantV6PublicationError(
            "publication JSON is outside the bounded root size limit"
        )
    _evaluation_checkpoint(evaluation_deadline)
    return _PreparedPublication(
        bindings=ordered_bindings,
        artifacts=tuple(artifacts[key] for key in sorted(artifacts)),
        manifest_sha256=manifest_sha256,
        publication_json=publication_bytes.decode("utf-8"),
        identity_sha256=hashlib.sha256(publication_bytes).hexdigest(),
        assessment_count=assessment_count,
        session_input_count=session_input_count,
        event_count=event_count,
        acquisition_outcomes=tuple(acquisition_outcomes),
        request_start_at=request_start_at,
        request_end_at=request_end_at,
    )


def _artifact_from_row(
    row: WatchlistQuantV6Artifact,
) -> EncodedQuantV6Artifact:
    if row.compression_level != QUANT_V6_ARTIFACT_COMPRESSION_LEVEL:
        raise QuantV6PublicationConflictError(
            "persisted artifact compression level conflicts with the contract"
        )
    payload = bytes(row.payload)
    return EncodedQuantV6Artifact(
        digest_sha256=row.digest_sha256,
        schema_version=row.schema_version,
        kind=row.kind,
        codec=row.codec,
        raw_size=row.raw_size,
        compressed_size=row.compressed_size,
        payload=payload,
    )


def _verify_artifact_row(
    row: WatchlistQuantV6Artifact,
    expected: EncodedQuantV6Artifact,
) -> None:
    persisted = _artifact_from_row(row)
    # ``expected`` has already passed bounded decode and strict typed replay in
    # ``_prepare_publication`` before any write transaction opens. Exact
    # envelope/byte equality therefore proves the persisted row is the same
    # validated evidence without decompressing it again under SQLite's writer
    # lock.
    _compare_encoded_artifacts(persisted, expected, label="persisted")
    _persisted_utc(row.created_at, label="artifact created_at")


def _load_artifact_rows(
    session: Session,
    digests: Sequence[str],
    *,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> dict[str, WatchlistQuantV6Artifact]:
    """Load content-addressed rows in bounded SQLite-safe batches."""
    _evaluation_checkpoint(evaluation_deadline)
    ordered_digests = tuple(dict.fromkeys(digests))
    rows: dict[str, WatchlistQuantV6Artifact] = {}
    for offset in range(0, len(ordered_digests), _ARTIFACT_QUERY_CHUNK_SIZE):
        _evaluation_checkpoint(evaluation_deadline)
        chunk = ordered_digests[offset:offset + _ARTIFACT_QUERY_CHUNK_SIZE]
        for row in session.scalars(
            select(WatchlistQuantV6Artifact).where(
                WatchlistQuantV6Artifact.digest_sha256.in_(chunk)
            )
        ):
            _evaluation_checkpoint(evaluation_deadline)
            if row.digest_sha256 in rows:
                raise QuantV6PublicationConflictError(
                    "artifact query returned a duplicate content identity"
                )
            rows[row.digest_sha256] = row
    return rows


def _decode_canonical_publication_payload(
    row: WatchlistQuantV6Publication,
) -> dict[str, Any]:
    if type(row.publication_json) is not str:
        raise QuantV6PublicationConflictError(
            "persisted publication JSON is not text"
        )
    raw = row.publication_json.encode("utf-8")
    if not raw or len(raw) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
        raise QuantV6PublicationConflictError(
            "persisted publication JSON is outside the size limit"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
        if type(decoded) is not dict:
            raise QuantV6PublicationConflictError(
                "persisted publication JSON root is not an object"
            )
        canonical = canonical_quant_v6_json(decoded)
    except QuantV6PublicationConflictError:
        raise
    except Exception as exc:
        raise QuantV6PublicationConflictError(
            "persisted publication JSON is not canonical"
        ) from exc
    if canonical != raw or not _same_digest(
        hashlib.sha256(canonical).hexdigest(),
        row.identity_sha256,
    ):
        raise QuantV6PublicationConflictError(
            "persisted publication identity failed canonical replay"
        )
    return decoded


def _publication_row_fields(
    *,
    registration_id: int,
    registration_identity_sha256: str,
    member_count: int,
    prepared: _PreparedPublication,
) -> dict[str, object]:
    return {
        "registration_id": registration_id,
        "registration_identity_sha256": registration_identity_sha256,
        "identity_sha256": prepared.identity_sha256,
        "schema_version": QUANT_V6_PUBLICATION_SCHEMA_VERSION,
        "contract_version": QUANT_V6_PUBLICATION_CONTRACT,
        "status": "PUBLISHED",
        "manifest_sha256": prepared.manifest_sha256,
        "publication_json": prepared.publication_json,
        "registered_member_count": member_count,
        "assessment_artifact_count": prepared.assessment_count,
        "session_input_artifact_count": prepared.session_input_count,
        "event_artifact_count": prepared.event_count,
        "binding_count": prepared.binding_count,
        "promotion_eligible": False,
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "short_entry_allowed": False,
        "position_add_on_allowed": False,
    }


def _verify_publication_row(
    row: WatchlistQuantV6Publication,
    *,
    registration: WatchlistQuantV6Registration,
    plan: QuantV6RegistrationPlan,
    prepared: _PreparedPublication,
) -> None:
    expected = _publication_row_fields(
        registration_id=registration.id,
        registration_identity_sha256=plan.identity_sha256,
        member_count=len(plan.members),
        prepared=prepared,
    )
    for field, expected_value in expected.items():
        actual_value = getattr(row, field)
        if actual_value != expected_value or type(actual_value) is not type(
            expected_value
        ):
            raise QuantV6PublicationConflictError(
                f"persisted publication {field} conflicts with evidence"
            )
    published_at = _persisted_utc(row.published_at, label="published_at")
    registered_at = _persisted_utc(
        registration.registered_at,
        label="registered_at",
    )
    if published_at < registered_at:
        raise QuantV6PublicationConflictError(
            "persisted publication predates registration"
        )
    _decode_canonical_publication_payload(row)


def _binding_from_row(
    row: WatchlistQuantV6PublicationArtifact,
    artifact: EncodedQuantV6Artifact,
) -> QuantV6PendingArtifactBinding:
    return QuantV6PendingArtifactBinding(
        member_ordinal=row.member_ordinal,
        symbol=row.symbol,
        market=row.market,
        role=row.role,
        artifact_ordinal=row.artifact_ordinal,
        session_date=row.session_date,
        artifact=artifact,
        binding_sha256=row.binding_sha256,
    )


def _parse_persisted_acquisition_outcomes(
    payload: dict[str, Any],
    *,
    plan: QuantV6RegistrationPlan,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> tuple[dict[str, object], ...]:
    _evaluation_checkpoint(evaluation_deadline)
    publication = _require_exact_dict(
        payload,
        label="persisted publication payload",
        keys=_PUBLICATION_KEYS,
    )
    acquisition = _require_exact_dict(
        publication["acquisition_outcome"],
        label="persisted acquisition outcome",
        keys=_ACQUISITION_OUTCOME_KEYS,
    )
    request_start_at = _parse_canonical_timestamp(
        acquisition["request_start_at"],
        label="persisted acquisition request_start_at",
    )
    request_end_at = _parse_canonical_timestamp(
        acquisition["request_end_at"],
        label="persisted acquisition request_end_at",
    )
    first_grid = quant_v6_expected_rth_bar_starts(
        plan.market,
        plan.training_session_dates[0],
    )
    if (
        not first_grid
        or request_start_at != first_grid[0]
        or request_end_at != plan.data_cutoff_at.astimezone(timezone.utc)
    ):
        raise QuantV6PublicationConflictError(
            "persisted acquisition request window conflicts with registration"
        )
    raw_members = _require_list(
        acquisition["members"],
        label="persisted acquisition members",
    )
    if len(raw_members) != len(plan.members):
        raise QuantV6PublicationConflictError(
            "persisted acquisition member count conflicts with registration"
        )
    grids = _scheduled_session_grids(plan)
    outcomes: list[dict[str, object]] = []
    for expected_member, raw_outcome in zip(
        plan.members,
        raw_members,
        strict=True,
    ):
        _evaluation_checkpoint(evaluation_deadline)
        outcome = _require_exact_dict(
            raw_outcome,
            label=f"persisted acquisition member {expected_member.ordinal}",
            keys=_MEMBER_ACQUISITION_OUTCOME_KEYS,
        )
        member_ordinal = _require_nonnegative_integer(
            outcome["member_ordinal"],
            label="persisted acquisition member ordinal",
        )
        symbol = _require_text(
            outcome["symbol"],
            label="persisted acquisition member symbol",
        )
        market = _require_text(
            outcome["market"],
            label="persisted acquisition member market",
        )
        pages = _require_nonnegative_integer(
            outcome["pages"],
            label="persisted acquisition pages",
        )
        raw_rows = _require_nonnegative_integer(
            outcome["raw_rows"],
            label="persisted acquisition raw rows",
        )
        accepted_bars = _require_nonnegative_integer(
            outcome["accepted_bars"],
            label="persisted acquisition accepted bars",
        )
        rejected_rows = _require_nonnegative_integer(
            outcome["rejected_rows"],
            label="persisted acquisition rejected rows",
        )
        if (
            member_ordinal != expected_member.ordinal
            or symbol != expected_member.symbol
            or market != expected_member.market
            or pages < 1
            or accepted_bars + rejected_rows > raw_rows
        ):
            raise QuantV6PublicationConflictError(
                "persisted acquisition member outcome conflicts"
            )
        projection = _projection_from_outcome(
            outcome,
            plan=plan,
            grids=grids,
            label=f"persisted {symbol} acquisition projection",
        )
        outcomes.append(projection.outcome)
    return tuple(outcomes)


def _assessment_summary_from_payload(
    payload: dict[str, Any],
    *,
    symbol: str,
) -> tuple[str, int, int, int, tuple[str, ...]]:
    assessment = _require_exact_dict(
        payload,
        label=f"persisted {symbol} assessment",
        keys=_ASSESSMENT_KEYS,
    )
    aggregates = _require_exact_dict(
        assessment["aggregates"],
        label=f"persisted {symbol} assessment aggregates",
        keys=_ASSESSMENT_AGGREGATE_KEYS,
    )
    policy = _require_exact_dict(
        assessment["policy"],
        label=f"persisted {symbol} assessment policy",
        keys=_ASSESSMENT_POLICY_KEYS,
    )
    action = _require_text(
        policy["recommended_action"],
        label=f"persisted {symbol} recommended action",
    )
    covered_sessions = _require_nonnegative_integer(
        aggregates["covered_sessions"],
        label=f"persisted {symbol} covered sessions",
    )
    event_count = _require_nonnegative_integer(
        aggregates["event_count"],
        label=f"persisted {symbol} event count",
    )
    event_sessions = _require_nonnegative_integer(
        aggregates["event_sessions"],
        label=f"persisted {symbol} event sessions",
    )
    blockers = _parse_canonical_text_list(
        assessment["blockers"],
        label=f"persisted {symbol} assessment blockers",
    )
    return action, covered_sessions, event_count, event_sessions, blockers


def _rebuild_persisted_evaluations(
    session: Session,
    *,
    publication: WatchlistQuantV6Publication,
    plan: QuantV6RegistrationPlan,
    publication_payload: dict[str, Any],
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> tuple[
    tuple[QuantV6CandidateEvaluation, ...],
    tuple[dict[str, object], ...],
]:
    outcomes = _parse_persisted_acquisition_outcomes(
        publication_payload,
        plan=plan,
        evaluation_deadline=evaluation_deadline,
    )
    _evaluation_checkpoint(evaluation_deadline)
    row_result = session.scalars(
        select(WatchlistQuantV6PublicationArtifact)
        .where(
            WatchlistQuantV6PublicationArtifact.publication_id
            == publication.id
        )
        .order_by(
            WatchlistQuantV6PublicationArtifact.member_ordinal,
            WatchlistQuantV6PublicationArtifact.role,
            WatchlistQuantV6PublicationArtifact.artifact_ordinal,
        )
    )
    loaded_rows: list[WatchlistQuantV6PublicationArtifact] = []
    for row in row_result:
        _evaluation_checkpoint(evaluation_deadline)
        loaded_rows.append(row)
    rows = tuple(loaded_rows)
    if len(rows) != publication.binding_count:
        raise QuantV6PublicationConflictError(
            "persisted publication binding rows conflict with its header"
        )
    artifact_rows = _load_artifact_rows(
        session,
        tuple(row.artifact_sha256 for row in rows),
        evaluation_deadline=evaluation_deadline,
    )
    grouped: dict[int, list[QuantV6PendingArtifactBinding]] = {
        member.ordinal: [] for member in plan.members
    }
    for row in rows:
        _evaluation_checkpoint(evaluation_deadline)
        if type(row.member_ordinal) is not int or row.member_ordinal not in grouped:
            raise QuantV6PublicationConflictError(
                "persisted binding member ordinal is outside registration"
            )
        artifact_row = artifact_rows.get(row.artifact_sha256)
        if artifact_row is None:
            raise QuantV6PublicationConflictError(
                "persisted binding references a missing artifact"
            )
        _persisted_utc(row.created_at, label="binding created_at")
        grouped[row.member_ordinal].append(_binding_from_row(
            row,
            _artifact_from_row(artifact_row),
        ))

    evaluations: list[QuantV6CandidateEvaluation] = []
    for member, outcome in zip(plan.members, outcomes, strict=True):
        _evaluation_checkpoint(evaluation_deadline)
        bindings = tuple(sorted(
            grouped[member.ordinal],
            key=lambda value: (
                _ROLE_RANK.get(value.role, len(_ROLE_RANK)),
                value.artifact_ordinal,
            ),
        ))
        assessments = tuple(
            binding for binding in bindings if binding.role == ASSESSMENT_ROLE
        )
        if len(assessments) != 1:
            raise QuantV6PublicationConflictError(
                "persisted member does not contain exactly one assessment"
            )
        assessment_binding = assessments[0]
        try:
            assessment_payload = _decode_and_verify_artifact(
                assessment_binding.artifact,
                label=f"persisted {member.symbol} assessment",
            )
            (
                action,
                covered_sessions,
                event_count,
                event_sessions,
                blockers,
            ) = _assessment_summary_from_payload(
                assessment_payload,
                symbol=member.symbol,
            )
        except QuantV6PublicationError as exc:
            raise QuantV6PublicationConflictError(
                "persisted assessment failed bounded summary replay"
            ) from exc
        evaluations.append(QuantV6CandidateEvaluation(
            member=member,
            recommended_action=action,
            covered_sessions=covered_sessions,
            event_count=event_count,
            event_sessions=event_sessions,
            blockers=blockers,
            assessment_artifact_sha256=(
                assessment_binding.artifact.digest_sha256
            ),
            bindings=bindings,
            fetched_pages=_require_nonnegative_integer(
                outcome["pages"],
                label="persisted acquisition pages",
            ),
            fetched_raw_rows=_require_nonnegative_integer(
                outcome["raw_rows"],
                label="persisted acquisition raw rows",
            ),
            fetched_accepted_bars=_require_nonnegative_integer(
                outcome["accepted_bars"],
                label="persisted acquisition accepted bars",
            ),
            fetched_bar_starts=(),
            rejected_rows=_require_nonnegative_integer(
                outcome["rejected_rows"],
                label="persisted acquisition rejected rows",
            ),
        ))
    _evaluation_checkpoint(evaluation_deadline)
    return tuple(evaluations), outcomes


class WatchlistQuantV6PublicationService:
    """Persist frozen quant-v6 cohorts without any execution capability."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), label="server clock")

    def register_plan(
        self,
        plan: QuantV6RegistrationPlan,
        *,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6RegistrationReceipt:
        """Commit one immutable registration before historical acquisition."""
        _evaluation_checkpoint(evaluation_deadline)
        validate_quant_v6_registration_plan(plan)
        fields = _registration_fields(plan)
        registered_at = max(
            self._now(),
            _aware_utc(plan.cohort_observed_at, label="cohort_observed_at"),
        )
        _evaluation_checkpoint(evaluation_deadline)
        session = self._session_factory()
        try:
            _evaluation_checkpoint(evaluation_deadline)
            existing = session.scalar(select(WatchlistQuantV6Registration).where(
                WatchlistQuantV6Registration.identity_sha256
                == plan.identity_sha256
            ))
            if existing is not None:
                _verify_registration_row(existing, plan)
                _evaluation_checkpoint(evaluation_deadline)
                return QuantV6RegistrationReceipt(
                    registration_id=existing.id,
                    identity_sha256=existing.identity_sha256,
                    created=False,
                )
            row = WatchlistQuantV6Registration(
                **fields,
                registered_at=registered_at,
            )
            session.add(row)
            _evaluation_checkpoint(evaluation_deadline)
            session.commit()
            session.refresh(row)
            _verify_registration_row(row, plan)
            return QuantV6RegistrationReceipt(
                registration_id=row.id,
                identity_sha256=row.identity_sha256,
                created=True,
            )
        except IntegrityError as exc:
            session.rollback()
            _evaluation_checkpoint(evaluation_deadline)
            existing = session.scalar(select(WatchlistQuantV6Registration).where(
                WatchlistQuantV6Registration.identity_sha256
                == plan.identity_sha256
            ))
            if existing is None:
                raise QuantV6PublicationConflictError(
                    "registration insert conflicted without an identical row"
                ) from exc
            _verify_registration_row(existing, plan)
            _evaluation_checkpoint(evaluation_deadline)
            return QuantV6RegistrationReceipt(
                registration_id=existing.id,
                identity_sha256=existing.identity_sha256,
                created=False,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _verify_complete_publication(
        self,
        session: Session,
        *,
        registration: WatchlistQuantV6Registration,
        publication: WatchlistQuantV6Publication,
        plan: QuantV6RegistrationPlan,
        prepared: _PreparedPublication,
        verify_artifact_payloads: bool = True,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt:
        _evaluation_checkpoint(evaluation_deadline)
        _verify_registration_row(registration, plan)
        _verify_publication_row(
            publication,
            registration=registration,
            plan=plan,
            prepared=prepared,
        )
        _evaluation_checkpoint(evaluation_deadline)
        row_result = session.scalars(
            select(WatchlistQuantV6PublicationArtifact)
            .where(
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication.id
            )
            .order_by(
                WatchlistQuantV6PublicationArtifact.member_ordinal,
                WatchlistQuantV6PublicationArtifact.role,
                WatchlistQuantV6PublicationArtifact.artifact_ordinal,
            )
        )
        loaded_rows: list[WatchlistQuantV6PublicationArtifact] = []
        for row in row_result:
            _evaluation_checkpoint(evaluation_deadline)
            loaded_rows.append(row)
        rows = tuple(loaded_rows)
        if len(rows) != prepared.binding_count:
            raise QuantV6PublicationConflictError(
                "persisted publication binding count is incomplete"
            )
        expected_by_key = {
            (
                binding.member_ordinal,
                binding.role,
                binding.artifact_ordinal,
            ): binding
            for binding in prepared.bindings
        }
        persisted_payloads: list[
            tuple[int, int, int, dict[str, object]]
        ] = []
        persisted_counts = {
            ASSESSMENT_ROLE: 0,
            SESSION_INPUT_ROLE: 0,
            EVENT_ROLE: 0,
        }
        artifact_rows = (
            _load_artifact_rows(
                session,
                tuple(row.artifact_sha256 for row in rows),
                evaluation_deadline=evaluation_deadline,
            )
            if verify_artifact_payloads
            else {}
        )
        for row in rows:
            _evaluation_checkpoint(evaluation_deadline)
            key = (row.member_ordinal, row.role, row.artifact_ordinal)
            expected = expected_by_key.pop(key, None)
            if expected is None:
                raise QuantV6PublicationConflictError(
                    "persisted publication contains an unexpected binding"
                )
            expected_fields: Mapping[str, object] = {
                "artifact_kind": expected.artifact.kind,
                "artifact_ordinal": expected.artifact_ordinal,
                "artifact_sha256": expected.artifact.digest_sha256,
                "binding_sha256": expected.binding_sha256,
                "market": expected.market,
                "member_ordinal": expected.member_ordinal,
                "role": expected.role,
                "session_date": expected.session_date,
                "symbol": expected.symbol,
            }
            for field, expected_value in expected_fields.items():
                persisted_value = getattr(row, field)
                if (
                    persisted_value != expected_value
                    or type(persisted_value) is not type(expected_value)
                ):
                    raise QuantV6PublicationConflictError(
                        f"persisted binding {field} conflicts with evidence"
                    )
            if verify_artifact_payloads:
                artifact_row = artifact_rows.get(row.artifact_sha256)
                if artifact_row is None:
                    raise QuantV6PublicationConflictError(
                        "persisted binding references a missing artifact"
                    )
                _verify_artifact_row(artifact_row, expected.artifact)
                _compare_encoded_artifacts(
                    _artifact_from_row(artifact_row),
                    expected.artifact,
                    label="persisted binding",
                )
            _persisted_utc(row.created_at, label="binding created_at")
            if row.role not in persisted_counts:
                raise QuantV6PublicationConflictError(
                    "persisted binding has an unsupported role"
                )
            persisted_counts[row.role] += 1
            persisted_payloads.append((
                row.member_ordinal,
                _ROLE_RANK[row.role],
                row.artifact_ordinal,
                _persisted_binding_payload(row),
            ))
        if expected_by_key:
            raise QuantV6PublicationConflictError(
                "persisted publication is missing expected bindings"
            )
        recomputed_manifest = _manifest_sha256(
            registration_identity_sha256=plan.identity_sha256,
            binding_payloads=tuple(
                value[3] for value in sorted(persisted_payloads)
            ),
            evaluation_deadline=evaluation_deadline,
        )
        if (
            not _same_digest(recomputed_manifest, prepared.manifest_sha256)
            or persisted_counts[ASSESSMENT_ROLE] != prepared.assessment_count
            or persisted_counts[SESSION_INPUT_ROLE]
            != prepared.session_input_count
            or persisted_counts[EVENT_ROLE] != prepared.event_count
            or sum(persisted_counts.values()) != prepared.binding_count
        ):
            raise QuantV6PublicationConflictError(
                "persisted publication manifest or counts conflict"
            )
        recomputed_payload = _publication_payload(
            registration_identity_sha256=plan.identity_sha256,
            registered_member_count=len(plan.members),
            manifest_sha256=recomputed_manifest,
            assessment_count=persisted_counts[ASSESSMENT_ROLE],
            session_input_count=persisted_counts[SESSION_INPUT_ROLE],
            event_count=persisted_counts[EVENT_ROLE],
            acquisition_outcomes=prepared.acquisition_outcomes,
            request_start_at=prepared.request_start_at,
            request_end_at=prepared.request_end_at,
        )
        recomputed_bytes = canonical_quant_v6_json(recomputed_payload)
        if (
            recomputed_bytes.decode("utf-8") != publication.publication_json
            or not _same_digest(
                hashlib.sha256(recomputed_bytes).hexdigest(),
                publication.identity_sha256,
            )
        ):
            raise QuantV6PublicationConflictError(
                "persisted publication root conflicts with its bindings"
            )
        _evaluation_checkpoint(evaluation_deadline)
        return QuantV6PublicationReceipt(
            publication_id=publication.id,
            registration_id=registration.id,
            registration_identity_sha256=plan.identity_sha256,
            identity_sha256=publication.identity_sha256,
            manifest_sha256=publication.manifest_sha256,
            binding_count=publication.binding_count,
            created=False,
        )

    def _load_existing_publication(
        self,
        *,
        plan: QuantV6RegistrationPlan,
        prepared: _PreparedPublication,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt | None:
        _evaluation_checkpoint(evaluation_deadline)
        session = self._session_factory()
        try:
            _evaluation_checkpoint(evaluation_deadline)
            registration = session.scalar(select(WatchlistQuantV6Registration).where(
                WatchlistQuantV6Registration.identity_sha256
                == plan.identity_sha256
            ))
            if registration is None:
                raise QuantV6PublicationConflictError(
                    "registered cohort disappeared before publication"
                )
            publication = session.scalar(select(WatchlistQuantV6Publication).where(
                WatchlistQuantV6Publication.registration_id == registration.id
            ))
            if publication is None:
                return None
            return self._verify_complete_publication(
                session,
                registration=registration,
                publication=publication,
                plan=plan,
                prepared=prepared,
                evaluation_deadline=evaluation_deadline,
            )
        finally:
            session.close()

    def _load_trusted_existing_publication(
        self,
        *,
        plan: QuantV6RegistrationPlan,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt | None:
        """Replay persisted evidence fully before skipping provider I/O."""
        _evaluation_checkpoint(evaluation_deadline)
        session = self._session_factory()
        try:
            _evaluation_checkpoint(evaluation_deadline)
            registration = session.scalar(select(WatchlistQuantV6Registration).where(
                WatchlistQuantV6Registration.identity_sha256
                == plan.identity_sha256
            ))
            if registration is None:
                raise QuantV6PublicationConflictError(
                    "registered cohort disappeared before provider fast-path"
                )
            _verify_registration_row(registration, plan)
            publication = session.scalar(select(WatchlistQuantV6Publication).where(
                WatchlistQuantV6Publication.registration_id == registration.id
            ))
            if publication is None:
                return None
            publication_payload = _decode_canonical_publication_payload(
                publication
            )
            _evaluation_checkpoint(evaluation_deadline)
            try:
                (
                    persisted_evaluations,
                    persisted_acquisition_outcomes,
                ) = _rebuild_persisted_evaluations(
                    session,
                    publication=publication,
                    plan=plan,
                    publication_payload=publication_payload,
                    evaluation_deadline=evaluation_deadline,
                )
                prepared = _prepare_publication(
                    plan=plan,
                    evaluations=persisted_evaluations,
                    persisted_acquisition_outcomes=(
                        persisted_acquisition_outcomes
                    ),
                    evaluation_deadline=evaluation_deadline,
                )
            except QuantV6PublicationConflictError:
                raise
            except QuantV6PublicationError as exc:
                raise QuantV6PublicationConflictError(
                    "persisted publication failed strict semantic replay"
                ) from exc
            return self._verify_complete_publication(
                session,
                registration=registration,
                publication=publication,
                plan=plan,
                prepared=prepared,
                evaluation_deadline=evaluation_deadline,
            )
        finally:
            session.close()

    def publish_registration(
        self,
        *,
        plan: QuantV6RegistrationPlan,
        evaluations: Sequence[QuantV6CandidateEvaluation],
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt:
        """Atomically publish every member and every evidence binding."""
        _evaluation_checkpoint(evaluation_deadline)
        validate_quant_v6_registration_plan(plan)
        prepared = _prepare_publication(
            plan=plan,
            evaluations=evaluations,
            evaluation_deadline=evaluation_deadline,
        )
        for attempt in range(2):
            _evaluation_checkpoint(evaluation_deadline)
            session = self._session_factory()
            try:
                _evaluation_checkpoint(evaluation_deadline)
                registration = session.scalar(
                    select(WatchlistQuantV6Registration).where(
                        WatchlistQuantV6Registration.identity_sha256
                        == plan.identity_sha256
                    )
                )
                if registration is None:
                    raise QuantV6PublicationError(
                        "cohort must be committed before publication"
                    )
                _verify_registration_row(registration, plan)
                existing = session.scalar(select(WatchlistQuantV6Publication).where(
                    WatchlistQuantV6Publication.registration_id
                    == registration.id
                ))
                if existing is not None:
                    return self._verify_complete_publication(
                        session,
                        registration=registration,
                        publication=existing,
                        plan=plan,
                        prepared=prepared,
                        evaluation_deadline=evaluation_deadline,
                    )

                published_at = max(
                    self._now(),
                    _persisted_utc(
                        registration.registered_at,
                        label="registered_at",
                    ),
                )
                existing_artifacts = _load_artifact_rows(
                    session,
                    tuple(
                        artifact.digest_sha256
                        for artifact in prepared.artifacts
                    ),
                    evaluation_deadline=evaluation_deadline,
                )
                missing_artifacts: list[EncodedQuantV6Artifact] = []
                for artifact in prepared.artifacts:
                    _evaluation_checkpoint(evaluation_deadline)
                    artifact_row = existing_artifacts.get(
                        artifact.digest_sha256
                    )
                    if artifact_row is not None:
                        _verify_artifact_row(artifact_row, artifact)
                        continue
                    missing_artifacts.append(artifact)
                artifact_rows_to_add: list[WatchlistQuantV6Artifact] = []
                for artifact in missing_artifacts:
                    _evaluation_checkpoint(evaluation_deadline)
                    artifact_rows_to_add.append(WatchlistQuantV6Artifact(
                        digest_sha256=artifact.digest_sha256,
                        schema_version=artifact.schema_version,
                        kind=artifact.kind,
                        codec=artifact.codec,
                        compression_level=(
                            QUANT_V6_ARTIFACT_COMPRESSION_LEVEL
                        ),
                        raw_size=artifact.raw_size,
                        compressed_size=artifact.compressed_size,
                        payload=artifact.payload,
                        created_at=published_at,
                    ))
                session.add_all(artifact_rows_to_add)
                _evaluation_checkpoint(evaluation_deadline)
                session.flush()

                _evaluation_checkpoint(evaluation_deadline)
                publication = WatchlistQuantV6Publication(
                    **_publication_row_fields(
                        registration_id=registration.id,
                        registration_identity_sha256=plan.identity_sha256,
                        member_count=len(plan.members),
                        prepared=prepared,
                    ),
                    published_at=published_at,
                )
                session.add(publication)
                session.flush()
                binding_rows_to_add: list[
                    WatchlistQuantV6PublicationArtifact
                ] = []
                for binding in prepared.bindings:
                    _evaluation_checkpoint(evaluation_deadline)
                    binding_rows_to_add.append(
                        WatchlistQuantV6PublicationArtifact(
                            publication_id=publication.id,
                            member_ordinal=binding.member_ordinal,
                            symbol=binding.symbol,
                            market=binding.market,
                            role=binding.role,
                            artifact_ordinal=binding.artifact_ordinal,
                            session_date=binding.session_date,
                            artifact_sha256=(
                                binding.artifact.digest_sha256
                            ),
                            artifact_kind=binding.artifact.kind,
                            binding_sha256=binding.binding_sha256,
                            created_at=published_at,
                        )
                    )
                session.add_all(binding_rows_to_add)
                _evaluation_checkpoint(evaluation_deadline)
                session.flush()
                _evaluation_checkpoint(evaluation_deadline)
                receipt = self._verify_complete_publication(
                    session,
                    registration=registration,
                    publication=publication,
                    plan=plan,
                    prepared=prepared,
                    verify_artifact_payloads=False,
                    evaluation_deadline=evaluation_deadline,
                )
                _evaluation_checkpoint(evaluation_deadline)
                session.commit()
                return QuantV6PublicationReceipt(
                    publication_id=receipt.publication_id,
                    registration_id=receipt.registration_id,
                    registration_identity_sha256=(
                        receipt.registration_identity_sha256
                    ),
                    identity_sha256=receipt.identity_sha256,
                    manifest_sha256=receipt.manifest_sha256,
                    binding_count=receipt.binding_count,
                    created=True,
                )
            except IntegrityError as exc:
                session.rollback()
                session.close()
                _evaluation_checkpoint(evaluation_deadline)
                existing_receipt = self._load_existing_publication(
                    plan=plan,
                    prepared=prepared,
                    evaluation_deadline=evaluation_deadline,
                )
                if existing_receipt is not None:
                    return existing_receipt
                if attempt == 0:
                    continue
                raise QuantV6PublicationConflictError(
                    "publication insert conflicted without identical evidence"
                ) from exc
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        raise QuantV6PublicationConflictError(
            "publication could not resolve a concurrent insert"
        )

    def register_evaluate_publish(
        self,
        *,
        plan: QuantV6RegistrationPlan,
        evaluation_callback: Callable[
            [QuantV6RegistrationPlan, Callable[[], None]],
            Sequence[QuantV6CandidateEvaluation],
        ],
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt:
        """Register, cooperatively evaluate, then atomically publish."""
        self.register_plan(
            plan,
            evaluation_deadline=evaluation_deadline,
        )
        _evaluation_checkpoint(evaluation_deadline)
        callback_checkpoint = (
            evaluation_deadline.checkpoint
            if evaluation_deadline is not None
            else _noop_evaluation_checkpoint
        )
        evaluations = tuple(evaluation_callback(plan, callback_checkpoint))
        _evaluation_checkpoint(evaluation_deadline)
        return self.publish_registration(
            plan=plan,
            evaluations=evaluations,
            evaluation_deadline=evaluation_deadline,
        )

    def register_provider_evaluate_publish(
        self,
        *,
        plan: QuantV6RegistrationPlan,
        provider: QuantV6HistoricalProvider,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> QuantV6PublicationReceipt:
        """Run the frozen server evaluator only after registration commits."""
        self.register_plan(
            plan,
            evaluation_deadline=evaluation_deadline,
        )
        _evaluation_checkpoint(evaluation_deadline)
        existing = self._load_trusted_existing_publication(
            plan=plan,
            evaluation_deadline=evaluation_deadline,
        )
        if existing is not None:
            return existing
        _evaluation_checkpoint(evaluation_deadline)
        evaluations = evaluate_quant_v6_registration(
            registration=plan,
            provider=provider,
            evaluation_deadline=evaluation_deadline,
        )
        _evaluation_checkpoint(evaluation_deadline)
        return self.publish_registration(
            plan=plan,
            evaluations=evaluations,
            evaluation_deadline=evaluation_deadline,
        )


__all__ = [
    "QUANT_V6_BINDING_MANIFEST_CONTRACT",
    "QUANT_V6_PUBLICATION_CONTRACT",
    "QUANT_V6_PUBLICATION_SCHEMA_VERSION",
    "QuantV6PublicationConflictError",
    "QuantV6PublicationError",
    "QuantV6PublicationReceipt",
    "QuantV6RegistrationReceipt",
    "WatchlistQuantV6PublicationService",
]
