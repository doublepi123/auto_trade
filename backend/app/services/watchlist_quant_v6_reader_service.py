from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, cast

from sqlalchemy import (
    LargeBinary,
    and_,
    case,
    cast as sql_cast,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Session, defer

from app.domain.watchlist_quant_v6 import (
    BAR_NEXT_OPEN_STRESSED,
    MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES,
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    QUANT_V6_ACQUISITION_SPEC,
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_ARTIFACT_CODEC,
    QUANT_V6_ARTIFACT_COMPRESSION_LEVEL,
    QUANT_V6_ARTIFACT_SCHEMA_VERSION,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_BAR_MINUTES,
    QUANT_V6_EVENT_ARTIFACT_KIND,
    QUANT_V6_EVENT_CONTRACT,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QUANT_V6_SEMANTIC_DIGEST,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    QUANT_V6_SESSION_INPUT_CONTRACT,
    QuantV6ArtifactError,
    QuantV6SemanticError,
    canonical_quant_v6_json,
    canonical_utc_timestamp,
    decode_quant_v6_artifact,
    quant_v6_consecutive_trading_session_dates,
    quant_v6_expected_rth_bar_starts,
    quant_v6_payload_sha256,
    quant_v6_previous_trading_session_dates,
)
from app.models import (
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
    WatchlistQuantV6Registration,
)
from app.schemas import (
    WatchlistQuantV6ArtifactResponse,
    WatchlistQuantV6BindingPage,
    WatchlistQuantV6BindingResponse,
    WatchlistQuantV6MemberAcquisitionResponse,
    WatchlistQuantV6MemberPage,
    WatchlistQuantV6MemberSummary,
    WatchlistQuantV6PolicyResponse,
    WatchlistQuantV6PublicationDetail,
    WatchlistQuantV6PublicationPage,
    WatchlistQuantV6PublicationSummary,
    WatchlistQuantV6RegistrationResponse,
    WatchlistQuantV6ValidationResponse,
)


_REGISTRATION_CONTRACT = "watchlist-quant-v6-registration-v1"
_PUBLICATION_CONTRACT = "watchlist-quant-v6-publication-v1"
_BINDING_CONTRACT = "watchlist-quant-v6-artifact-binding-v1"
_MANIFEST_CONTRACT = "watchlist-quant-v6-binding-manifest-v1"
# Quant-v6 v1 is an immutable evidence contract. New strategy semantics must
# use a new version rather than mutating these frozen reader expectations.
_SELECTION_RULE_VERSION = (
    "rotation-research-catalog-active-at-first-target-v1"
)
_COHORT_SOURCE = "ROTATION_RESEARCH_CATALOG_PIT"
_DATA_SETTLEMENT_DELAY = timedelta(minutes=15)
_SCHEDULED_GRID_DIGEST_CONTRACT = (
    "watchlist-quant-v6-scheduled-grid-starts-digest-v1"
)
_MEMBERSHIP_RESOURCE_KEY = (
    "app/domain/universe_selection/data/index_membership_history.json"
)
_MAX_MEMBERS = 1_000
_MAX_BINDINGS = 50_000
_MAX_PUBLICATIONS = 100_000
_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CURSOR_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 512
_CURSOR_CHECKSUM_DOMAIN = b"watchlist-quant-v6-reader-cursor-v1\0"

_ASSESSMENT_ROLE = "ASSESSMENT"
_SESSION_INPUT_ROLE = "SESSION_INPUT"
_EVENT_ROLE = "EVENT"
_ROLE_RANK = {
    _ASSESSMENT_ROLE: 0,
    _SESSION_INPUT_ROLE: 1,
    _EVENT_ROLE: 2,
}
_KIND_BY_ROLE = {
    _ASSESSMENT_ROLE: QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    _SESSION_INPUT_ROLE: QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    _EVENT_ROLE: QUANT_V6_EVENT_ARTIFACT_KIND,
}
_REGISTRATION_ROOT_KEYS = frozenset({
    "acquisition",
    "acquisition_spec_sha256",
    "algorithm_version",
    "cohort",
    "cohort_observed_at",
    "contract",
    "data_cutoff_at",
    "evaluator_digest_sha256",
    "evaluator_manifest",
    "market",
    "policy",
    "schedule",
    "schedule_sha256",
    "schema_version",
    "semantic_digest_sha256",
    "source_snapshot",
    "source_snapshot_sha256",
})
_REGISTRATION_COHORT_KEYS = frozenset({
    "cohort_manifest_sha256",
    "cohort_source",
    "member_count",
    "members",
    "selection_rule_version",
})
_REGISTRATION_MEMBER_KEYS = frozenset({
    "alias",
    "market",
    "memberships",
    "ordinal",
    "sector",
    "symbol",
})
_REGISTRATION_SCHEDULE_KEYS = frozenset({
    "market",
    "schema_version",
    "target_session_dates",
    "training_session_dates",
})
_SOURCE_SNAPSHOT_KEYS = frozenset({
    "catalog_members",
    "catalog_source_version",
    "membership_history",
    "membership_metadata",
    "membership_resource_sha256",
    "schema_version",
})
_PROVIDER_CONTRACT_KEYS = frozenset({
    "acquisition_spec_sha256",
    "adjustment_mode",
    "bounded_context_close",
    "bounded_context_creation",
    "fallback_allowed",
    "forward_paging",
    "max_bars",
    "max_inflight_sdk_calls",
    "max_pages",
    "max_range_days",
    "max_raw_rows",
    "naive_sdk_timestamp_policy",
    "page_boundary",
    "page_rows_must_not_exceed_page_size",
    "page_size",
    "page_timeout_milliseconds",
    "period",
    "provider_contract_version",
    "quote_context_only",
    "retry_base_milliseconds",
    "retry_max",
    "runtime_local_timezone_required",
    "schema_version",
})
_PROVIDER_BOOLEAN_VALUES = {
    "bounded_context_close": True,
    "bounded_context_creation": True,
    "fallback_allowed": False,
    "forward_paging": True,
    "page_rows_must_not_exceed_page_size": True,
    "quote_context_only": True,
}
_PROVIDER_MAX_BARS = 10_000
_PROVIDER_MAX_PAGES = 16
_PROVIDER_MAX_RAW_ROWS = 16_000
_PROVIDER_PAGE_SIZE = 1_000
_PROVIDER_FIXED_INTEGER_VALUES = {
    "max_bars": _PROVIDER_MAX_BARS,
    "max_inflight_sdk_calls": 1,
    "max_pages": _PROVIDER_MAX_PAGES,
    "max_range_days": 90,
    "max_raw_rows": _PROVIDER_MAX_RAW_ROWS,
    "page_size": _PROVIDER_PAGE_SIZE,
    "schema_version": 1,
}
_PROVIDER_FIXED_TEXT_VALUES = {
    "adjustment_mode": "NO_ADJUST",
    "naive_sdk_timestamp_policy": "UTC_HOST_LOCAL_ONLY",
    "period": "MIN_5",
    "runtime_local_timezone_required": "UTC",
}
_PROVIDER_PAGE_BOUNDARY_BY_VERSION = {
    "watchlist-quant-v6-longport-quote-only-history-v1": (
        "EXCLUSIVE_AFTER_LAST_ACCEPTED_TIMESTAMP"
    ),
    "watchlist-quant-v6-longport-quote-only-history-v2": (
        "EXCLUSIVE_AFTER_CURSOR_WITH_EXACT_VALID_SINGLETON_TERMINAL_REPEAT"
    ),
}
_REGISTRATION_POLICY_KEYS = frozenset({
    "automatic_promotion_allowed",
    "order_submission_allowed",
    "position_add_on_allowed",
    "short_entry_allowed",
})
_PUBLICATION_ROOT_KEYS = frozenset({
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
_PUBLICATION_POLICY_KEYS = frozenset({
    *_REGISTRATION_POLICY_KEYS,
    "promotion_eligible",
})
_ACQUISITION_OUTCOME_KEYS = frozenset({
    "members",
    "request_end_at",
    "request_start_at",
})
_MEMBER_ACQUISITION_KEYS = frozenset({
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
_HISTORICAL_EVALUATOR_SOURCE_KEYS_V1 = frozenset({
    "app.domain.universe_selection.catalog",
    "app.domain.universe_selection.membership_history",
    "app.services.watchlist_quant_v6_evaluation_service",
    "app.services.watchlist_quant_v6_historical_provider",
})
_HISTORICAL_EVALUATOR_SOURCE_KEYS_BY_VERSION = {
    1: _HISTORICAL_EVALUATOR_SOURCE_KEYS_V1,
    2: frozenset({
        *_HISTORICAL_EVALUATOR_SOURCE_KEYS_V1,
        "app.services.watchlist_quant_v6_deadline",
    }),
    3: frozenset({
        *_HISTORICAL_EVALUATOR_SOURCE_KEYS_V1,
        "app.services.watchlist_quant_v6_deadline",
        "app.services.watchlist_quant_v6_publication_service",
        "app.services.watchlist_quant_v6_spawn_supervisor",
    }),
}
_DOMAIN_EVALUATOR_SOURCE_KEYS = frozenset({
    "app.core.holiday_calendar",
    "app.core.market_calendar",
    "app.domain.watchlist_quant_v6.artifact",
    "app.domain.watchlist_quant_v6.assessment",
    "app.domain.watchlist_quant_v6.evaluator",
    "app.domain.watchlist_quant_v6.semantics",
})
_ASSESSMENT_PAYLOAD_KEYS = frozenset({
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
_SESSION_INPUT_PAYLOAD_KEYS = frozenset({
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
_EVENT_PAYLOAD_KEYS = frozenset({
    "algorithm_version",
    "capture",
    "contract",
    "costs",
    "execution",
    "identity",
    "p0",
    "schema_version",
    "semantic_digest",
    "signal",
})
_PAYLOAD_KEYS_BY_KIND = {
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND: _ASSESSMENT_PAYLOAD_KEYS,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND: _SESSION_INPUT_PAYLOAD_KEYS,
    QUANT_V6_EVENT_ARTIFACT_KIND: _EVENT_PAYLOAD_KEYS,
}
_PAYLOAD_CONTRACT_BY_KIND = {
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND: QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND: QUANT_V6_SESSION_INPUT_CONTRACT,
    QUANT_V6_EVENT_ARTIFACT_KIND: QUANT_V6_EVENT_CONTRACT,
}

Market = Literal["US", "HK"]
BindingRole = Literal["ASSESSMENT", "SESSION_INPUT", "EVENT"]
ArtifactKind = Literal[
    "WATCHLIST_QUANT_V6_ASSESSMENT",
    "WATCHLIST_QUANT_V6_SESSION_INPUT",
    "WATCHLIST_QUANT_V6_EVENT",
]


class QuantV6ReadError(RuntimeError):
    """Base class for projection-only quant-v6 read failures."""


class QuantV6ReadNotFoundError(QuantV6ReadError):
    """Raised when a requested immutable publication object is absent."""


class QuantV6ReadCursorError(QuantV6ReadError):
    """Raised when an opaque pagination cursor is invalid for this query."""


class QuantV6ReadIntegrityError(QuantV6ReadError):
    """Raised when persisted immutable evidence fails bounded validation."""


def _integrity(message: str) -> QuantV6ReadIntegrityError:
    return QuantV6ReadIntegrityError(message)


def _cursor_error() -> QuantV6ReadCursorError:
    return QuantV6ReadCursorError("invalid quant-v6 pagination cursor")


def _encode_cursor(kind: str, state: Mapping[str, object]) -> str:
    core: dict[str, object] = {
        "k": kind,
        "s": dict(state),
        "v": _CURSOR_VERSION,
    }
    core_bytes = canonical_quant_v6_json(core)
    checksum = hashlib.sha256(
        _CURSOR_CHECKSUM_DOMAIN + core_bytes
    ).hexdigest()
    envelope = dict(core)
    envelope["h"] = checksum
    encoded = base64.urlsafe_b64encode(
        canonical_quant_v6_json(envelope)
    ).decode("ascii").rstrip("=")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        raise _integrity("pagination cursor exceeds its encoding limit")
    return encoded


def _decode_cursor(cursor: str, *, kind: str) -> dict[str, object]:
    if (
        type(cursor) is not str
        or not cursor
        or len(cursor) > _MAX_CURSOR_LENGTH
        or _CURSOR_TOKEN_PATTERN.fullmatch(cursor) is None
    ):
        raise _cursor_error()
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        canonical_token = base64.urlsafe_b64encode(raw).decode(
            "ascii"
        ).rstrip("=")
        if not hmac.compare_digest(cursor, canonical_token):
            raise _cursor_error()
        decoded = json.loads(raw.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise _cursor_error() from exc
    if type(decoded) is not dict:
        raise _cursor_error()
    envelope = cast(dict[str, object], decoded)
    if set(envelope) != {"h", "k", "s", "v"}:
        raise _cursor_error()
    checksum = envelope.get("h")
    state = envelope.get("s")
    version = envelope.get("v")
    if (
        envelope.get("k") != kind
        or type(version) is not int
        or version != _CURSOR_VERSION
        or type(checksum) is not str
        or _SHA256_PATTERN.fullmatch(checksum) is None
        or type(state) is not dict
    ):
        raise _cursor_error()
    core = {
        "k": kind,
        "s": state,
        "v": _CURSOR_VERSION,
    }
    try:
        if canonical_quant_v6_json(envelope) != raw:
            raise _cursor_error()
        expected_checksum = hashlib.sha256(
            _CURSOR_CHECKSUM_DOMAIN + canonical_quant_v6_json(core)
        ).hexdigest()
    except QuantV6ReadCursorError:
        raise
    except Exception as exc:
        raise _cursor_error() from exc
    if not hmac.compare_digest(checksum, expected_checksum):
        raise _cursor_error()
    return cast(dict[str, object], state)


def _cursor_integer(
    state: Mapping[str, object],
    key: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = state.get(key)
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise _cursor_error()
    return value


def _cursor_digest(state: Mapping[str, object], key: str) -> str:
    value = state.get(key)
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise _cursor_error()
    return value


def _persisted_utc(value: object, *, label: str) -> datetime:
    if type(value) is not datetime:
        raise _integrity(f"persisted {label} is not a timestamp")
    timestamp = cast(datetime, value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _require_mapping(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise _integrity(f"persisted {label} is not an object")
    return cast(dict[str, Any], value)


def _require_exact_mapping(
    value: object,
    *,
    label: str,
    keys: frozenset[str],
) -> dict[str, Any]:
    mapping = _require_mapping(value, label=label)
    if set(mapping) != keys:
        raise _integrity(f"persisted {label} has an invalid field set")
    return mapping


def _require_list(value: object, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise _integrity(f"persisted {label} is not an array")
    return cast(list[Any], value)


def _require_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise _integrity(f"persisted {label} is not non-empty text")
    return value


def _require_integer(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise _integrity(f"persisted {label} is not a nonnegative integer")
    return value


def _require_boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise _integrity(f"persisted {label} is not boolean")
    return value


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise _integrity(f"persisted {label} is not lowercase SHA-256")
    return value


def _require_canonical_date(value: object, *, label: str) -> date:
    if type(value) is not str:
        raise _integrity(f"persisted {label} is not a canonical date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _integrity(f"persisted {label} is not a canonical date") from exc
    if parsed.isoformat() != value:
        raise _integrity(f"persisted {label} is not a canonical date")
    return parsed


def _validate_digest_mapping(value: object, *, label: str) -> dict[str, Any]:
    mapping = _require_mapping(value, label=label)
    if not mapping:
        raise _integrity(f"persisted {label} is empty")
    for key, digest in mapping.items():
        if type(key) is not str or not key:
            raise _integrity(f"persisted {label} has an invalid key")
        _require_digest(digest, label=f"{label} digest")
    return mapping


def _validate_provider_contract(value: object) -> dict[str, Any]:
    contract = _require_exact_mapping(
        value,
        label="registration provider contract",
        keys=_PROVIDER_CONTRACT_KEYS,
    )
    acquisition_digest = _require_digest(
        contract.get("acquisition_spec_sha256"),
        label="registration provider acquisition spec",
    )
    if acquisition_digest != QUANT_V6_ACQUISITION_SPEC_DIGEST:
        raise _integrity(
            "persisted registration provider acquisition spec conflicts"
        )
    for key, expected in _PROVIDER_BOOLEAN_VALUES.items():
        if _require_boolean(
            contract.get(key),
            label=f"registration provider contract.{key}",
        ) is not expected:
            raise _integrity(
                "persisted registration provider boolean contract conflicts"
            )
    for key, expected in _PROVIDER_FIXED_INTEGER_VALUES.items():
        if _require_integer(
            contract.get(key),
            label=f"registration provider contract.{key}",
        ) != expected:
            raise _integrity(
                "persisted registration provider integer contract conflicts"
            )
    page_timeout = _require_integer(
        contract.get("page_timeout_milliseconds"),
        label="registration provider contract.page_timeout_milliseconds",
    )
    _require_integer(
        contract.get("retry_base_milliseconds"),
        label="registration provider contract.retry_base_milliseconds",
    )
    _require_integer(
        contract.get("retry_max"),
        label="registration provider contract.retry_max",
    )
    if not 5_000 <= page_timeout <= 120_000:
        raise _integrity(
            "persisted registration provider timeout contract conflicts"
        )
    for key, expected in _PROVIDER_FIXED_TEXT_VALUES.items():
        if _require_text(
            contract.get(key),
            label=f"registration provider contract.{key}",
        ) != expected:
            raise _integrity(
                "persisted registration provider text contract conflicts"
            )
    provider_version = _require_text(
        contract.get("provider_contract_version"),
        label="registration provider contract.provider_contract_version",
    )
    expected_page_boundary = _PROVIDER_PAGE_BOUNDARY_BY_VERSION.get(
        provider_version
    )
    if expected_page_boundary is None:
        raise _integrity(
            "persisted registration provider version is unsupported"
        )
    if _require_text(
        contract.get("page_boundary"),
        label="registration provider contract.page_boundary",
    ) != expected_page_boundary:
        raise _integrity(
            "persisted registration provider page boundary conflicts"
        )
    return contract


def _require_json_length(value: object, *, label: str) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > MAX_QUANT_V6_ARTIFACT_RAW_BYTES
    ):
        raise _integrity(f"persisted {label} is outside the read size limit")
    return value


def _decode_canonical_json(
    value: object,
    *,
    identity_sha256: object,
    label: str,
) -> dict[str, Any]:
    text = _require_text(value, label=label)
    expected_digest = _require_digest(
        identity_sha256,
        label=f"{label} identity",
    )
    raw = text.encode("utf-8")
    if len(raw) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
        raise _integrity(f"persisted {label} exceeds the read size limit")
    try:
        decoded = json.loads(raw.decode("utf-8"))
        payload = _require_mapping(decoded, label=label)
        canonical = canonical_quant_v6_json(payload)
    except QuantV6ReadIntegrityError:
        raise
    except Exception as exc:
        raise _integrity(f"persisted {label} is not canonical JSON") from exc
    if canonical != raw or not hmac.compare_digest(
        hashlib.sha256(canonical).hexdigest(),
        expected_digest,
    ):
        raise _integrity(f"persisted {label} identity failed replay")
    return payload


def _false_policy() -> WatchlistQuantV6PolicyResponse:
    return WatchlistQuantV6PolicyResponse(
        promotion_eligible=False,
        automatic_promotion_allowed=False,
        order_submission_allowed=False,
        short_entry_allowed=False,
        position_add_on_allowed=False,
    )


def _validate_policy(
    value: object,
    *,
    label: str,
    require_promotion_eligible: bool,
) -> dict[str, Any]:
    expected_keys = (
        _PUBLICATION_POLICY_KEYS
        if require_promotion_eligible
        else _REGISTRATION_POLICY_KEYS
    )
    policy = _require_exact_mapping(
        value,
        label=label,
        keys=expected_keys,
    )
    keys = sorted(expected_keys)
    for key in keys:
        if _require_boolean(policy.get(key), label=f"{label}.{key}"):
            raise _integrity(f"persisted {label}.{key} violates P0")
    return policy


def _registration_members(
    payload: Mapping[str, Any],
    *,
    registration: WatchlistQuantV6Registration,
) -> dict[int, dict[str, Any]]:
    cohort = _require_exact_mapping(
        payload.get("cohort"),
        label="registration cohort",
        keys=_REGISTRATION_COHORT_KEYS,
    )
    raw_members = _require_list(
        cohort.get("members"),
        label="registration cohort members",
    )
    member_count = _require_integer(
        cohort.get("member_count"),
        label="registration cohort member_count",
    )
    if (
        cohort.get("cohort_manifest_sha256")
        != registration.cohort_manifest_sha256
        or member_count != registration.cohort_member_count
        or cohort.get("cohort_source") != registration.cohort_source
        or cohort.get("selection_rule_version")
        != registration.selection_rule_version
        or len(raw_members) != registration.cohort_member_count
        or quant_v6_payload_sha256({
            "members": raw_members,
            "schema_version": 1,
        })
        != registration.cohort_manifest_sha256
    ):
        raise _integrity("persisted registration cohort conflicts with its header")
    members: dict[int, dict[str, Any]] = {}
    seen_symbols: set[str] = set()
    for ordinal, raw_member in enumerate(raw_members):
        member = _require_exact_mapping(
            raw_member,
            label=f"registration member {ordinal}",
            keys=_REGISTRATION_MEMBER_KEYS,
        )
        symbol = _require_text(
            member.get("symbol"),
            label=f"registration member {ordinal} symbol",
        )
        alias = member.get("alias")
        sector = member.get("sector")
        memberships = _require_list(
            member.get("memberships"),
            label=f"registration member {ordinal} memberships",
        )
        persisted_ordinal = _require_integer(
            member.get("ordinal"),
            label=f"registration member {ordinal} ordinal",
        )
        if (
            persisted_ordinal != ordinal
            or member.get("market") != registration.market
            or symbol != symbol.strip().upper()
            or not 4 <= len(symbol) <= 50
            or not symbol.endswith(f".{registration.market}")
            or symbol in seen_symbols
            or type(alias) is not str
            or len(alias) > 160
            or type(sector) is not str
            or len(sector) > 160
            or len(memberships) > 20
            or any(type(value) is not str or not value for value in memberships)
        ):
            raise _integrity("persisted registration member ordering is invalid")
        seen_symbols.add(symbol)
        members[ordinal] = member
    return members


def _validate_registration_header(row: WatchlistQuantV6Registration) -> None:
    if (
        type(row.id) is not int
        or not 1 <= row.id <= _SQLITE_MAX_INTEGER
        or row.schema_version != 1
        or row.contract_version != _REGISTRATION_CONTRACT
        or row.selection_rule_version != _SELECTION_RULE_VERSION
        or row.algorithm_version != QUANT_V6_ALGORITHM_VERSION
        or row.semantic_digest_sha256 != QUANT_V6_SEMANTIC_DIGEST
        or row.cohort_source != _COHORT_SOURCE
        or row.market not in {"US", "HK"}
        or type(row.cohort_member_count) is not int
        or not 1 <= row.cohort_member_count <= _MAX_MEMBERS
        or row.training_session_count != 10
        or row.target_session_count != 30
        or row.bar_period != "MIN_5"
        or row.adjustment_mode != "NO_ADJUST"
        or row.server_generated is not True
        or row.short_entry_allowed is not False
        or row.position_add_on_allowed is not False
        or row.order_submission_allowed is not False
        or row.automatic_promotion_allowed is not False
        or type(row.first_training_session_date) is not date
        or type(row.first_target_session_date) is not date
        or type(row.last_target_session_date) is not date
        or not (
            row.first_training_session_date
            < row.first_target_session_date
            <= row.last_target_session_date
        )
    ):
        raise _integrity("persisted registration header violates its contract")
    for digest, label in (
        (row.identity_sha256, "registration identity"),
        (row.evaluator_digest_sha256, "registration evaluator"),
        (row.acquisition_spec_sha256, "registration acquisition"),
        (row.source_snapshot_sha256, "registration source snapshot"),
        (row.cohort_manifest_sha256, "registration cohort"),
        (row.schedule_sha256, "registration schedule"),
    ):
        _require_digest(digest, label=label)
    data_cutoff_at = _persisted_utc(
        row.data_cutoff_at,
        label="data_cutoff_at",
    )
    cohort_observed_at = _persisted_utc(
        row.cohort_observed_at,
        label="cohort_observed_at",
    )
    registered_at = _persisted_utc(
        row.registered_at,
        label="registered_at",
    )
    if not data_cutoff_at <= cohort_observed_at <= registered_at:
        raise _integrity("persisted registration timestamps conflict")


def _validate_registration(
    row: WatchlistQuantV6Registration,
) -> dict[str, Any]:
    _validate_registration_header(row)
    payload = _decode_canonical_json(
        row.registration_json,
        identity_sha256=row.identity_sha256,
        label="registration JSON",
    )
    schema_version = _require_integer(
        payload.get("schema_version"),
        label="registration schema_version",
    )
    if set(payload) != _REGISTRATION_ROOT_KEYS or (
        schema_version != 1
        or payload.get("contract") != _REGISTRATION_CONTRACT
        or payload.get("market") != row.market
        or payload.get("algorithm_version") != row.algorithm_version
        or payload.get("semantic_digest_sha256")
        != row.semantic_digest_sha256
        or payload.get("evaluator_digest_sha256")
        != row.evaluator_digest_sha256
        or payload.get("acquisition_spec_sha256")
        != row.acquisition_spec_sha256
        or payload.get("source_snapshot_sha256")
        != row.source_snapshot_sha256
        or payload.get("schedule_sha256") != row.schedule_sha256
    ):
        raise _integrity("persisted registration JSON conflicts with its header")
    _validate_policy(
        payload.get("policy"),
        label="registration policy",
        require_promotion_eligible=False,
    )
    _registration_members(payload, registration=row)
    schedule = _require_exact_mapping(
        payload.get("schedule"),
        label="registration schedule",
        keys=_REGISTRATION_SCHEDULE_KEYS,
    )
    training_dates = _require_list(
        schedule.get("training_session_dates"),
        label="registration training dates",
    )
    target_dates = _require_list(
        schedule.get("target_session_dates"),
        label="registration target dates",
    )
    parsed_training_dates = tuple(
        _require_canonical_date(
            value,
            label=f"registration training date {index}",
        )
        for index, value in enumerate(training_dates)
    )
    parsed_target_dates = tuple(
        _require_canonical_date(
            value,
            label=f"registration target date {index}",
        )
        for index, value in enumerate(target_dates)
    )
    try:
        expected_training_dates = quant_v6_previous_trading_session_dates(
            row.market,
            row.first_target_session_date,
            count=row.training_session_count,
        )
        expected_target_dates = quant_v6_consecutive_trading_session_dates(
            row.market,
            row.first_target_session_date,
            count=row.target_session_count,
        )
        last_grid = quant_v6_expected_rth_bar_starts(
            row.market,
            row.last_target_session_date,
        )
    except QuantV6SemanticError as exc:
        raise _integrity(
            "persisted registration schedule is outside the calendar"
        ) from exc
    expected_cutoff_at = (
        last_grid[-1] + timedelta(minutes=QUANT_V6_BAR_MINUTES)
        if last_grid
        else None
    )
    persisted_cutoff_at = _persisted_utc(
        row.data_cutoff_at,
        label="data_cutoff_at",
    )
    persisted_observed_at = _persisted_utc(
        row.cohort_observed_at,
        label="cohort_observed_at",
    )
    schedule_schema_version = _require_integer(
        schedule.get("schema_version"),
        label="registration schedule schema_version",
    )
    if (
        schedule.get("market") != row.market
        or schedule_schema_version != 1
        or quant_v6_payload_sha256(schedule) != row.schedule_sha256
        or len(training_dates) != row.training_session_count
        or len(target_dates) != row.target_session_count
        or parsed_training_dates != expected_training_dates
        or parsed_target_dates != expected_target_dates
        or parsed_training_dates[0] != row.first_training_session_date
        or parsed_target_dates[0] != row.first_target_session_date
        or parsed_target_dates[-1] != row.last_target_session_date
        or expected_cutoff_at is None
        or persisted_cutoff_at != expected_cutoff_at
        or persisted_observed_at
        != expected_cutoff_at + _DATA_SETTLEMENT_DELAY
        or payload.get("data_cutoff_at")
        != canonical_utc_timestamp(persisted_cutoff_at)
        or payload.get("cohort_observed_at")
        != canonical_utc_timestamp(persisted_observed_at)
    ):
        raise _integrity("persisted registration schedule conflicts")
    acquisition = _require_exact_mapping(
        payload.get("acquisition"),
        label="registration acquisition",
        keys=frozenset({
            "domain_acquisition_spec",
            "domain_acquisition_spec_sha256",
            "provider_contract",
            "provider_contract_sha256",
            "schema_version",
        }),
    )
    evaluator_manifest = _require_exact_mapping(
        payload.get("evaluator_manifest"),
        label="registration evaluator manifest",
        keys=frozenset({
            "domain_evaluator_digest_sha256",
            "domain_evaluator_manifest",
            "manifest_version",
            "provider_contract_digest_sha256",
            "resource_sha256",
            "source_sha256",
        }),
    )
    source_snapshot = _require_exact_mapping(
        payload.get("source_snapshot"),
        label="registration source snapshot",
        keys=_SOURCE_SNAPSHOT_KEYS,
    )
    domain_acquisition_spec = _require_mapping(
        acquisition.get("domain_acquisition_spec"),
        label="registration domain acquisition spec",
    )
    provider_contract = _validate_provider_contract(
        acquisition.get("provider_contract")
    )
    provider_contract_digest = _require_digest(
        acquisition.get("provider_contract_sha256"),
        label="registration provider contract",
    )
    domain_evaluator_manifest = _require_exact_mapping(
        evaluator_manifest.get("domain_evaluator_manifest"),
        label="registration domain evaluator manifest",
        keys=frozenset({
            "algorithm_version",
            "manifest_version",
            "semantic_digest",
            "source_sha256",
        }),
    )
    domain_source_digests = _validate_digest_mapping(
        domain_evaluator_manifest.get("source_sha256"),
        label="registration domain evaluator sources",
    )
    historical_source_digests = _validate_digest_mapping(
        evaluator_manifest.get("source_sha256"),
        label="registration historical evaluator sources",
    )
    resource_digests = _validate_digest_mapping(
        evaluator_manifest.get("resource_sha256"),
        label="registration evaluator resources",
    )
    membership_resource_digest = _require_digest(
        source_snapshot.get("membership_resource_sha256"),
        label="registration membership resource",
    )
    _require_text(
        source_snapshot.get("catalog_source_version"),
        label="registration catalog source version",
    )
    _require_list(
        source_snapshot.get("catalog_members"),
        label="registration catalog members",
    )
    _require_mapping(
        source_snapshot.get("membership_history"),
        label="registration membership history",
    )
    _require_mapping(
        source_snapshot.get("membership_metadata"),
        label="registration membership metadata",
    )
    source_snapshot_schema_version = _require_integer(
        source_snapshot.get("schema_version"),
        label="registration source snapshot schema_version",
    )
    acquisition_schema_version = _require_integer(
        acquisition.get("schema_version"),
        label="registration acquisition schema_version",
    )
    evaluator_manifest_version = _require_integer(
        evaluator_manifest.get("manifest_version"),
        label="registration evaluator manifest_version",
    )
    domain_manifest_version = _require_integer(
        domain_evaluator_manifest.get("manifest_version"),
        label="registration domain evaluator manifest_version",
    )
    expected_historical_source_digests = (
        _HISTORICAL_EVALUATOR_SOURCE_KEYS_BY_VERSION.get(
            evaluator_manifest_version
        )
    )
    if (
        quant_v6_payload_sha256(acquisition) != row.acquisition_spec_sha256
        or quant_v6_payload_sha256(evaluator_manifest)
        != row.evaluator_digest_sha256
        or quant_v6_payload_sha256(source_snapshot)
        != row.source_snapshot_sha256
        or source_snapshot_schema_version != 1
        or acquisition_schema_version != 1
        or domain_acquisition_spec != dict(QUANT_V6_ACQUISITION_SPEC)
        or acquisition.get("domain_acquisition_spec_sha256")
        != QUANT_V6_ACQUISITION_SPEC_DIGEST
        or quant_v6_payload_sha256(domain_acquisition_spec)
        != QUANT_V6_ACQUISITION_SPEC_DIGEST
        or quant_v6_payload_sha256(provider_contract)
        != provider_contract_digest
        or provider_contract.get("acquisition_spec_sha256")
        != QUANT_V6_ACQUISITION_SPEC_DIGEST
        or expected_historical_source_digests is None
        or evaluator_manifest.get("provider_contract_digest_sha256")
        != provider_contract_digest
        or set(resource_digests) != {_MEMBERSHIP_RESOURCE_KEY}
        or resource_digests.get(_MEMBERSHIP_RESOURCE_KEY)
        != membership_resource_digest
        or evaluator_manifest.get("domain_evaluator_digest_sha256")
        != quant_v6_payload_sha256(domain_evaluator_manifest)
        or domain_evaluator_manifest.get("algorithm_version")
        != row.algorithm_version
        or domain_manifest_version != 1
        or domain_evaluator_manifest.get("semantic_digest")
        != row.semantic_digest_sha256
        or set(domain_source_digests) != _DOMAIN_EVALUATOR_SOURCE_KEYS
        or set(historical_source_digests)
        != expected_historical_source_digests
    ):
        raise _integrity("persisted registration nested digest failed replay")
    return payload


def _publication_acquisition_members(
    payload: Mapping[str, Any],
    *,
    registration: WatchlistQuantV6Registration,
    registration_members: Mapping[int, Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    acquisition = _require_exact_mapping(
        payload.get("acquisition_outcome"),
        label="publication acquisition outcome",
        keys=_ACQUISITION_OUTCOME_KEYS,
    )
    request_start_at = _canonical_timestamp(
        acquisition.get("request_start_at"),
        label="acquisition request start",
    )
    request_end_at = _canonical_timestamp(
        acquisition.get("request_end_at"),
        label="acquisition request end",
    )
    try:
        first_grid = quant_v6_expected_rth_bar_starts(
            registration.market,
            registration.first_training_session_date,
        )
    except QuantV6SemanticError as exc:
        raise _integrity(
            "persisted acquisition window is outside the calendar"
        ) from exc
    if (
        not first_grid
        or request_start_at != first_grid[0]
        or request_end_at
        != _persisted_utc(registration.data_cutoff_at, label="data_cutoff_at")
        or request_start_at >= request_end_at
    ):
        raise _integrity("persisted acquisition request window conflicts")
    raw_members = _require_list(
        acquisition.get("members"),
        label="publication acquisition members",
    )
    if len(raw_members) != registration.cohort_member_count:
        raise _integrity("persisted acquisition member count conflicts")
    members: dict[int, dict[str, Any]] = {}
    for expected_ordinal, raw_member in enumerate(raw_members):
        member = _require_exact_mapping(
            raw_member,
            label="acquisition member",
            keys=_MEMBER_ACQUISITION_KEYS,
        )
        ordinal = _require_integer(
            member.get("member_ordinal"),
            label="acquisition member ordinal",
        )
        symbol = _require_text(
            member.get("symbol"),
            label="acquisition member symbol",
        )
        if (
            ordinal != expected_ordinal
            or ordinal in members
            or ordinal >= registration.cohort_member_count
            or member.get("market") != registration.market
            or registration_members.get(ordinal, {}).get("symbol") != symbol
            or registration_members.get(ordinal, {}).get("market")
            != registration.market
            or symbol != symbol.strip().upper()
            or not symbol.endswith(f".{registration.market}")
        ):
            raise _integrity("persisted acquisition member identity conflicts")
        members[ordinal] = member
    if set(members) != set(range(registration.cohort_member_count)):
        raise _integrity("persisted acquisition member ordinals are incomplete")
    return members


def _target_session_dates(
    registration: WatchlistQuantV6Registration,
) -> tuple[date, ...]:
    try:
        values = quant_v6_consecutive_trading_session_dates(
            registration.market,
            registration.first_target_session_date,
            count=registration.target_session_count,
        )
    except QuantV6SemanticError as exc:
        raise _integrity(
            "persisted target schedule is outside the calendar"
        ) from exc
    if not values or values[-1] != registration.last_target_session_date:
        raise _integrity("persisted target schedule conflicts")
    return values


def _scheduled_session_grids(
    registration: WatchlistQuantV6Registration,
) -> tuple[tuple[date, tuple[datetime, ...]], ...]:
    try:
        training_dates = quant_v6_previous_trading_session_dates(
            registration.market,
            registration.first_target_session_date,
            count=registration.training_session_count,
        )
        target_dates = _target_session_dates(registration)
        grids = tuple(
            (
                session_date,
                quant_v6_expected_rth_bar_starts(
                    registration.market,
                    session_date,
                ),
            )
            for session_date in (*training_dates, *target_dates)
        )
    except QuantV6SemanticError as exc:
        raise _integrity(
            "persisted scheduled grid is outside the calendar"
        ) from exc
    flattened = tuple(
        start_at
        for _session_date, grid in grids
        for start_at in grid
    )
    if (
        training_dates[0] != registration.first_training_session_date
        or any(not grid for _session_date, grid in grids)
        or len(flattened) != len(set(flattened))
        or any(
            current <= previous
            for previous, current in zip(flattened, flattened[1:])
        )
    ):
        raise _integrity("persisted scheduled grid is not canonical")
    return grids


def _scheduled_grid_digest(
    registration_identity_sha256: str,
    starts: tuple[datetime, ...],
) -> str:
    digest = hashlib.sha256()
    for value in (
        _SCHEDULED_GRID_DIGEST_CONTRACT,
        registration_identity_sha256,
        *(canonical_utc_timestamp(start_at) for start_at in starts),
    ):
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_publication_header(
    row: WatchlistQuantV6Publication,
    *,
    registration: WatchlistQuantV6Registration,
) -> None:
    if (
        type(row.id) is not int
        or not 1 <= row.id <= _SQLITE_MAX_INTEGER
        or row.registration_id != registration.id
        or row.registration_identity_sha256 != registration.identity_sha256
        or row.schema_version != 1
        or row.contract_version != _PUBLICATION_CONTRACT
        or row.status != "PUBLISHED"
        or type(row.registered_member_count) is not int
        or type(row.assessment_artifact_count) is not int
        or type(row.session_input_artifact_count) is not int
        or type(row.event_artifact_count) is not int
        or type(row.binding_count) is not int
        or row.registered_member_count != registration.cohort_member_count
        or row.assessment_artifact_count != row.registered_member_count
        or row.session_input_artifact_count < 0
        or row.event_artifact_count < 0
        or row.binding_count
        != row.assessment_artifact_count
        + row.session_input_artifact_count
        + row.event_artifact_count
        or row.binding_count < 1
        or row.binding_count > _MAX_BINDINGS
        or row.promotion_eligible is not False
        or row.automatic_promotion_allowed is not False
        or row.order_submission_allowed is not False
        or row.short_entry_allowed is not False
        or row.position_add_on_allowed is not False
    ):
        raise _integrity("persisted publication header violates its contract")
    _require_digest(row.identity_sha256, label="publication identity")
    _require_digest(row.manifest_sha256, label="publication manifest")
    if _persisted_utc(
        row.published_at,
        label="published_at",
    ) < _persisted_utc(registration.registered_at, label="registered_at"):
        raise _integrity("persisted publication predates its registration")


def _validate_publication(
    row: WatchlistQuantV6Publication,
    *,
    registration: WatchlistQuantV6Registration,
    registration_members: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    _validate_publication_header(row, registration=registration)
    payload = _decode_canonical_json(
        row.publication_json,
        identity_sha256=row.identity_sha256,
        label="publication JSON",
    )
    counts = _require_exact_mapping(
        payload.get("artifact_counts"),
        label="publication artifact counts",
        keys=frozenset({"assessment", "binding", "event", "session_input"}),
    )
    schema_version = _require_integer(
        payload.get("schema_version"),
        label="publication schema_version",
    )
    registered_member_count = _require_integer(
        payload.get("registered_member_count"),
        label="publication registered_member_count",
    )
    assessment_count = _require_integer(
        counts.get("assessment"),
        label="publication assessment artifact count",
    )
    session_input_count = _require_integer(
        counts.get("session_input"),
        label="publication session input artifact count",
    )
    event_count = _require_integer(
        counts.get("event"),
        label="publication event artifact count",
    )
    binding_count = _require_integer(
        counts.get("binding"),
        label="publication binding count",
    )
    if set(payload) != _PUBLICATION_ROOT_KEYS or (
        schema_version != 1
        or payload.get("contract") != _PUBLICATION_CONTRACT
        or payload.get("manifest_contract") != _MANIFEST_CONTRACT
        or payload.get("manifest_sha256") != row.manifest_sha256
        or payload.get("registration_identity_sha256")
        != registration.identity_sha256
        or registered_member_count != row.registered_member_count
        or payload.get("status") != "PUBLISHED"
        or assessment_count != row.assessment_artifact_count
        or session_input_count != row.session_input_artifact_count
        or event_count != row.event_artifact_count
        or binding_count != row.binding_count
    ):
        raise _integrity("persisted publication JSON conflicts with its header")
    _validate_policy(
        payload.get("policy"),
        label="publication policy",
        require_promotion_eligible=True,
    )
    acquisition_members = _publication_acquisition_members(
        payload,
        registration=registration,
        registration_members=registration_members,
    )
    grids = _scheduled_session_grids(registration)
    expected_session_input_count = 0
    for acquisition in acquisition_members.values():
        _, covered_target_ordinals = _member_acquisition(
            acquisition,
            grids=grids,
            registration=registration,
        )
        expected_session_input_count += len(covered_target_ordinals)
    if expected_session_input_count != row.session_input_artifact_count:
        raise _integrity(
            "persisted session input count conflicts with acquisition coverage"
        )
    return payload


def _binding_manifest_payload(
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


def _validate_binding(
    row: WatchlistQuantV6PublicationArtifact,
    *,
    registration: WatchlistQuantV6Registration,
    publication: WatchlistQuantV6Publication,
    members: Mapping[int, Mapping[str, Any]],
    target_session_dates: tuple[date, ...],
) -> dict[str, object]:
    if (
        type(row.publication_id) is not int
        or type(row.member_ordinal) is not int
        or type(row.symbol) is not str
        or type(row.market) is not str
        or type(row.role) is not str
        or type(row.artifact_kind) is not str
        or type(row.artifact_ordinal) is not int
        or (
            row.session_date is not None
            and type(row.session_date) is not date
        )
    ):
        raise _integrity("persisted binding field types are invalid")
    member = members.get(row.member_ordinal)
    if (
        row.publication_id != publication.id
        or row.member_ordinal < 0
        or row.member_ordinal >= registration.cohort_member_count
        or row.role not in _ROLE_RANK
        or row.artifact_kind != _KIND_BY_ROLE.get(row.role)
        or row.artifact_ordinal < 0
        or row.artifact_ordinal >= _MAX_BINDINGS
        or row.market != registration.market
        or row.symbol != row.symbol.strip().upper()
        or not row.symbol.endswith(f".{row.market}")
        or member is None
        or member.get("symbol") != row.symbol
        or member.get("market") != row.market
    ):
        raise _integrity("persisted binding header violates its contract")
    if (
        row.role == _ASSESSMENT_ROLE
        and (row.session_date is not None or row.artifact_ordinal != 0)
    ) or (
        row.role == _SESSION_INPUT_ROLE
        and (
            row.artifact_ordinal >= len(target_session_dates)
            or row.session_date
            != target_session_dates[row.artifact_ordinal]
        )
    ) or (
        row.role == _EVENT_ROLE
        and row.session_date not in target_session_dates
    ):
        raise _integrity("persisted binding role envelope is invalid")
    _require_digest(row.artifact_sha256, label="binding artifact digest")
    binding_digest = _require_digest(
        row.binding_sha256,
        label="binding identity",
    )
    payload = _binding_manifest_payload(row)
    preimage = dict(payload)
    preimage.pop("binding_sha256")
    preimage.update({
        "contract": _BINDING_CONTRACT,
        "registration_identity_sha256": registration.identity_sha256,
        "schema_version": 1,
    })
    actual_digest = quant_v6_payload_sha256(preimage)
    if not hmac.compare_digest(actual_digest, binding_digest):
        raise _integrity("persisted binding identity failed replay")
    if _persisted_utc(
        row.created_at,
        label="binding created_at",
    ) != _persisted_utc(publication.published_at, label="published_at"):
        raise _integrity("persisted binding timestamp conflicts")
    return payload


def _validate_artifact_metadata(
    *,
    digest_sha256: object,
    schema_version: object,
    kind: object,
    codec: object,
    compression_level: object,
    raw_size: object,
    compressed_size: object,
    created_at: object,
    binding: WatchlistQuantV6PublicationArtifact,
) -> tuple[
    Literal[1],
    ArtifactKind,
    Literal["zlib"],
    Literal[9],
    int,
    int,
    datetime,
]:
    digest = _require_digest(digest_sha256, label="artifact digest")
    if (
        digest != binding.artifact_sha256
        or type(schema_version) is not int
        or schema_version != QUANT_V6_ARTIFACT_SCHEMA_VERSION
        or type(kind) is not str
        or kind != binding.artifact_kind
        or type(codec) is not str
        or codec != QUANT_V6_ARTIFACT_CODEC
        or type(compression_level) is not int
        or compression_level != QUANT_V6_ARTIFACT_COMPRESSION_LEVEL
        or type(raw_size) is not int
        or not 0 < raw_size <= MAX_QUANT_V6_ARTIFACT_RAW_BYTES
        or type(compressed_size) is not int
        or not 0 < compressed_size
        <= MAX_QUANT_V6_ARTIFACT_COMPRESSED_BYTES
    ):
        raise _integrity("persisted artifact metadata violates its contract")
    artifact_created_at = _persisted_utc(
        created_at,
        label="artifact created_at",
    )
    if artifact_created_at > _persisted_utc(
        binding.created_at,
        label="binding created_at",
    ):
        raise _integrity("persisted artifact postdates its binding")
    return (
        cast(Literal[1], schema_version),
        cast(ArtifactKind, kind),
        cast(Literal["zlib"], codec),
        cast(Literal[9], compression_level),
        raw_size,
        compressed_size,
        artifact_created_at,
    )


def _decode_artifact_payload(
    *,
    digest_sha256: str,
    schema_version: int,
    kind: str,
    codec: str,
    raw_size: int,
    compressed_size: int,
    payload: bytes,
) -> dict[str, Any]:
    try:
        return decode_quant_v6_artifact(
            digest_sha256=digest_sha256,
            schema_version=schema_version,
            kind=kind,
            codec=codec,
            raw_size=raw_size,
            compressed_size=compressed_size,
            payload=payload,
        )
    except QuantV6ArtifactError as exc:
        raise _integrity("persisted artifact payload failed bounded replay") from exc


def _validate_artifact_payload_binding(
    payload: Mapping[str, Any],
    *,
    kind: str,
    binding: WatchlistQuantV6PublicationArtifact,
    target_session_dates: tuple[date, ...],
) -> None:
    expected_keys = _PAYLOAD_KEYS_BY_KIND.get(kind)
    expected_contract = _PAYLOAD_CONTRACT_BY_KIND.get(kind)
    if expected_keys is None or expected_contract is None:
        raise _integrity("persisted artifact payload kind is unsupported")
    exact = _require_exact_mapping(
        payload,
        label="artifact payload",
        keys=expected_keys,
    )
    if (
        _require_integer(
            exact.get("schema_version"),
            label="artifact payload schema_version",
        )
        != QUANT_V6_PAYLOAD_SCHEMA_VERSION
        or _require_text(
            exact.get("contract"),
            label="artifact payload contract",
        )
        != expected_contract
        or _require_text(
            exact.get("algorithm_version"),
            label="artifact payload algorithm_version",
        )
        != QUANT_V6_ALGORITHM_VERSION
        or _require_digest(
            exact.get("semantic_digest"),
            label="artifact payload semantic digest",
        )
        != QUANT_V6_SEMANTIC_DIGEST
    ):
        raise _integrity("persisted artifact payload envelope conflicts")
    identity_keys: frozenset[str]
    if kind == QUANT_V6_ASSESSMENT_ARTIFACT_KIND:
        identity_keys = frozenset({
            "market", "symbol", "window_digest_sha256"
        })
    elif kind == QUANT_V6_SESSION_INPUT_ARTIFACT_KIND:
        identity_keys = frozenset({"market", "session_date", "symbol"})
    else:
        identity_keys = frozenset({
            "event_key_sha256",
            "market",
            "session_date",
            "signal_bar_start_at",
            "symbol",
        })
    identity = _require_exact_mapping(
        exact.get("identity"),
        label="artifact payload identity",
        keys=identity_keys,
    )
    symbol = _require_text(
        identity.get("symbol"),
        label="artifact payload identity symbol",
    )
    market = _require_text(
        identity.get("market"),
        label="artifact payload identity market",
    )
    if symbol != binding.symbol or market != binding.market:
        raise _integrity(
            "persisted artifact payload identity conflicts with its binding"
        )

    if kind == QUANT_V6_ASSESSMENT_ARTIFACT_KIND:
        if (
            _require_text(
                exact.get("capture_mode"),
                label="assessment payload capture_mode",
            )
            != BAR_NEXT_OPEN_STRESSED
        ):
            raise _integrity("persisted assessment capture mode conflicts")
        window_digest = _require_digest(
            identity.get("window_digest_sha256"),
            label="assessment payload window digest",
        )
        expected_window_digest = quant_v6_payload_sha256({
            "market": binding.market,
            "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
            "session_dates": [
                session_date.isoformat()
                for session_date in target_session_dates
            ],
            "session_denominator": len(target_session_dates),
        })
        if not hmac.compare_digest(window_digest, expected_window_digest):
            raise _integrity("persisted assessment window identity conflicts")
        policy = _require_exact_mapping(
            exact.get("policy"),
            label="assessment payload policy",
            keys=frozenset({
                "automatic_promotion_allowed",
                "order_submission_allowed",
                "position_add_on_allowed",
                "promotion_eligible",
                "recommended_action",
                "short_entry_allowed",
            }),
        )
        for key in (
            "automatic_promotion_allowed",
            "order_submission_allowed",
            "position_add_on_allowed",
            "promotion_eligible",
            "short_entry_allowed",
        ):
            if _require_boolean(
                policy.get(key),
                label=f"assessment payload policy.{key}",
            ):
                raise _integrity("persisted assessment payload violates P0")
        if _require_text(
            policy.get("recommended_action"),
            label="assessment payload recommended_action",
        ) not in {"AVOID", "WATCH"}:
            raise _integrity("persisted assessment recommendation conflicts")
        return

    payload_session_date = _require_canonical_date(
        identity.get("session_date"),
        label="artifact payload identity session_date",
    )
    if payload_session_date != binding.session_date:
        raise _integrity(
            "persisted artifact payload session conflicts with its binding"
        )
    if kind == QUANT_V6_SESSION_INPUT_ARTIFACT_KIND:
        if (
            _require_text(
                exact.get("capture_mode"),
                label="session input payload capture_mode",
            )
            != BAR_NEXT_OPEN_STRESSED
        ):
            raise _integrity("persisted session input capture mode conflicts")
        _validate_policy(
            exact.get("p0"),
            label="session input payload p0",
            require_promotion_eligible=False,
        )
        return

    event_key = _require_digest(
        identity.get("event_key_sha256"),
        label="event payload identity event key",
    )
    signal_start_at = _canonical_timestamp(
        identity.get("signal_bar_start_at"),
        label="event payload identity signal_bar_start_at",
    )
    try:
        expected_grid = quant_v6_expected_rth_bar_starts(
            binding.market,
            payload_session_date,
        )
    except QuantV6SemanticError as exc:
        raise _integrity("persisted event identity is outside the calendar") from exc
    if signal_start_at not in expected_grid:
        raise _integrity("persisted event signal is outside the session grid")
    expected_event_key = quant_v6_payload_sha256({
        "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
        "capture_mode": BAR_NEXT_OPEN_STRESSED,
        "market": binding.market,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "session_date": payload_session_date.isoformat(),
        "signal_bar_start_at": canonical_utc_timestamp(signal_start_at),
        "symbol": binding.symbol,
    })
    if not hmac.compare_digest(event_key, expected_event_key):
        raise _integrity("persisted event key identity conflicts")
    event_p0 = _require_exact_mapping(
        exact.get("p0"),
        label="event payload p0",
        keys=frozenset({
            "automatic_promotion_allowed",
            "order_submission_allowed",
            "short_entry_allowed",
        }),
    )
    for key in sorted(event_p0):
        if _require_boolean(
            event_p0.get(key),
            label=f"event payload p0.{key}",
        ):
            raise _integrity("persisted event payload violates P0")
    capture = _require_exact_mapping(
        exact.get("capture"),
        label="event payload capture",
        keys=frozenset({"historical_only", "mode", "promotion_eligible"}),
    )
    if (
        _require_boolean(
            capture.get("historical_only"),
            label="event payload capture.historical_only",
        )
        is not True
        or _require_boolean(
            capture.get("promotion_eligible"),
            label="event payload capture.promotion_eligible",
        )
        is not False
        or _require_text(
            capture.get("mode"),
            label="event payload capture.mode",
        )
        != BAR_NEXT_OPEN_STRESSED
    ):
        raise _integrity("persisted event capture envelope conflicts")
    execution = _require_mapping(
        exact.get("execution"),
        label="event payload execution",
    )
    if (
        _require_text(
            execution.get("side"),
            label="event payload execution.side",
        )
        != "LONG"
        or _require_boolean(
            execution.get("overlap_allowed"),
            label="event payload execution.overlap_allowed",
        )
        is not False
        or _require_boolean(
            execution.get("position_add_on_allowed"),
            label="event payload execution.position_add_on_allowed",
        )
        is not False
    ):
        raise _integrity("persisted event execution envelope violates P0")


def _summary(
    publication: WatchlistQuantV6Publication,
    registration: WatchlistQuantV6Registration,
) -> WatchlistQuantV6PublicationSummary:
    return WatchlistQuantV6PublicationSummary(
        publication_id=publication.id,
        registration_id=registration.id,
        market=cast(Market, registration.market),
        status="PUBLISHED",
        contract_version=publication.contract_version,
        algorithm_version=registration.algorithm_version,
        registration_identity_sha256=registration.identity_sha256,
        identity_sha256=publication.identity_sha256,
        manifest_sha256=publication.manifest_sha256,
        registered_member_count=publication.registered_member_count,
        assessment_artifact_count=publication.assessment_artifact_count,
        session_input_artifact_count=(
            publication.session_input_artifact_count
        ),
        event_artifact_count=publication.event_artifact_count,
        binding_count=publication.binding_count,
        first_training_session_date=registration.first_training_session_date,
        first_target_session_date=registration.first_target_session_date,
        last_target_session_date=registration.last_target_session_date,
        data_cutoff_at=_persisted_utc(
            registration.data_cutoff_at,
            label="data_cutoff_at",
        ),
        registered_at=_persisted_utc(
            registration.registered_at,
            label="registered_at",
        ),
        published_at=_persisted_utc(
            publication.published_at,
            label="published_at",
        ),
        policy=_false_policy(),
    )


def _registration_response(
    row: WatchlistQuantV6Registration,
) -> WatchlistQuantV6RegistrationResponse:
    return WatchlistQuantV6RegistrationResponse(
        registration_id=row.id,
        identity_sha256=row.identity_sha256,
        schema_version=1,
        contract_version=row.contract_version,
        selection_rule_version=row.selection_rule_version,
        algorithm_version=row.algorithm_version,
        semantic_digest_sha256=row.semantic_digest_sha256,
        evaluator_digest_sha256=row.evaluator_digest_sha256,
        acquisition_spec_sha256=row.acquisition_spec_sha256,
        cohort_source="ROTATION_RESEARCH_CATALOG_PIT",
        market=cast(Market, row.market),
        source_snapshot_sha256=row.source_snapshot_sha256,
        cohort_manifest_sha256=row.cohort_manifest_sha256,
        cohort_member_count=row.cohort_member_count,
        schedule_sha256=row.schedule_sha256,
        training_session_count=10,
        target_session_count=30,
        first_training_session_date=row.first_training_session_date,
        first_target_session_date=row.first_target_session_date,
        last_target_session_date=row.last_target_session_date,
        data_cutoff_at=_persisted_utc(
            row.data_cutoff_at,
            label="data_cutoff_at",
        ),
        bar_period="MIN_5",
        adjustment_mode="NO_ADJUST",
        server_generated=True,
        short_entry_allowed=False,
        position_add_on_allowed=False,
        order_submission_allowed=False,
        automatic_promotion_allowed=False,
        cohort_observed_at=_persisted_utc(
            row.cohort_observed_at,
            label="cohort_observed_at",
        ),
        registered_at=_persisted_utc(
            row.registered_at,
            label="registered_at",
        ),
    )


def _canonical_timestamp(value: object, *, label: str) -> datetime:
    text = _require_text(value, label=label)
    if not text.endswith("Z"):
        raise _integrity(f"persisted {label} is not canonical UTC")
    try:
        timestamp = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise _integrity(f"persisted {label} is not a timestamp") from exc
    if canonical_utc_timestamp(timestamp) != text:
        raise _integrity(f"persisted {label} is not canonical UTC")
    return timestamp.astimezone(timezone.utc)


def _member_acquisition(
    value: Mapping[str, Any],
    *,
    grids: tuple[tuple[date, tuple[datetime, ...]], ...],
    registration: WatchlistQuantV6Registration,
) -> tuple[WatchlistQuantV6MemberAcquisitionResponse, frozenset[int]]:
    pages = _require_integer(
        value.get("pages"),
        label="acquisition pages",
    )
    accepted = _require_integer(
        value.get("accepted_bars"),
        label="acquisition accepted bars",
    )
    rejected = _require_integer(
        value.get("rejected_rows"),
        label="acquisition rejected rows",
    )
    raw_rows = _require_integer(
        value.get("raw_rows"),
        label="acquisition raw rows",
    )
    complete_sessions = _require_integer(
        value.get("complete_session_count"),
        label="acquisition complete sessions",
    )
    off_grid = _require_integer(
        value.get("off_grid_accepted_bars"),
        label="acquisition off-grid bars",
    )
    present_bars = _require_integer(
        value.get("scheduled_grid_present_bars"),
        label="acquisition present bars",
    )
    if (
        pages < 1
        or pages > _PROVIDER_MAX_PAGES
        or raw_rows > _PROVIDER_MAX_RAW_ROWS
        or raw_rows > pages * _PROVIDER_PAGE_SIZE
        or raw_rows < pages - 1
        or accepted > _PROVIDER_MAX_BARS
        or accepted + rejected > raw_rows
        or complete_sessions > 40
        or off_grid > accepted
        or present_bars > accepted
    ):
        raise _integrity("persisted acquisition row counts conflict")
    coverage = _require_text(
        value.get("scheduled_grid_coverage_bitset_hex"),
        label="acquisition coverage bitset",
    )
    flat_grid = tuple(
        start_at
        for _session_date, grid in grids
        for start_at in grid
    )
    expected_bytes = (len(flat_grid) + 7) // 8
    if (
        len(coverage) != expected_bytes * 2
        or re.fullmatch(r"[0-9a-f]+", coverage) is None
    ):
        raise _integrity("persisted acquisition coverage bitset is invalid")
    encoded_coverage = bytes.fromhex(coverage)
    unused_bits = expected_bytes * 8 - len(flat_grid)
    if (
        unused_bits
        and encoded_coverage
        and encoded_coverage[-1] & ((1 << unused_bits) - 1)
    ):
        raise _integrity("persisted acquisition coverage padding is invalid")
    flags = tuple(
        bool(
            encoded_coverage[index // 8]
            & (1 << (7 - (index % 8)))
        )
        for index in range(len(flat_grid))
    )
    present_starts = tuple(
        start_at
        for start_at, present in zip(flat_grid, flags, strict=True)
        if present
    )
    complete_from_coverage = 0
    offset = 0
    for _session_date, grid in grids:
        grid_flags = flags[offset:offset + len(grid)]
        complete_from_coverage += int(all(grid_flags))
        offset += len(grid)
    present_digest = _require_digest(
        value.get("scheduled_grid_present_starts_sha256"),
        label="acquisition present starts digest",
    )
    if (
        present_bars != len(present_starts)
        or off_grid != accepted - present_bars
        or complete_sessions != complete_from_coverage
        or not hmac.compare_digest(
            present_digest,
            _scheduled_grid_digest(
                registration.identity_sha256,
                present_starts,
            ),
        )
    ):
        raise _integrity("persisted acquisition coverage conflicts")
    present_start_set = frozenset(present_starts)
    covered_target_ordinals = frozenset(
        ordinal
        for ordinal in range(registration.target_session_count)
        if all(
            start_at in present_start_set
            for _session_date, grid in grids[
                ordinal:ordinal + registration.training_session_count + 1
            ]
            for start_at in grid
        )
    )
    response = WatchlistQuantV6MemberAcquisitionResponse(
        pages=pages,
        raw_rows=raw_rows,
        accepted_bars=accepted,
        rejected_rows=rejected,
        complete_session_count=complete_sessions,
        off_grid_accepted_bars=off_grid,
        scheduled_grid_present_bars=present_bars,
        accepted_bar_starts_sha256=_require_digest(
            value.get("accepted_bar_starts_sha256"),
            label="acquisition accepted starts digest",
        ),
        scheduled_grid_present_starts_sha256=present_digest,
        scheduled_grid_coverage_bitset_hex=coverage,
    )
    return response, covered_target_ordinals


def _member_summary(
    *,
    binding: WatchlistQuantV6PublicationArtifact,
    member: Mapping[str, Any],
    acquisition: Mapping[str, Any],
    grids: tuple[tuple[date, tuple[datetime, ...]], ...],
    registration: WatchlistQuantV6Registration,
) -> WatchlistQuantV6MemberSummary:
    alias = member.get("alias")
    sector = member.get("sector")
    memberships_raw = _require_list(
        member.get("memberships"),
        label="registration member memberships",
    )
    memberships = [
        _require_text(value, label="registration membership")
        for value in memberships_raw
    ]
    member_ordinal = _require_integer(
        member.get("ordinal"),
        label="registration member ordinal",
    )
    acquisition_ordinal = _require_integer(
        acquisition.get("member_ordinal"),
        label="acquisition member ordinal",
    )
    if (
        member_ordinal != binding.member_ordinal
        or member.get("symbol") != binding.symbol
        or member.get("market") != binding.market
        or type(alias) is not str
        or type(sector) is not str
        or acquisition_ordinal != binding.member_ordinal
        or acquisition.get("symbol") != binding.symbol
        or acquisition.get("market") != binding.market
    ):
        raise _integrity("persisted member evidence identities conflict")
    return WatchlistQuantV6MemberSummary(
        member_ordinal=binding.member_ordinal,
        symbol=binding.symbol,
        market=cast(Market, binding.market),
        alias=alias,
        sector=sector,
        memberships=memberships,
        assessment_artifact_sha256=binding.artifact_sha256,
        assessment_binding_sha256=binding.binding_sha256,
        acquisition=_member_acquisition(
            acquisition,
            grids=grids,
            registration=registration,
        )[0],
    )


class WatchlistQuantV6ReaderService:
    """Read immutable quant-v6 evidence without evaluator or provider imports."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def _publication_pair(
        self,
        publication_id: int,
    ) -> tuple[
        WatchlistQuantV6Publication,
        WatchlistQuantV6Registration,
        dict[str, Any],
        dict[str, Any],
    ]:
        if (
            type(publication_id) is not int
            or publication_id < 1
            or publication_id > _SQLITE_MAX_INTEGER
        ):
            raise ValueError("publication_id must be a positive integer")
        length_row = self._db.execute(
            select(
                WatchlistQuantV6Publication.id,
                WatchlistQuantV6Registration.id,
                func.length(sql_cast(
                    WatchlistQuantV6Publication.publication_json,
                    LargeBinary,
                )),
                func.length(sql_cast(
                    WatchlistQuantV6Registration.registration_json,
                    LargeBinary,
                )),
            )
            .outerjoin(
                WatchlistQuantV6Registration,
                and_(
                    WatchlistQuantV6Registration.id
                    == WatchlistQuantV6Publication.registration_id,
                    WatchlistQuantV6Registration.identity_sha256
                    == WatchlistQuantV6Publication.registration_identity_sha256,
                ),
            )
            .where(WatchlistQuantV6Publication.id == publication_id)
        ).one_or_none()
        if length_row is None:
            raise QuantV6ReadNotFoundError("quant-v6 publication not found")
        if length_row[1] is None:
            raise _integrity("persisted publication registration is missing")
        _require_json_length(length_row[2], label="publication JSON")
        _require_json_length(length_row[3], label="registration JSON")
        row = self._db.execute(
            select(
                WatchlistQuantV6Publication,
                WatchlistQuantV6Registration,
            )
            .outerjoin(
                WatchlistQuantV6Registration,
                and_(
                    WatchlistQuantV6Registration.id
                    == WatchlistQuantV6Publication.registration_id,
                    WatchlistQuantV6Registration.identity_sha256
                    == WatchlistQuantV6Publication.registration_identity_sha256,
                ),
            )
            .where(WatchlistQuantV6Publication.id == publication_id)
        ).one_or_none()
        if row is None:
            raise QuantV6ReadNotFoundError("quant-v6 publication not found")
        publication = row[0]
        registration = row[1]
        if not isinstance(publication, WatchlistQuantV6Publication):
            raise _integrity("persisted publication projection is invalid")
        if not isinstance(registration, WatchlistQuantV6Registration):
            raise _integrity("persisted publication registration is missing")
        registration_payload = _validate_registration(registration)
        registration_members = _registration_members(
            registration_payload,
            registration=registration,
        )
        publication_payload = _validate_publication(
            publication,
            registration=registration,
            registration_members=registration_members,
        )
        return (
            publication,
            registration,
            registration_payload,
            publication_payload,
        )

    def list_publications(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        market: Market | None = None,
    ) -> WatchlistQuantV6PublicationPage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if market not in {None, "US", "HK"}:
            raise ValueError("market must be US or HK")
        snapshot_max_id: int | None = None
        snapshot_identity: str | None = None
        last_id: int | None = None
        if cursor is not None:
            state = _decode_cursor(cursor, kind="publications")
            if (
                set(state) != {"a", "l", "m", "s"}
                or state.get("m") != market
            ):
                raise _cursor_error()
            snapshot_identity = _cursor_digest(state, "a")
            snapshot_max_id = _cursor_integer(
                state,
                "s",
                minimum=1,
                maximum=_SQLITE_MAX_INTEGER,
            )
            last_id = _cursor_integer(
                state,
                "l",
                minimum=1,
                maximum=snapshot_max_id,
            )
        filters: list[Any] = []
        if market is not None:
            filters.append(WatchlistQuantV6Registration.market == market)
        join_condition = and_(
            WatchlistQuantV6Registration.id
            == WatchlistQuantV6Publication.registration_id,
            WatchlistQuantV6Registration.identity_sha256
            == WatchlistQuantV6Publication.registration_identity_sha256,
        )
        with self._db.no_autoflush:
            if snapshot_max_id is None:
                bounded_publications = (
                    select(
                        WatchlistQuantV6Publication.id.label(
                            "publication_id"
                        )
                    )
                    .select_from(WatchlistQuantV6Publication)
                    .outerjoin(
                        WatchlistQuantV6Registration,
                        join_condition,
                    )
                    .where(*filters)
                    .limit(_MAX_PUBLICATIONS + 1)
                    .subquery()
                )
                statistics = self._db.execute(
                    select(
                        func.max(bounded_publications.c.publication_id),
                        func.count(),
                    )
                    .select_from(bounded_publications)
                ).one()
                snapshot_value = statistics[0]
                total = int(statistics[1] or 0)
                if total > _MAX_PUBLICATIONS:
                    raise _integrity(
                        "persisted publication set exceeds read bound"
                    )
                if total == 0:
                    return WatchlistQuantV6PublicationPage(
                        items=[],
                        total=0,
                        limit=limit,
                        next_cursor=None,
                    )
                if type(snapshot_value) is not int or snapshot_value < 1:
                    raise _integrity(
                        "persisted publication snapshot is invalid"
                    )
                snapshot_max_id = snapshot_value
            else:
                bounded_publications = (
                    select(
                        WatchlistQuantV6Publication.id.label(
                            "publication_id"
                        )
                    )
                    .select_from(WatchlistQuantV6Publication)
                    .outerjoin(
                        WatchlistQuantV6Registration,
                        join_condition,
                    )
                    .where(
                        *filters,
                        WatchlistQuantV6Publication.id <= snapshot_max_id,
                    )
                    .limit(_MAX_PUBLICATIONS + 1)
                    .subquery()
                )
                total = int(self._db.scalar(
                    select(func.count())
                    .select_from(bounded_publications)
                ) or 0)
                if total > _MAX_PUBLICATIONS:
                    raise _integrity(
                        "persisted publication set exceeds read bound"
                    )
                persisted_snapshot_identity = self._db.scalar(
                    select(WatchlistQuantV6Publication.identity_sha256)
                    .outerjoin(
                        WatchlistQuantV6Registration,
                        join_condition,
                    )
                    .where(
                        *filters,
                        WatchlistQuantV6Publication.id == snapshot_max_id,
                    )
                )
                if (
                    type(persisted_snapshot_identity) is not str
                    or snapshot_identity is None
                    or not hmac.compare_digest(
                        persisted_snapshot_identity,
                        snapshot_identity,
                    )
                ):
                    raise _cursor_error()
            page_filters = [
                *filters,
                WatchlistQuantV6Publication.id <= snapshot_max_id,
            ]
            if last_id is not None:
                page_filters.append(
                    WatchlistQuantV6Publication.id < last_id
                )
            projected_rows = tuple(self._db.execute(
                select(
                    WatchlistQuantV6Publication,
                    WatchlistQuantV6Registration,
                )
                .options(
                    defer(
                        WatchlistQuantV6Publication.publication_json,
                        raiseload=True,
                    ),
                    defer(
                        WatchlistQuantV6Registration.registration_json,
                        raiseload=True,
                    ),
                )
                .outerjoin(
                    WatchlistQuantV6Registration,
                    join_condition,
                )
                .where(*page_filters)
                .order_by(WatchlistQuantV6Publication.id.desc())
                .limit(limit + 1)
            ))
            has_more = len(projected_rows) > limit
            page_rows = projected_rows[:limit]
        items: list[WatchlistQuantV6PublicationSummary] = []
        publication_ids: list[int] = []
        for row in page_rows:
            publication = row[0]
            registration = row[1]
            if not isinstance(publication, WatchlistQuantV6Publication):
                raise _integrity("persisted publication projection is invalid")
            if not isinstance(registration, WatchlistQuantV6Registration):
                raise _integrity("persisted publication registration is missing")
            _validate_registration_header(registration)
            _validate_publication_header(
                publication,
                registration=registration,
            )
            publication_ids.append(publication.id)
            items.append(_summary(publication, registration))
        if snapshot_identity is None:
            if (
                not page_rows
                or publication_ids[0] != snapshot_max_id
            ):
                raise _integrity("persisted publication snapshot is invalid")
            snapshot_identity = page_rows[0][0].identity_sha256
        next_cursor = None
        if has_more:
            if not publication_ids:
                raise _integrity("persisted publication page is invalid")
            next_cursor = _encode_cursor(
                "publications",
                {
                    "a": snapshot_identity,
                    "l": publication_ids[-1],
                    "m": market,
                    "s": snapshot_max_id,
                },
            )
        return WatchlistQuantV6PublicationPage(
            items=items,
            total=total,
            limit=limit,
            next_cursor=next_cursor,
        )

    def get_publication(
        self,
        publication_id: int,
    ) -> WatchlistQuantV6PublicationDetail:
        with self._db.no_autoflush:
            (
                publication,
                registration,
                registration_payload,
                publication_payload,
            ) = self._publication_pair(publication_id)
            members = _registration_members(
                registration_payload,
                registration=registration,
            )
            target_dates = _target_session_dates(registration)
            grids = _scheduled_session_grids(registration)
            acquisition_members = _publication_acquisition_members(
                publication_payload,
                registration=registration,
                registration_members=members,
            )
            expected_session_ordinals = {
                ordinal: _member_acquisition(
                    acquisition_members[ordinal],
                    grids=grids,
                    registration=registration,
                )[1]
                for ordinal in members
            }
            observed_session_ordinals = {
                ordinal: set[int]() for ordinal in members
            }
            target_ordinal_by_date = {
                session_date: ordinal
                for ordinal, session_date in enumerate(target_dates)
            }
            next_event_ordinals = {
                ordinal: 0 for ordinal in members
            }
            last_event_session_ordinals = {
                ordinal: -1 for ordinal in members
            }
            bounded_binding_ids = (
                select(
                    WatchlistQuantV6PublicationArtifact.member_ordinal
                )
                .where(
                    WatchlistQuantV6PublicationArtifact.publication_id
                    == publication.id
                )
                .limit(_MAX_BINDINGS + 1)
                .subquery()
            )
            persisted_binding_count = self._db.scalar(
                select(func.count()).select_from(bounded_binding_ids)
            )
            if (
                type(persisted_binding_count) is not int
                or persisted_binding_count != publication.binding_count
            ):
                raise _integrity(
                    "persisted publication binding set is incomplete"
                )
            role_rank = case(
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _ASSESSMENT_ROLE,
                    0,
                ),
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _SESSION_INPUT_ROLE,
                    1,
                ),
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _EVENT_ROLE,
                    2,
                ),
                else_=3,
            )
            binding_rows = self._db.execute(
                select(
                    WatchlistQuantV6PublicationArtifact,
                    WatchlistQuantV6Artifact.digest_sha256,
                )
                .outerjoin(
                    WatchlistQuantV6Artifact,
                    and_(
                        WatchlistQuantV6Artifact.digest_sha256
                        == WatchlistQuantV6PublicationArtifact.artifact_sha256,
                        WatchlistQuantV6Artifact.kind
                        == WatchlistQuantV6PublicationArtifact.artifact_kind,
                    ),
                )
                .where(
                    WatchlistQuantV6PublicationArtifact.publication_id
                    == publication.id
                )
                .order_by(
                    WatchlistQuantV6PublicationArtifact.member_ordinal,
                    role_rank,
                    WatchlistQuantV6PublicationArtifact.artifact_ordinal,
                )
                .limit(publication.binding_count + 1)
            ).yield_per(400)
            manifest_digest = hashlib.sha256()
            manifest_digest.update(b'{"bindings":[')
            counts = {role: 0 for role in _ROLE_RANK}
            seen_artifact_digests: set[str] = set()
            row_count = 0
            for projected in binding_rows:
                binding = projected[0]
                artifact_digest = projected[1]
                if (
                    not isinstance(
                        binding,
                        WatchlistQuantV6PublicationArtifact,
                    )
                    or artifact_digest != binding.artifact_sha256
                ):
                    raise _integrity(
                        "persisted manifest artifact identity is missing"
                    )
                payload = _validate_binding(
                    binding,
                    registration=registration,
                    publication=publication,
                    members=members,
                    target_session_dates=target_dates,
                )
                if binding.artifact_sha256 in seen_artifact_digests:
                    raise _integrity(
                        "persisted publication reuses an artifact binding"
                    )
                seen_artifact_digests.add(binding.artifact_sha256)
                if row_count:
                    manifest_digest.update(b",")
                manifest_digest.update(canonical_quant_v6_json(payload))
                row_count += 1
                counts[binding.role] += 1
                if binding.role == _SESSION_INPUT_ROLE:
                    observed_session_ordinals[binding.member_ordinal].add(
                        binding.artifact_ordinal
                    )
                elif binding.role == _EVENT_ROLE:
                    event_session_date = binding.session_date
                    target_ordinal = (
                        target_ordinal_by_date.get(event_session_date)
                        if event_session_date is not None
                        else None
                    )
                    member_ordinal = binding.member_ordinal
                    if (
                        target_ordinal is None
                        or target_ordinal
                        not in expected_session_ordinals[member_ordinal]
                        or target_ordinal
                        not in observed_session_ordinals[member_ordinal]
                        or binding.artifact_ordinal
                        != next_event_ordinals[member_ordinal]
                        or target_ordinal
                        < last_event_session_ordinals[member_ordinal]
                    ):
                        raise _integrity(
                            "persisted event binding conflicts with covered "
                            "session closure"
                        )
                    next_event_ordinals[member_ordinal] += 1
                    last_event_session_ordinals[member_ordinal] = (
                        target_ordinal
                    )
            manifest_digest.update(b'],"contract":"')
            manifest_digest.update(_MANIFEST_CONTRACT.encode("ascii"))
            manifest_digest.update(
                b'","registration_identity_sha256":"'
            )
            manifest_digest.update(registration.identity_sha256.encode("ascii"))
            manifest_digest.update(b'","schema_version":1}')
            acquisition = _require_mapping(
                publication_payload.get("acquisition_outcome"),
                label="publication acquisition outcome",
            )
        if row_count != publication.binding_count:
            raise _integrity("persisted publication binding set is incomplete")
        if (
            counts[_ASSESSMENT_ROLE] != publication.assessment_artifact_count
            or counts[_SESSION_INPUT_ROLE]
            != publication.session_input_artifact_count
            or counts[_EVENT_ROLE] != publication.event_artifact_count
        ):
            raise _integrity("persisted publication binding counts conflict")
        if any(
            frozenset(observed_session_ordinals[ordinal])
            != expected_session_ordinals[ordinal]
            for ordinal in members
        ):
            raise _integrity(
                "persisted session bindings conflict with acquisition coverage"
            )
        if not hmac.compare_digest(
            manifest_digest.hexdigest(),
            publication.manifest_sha256,
        ):
            raise _integrity("persisted publication manifest failed replay")
        return WatchlistQuantV6PublicationDetail(
            publication=_summary(publication, registration),
            registration=_registration_response(registration),
            acquisition_request_start_at=_canonical_timestamp(
                acquisition.get("request_start_at"),
                label="acquisition request start",
            ),
            acquisition_request_end_at=_canonical_timestamp(
                acquisition.get("request_end_at"),
                label="acquisition request end",
            ),
            validation=WatchlistQuantV6ValidationResponse(),
        )

    def list_members(
        self,
        publication_id: int,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> WatchlistQuantV6MemberPage:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        last_ordinal = -1
        cursor_publication_identity: str | None = None
        if cursor is not None:
            state = _decode_cursor(cursor, kind="members")
            if set(state) != {"i", "l", "p"}:
                raise _cursor_error()
            cursor_publication_identity = _cursor_digest(state, "i")
            if _cursor_integer(
                state,
                "p",
                minimum=1,
                maximum=_SQLITE_MAX_INTEGER,
            ) != publication_id:
                raise _cursor_error()
            last_ordinal = _cursor_integer(state, "l")
        with self._db.no_autoflush:
            (
                publication,
                registration,
                registration_payload,
                publication_payload,
            ) = self._publication_pair(publication_id)
            if cursor_publication_identity is not None and not hmac.compare_digest(
                cursor_publication_identity,
                publication.identity_sha256,
            ):
                raise _cursor_error()
            members = _registration_members(
                registration_payload,
                registration=registration,
            )
            acquisitions = _publication_acquisition_members(
                publication_payload,
                registration=registration,
                registration_members=members,
            )
            target_dates = _target_session_dates(registration)
            grids = _scheduled_session_grids(registration)
            total = registration.cohort_member_count
            if last_ordinal >= total:
                raise _cursor_error()
            start = last_ordinal + 1
            stop = min(total, start + limit)
            ordinals = tuple(range(start, stop))
            metadata_rows = tuple(self._db.execute(
                select(
                    WatchlistQuantV6PublicationArtifact,
                    WatchlistQuantV6Artifact.schema_version,
                    WatchlistQuantV6Artifact.kind,
                    WatchlistQuantV6Artifact.codec,
                    WatchlistQuantV6Artifact.compression_level,
                    WatchlistQuantV6Artifact.raw_size,
                    WatchlistQuantV6Artifact.compressed_size,
                    WatchlistQuantV6Artifact.created_at,
                )
                .outerjoin(
                    WatchlistQuantV6Artifact,
                    and_(
                        WatchlistQuantV6Artifact.digest_sha256
                        == WatchlistQuantV6PublicationArtifact.artifact_sha256,
                        WatchlistQuantV6Artifact.kind
                        == WatchlistQuantV6PublicationArtifact.artifact_kind,
                    ),
                )
                .where(
                    WatchlistQuantV6PublicationArtifact.publication_id
                    == publication.id,
                    WatchlistQuantV6PublicationArtifact.role
                    == _ASSESSMENT_ROLE,
                    WatchlistQuantV6PublicationArtifact.member_ordinal.in_(
                        ordinals
                    ),
                )
                .order_by(
                    WatchlistQuantV6PublicationArtifact.member_ordinal
                )
                .limit(len(ordinals) + 1)
            )) if ordinals else ()
        if len(metadata_rows) != len(ordinals):
            raise _integrity("persisted assessment page is incomplete")
        bindings: dict[int, WatchlistQuantV6PublicationArtifact] = {}
        for projected in metadata_rows:
            binding = projected[0]
            if not isinstance(binding, WatchlistQuantV6PublicationArtifact):
                raise _integrity("persisted assessment projection is invalid")
            _validate_binding(
                binding,
                registration=registration,
                publication=publication,
                members=members,
                target_session_dates=target_dates,
            )
            _validate_artifact_metadata(
                digest_sha256=binding.artifact_sha256,
                schema_version=projected[1],
                kind=projected[2],
                codec=projected[3],
                compression_level=projected[4],
                raw_size=projected[5],
                compressed_size=projected[6],
                created_at=projected[7],
                binding=binding,
            )
            if binding.member_ordinal in bindings:
                raise _integrity("persisted assessment member is duplicated")
            bindings[binding.member_ordinal] = binding
        items: list[WatchlistQuantV6MemberSummary] = []
        for ordinal in ordinals:
            binding = bindings.get(ordinal)
            if binding is None:
                raise _integrity("persisted assessment member is missing")
            items.append(_member_summary(
                binding=binding,
                member=members[ordinal],
                acquisition=acquisitions[ordinal],
                grids=grids,
                registration=registration,
            ))
        next_cursor = None
        if stop < total:
            if not ordinals:
                raise _integrity("persisted member page is invalid")
            next_cursor = _encode_cursor(
                "members",
                {
                    "i": publication.identity_sha256,
                    "l": ordinals[-1],
                    "p": publication.id,
                },
            )
        return WatchlistQuantV6MemberPage(
            publication_id=publication.id,
            items=items,
            total=total,
            limit=limit,
            next_cursor=next_cursor,
        )

    def list_bindings(
        self,
        publication_id: int,
        *,
        limit: int,
        cursor: str | None = None,
        member_ordinal: int | None = None,
        role: BindingRole | None = None,
        session_date: date | None = None,
    ) -> WatchlistQuantV6BindingPage:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if member_ordinal is not None and (
            type(member_ordinal) is not int
            or member_ordinal < 0
            or member_ordinal >= _MAX_MEMBERS
        ):
            raise ValueError("member_ordinal is outside the cohort limit")
        if role is not None and role not in _ROLE_RANK:
            raise ValueError("role is unsupported")
        if session_date is not None and type(session_date) is not date:
            raise ValueError("session_date must be a date")
        last_key: tuple[int, int, int] | None = None
        cursor_publication_identity: str | None = None
        if cursor is not None:
            state = _decode_cursor(cursor, kind="bindings")
            expected_date = (
                session_date.isoformat()
                if session_date is not None
                else None
            )
            cursor_member = state.get("m")
            member_filter_matches = (
                cursor_member is None
                if member_ordinal is None
                else (
                    type(cursor_member) is int
                    and cursor_member == member_ordinal
                )
            )
            if (
                set(state) != {
                    "d", "i", "la", "lm", "lr", "m", "p", "r"
                }
                or not member_filter_matches
                or state.get("r") != role
                or state.get("d") != expected_date
                or _cursor_integer(
                    state,
                    "p",
                    minimum=1,
                    maximum=_SQLITE_MAX_INTEGER,
                ) != publication_id
            ):
                raise _cursor_error()
            cursor_publication_identity = _cursor_digest(state, "i")
            last_key = (
                _cursor_integer(state, "lm", maximum=_MAX_MEMBERS - 1),
                _cursor_integer(state, "lr", maximum=2),
                _cursor_integer(state, "la", maximum=_MAX_BINDINGS - 1),
            )
            if (
                member_ordinal is not None
                and last_key[0] != member_ordinal
            ) or (
                role is not None and last_key[1] != _ROLE_RANK[role]
            ):
                raise _cursor_error()
        with self._db.no_autoflush:
            (
                publication,
                registration,
                registration_payload,
                publication_payload,
            ) = self._publication_pair(publication_id)
            if cursor_publication_identity is not None and not hmac.compare_digest(
                cursor_publication_identity,
                publication.identity_sha256,
            ):
                raise _cursor_error()
            members = _registration_members(
                registration_payload,
                registration=registration,
            )
            acquisitions = _publication_acquisition_members(
                publication_payload,
                registration=registration,
                registration_members=members,
            )
            target_dates = _target_session_dates(registration)
            grids = _scheduled_session_grids(registration)
            filters: list[Any] = [
                WatchlistQuantV6PublicationArtifact.publication_id
                == publication.id
            ]
            if member_ordinal is not None:
                filters.append(
                    WatchlistQuantV6PublicationArtifact.member_ordinal
                    == member_ordinal
                )
            if role is not None:
                filters.append(
                    WatchlistQuantV6PublicationArtifact.role == role
                )
            if session_date is not None:
                filters.append(
                    WatchlistQuantV6PublicationArtifact.session_date
                    == session_date
                )
            role_rank = case(
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _ASSESSMENT_ROLE,
                    0,
                ),
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _SESSION_INPUT_ROLE,
                    1,
                ),
                (
                    WatchlistQuantV6PublicationArtifact.role
                    == _EVENT_ROLE,
                    2,
                ),
                else_=3,
            )
            bounded_count = (
                select(WatchlistQuantV6PublicationArtifact.member_ordinal)
                .where(*filters)
                .limit(_MAX_BINDINGS + 1)
                .subquery()
            )
            total = int(self._db.scalar(
                select(func.count()).select_from(bounded_count)
            ) or 0)
            has_binding_filters = any(
                value is not None
                for value in (member_ordinal, role, session_date)
            )
            if total > publication.binding_count or (
                not has_binding_filters
                and total != publication.binding_count
            ):
                raise _integrity("persisted binding count conflicts")
            page_filters = list(filters)
            if last_key is not None:
                last_member, last_role_rank, last_artifact = last_key
                page_filters.append(or_(
                    WatchlistQuantV6PublicationArtifact.member_ordinal
                    > last_member,
                    and_(
                        WatchlistQuantV6PublicationArtifact.member_ordinal
                        == last_member,
                        role_rank > last_role_rank,
                    ),
                    and_(
                        WatchlistQuantV6PublicationArtifact.member_ordinal
                        == last_member,
                        role_rank == last_role_rank,
                        WatchlistQuantV6PublicationArtifact.artifact_ordinal
                        > last_artifact,
                    ),
                ))
            rows = tuple(self._db.execute(
                select(
                    WatchlistQuantV6PublicationArtifact,
                    WatchlistQuantV6Artifact.schema_version,
                    WatchlistQuantV6Artifact.kind,
                    WatchlistQuantV6Artifact.codec,
                    WatchlistQuantV6Artifact.compression_level,
                    WatchlistQuantV6Artifact.raw_size,
                    WatchlistQuantV6Artifact.compressed_size,
                    WatchlistQuantV6Artifact.created_at,
                )
                .outerjoin(
                    WatchlistQuantV6Artifact,
                    and_(
                        WatchlistQuantV6Artifact.digest_sha256
                        == WatchlistQuantV6PublicationArtifact.artifact_sha256,
                        WatchlistQuantV6Artifact.kind
                        == WatchlistQuantV6PublicationArtifact.artifact_kind,
                    ),
                )
                .where(*page_filters)
                .order_by(
                    WatchlistQuantV6PublicationArtifact.member_ordinal,
                    role_rank,
                    WatchlistQuantV6PublicationArtifact.artifact_ordinal,
                )
                .limit(limit + 1)
            ))
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[WatchlistQuantV6BindingResponse] = []
        covered_target_ordinals: dict[int, frozenset[int]] = {}
        target_ordinal_by_date = {
            target_date: ordinal
            for ordinal, target_date in enumerate(target_dates)
        }
        for projected in page_rows:
            binding = projected[0]
            if not isinstance(binding, WatchlistQuantV6PublicationArtifact):
                raise _integrity("persisted binding projection is invalid")
            _validate_binding(
                binding,
                registration=registration,
                publication=publication,
                members=members,
                target_session_dates=target_dates,
            )
            if binding.role == _EVENT_ROLE:
                member_ordinal = binding.member_ordinal
                covered = covered_target_ordinals.get(member_ordinal)
                if covered is None:
                    covered = _member_acquisition(
                        acquisitions[member_ordinal],
                        grids=grids,
                        registration=registration,
                    )[1]
                    covered_target_ordinals[member_ordinal] = covered
                event_session_date = binding.session_date
                target_ordinal = (
                    target_ordinal_by_date.get(event_session_date)
                    if event_session_date is not None
                    else None
                )
                if target_ordinal is None or target_ordinal not in covered:
                    raise _integrity(
                        "persisted event binding is outside acquired coverage"
                    )
            metadata = _validate_artifact_metadata(
                digest_sha256=binding.artifact_sha256,
                schema_version=projected[1],
                kind=projected[2],
                codec=projected[3],
                compression_level=projected[4],
                raw_size=projected[5],
                compressed_size=projected[6],
                created_at=projected[7],
                binding=binding,
            )
            items.append(WatchlistQuantV6BindingResponse(
                publication_id=publication.id,
                member_ordinal=binding.member_ordinal,
                symbol=binding.symbol,
                market=cast(Market, binding.market),
                role=cast(BindingRole, binding.role),
                artifact_ordinal=binding.artifact_ordinal,
                session_date=binding.session_date,
                artifact_sha256=binding.artifact_sha256,
                artifact_kind=cast(ArtifactKind, binding.artifact_kind),
                binding_sha256=binding.binding_sha256,
                artifact_schema_version=metadata[0],
                artifact_codec=cast(Literal["zlib"], metadata[2]),
                artifact_compression_level=cast(Literal[9], metadata[3]),
                artifact_raw_size=metadata[4],
                artifact_compressed_size=metadata[5],
                binding_created_at=_persisted_utc(
                    binding.created_at,
                    label="binding created_at",
                ),
                artifact_created_at=metadata[6],
                binding_identity_verified=True,
            ))
        next_cursor = None
        if has_more:
            if not page_rows:
                raise _integrity("persisted binding page is invalid")
            last_binding = page_rows[-1][0]
            if not isinstance(
                last_binding,
                WatchlistQuantV6PublicationArtifact,
            ) or last_binding.role not in _ROLE_RANK:
                raise _integrity("persisted binding projection is invalid")
            next_cursor = _encode_cursor(
                "bindings",
                {
                    "d": (
                        session_date.isoformat()
                        if session_date is not None
                        else None
                    ),
                    "i": publication.identity_sha256,
                    "la": last_binding.artifact_ordinal,
                    "lm": last_binding.member_ordinal,
                    "lr": _ROLE_RANK[last_binding.role],
                    "m": member_ordinal,
                    "p": publication.id,
                    "r": role,
                },
            )
        return WatchlistQuantV6BindingPage(
            publication_id=publication.id,
            items=items,
            total=total,
            limit=limit,
            next_cursor=next_cursor,
        )

    def get_artifact(
        self,
        publication_id: int,
        digest_sha256: str,
    ) -> WatchlistQuantV6ArtifactResponse:
        if _SHA256_PATTERN.fullmatch(digest_sha256) is None:
            raise ValueError("digest_sha256 must be lowercase SHA-256")
        with self._db.no_autoflush:
            (
                publication,
                registration,
                registration_payload,
                publication_payload,
            ) = self._publication_pair(publication_id)
            members = _registration_members(
                registration_payload,
                registration=registration,
            )
            acquisitions = _publication_acquisition_members(
                publication_payload,
                registration=registration,
                registration_members=members,
            )
            target_dates = _target_session_dates(registration)
            grids = _scheduled_session_grids(registration)
            target_ordinal_by_date = {
                target_date: ordinal
                for ordinal, target_date in enumerate(target_dates)
            }
            covered_target_ordinals: dict[int, frozenset[int]] = {}
            bindings = tuple(self._db.scalars(
                select(WatchlistQuantV6PublicationArtifact)
                .where(
                    WatchlistQuantV6PublicationArtifact.publication_id
                    == publication.id,
                    WatchlistQuantV6PublicationArtifact.artifact_sha256
                    == digest_sha256,
                )
                .order_by(
                    WatchlistQuantV6PublicationArtifact.member_ordinal,
                    WatchlistQuantV6PublicationArtifact.role,
                    WatchlistQuantV6PublicationArtifact.artifact_ordinal,
                )
                .limit(2)
            ))
            if not bindings:
                raise QuantV6ReadNotFoundError(
                    "quant-v6 artifact is not bound to this publication"
                )
            if len(bindings) != 1:
                raise _integrity(
                    "artifact binding fan-out violates the publication contract"
                )
            for binding in bindings:
                _validate_binding(
                    binding,
                    registration=registration,
                    publication=publication,
                    members=members,
                    target_session_dates=target_dates,
                )
                if binding.role == _EVENT_ROLE:
                    member_ordinal = binding.member_ordinal
                    covered = covered_target_ordinals.get(member_ordinal)
                    if covered is None:
                        covered = _member_acquisition(
                            acquisitions[member_ordinal],
                            grids=grids,
                            registration=registration,
                        )[1]
                        covered_target_ordinals[member_ordinal] = covered
                    event_session_date = binding.session_date
                    target_ordinal = (
                        target_ordinal_by_date.get(event_session_date)
                        if event_session_date is not None
                        else None
                    )
                    if (
                        target_ordinal is None
                        or target_ordinal not in covered
                    ):
                        raise _integrity(
                            "persisted event binding is outside acquired "
                            "coverage"
                        )
            artifact_metadata = self._db.execute(
                select(
                    WatchlistQuantV6Artifact.digest_sha256,
                    WatchlistQuantV6Artifact.schema_version,
                    WatchlistQuantV6Artifact.kind,
                    WatchlistQuantV6Artifact.codec,
                    WatchlistQuantV6Artifact.compression_level,
                    WatchlistQuantV6Artifact.raw_size,
                    WatchlistQuantV6Artifact.compressed_size,
                    WatchlistQuantV6Artifact.created_at,
                    func.length(WatchlistQuantV6Artifact.payload),
                ).where(
                    WatchlistQuantV6Artifact.digest_sha256 == digest_sha256
                )
            ).one_or_none()
            if artifact_metadata is None:
                raise _integrity("persisted bound artifact is missing")
            first_binding = bindings[0]
            metadata = _validate_artifact_metadata(
                digest_sha256=artifact_metadata[0],
                schema_version=artifact_metadata[1],
                kind=artifact_metadata[2],
                codec=artifact_metadata[3],
                compression_level=artifact_metadata[4],
                raw_size=artifact_metadata[5],
                compressed_size=artifact_metadata[6],
                created_at=artifact_metadata[7],
                binding=first_binding,
            )
            if (
                artifact_metadata[8] != metadata[5]
                or any(
                    binding.artifact_kind != metadata[1]
                    for binding in bindings
                )
            ):
                raise _integrity("persisted artifact binding metadata conflicts")
            payload_bytes = self._db.scalar(
                select(WatchlistQuantV6Artifact.payload).where(
                    WatchlistQuantV6Artifact.digest_sha256 == digest_sha256
                )
            )
        if type(payload_bytes) is not bytes:
            raise _integrity("persisted bound artifact is missing")
        payload = _decode_artifact_payload(
            digest_sha256=digest_sha256,
            schema_version=metadata[0],
            kind=metadata[1],
            codec=metadata[2],
            raw_size=metadata[4],
            compressed_size=metadata[5],
            payload=payload_bytes,
        )
        _validate_artifact_payload_binding(
            payload,
            kind=metadata[1],
            binding=first_binding,
            target_session_dates=target_dates,
        )
        return WatchlistQuantV6ArtifactResponse(
            publication_id=publication.id,
            digest_sha256=digest_sha256,
            schema_version=metadata[0],
            kind=cast(ArtifactKind, metadata[1]),
            codec=cast(Literal["zlib"], metadata[2]),
            compression_level=cast(Literal[9], metadata[3]),
            raw_size=metadata[4],
            compressed_size=metadata[5],
            created_at=metadata[6],
            binding_count=1,
            payload=payload,
            payload_identity_verified=True,
            bound_to_publication=True,
        )


__all__ = [
    "QuantV6ReadCursorError",
    "QuantV6ReadError",
    "QuantV6ReadIntegrityError",
    "QuantV6ReadNotFoundError",
    "WatchlistQuantV6ReaderService",
]
