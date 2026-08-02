from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Protocol

import app.domain.universe_selection.catalog as catalog_module
import app.domain.universe_selection.membership_history as membership_module
import app.services.watchlist_quant_v6_deadline as deadline_module
import app.services.watchlist_quant_v6_historical_provider as provider_module
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
    IndexCandidate,
    IndexMembershipHistory,
)
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC,
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_ASSESSMENT_SESSIONS,
    QUANT_V6_EVENT_ARTIFACT_KIND,
    MAX_QUANT_V6_ARTIFACT_RAW_BYTES,
    QUANT_V6_SEMANTIC_DIGEST,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
    SESSION_COVERED,
    SESSION_MISSING,
    EncodedQuantV6Artifact,
    QuantV6AssessmentError,
    QuantV6Bar,
    QuantV6SemanticError,
    QuantV6SessionLeaf,
    QuantV6TrainingSession,
    build_bar_next_open_stressed_session_events,
    build_quant_v6_threshold_evidence,
    canonical_quant_v6_json,
    canonical_utc_timestamp,
    quant_v6_evaluator_digest_sha256,
    quant_v6_evaluator_manifest,
    quant_v6_expected_rth_bar_starts,
    quant_v6_consecutive_trading_session_dates,
    quant_v6_payload_sha256,
    quant_v6_previous_trading_session_dates,
    quant_v6_fee_rate,
)
from app.domain.watchlist_quant_v6.assessment import (
    _assess_and_encode_bar_next_open_stressed_window,
)
from app.services.watchlist_quant_v6_historical_provider import (
    QuantV6HistoricalBarFetch,
    quant_v6_historical_provider_contract,
    quant_v6_historical_provider_digest_sha256,
)
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationDeadline,
)


logger = logging.getLogger("auto_trade.watchlist_quant_v6_evaluation_service")


QUANT_V6_REGISTRATION_SCHEMA_VERSION = 1
QUANT_V6_REGISTRATION_CONTRACT = "watchlist-quant-v6-registration-v1"
QUANT_V6_SELECTION_RULE_VERSION = (
    "rotation-research-catalog-active-at-first-target-v1"
)
QUANT_V6_COHORT_SOURCE = "ROTATION_RESEARCH_CATALOG_PIT"
QUANT_V6_HISTORICAL_EVALUATOR_MANIFEST_VERSION = 2
QUANT_V6_BINDING_CONTRACT = "watchlist-quant-v6-artifact-binding-v1"
QUANT_V6_DATA_SETTLEMENT_DELAY = timedelta(minutes=15)

ASSESSMENT_ROLE = "ASSESSMENT"
SESSION_INPUT_ROLE = "SESSION_INPUT"
EVENT_ROLE = "EVENT"
_ROLE_RANK: Mapping[str, int] = MappingProxyType({
    ASSESSMENT_ROLE: 0,
    SESSION_INPUT_ROLE: 1,
    EVENT_ROLE: 2,
})
_SOURCE_KEYS = (
    "app.domain.universe_selection.catalog",
    "app.domain.universe_selection.membership_history",
    "app.services.watchlist_quant_v6_deadline",
    "app.services.watchlist_quant_v6_evaluation_service",
    "app.services.watchlist_quant_v6_historical_provider",
)
_MEMBERSHIP_RESOURCE_KEY = (
    "app/domain/universe_selection/data/index_membership_history.json"
)
_membership_module_path = membership_module.__file__
if _membership_module_path is None:
    raise RuntimeError("membership history module has no repository path")
_MEMBERSHIP_RESOURCE_PATH = (
    Path(_membership_module_path).resolve().parent
    / "data"
    / "index_membership_history.json"
)
try:
    _MEMBERSHIP_RESOURCE_SHA256 = hashlib.sha256(
        _MEMBERSHIP_RESOURCE_PATH.read_bytes()
    ).hexdigest()
except OSError as exc:
    raise RuntimeError(
        "membership history repository resource is unavailable"
    ) from exc


class QuantV6HistoricalEvaluationError(RuntimeError):
    """Raised when a frozen historical cohort cannot be evaluated safely."""


class QuantV6HistoricalProvider(Protocol):
    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch: ...


@dataclass(frozen=True)
class QuantV6CohortMember:
    ordinal: int
    symbol: str
    market: str
    alias: str
    sector: str
    memberships: tuple[str, ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "market": self.market,
            "memberships": list(self.memberships),
            "ordinal": self.ordinal,
            "sector": self.sector,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class QuantV6RegistrationPlan:
    identity_sha256: str
    registration_json: str
    evaluator_digest_sha256: str
    acquisition_spec_sha256: str
    source_snapshot_sha256: str
    cohort_manifest_sha256: str
    schedule_sha256: str
    market: str
    members: tuple[QuantV6CohortMember, ...]
    training_session_dates: tuple[date, ...]
    target_session_dates: tuple[date, ...]
    data_cutoff_at: datetime
    cohort_observed_at: datetime


@dataclass(frozen=True)
class QuantV6PendingArtifactBinding:
    member_ordinal: int
    symbol: str
    market: str
    role: str
    artifact_ordinal: int
    session_date: date | None
    artifact: EncodedQuantV6Artifact
    binding_sha256: str

    def manifest_payload(self) -> dict[str, object]:
        return {
            "artifact_kind": self.artifact.kind,
            "artifact_ordinal": self.artifact_ordinal,
            "artifact_sha256": self.artifact.digest_sha256,
            "binding_sha256": self.binding_sha256,
            "market": self.market,
            "member_ordinal": self.member_ordinal,
            "role": self.role,
            "session_date": (
                self.session_date.isoformat()
                if self.session_date is not None
                else None
            ),
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class QuantV6CandidateEvaluation:
    member: QuantV6CohortMember
    recommended_action: str
    covered_sessions: int
    event_count: int
    event_sessions: int
    blockers: tuple[str, ...]
    assessment_artifact_sha256: str
    bindings: tuple[QuantV6PendingArtifactBinding, ...]
    fetched_pages: int
    fetched_raw_rows: int
    fetched_accepted_bars: int
    fetched_bar_starts: tuple[datetime, ...]
    rejected_rows: int


@dataclass(frozen=True)
class _VerifiedCandidateFetchRequest:
    registration: QuantV6RegistrationPlan
    member: QuantV6CohortMember
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class _CompletedCandidateFetch:
    request: _VerifiedCandidateFetchRequest
    fetched: QuantV6HistoricalBarFetch
    fetch_ms: int


def _normalized_source_sha256(module: ModuleType) -> str:
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError) as exc:
        raise QuantV6HistoricalEvaluationError(
            f"source is unavailable for {module.__name__}"
        ) from exc
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in source.split("\n"))
    return hashlib.sha256(f"{normalized.rstrip()}\n".encode("utf-8")).hexdigest()


def quant_v6_historical_evaluator_manifest() -> dict[str, object]:
    modules: Mapping[str, ModuleType] = {
        "app.domain.universe_selection.catalog": catalog_module,
        "app.domain.universe_selection.membership_history": membership_module,
        "app.services.watchlist_quant_v6_evaluation_service": sys.modules[__name__],
        "app.services.watchlist_quant_v6_deadline": deadline_module,
        "app.services.watchlist_quant_v6_historical_provider": provider_module,
    }
    if tuple(sorted(modules)) != _SOURCE_KEYS:
        raise QuantV6HistoricalEvaluationError(
            "historical evaluator source closure is incomplete"
        )
    return {
        "domain_evaluator_digest_sha256": (
            quant_v6_evaluator_digest_sha256()
        ),
        "domain_evaluator_manifest": quant_v6_evaluator_manifest(),
        "manifest_version": QUANT_V6_HISTORICAL_EVALUATOR_MANIFEST_VERSION,
        "provider_contract_digest_sha256": (
            quant_v6_historical_provider_digest_sha256()
        ),
        "resource_sha256": {
            _MEMBERSHIP_RESOURCE_KEY: _MEMBERSHIP_RESOURCE_SHA256,
        },
        "source_sha256": {
            key: _normalized_source_sha256(modules[key])
            for key in _SOURCE_KEYS
        },
    }


@lru_cache(maxsize=1)
def quant_v6_historical_evaluator_digest_sha256() -> str:
    return quant_v6_payload_sha256(quant_v6_historical_evaluator_manifest())


def quant_v6_registration_acquisition_spec() -> dict[str, object]:
    provider_contract = quant_v6_historical_provider_contract()
    return {
        "domain_acquisition_spec": dict(QUANT_V6_ACQUISITION_SPEC),
        "domain_acquisition_spec_sha256": QUANT_V6_ACQUISITION_SPEC_DIGEST,
        "provider_contract": provider_contract,
        "provider_contract_sha256": (
            quant_v6_historical_provider_digest_sha256()
        ),
        "schema_version": 1,
    }


def _last_complete_session_date(market: str, observed_at: datetime) -> date:
    observed = observed_at.astimezone(timezone.utc)
    cursor = observed.date() + timedelta(days=1)
    for _ in range(370):
        cursor -= timedelta(days=1)
        starts = quant_v6_expected_rth_bar_starts(market, cursor)
        if not starts:
            continue
        complete_after = starts[-1] + timedelta(minutes=5) + (
            QUANT_V6_DATA_SETTLEMENT_DELAY
        )
        if observed >= complete_after:
            return cursor
    raise QuantV6HistoricalEvaluationError(
        "no complete session exists inside calendar coverage"
    )


def _catalog_member_payload(candidate: IndexCandidate) -> dict[str, object]:
    return {
        "alias": candidate.alias,
        "market": candidate.market,
        "memberships": list(candidate.memberships),
        "sector": candidate.sector,
        "symbol": candidate.symbol,
    }


def _canonical_snapshot_value(value: object) -> object:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {
            key: _canonical_snapshot_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_snapshot_value(item) for item in value]
    return value


def _membership_history_payload(
    membership_history: IndexMembershipHistory,
) -> dict[str, object]:
    return {
        "catalog_snapshot_date": (
            membership_history.catalog_snapshot_date.isoformat()
        ),
        "effective_start_date": (
            membership_history.effective_start_date.isoformat()
        ),
        "intervals": {
            membership: {
                symbol: [
                    [
                        interval.start.isoformat(),
                        (
                            interval.end.isoformat()
                            if interval.end is not None
                            else ""
                        ),
                    ]
                    for interval in intervals
                ]
                for symbol, intervals in symbols.items()
            }
            for membership, symbols in membership_history.intervals.items()
        },
        "snapshot_overrides": {
            membership: {
                symbol: start.isoformat()
                for symbol, start in symbols.items()
            }
            for membership, symbols in (
                membership_history.snapshot_overrides.items()
            )
        },
        "source_version": membership_history.source_version,
        "sources": [dict(source) for source in membership_history.sources],
    }


def _build_registration_plan(
    *,
    observed_at: datetime,
    market: str,
    candidates: Sequence[IndexCandidate],
    membership_history: IndexMembershipHistory,
) -> QuantV6RegistrationPlan:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise QuantV6HistoricalEvaluationError(
            "cohort observation timestamp must be timezone-aware"
        )
    observed = observed_at.astimezone(timezone.utc)
    last_target = _last_complete_session_date(market, observed)
    preceding_targets = quant_v6_previous_trading_session_dates(
        market,
        last_target,
        count=QUANT_V6_ASSESSMENT_SESSIONS - 1,
    )
    target_dates = (*preceding_targets, last_target)
    first_target = target_dates[0]
    training_dates = quant_v6_previous_trading_session_dates(
        market,
        first_target,
        count=QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
    )
    catalog = tuple(sorted(candidates, key=lambda item: item.symbol))
    if len({candidate.symbol for candidate in catalog}) != len(catalog):
        raise QuantV6HistoricalEvaluationError(
            "rotation research catalog contains duplicate symbols"
        )
    selected = tuple(
        candidate
        for candidate in catalog
        if candidate.market == market
        and membership_history.is_active(candidate, first_target)
    )
    if not selected:
        raise QuantV6HistoricalEvaluationError(
            "point-in-time cohort contains no candidates"
        )
    members = tuple(
        QuantV6CohortMember(
            ordinal=ordinal,
            symbol=candidate.symbol,
            market=candidate.market,
            alias=candidate.alias,
            sector=candidate.sector,
            memberships=tuple(candidate.memberships),
        )
        for ordinal, candidate in enumerate(selected)
    )
    member_payloads = [member.canonical_payload() for member in members]
    cohort_manifest_sha256 = quant_v6_payload_sha256({
        "members": member_payloads,
        "schema_version": 1,
    })
    source_snapshot = {
        "catalog_members": [
            _catalog_member_payload(candidate) for candidate in catalog
        ],
        "catalog_source_version": CATALOG_SOURCE_VERSION,
        "membership_history": _membership_history_payload(
            membership_history
        ),
        "membership_metadata": _canonical_snapshot_value(
            membership_history.metadata(catalog)
        ),
        "membership_resource_sha256": _MEMBERSHIP_RESOURCE_SHA256,
        "schema_version": 1,
    }
    source_snapshot_sha256 = quant_v6_payload_sha256(source_snapshot)
    schedule = {
        "market": market,
        "schema_version": 1,
        "target_session_dates": [value.isoformat() for value in target_dates],
        "training_session_dates": [
            value.isoformat() for value in training_dates
        ],
    }
    schedule_sha256 = quant_v6_payload_sha256(schedule)
    acquisition = quant_v6_registration_acquisition_spec()
    acquisition_digest = quant_v6_payload_sha256(acquisition)
    evaluator_digest = quant_v6_historical_evaluator_digest_sha256()
    last_grid = quant_v6_expected_rth_bar_starts(market, last_target)
    data_cutoff_at = last_grid[-1] + timedelta(minutes=5)
    cohort_observed_at = data_cutoff_at + QUANT_V6_DATA_SETTLEMENT_DELAY
    registration_payload = {
        "acquisition": acquisition,
        "acquisition_spec_sha256": acquisition_digest,
        "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
        "cohort": {
            "cohort_manifest_sha256": cohort_manifest_sha256,
            "cohort_source": QUANT_V6_COHORT_SOURCE,
            "member_count": len(members),
            "members": member_payloads,
            "selection_rule_version": QUANT_V6_SELECTION_RULE_VERSION,
        },
        "contract": QUANT_V6_REGISTRATION_CONTRACT,
        "cohort_observed_at": canonical_utc_timestamp(cohort_observed_at),
        "data_cutoff_at": canonical_utc_timestamp(data_cutoff_at),
        "evaluator_digest_sha256": evaluator_digest,
        "evaluator_manifest": quant_v6_historical_evaluator_manifest(),
        "market": market,
        "policy": {
            "automatic_promotion_allowed": False,
            "order_submission_allowed": False,
            "position_add_on_allowed": False,
            "short_entry_allowed": False,
        },
        "schedule": schedule,
        "schedule_sha256": schedule_sha256,
        "schema_version": QUANT_V6_REGISTRATION_SCHEMA_VERSION,
        "semantic_digest_sha256": QUANT_V6_SEMANTIC_DIGEST,
        "source_snapshot": source_snapshot,
        "source_snapshot_sha256": source_snapshot_sha256,
    }
    raw_registration = canonical_quant_v6_json(registration_payload)
    return QuantV6RegistrationPlan(
        identity_sha256=hashlib.sha256(raw_registration).hexdigest(),
        registration_json=raw_registration.decode("utf-8"),
        evaluator_digest_sha256=evaluator_digest,
        acquisition_spec_sha256=acquisition_digest,
        source_snapshot_sha256=source_snapshot_sha256,
        cohort_manifest_sha256=cohort_manifest_sha256,
        schedule_sha256=schedule_sha256,
        market=market,
        members=members,
        training_session_dates=tuple(training_dates),
        target_session_dates=tuple(target_dates),
        data_cutoff_at=data_cutoff_at,
        cohort_observed_at=cohort_observed_at,
    )


def build_latest_quant_v6_registration_plan(
    *,
    observed_at: datetime,
    market: str = "US",
) -> QuantV6RegistrationPlan:
    """Build the server-owned PIT cohort; callers cannot supply symbols."""
    return _build_registration_plan(
        observed_at=observed_at,
        market=market,
        candidates=ROTATION_RESEARCH_CANDIDATE_CATALOG,
        membership_history=INDEX_MEMBERSHIP_HISTORY,
    )


def validate_quant_v6_registration_plan(
    registration: QuantV6RegistrationPlan,
) -> None:
    if type(registration) is not QuantV6RegistrationPlan:
        raise QuantV6HistoricalEvaluationError(
            "registration plan has an unsupported type"
        )
    if any(type(member) is not QuantV6CohortMember for member in registration.members):
        raise QuantV6HistoricalEvaluationError(
            "registration contains an unsupported member type"
        )
    if (
        type(registration.market) is not str
        or registration.market not in {"US", "HK"}
        or type(registration.members) is not tuple
        or not registration.members
        or type(registration.training_session_dates) is not tuple
        or type(registration.target_session_dates) is not tuple
        or len(registration.training_session_dates)
        != QUANT_V6_THRESHOLD_TRAINING_SESSIONS
        or len(registration.target_session_dates)
        != QUANT_V6_ASSESSMENT_SESSIONS
        or any(
            type(value) is not date
            for value in (
                *registration.training_session_dates,
                *registration.target_session_dates,
            )
        )
        or type(registration.cohort_observed_at) is not datetime
        or registration.cohort_observed_at.tzinfo is None
        or registration.cohort_observed_at.utcoffset() is None
        or type(registration.data_cutoff_at) is not datetime
        or registration.data_cutoff_at.tzinfo is None
        or registration.data_cutoff_at.utcoffset() is None
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration schedule envelope is invalid"
        )
    if (
        [member.ordinal for member in registration.members]
        != list(range(len(registration.members)))
        or len({member.symbol for member in registration.members})
        != len(registration.members)
        or any(
            member.market != registration.market
            or type(member.symbol) is not str
            or member.symbol != member.symbol.strip().upper()
            or not member.symbol.endswith(f".{registration.market}")
            for member in registration.members
        )
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration member envelope is invalid"
        )
    try:
        expected_training_dates = quant_v6_previous_trading_session_dates(
            registration.market,
            registration.target_session_dates[0],
            count=QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
        )
        expected_target_dates = quant_v6_consecutive_trading_session_dates(
            registration.market,
            registration.target_session_dates[0],
            count=QUANT_V6_ASSESSMENT_SESSIONS,
        )
        expected_last_target = _last_complete_session_date(
            registration.market,
            registration.cohort_observed_at,
        )
        last_grid = quant_v6_expected_rth_bar_starts(
            registration.market,
            registration.target_session_dates[-1],
        )
    except (QuantV6SemanticError, IndexError) as exc:
        raise QuantV6HistoricalEvaluationError(
            "registration schedule is not calendar-valid"
        ) from exc
    if (
        registration.training_session_dates != expected_training_dates
        or registration.target_session_dates != expected_target_dates
        or registration.target_session_dates[-1] != expected_last_target
        or not last_grid
        or registration.data_cutoff_at.astimezone(timezone.utc)
        != last_grid[-1] + timedelta(minutes=5)
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration schedule failed canonical replay"
        )
    raw = registration.registration_json.encode("utf-8")
    if not raw or len(raw) > MAX_QUANT_V6_ARTIFACT_RAW_BYTES:
        raise QuantV6HistoricalEvaluationError(
            "registration JSON is outside the size limit"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise QuantV6HistoricalEvaluationError(
            "registration JSON is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise QuantV6HistoricalEvaluationError(
            "registration JSON root must be an object"
        )
    try:
        if canonical_quant_v6_json(payload) != raw:
            raise QuantV6HistoricalEvaluationError(
                "registration JSON is not canonical"
            )
    except QuantV6HistoricalEvaluationError:
        raise
    except Exception as exc:
        raise QuantV6HistoricalEvaluationError(
            "registration JSON violates the canonical contract"
        ) from exc
    actual_identity = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual_identity, registration.identity_sha256):
        raise QuantV6HistoricalEvaluationError(
            "registration identity digest mismatch"
        )

    members = [member.canonical_payload() for member in registration.members]
    cohort = payload.get("cohort")
    schedule = payload.get("schedule")
    acquisition = payload.get("acquisition")
    source_snapshot = payload.get("source_snapshot")
    if not all(
        isinstance(value, dict)
        for value in (cohort, schedule, acquisition, source_snapshot)
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration evidence sections are invalid"
        )
    assert isinstance(cohort, dict)
    assert isinstance(schedule, dict)
    assert isinstance(acquisition, dict)
    assert isinstance(source_snapshot, dict)
    expected_cohort_digest = quant_v6_payload_sha256({
        "members": members,
        "schema_version": 1,
    })
    expected_schedule = {
        "market": registration.market,
        "schema_version": 1,
        "target_session_dates": [
            value.isoformat() for value in registration.target_session_dates
        ],
        "training_session_dates": [
            value.isoformat() for value in registration.training_session_dates
        ],
    }
    expected_schedule_digest = quant_v6_payload_sha256(expected_schedule)
    expected_acquisition = quant_v6_registration_acquisition_spec()
    expected_acquisition_digest = quant_v6_payload_sha256(expected_acquisition)
    expected_evaluator_manifest = quant_v6_historical_evaluator_manifest()
    expected_evaluator_digest = quant_v6_historical_evaluator_digest_sha256()
    if (
        payload.get("contract") != QUANT_V6_REGISTRATION_CONTRACT
        or payload.get("schema_version") != QUANT_V6_REGISTRATION_SCHEMA_VERSION
        or payload.get("algorithm_version") != QUANT_V6_ALGORITHM_VERSION
        or payload.get("market") != registration.market
        or payload.get("semantic_digest_sha256") != QUANT_V6_SEMANTIC_DIGEST
        or payload.get("evaluator_manifest") != expected_evaluator_manifest
        or payload.get("evaluator_digest_sha256") != expected_evaluator_digest
        or registration.evaluator_digest_sha256 != expected_evaluator_digest
        or acquisition != expected_acquisition
        or payload.get("acquisition_spec_sha256") != expected_acquisition_digest
        or registration.acquisition_spec_sha256 != expected_acquisition_digest
        or cohort.get("cohort_source") != QUANT_V6_COHORT_SOURCE
        or cohort.get("selection_rule_version") != QUANT_V6_SELECTION_RULE_VERSION
        or cohort.get("member_count") != len(members)
        or cohort.get("members") != members
        or cohort.get("cohort_manifest_sha256") != expected_cohort_digest
        or registration.cohort_manifest_sha256 != expected_cohort_digest
        or schedule != expected_schedule
        or payload.get("schedule_sha256") != expected_schedule_digest
        or registration.schedule_sha256 != expected_schedule_digest
        or payload.get("source_snapshot_sha256")
        != quant_v6_payload_sha256(source_snapshot)
        or registration.source_snapshot_sha256
        != quant_v6_payload_sha256(source_snapshot)
        or payload.get("data_cutoff_at")
        != canonical_utc_timestamp(registration.data_cutoff_at)
        or payload.get("cohort_observed_at")
        != canonical_utc_timestamp(registration.cohort_observed_at)
        or payload.get("policy") != {
            "automatic_promotion_allowed": False,
            "order_submission_allowed": False,
            "position_add_on_allowed": False,
            "short_entry_allowed": False,
        }
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration failed canonical replay"
        )
    if (
        registration.cohort_observed_at.tzinfo is None
        or registration.cohort_observed_at.utcoffset() is None
        or registration.cohort_observed_at.astimezone(timezone.utc)
        != registration.data_cutoff_at + QUANT_V6_DATA_SETTLEMENT_DELAY
    ):
        raise QuantV6HistoricalEvaluationError(
            "registration predates the frozen data settlement cutoff"
        )


def _binding(
    *,
    registration_identity_sha256: str,
    member: QuantV6CohortMember,
    role: str,
    artifact_ordinal: int,
    session_date: date | None,
    artifact: EncodedQuantV6Artifact,
) -> QuantV6PendingArtifactBinding:
    if role not in _ROLE_RANK:
        raise QuantV6HistoricalEvaluationError("unsupported artifact binding role")
    preimage = {
        "artifact_kind": artifact.kind,
        "artifact_ordinal": artifact_ordinal,
        "artifact_sha256": artifact.digest_sha256,
        "contract": QUANT_V6_BINDING_CONTRACT,
        "market": member.market,
        "member_ordinal": member.ordinal,
        "registration_identity_sha256": registration_identity_sha256,
        "role": role,
        "schema_version": 1,
        "session_date": session_date.isoformat() if session_date else None,
        "symbol": member.symbol,
    }
    return QuantV6PendingArtifactBinding(
        member_ordinal=member.ordinal,
        symbol=member.symbol,
        market=member.market,
        role=role,
        artifact_ordinal=artifact_ordinal,
        session_date=session_date,
        artifact=artifact,
        binding_sha256=quant_v6_payload_sha256(preimage),
    )


def _complete_session_bars(
    fetched_bars: Sequence[QuantV6Bar],
    *,
    market: str,
    session_date: date,
) -> tuple[QuantV6Bar, ...] | None:
    expected = quant_v6_expected_rth_bar_starts(market, session_date)
    expected_set = frozenset(expected)
    selected = tuple(
        bar for bar in fetched_bars if bar.start_at in expected_set
    )
    if tuple(bar.start_at for bar in selected) != expected:
        return None
    return selected


def _candidate_fetch_window(
    registration: QuantV6RegistrationPlan,
) -> tuple[datetime, datetime]:
    all_dates = (
        *registration.training_session_dates,
        *registration.target_session_dates,
    )
    first_grid = quant_v6_expected_rth_bar_starts(
        registration.market,
        all_dates[0],
    )
    last_grid = quant_v6_expected_rth_bar_starts(
        registration.market,
        all_dates[-1],
    )
    return first_grid[0], last_grid[-1] + timedelta(minutes=5)


def _verified_candidate_fetch_request_from_validated_registration(
    *,
    registration: QuantV6RegistrationPlan,
    member: QuantV6CohortMember,
) -> _VerifiedCandidateFetchRequest:
    if type(member) is not QuantV6CohortMember:
        raise QuantV6HistoricalEvaluationError(
            "candidate member has an unsupported type"
        )
    if (
        member.ordinal < 0
        or member.ordinal >= len(registration.members)
        or registration.members[member.ordinal].canonical_payload()
        != member.canonical_payload()
    ):
        raise QuantV6HistoricalEvaluationError(
            "candidate is outside the frozen registration"
        )
    start_at, end_at = _candidate_fetch_window(registration)
    return _VerifiedCandidateFetchRequest(
        registration=registration,
        member=member,
        start_at=start_at,
        end_at=end_at,
    )


def _verified_candidate_fetch_request(
    *,
    registration: QuantV6RegistrationPlan,
    member: QuantV6CohortMember,
    evaluation_deadline: QuantV6EvaluationDeadline | None,
) -> _VerifiedCandidateFetchRequest:
    if evaluation_deadline is not None:
        evaluation_deadline.checkpoint()
    validate_quant_v6_registration_plan(registration)
    return _verified_candidate_fetch_request_from_validated_registration(
        registration=registration,
        member=member,
    )


def _fetch_quant_v6_candidate(
    *,
    request: _VerifiedCandidateFetchRequest,
    provider: QuantV6HistoricalProvider,
) -> _CompletedCandidateFetch:
    if type(request) is not _VerifiedCandidateFetchRequest:
        raise QuantV6HistoricalEvaluationError(
            "candidate fetch request has an unsupported type"
        )
    started_at = time.monotonic_ns()
    fetched = provider.fetch_five_minute_no_adjust(
        request.member.symbol,
        start_at=request.start_at,
        end_at=request.end_at,
    )
    if type(fetched) is not QuantV6HistoricalBarFetch:
        raise QuantV6HistoricalEvaluationError(
            "historical provider returned an unsupported fetch type"
        )
    elapsed_ns = max(0, time.monotonic_ns() - started_at)
    return _CompletedCandidateFetch(
        request=request,
        fetched=fetched,
        fetch_ms=elapsed_ns // 1_000_000,
    )


def _evaluate_candidate_from_fetch(
    *,
    completed_fetch: _CompletedCandidateFetch,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> QuantV6CandidateEvaluation:
    if type(completed_fetch) is not _CompletedCandidateFetch:
        raise QuantV6HistoricalEvaluationError(
            "completed candidate fetch has an unsupported type"
        )
    request = completed_fetch.request
    if type(request) is not _VerifiedCandidateFetchRequest:
        raise QuantV6HistoricalEvaluationError(
            "completed candidate request has an unsupported type"
        )
    registration = request.registration
    member = request.member
    fetched = completed_fetch.fetched
    if evaluation_deadline is not None:
        evaluation_deadline.checkpoint()
    all_dates = (
        *registration.training_session_dates,
        *registration.target_session_dates,
    )
    complete_by_date: dict[date, tuple[QuantV6Bar, ...] | None] = {}
    for session_date in all_dates:
        if evaluation_deadline is not None:
            evaluation_deadline.checkpoint()
        complete_by_date[session_date] = _complete_session_bars(
            fetched.bars,
            market=registration.market,
            session_date=session_date,
        )
    if evaluation_deadline is not None:
        evaluation_deadline.checkpoint()
    leaves: list[QuantV6SessionLeaf] = []
    session_artifacts: list[tuple[int, date, EncodedQuantV6Artifact]] = []
    event_artifacts: list[tuple[int, date, EncodedQuantV6Artifact]] = []
    event_ordinal = 0
    for target_ordinal, target_date in enumerate(
        registration.target_session_dates
    ):
        if evaluation_deadline is not None:
            evaluation_deadline.checkpoint()
        prior_dates = quant_v6_previous_trading_session_dates(
            registration.market,
            target_date,
            count=QUANT_V6_THRESHOLD_TRAINING_SESSIONS,
        )
        target_bars = complete_by_date[target_date]
        training_bars = tuple(complete_by_date[value] for value in prior_dates)
        blockers: list[str] = []
        if target_bars is None:
            blockers.append("MISSING_COMPLETE_TARGET_BAR_INPUT")
        if any(value is None for value in training_bars):
            blockers.append("MISSING_COMPLETE_TRAINING_BAR_INPUT")
        if blockers:
            leaves.append(QuantV6SessionLeaf(
                session_date=target_date,
                status=SESSION_MISSING,
                blockers=tuple(blockers),
            ))
            continue
        exact_target_bars = tuple(target_bars or ())
        exact_training_bars = tuple(training_bars)
        try:
            training_session_values: list[QuantV6TrainingSession] = []
            for value, bars in zip(
                prior_dates,
                exact_training_bars,
                strict=True,
            ):
                if evaluation_deadline is not None:
                    evaluation_deadline.checkpoint()
                if bars is None:
                    continue
                training_session_values.append(QuantV6TrainingSession(
                    session_date=value,
                    bars=bars,
                ))
            training_sessions = tuple(training_session_values)
            if evaluation_deadline is not None:
                evaluation_deadline.checkpoint()
            threshold = build_quant_v6_threshold_evidence(
                symbol=member.symbol,
                market=member.market,
                target_session_date=target_date,
                training_sessions=training_sessions,
            )
            if evaluation_deadline is not None:
                evaluation_deadline.checkpoint()
            events = build_bar_next_open_stressed_session_events(
                symbol=member.symbol,
                market=member.market,
                session_date=target_date,
                bars=exact_target_bars,
                threshold_evidence=threshold,
                fee_rate=quant_v6_fee_rate(member.market),
            )
            if evaluation_deadline is not None:
                evaluation_deadline.checkpoint()
            leaf = QuantV6SessionLeaf(
                session_date=target_date,
                status=SESSION_COVERED,
                session_bars=exact_target_bars,
                threshold_evidence=threshold,
                fee_rate=quant_v6_fee_rate(member.market),
                events=events,
            )
        except QuantV6SemanticError as exc:
            # A complete timestamp grid must be replayable from provider-owned
            # ``QuantV6Bar`` values. Treat a semantic failure as an acquisition
            # failure instead of emitting an unauditable MISSING declaration.
            raise QuantV6HistoricalEvaluationError(
                "candidate canonical replay input is invalid"
            ) from exc
        leaves.append(leaf)
        session_artifacts.append((
            target_ordinal,
            target_date,
            leaf.encoded_replay_input(
                symbol=member.symbol,
                market=member.market,
                checkpoint=(
                    evaluation_deadline.checkpoint
                    if evaluation_deadline is not None
                    else None
                ),
            ),
        ))
        for event in events:
            if evaluation_deadline is not None:
                evaluation_deadline.checkpoint()
            event_artifacts.append((
                event_ordinal,
                target_date,
                event.encoded_artifact(),
            ))
            event_ordinal += 1
    try:
        assessment, assessment_artifact = (
            _assess_and_encode_bar_next_open_stressed_window(
                symbol=member.symbol,
                market=member.market,
                leaves=leaves,
                checkpoint=(
                    evaluation_deadline.checkpoint
                    if evaluation_deadline is not None
                    else None
                ),
            )
        )
    except QuantV6AssessmentError as exc:
        raise QuantV6HistoricalEvaluationError(
            "candidate assessment failed canonical replay"
        ) from exc
    bindings = [
        _binding(
            registration_identity_sha256=registration.identity_sha256,
            member=member,
            role=ASSESSMENT_ROLE,
            artifact_ordinal=0,
            session_date=None,
            artifact=assessment_artifact,
        )
    ]
    for ordinal, session_date, artifact in session_artifacts:
        if evaluation_deadline is not None:
            evaluation_deadline.checkpoint()
        bindings.append(_binding(
            registration_identity_sha256=registration.identity_sha256,
            member=member,
            role=SESSION_INPUT_ROLE,
            artifact_ordinal=ordinal,
            session_date=session_date,
            artifact=artifact,
        ))
    for ordinal, session_date, artifact in event_artifacts:
        if evaluation_deadline is not None:
            evaluation_deadline.checkpoint()
        bindings.append(_binding(
            registration_identity_sha256=registration.identity_sha256,
            member=member,
            role=EVENT_ROLE,
            artifact_ordinal=ordinal,
            session_date=session_date,
            artifact=artifact,
        ))
    ordered_bindings = tuple(sorted(
        bindings,
        key=lambda item: (
            _ROLE_RANK[item.role],
            item.artifact_ordinal,
        ),
    ))
    if evaluation_deadline is not None:
        evaluation_deadline.checkpoint()
    return QuantV6CandidateEvaluation(
        member=member,
        recommended_action=assessment.recommended_action,
        covered_sessions=assessment.covered_sessions,
        event_count=assessment.event_count,
        event_sessions=assessment.event_sessions,
        blockers=assessment.blockers,
        assessment_artifact_sha256=assessment_artifact.digest_sha256,
        bindings=ordered_bindings,
        fetched_pages=fetched.pages,
        fetched_raw_rows=fetched.raw_rows,
        fetched_accepted_bars=len(fetched.bars),
        fetched_bar_starts=tuple(bar.start_at for bar in fetched.bars),
        rejected_rows=fetched.rejected_rows,
    )


def evaluate_quant_v6_candidate(
    *,
    registration: QuantV6RegistrationPlan,
    member: QuantV6CohortMember,
    provider: QuantV6HistoricalProvider,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> QuantV6CandidateEvaluation:
    """Synchronously fetch and evaluate one frozen cohort member."""
    request = _verified_candidate_fetch_request(
        registration=registration,
        member=member,
        evaluation_deadline=evaluation_deadline,
    )
    completed_fetch = _fetch_quant_v6_candidate(
        request=request,
        provider=provider,
    )
    return _evaluate_candidate_from_fetch(
        completed_fetch=completed_fetch,
        evaluation_deadline=evaluation_deadline,
    )


def _log_candidate_start(
    request: _VerifiedCandidateFetchRequest,
    *,
    total: int,
) -> None:
    logger.info(
        "quant-v6 candidate fetch started ordinal=%d ordinal_base=0 total=%d",
        request.member.ordinal,
        total,
    )


def _log_candidate_complete(
    completed_fetch: _CompletedCandidateFetch,
    evaluation: QuantV6CandidateEvaluation,
    *,
    total: int,
    compute_ms: int,
) -> None:
    logger.info(
        "quant-v6 candidate evaluation completed ordinal=%d ordinal_base=0 "
        "completed_count=%d total=%d "
        "fetch_ms=%d compute_ms=%d pages=%d rows=%d bars=%d events=%d",
        evaluation.member.ordinal,
        evaluation.member.ordinal + 1,
        total,
        completed_fetch.fetch_ms,
        compute_ms,
        completed_fetch.fetched.pages,
        completed_fetch.fetched.raw_rows,
        len(completed_fetch.fetched.bars),
        evaluation.event_count,
    )


def _evaluate_completed_candidate_with_logging(
    *,
    completed_fetch: _CompletedCandidateFetch,
    total: int,
    evaluation_deadline: QuantV6EvaluationDeadline | None,
) -> QuantV6CandidateEvaluation:
    started_at = time.monotonic_ns()
    evaluation = _evaluate_candidate_from_fetch(
        completed_fetch=completed_fetch,
        evaluation_deadline=evaluation_deadline,
    )
    compute_ns = max(0, time.monotonic_ns() - started_at)
    _log_candidate_complete(
        completed_fetch,
        evaluation,
        total=total,
        compute_ms=compute_ns // 1_000_000,
    )
    return evaluation


def _await_candidate_fetch(
    future: Future[_CompletedCandidateFetch],
    *,
    evaluation_deadline: QuantV6EvaluationDeadline,
) -> _CompletedCandidateFetch:
    while True:
        wait_seconds = min(
            0.1,
            evaluation_deadline.remaining_seconds(),
        )
        done, _not_done = wait((future,), timeout=wait_seconds)
        if future in done:
            return future.result()
        evaluation_deadline.checkpoint()


def _cancel_and_drain_candidate_fetch(
    future: Future[_CompletedCandidateFetch] | None,
    *,
    evaluation_deadline: QuantV6EvaluationDeadline,
) -> None:
    if future is None or future.done():
        return
    if future.cancel():
        return
    if future.done():
        return
    # The production provider observes this same deadline from every bounded
    # SDK wait. Cancelling the doomed tick makes the sole look-ahead future
    # return promptly without widening the provider protocol.
    evaluation_deadline.cancel()
    while True:
        done, _not_done = wait((future,), timeout=0.1)
        if future in done:
            break
    try:
        future.result()
    except BaseException:
        return


def _evaluate_quant_v6_registration_sequential(
    *,
    registration: QuantV6RegistrationPlan,
    provider: QuantV6HistoricalProvider,
) -> tuple[QuantV6CandidateEvaluation, ...]:
    evaluations: list[QuantV6CandidateEvaluation] = []
    total = len(registration.members)
    for member in registration.members:
        request = _verified_candidate_fetch_request(
            registration=registration,
            member=member,
            evaluation_deadline=None,
        )
        _log_candidate_start(request, total=total)
        completed_fetch = _fetch_quant_v6_candidate(
            request=request,
            provider=provider,
        )
        evaluations.append(_evaluate_completed_candidate_with_logging(
            completed_fetch=completed_fetch,
            total=total,
            evaluation_deadline=None,
        ))
    return tuple(evaluations)


def evaluate_quant_v6_registration(
    *,
    registration: QuantV6RegistrationPlan,
    provider: QuantV6HistoricalProvider,
    evaluation_deadline: QuantV6EvaluationDeadline | None = None,
) -> tuple[QuantV6CandidateEvaluation, ...]:
    """Evaluate every frozen member; any provider failure aborts the cohort.

    Look-ahead requires the provider to cooperatively observe the exact
    ``evaluation_deadline`` supplied here. Production construction binds that
    same object to ``QuantV6HistoricalBarProvider``. Protocol implementations
    without this cancellation invariant must omit the deadline and use the
    synchronous path so an in-flight Python thread never needs forced cleanup.
    """
    if evaluation_deadline is None:
        # Without a shared cancellation object there is no way to force an
        # arbitrary protocol implementation to drain a running Python thread.
        # Preserve the original synchronous behavior for this public mode.
        return _evaluate_quant_v6_registration_sequential(
            registration=registration,
            provider=provider,
        )

    evaluations: list[QuantV6CandidateEvaluation] = []
    total = len(registration.members)
    evaluation_deadline.checkpoint()
    validate_quant_v6_registration_plan(registration)
    first_request = (
        _verified_candidate_fetch_request_from_validated_registration(
            registration=registration,
            member=registration.members[0],
        )
    )
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="quant-v6-prefetch",
    )
    pending: Future[_CompletedCandidateFetch] | None = None
    try:
        _log_candidate_start(first_request, total=total)
        pending = executor.submit(
            _fetch_quant_v6_candidate,
            request=first_request,
            provider=provider,
        )
        for index, member in enumerate(registration.members):
            evaluation_deadline.checkpoint()
            if index:
                # Preserve the former per-member strict registration replay.
                # A hostile cooperative callback cannot mutate the frozen
                # caller graph after the first fetch and have a prefetched
                # result consumed under stale registration evidence.
                validate_quant_v6_registration_plan(registration)
            assert pending is not None
            completed_fetch = _await_candidate_fetch(
                pending,
                evaluation_deadline=evaluation_deadline,
            )
            if (
                completed_fetch.request.registration is not registration
                or completed_fetch.request.member is not member
            ):
                raise QuantV6HistoricalEvaluationError(
                    "candidate look-ahead result is outside registration order"
                )
            next_pending: Future[_CompletedCandidateFetch] | None = None
            if index + 1 < total:
                evaluation_deadline.checkpoint()
                next_request = (
                    _verified_candidate_fetch_request_from_validated_registration(
                        registration=registration,
                        member=registration.members[index + 1],
                    )
                )
                _log_candidate_start(next_request, total=total)
                next_pending = executor.submit(
                    _fetch_quant_v6_candidate,
                    request=next_request,
                    provider=provider,
                )
            pending = next_pending
            try:
                evaluations.append(
                    _evaluate_completed_candidate_with_logging(
                        completed_fetch=completed_fetch,
                        total=total,
                        evaluation_deadline=evaluation_deadline,
                    )
                )
            except BaseException:
                _cancel_and_drain_candidate_fetch(
                    pending,
                    evaluation_deadline=evaluation_deadline,
                )
                raise
            evaluation_deadline.checkpoint()
        return tuple(evaluations)
    except BaseException:
        _cancel_and_drain_candidate_fetch(
            pending,
            evaluation_deadline=evaluation_deadline,
        )
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def quant_v6_binding_manifest(
    evaluations: Sequence[QuantV6CandidateEvaluation],
) -> tuple[dict[str, object], ...]:
    return tuple(
        binding.manifest_payload()
        for evaluation in sorted(
            evaluations,
            key=lambda item: item.member.ordinal,
        )
        for binding in evaluation.bindings
    )


__all__ = [
    "ASSESSMENT_ROLE",
    "EVENT_ROLE",
    "QUANT_V6_COHORT_SOURCE",
    "QUANT_V6_REGISTRATION_CONTRACT",
    "QUANT_V6_REGISTRATION_SCHEMA_VERSION",
    "QUANT_V6_SELECTION_RULE_VERSION",
    "SESSION_INPUT_ROLE",
    "QuantV6CandidateEvaluation",
    "QuantV6CohortMember",
    "QuantV6HistoricalEvaluationError",
    "QuantV6HistoricalProvider",
    "QuantV6PendingArtifactBinding",
    "QuantV6RegistrationPlan",
    "build_latest_quant_v6_registration_plan",
    "evaluate_quant_v6_candidate",
    "evaluate_quant_v6_registration",
    "quant_v6_binding_manifest",
    "quant_v6_historical_evaluator_digest_sha256",
    "quant_v6_historical_evaluator_manifest",
    "quant_v6_registration_acquisition_spec",
    "validate_quant_v6_registration_plan",
]
