from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, ClassVar, TypeGuard

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, defer, load_only

from app.core.market_calendar import get_session
from app.domain.strategy_v2.forward_replay_artifact import (
    FORWARD_REPLAY_ARTIFACT_ROLE,
    forward_replay_artifact_binding_sha256,
)
from app.domain.strategy_v2.frozen_disproof_queue import (
    FORWARD_CANDIDATE_ALGORITHM_VERSION,
    FROZEN_EVALUATOR_DIGEST,
    FROZEN_QUEUE_ENTRIES,
)
from app.domain.strategy_v2.trusted_frozen_assessment import (
    TRUSTED_ASSESSMENT_WINDOW_END,
    TRUSTED_ASSESSMENT_WINDOW_START,
    TrustedAssessmentError,
    TrustedDailyLeaf,
    TrustedSymbolEvidence,
    build_trusted_assessment_report,
    trusted_assessment_sessions,
    trusted_daily_binding_sha256,
    trusted_producer_cutoff,
    validate_replay_trade_track,
)
from app.models import (
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardRegistration,
    StrategyV2ForwardReplayArtifact,
    StrategyV2ShadowVersion,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


class _TrustedLegacyValidator(StrategyV2ShadowService):
    """Use the pinned legacy validators with an already-batched snapshot map."""

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self._trusted_source_params: dict[
            tuple[str, str],
            dict[str, object],
        ] = {}

    def bind_source_snapshots(
        self,
        snapshots: Mapping[
            tuple[str, str],
            Sequence[StrategyV2ShadowVersion],
        ],
    ) -> None:
        values: dict[tuple[str, str], dict[str, object]] = {}
        for key, rows in snapshots.items():
            if len(rows) != 1:
                continue
            try:
                values[key] = _strict_json_object(rows[0].config_json)
            except (TrustedAssessmentError, TypeError, ValueError):
                continue
        self._trusted_source_params = values

    def _version_params(self, symbol: str, config_version: str) -> dict[str, Any]:
        return dict(self._trusted_source_params.get((symbol, config_version), {}))


class TrustedFrozenAssessmentService:
    """Build the frozen v3 assessment using direct, read-only database reads."""

    _MAX_TOTAL_COMPRESSED_BYTES = 512 * 1024 * 1024
    _MAX_TOTAL_RAW_BYTES = 3 * 1024 * 1024 * 1024
    _VALIDATION_CACHE_MAX = 2_048
    _validation_cache: ClassVar[
        OrderedDict[str, tuple[str, TrustedDailyLeaf]]
    ] = OrderedDict()
    _validation_cache_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._legacy = _TrustedLegacyValidator(db)

    def get_report(self) -> dict[str, object]:
        with self.db.no_autoflush:
            now = self._clock()
            cutoff = trusted_producer_cutoff(now)
            registrations = self._exact_registrations()
            source_snapshots = self._exact_source_snapshots()
            self._legacy.bind_source_snapshots(source_snapshots)
            registration_ids = [
                row.id for rows in registrations.values() for row in rows
                if row.id is not None
            ]
            evidence_rows = self._evidence_rows(registration_ids)
            expected_dates = set(trusted_assessment_sessions())
            artifact_evidence_ids = [
                row.id
                for row in evidence_rows
                if (
                    row.id is not None
                    and row.disposition == "INCLUDED"
                    and row.target_session_date in expected_dates
                    and cutoff.complete_through is not None
                    and row.target_session_date <= cutoff.complete_through
                )
            ]
            links = self._artifact_links(artifact_evidence_ids)
            artifacts = self._artifacts(links)
            artifact_resource_cap_exceeded = (
                self._artifact_resource_cap_exceeded(
                    evidence_rows=evidence_rows,
                    links=links,
                    artifacts=artifacts,
                    expected_dates=expected_dates,
                    complete_through=cutoff.complete_through,
                )
            )

            symbols = tuple(
                self._symbol_evidence(
                    symbol=symbol,
                    role=role,
                    reason=reason,
                    config_hash=config_hash,
                    registrations=registrations.get(symbol, ()),
                    source_snapshots=source_snapshots.get(
                        (symbol, config_hash),
                        (),
                    ),
                    evidence_rows=evidence_rows,
                    links=links,
                    artifacts=artifacts,
                    observed_at=cutoff.observed_at,
                    complete_through=cutoff.complete_through,
                    artifact_resource_cap_exceeded=(
                        artifact_resource_cap_exceeded
                    ),
                )
                for symbol, role, reason, config_hash in FROZEN_QUEUE_ENTRIES
            )
            return build_trusted_assessment_report(
                symbols,
                producer_cutoff=cutoff,
            )

    def _exact_registrations(
        self,
    ) -> dict[str, tuple[StrategyV2ForwardRegistration, ...]]:
        identities = tuple(
            (symbol, config_hash)
            for symbol, _, _, config_hash in FROZEN_QUEUE_ENTRIES
        )
        filters = tuple(
            and_(
                StrategyV2ForwardRegistration.symbol == symbol,
                StrategyV2ForwardRegistration.source_config_version
                == config_hash,
            )
            for symbol, config_hash in identities
        )
        rows = (
            self.db.query(StrategyV2ForwardRegistration)
            .filter(
                StrategyV2ForwardRegistration.candidate_algorithm_version
                == FORWARD_CANDIDATE_ALGORITHM_VERSION,
                StrategyV2ForwardRegistration.evaluator_digest
                == FROZEN_EVALUATOR_DIGEST,
                or_(*filters),
            )
            .order_by(
                StrategyV2ForwardRegistration.symbol.asc(),
                StrategyV2ForwardRegistration.id.asc(),
            )
            .all()
        )
        grouped: dict[str, list[StrategyV2ForwardRegistration]] = defaultdict(list)
        for row in rows:
            grouped[row.symbol].append(row)
        return {symbol: tuple(values) for symbol, values in grouped.items()}

    def _exact_source_snapshots(
        self,
    ) -> dict[tuple[str, str], tuple[StrategyV2ShadowVersion, ...]]:
        filters = tuple(
            and_(
                StrategyV2ShadowVersion.symbol == symbol,
                StrategyV2ShadowVersion.config_version == config_hash,
            )
            for symbol, _, _, config_hash in FROZEN_QUEUE_ENTRIES
        )
        rows = (
            self.db.query(StrategyV2ShadowVersion)
            .filter(or_(*filters))
            .order_by(
                StrategyV2ShadowVersion.symbol.asc(),
                StrategyV2ShadowVersion.id.asc(),
            )
            .all()
        )
        grouped: dict[
            tuple[str, str],
            list[StrategyV2ShadowVersion],
        ] = defaultdict(list)
        for row in rows:
            grouped[(row.symbol, row.config_version)].append(row)
        return {key: tuple(values) for key, values in grouped.items()}

    def _evidence_rows(
        self,
        registration_ids: Sequence[int],
    ) -> tuple[StrategyV2ForwardEvidence, ...]:
        if not registration_ids:
            return ()
        return tuple(
            self.db.query(StrategyV2ForwardEvidence)
            .filter(
                StrategyV2ForwardEvidence.registration_id.in_(
                    tuple(registration_ids)
                )
            )
            .order_by(
                StrategyV2ForwardEvidence.registration_id.asc(),
                StrategyV2ForwardEvidence.target_session_date.asc(),
                StrategyV2ForwardEvidence.id.asc(),
            )
            .all()
        )

    def _artifact_links(
        self,
        evidence_ids: Sequence[int],
    ) -> tuple[StrategyV2ForwardEvidenceArtifact, ...]:
        if not evidence_ids:
            return ()
        return tuple(
            self.db.query(StrategyV2ForwardEvidenceArtifact)
            .filter(
                StrategyV2ForwardEvidenceArtifact.evidence_id.in_(
                    tuple(evidence_ids)
                )
            )
            .order_by(
                StrategyV2ForwardEvidenceArtifact.evidence_id.asc(),
                StrategyV2ForwardEvidenceArtifact.role.asc(),
            )
            .all()
        )

    def _artifacts(
        self,
        links: Sequence[StrategyV2ForwardEvidenceArtifact],
    ) -> dict[str, tuple[StrategyV2ForwardReplayArtifact, ...]]:
        digests = tuple(dict.fromkeys(row.artifact_sha256 for row in links))
        if not digests:
            return {}
        rows = (
            self.db.query(StrategyV2ForwardReplayArtifact)
            .options(
                load_only(
                    StrategyV2ForwardReplayArtifact.digest_sha256,
                    StrategyV2ForwardReplayArtifact.schema_version,
                    StrategyV2ForwardReplayArtifact.kind,
                    StrategyV2ForwardReplayArtifact.codec,
                    StrategyV2ForwardReplayArtifact.raw_size,
                    StrategyV2ForwardReplayArtifact.compressed_size,
                    StrategyV2ForwardReplayArtifact.created_at,
                ),
                defer(StrategyV2ForwardReplayArtifact.payload),
            )
            .filter(StrategyV2ForwardReplayArtifact.digest_sha256.in_(digests))
            .order_by(StrategyV2ForwardReplayArtifact.digest_sha256.asc())
            .all()
        )
        grouped: dict[str, list[StrategyV2ForwardReplayArtifact]] = defaultdict(list)
        for row in rows:
            grouped[row.digest_sha256].append(row)
        return {digest: tuple(values) for digest, values in grouped.items()}

    def _artifact_resource_cap_exceeded(
        self,
        *,
        evidence_rows: Sequence[StrategyV2ForwardEvidence],
        links: Sequence[StrategyV2ForwardEvidenceArtifact],
        artifacts: Mapping[
            str,
            Sequence[StrategyV2ForwardReplayArtifact],
        ],
        expected_dates: set[date],
        complete_through: date | None,
    ) -> bool:
        """Preflight the complete closed-window replay workload."""

        if complete_through is None:
            return False
        links_by_evidence: dict[
            int,
            list[StrategyV2ForwardEvidenceArtifact],
        ] = defaultdict(list)
        for link in links:
            links_by_evidence[link.evidence_id].append(link)

        total_compressed = 0
        total_raw = 0
        for row in evidence_rows:
            if (
                row.id is None
                or row.disposition != "INCLUDED"
                or row.target_session_date not in expected_dates
                or row.target_session_date > complete_through
            ):
                continue
            evidence_links = links_by_evidence.get(row.id, ())
            if (
                len(evidence_links) != 1
                or evidence_links[0].role != FORWARD_REPLAY_ARTIFACT_ROLE
            ):
                continue
            artifact_rows = artifacts.get(
                evidence_links[0].artifact_sha256,
                (),
            )
            if len(artifact_rows) != 1:
                continue
            artifact = artifact_rows[0]
            if not self._artifact_metadata_safe(artifact):
                continue
            total_compressed += artifact.compressed_size
            total_raw += artifact.raw_size
            if (
                total_compressed > self._MAX_TOTAL_COMPRESSED_BYTES
                or total_raw > self._MAX_TOTAL_RAW_BYTES
            ):
                return True
        return False

    def _symbol_evidence(
        self,
        *,
        symbol: str,
        role: str,
        reason: str,
        config_hash: str,
        registrations: Sequence[StrategyV2ForwardRegistration],
        source_snapshots: Sequence[StrategyV2ShadowVersion],
        evidence_rows: Sequence[StrategyV2ForwardEvidence],
        links: Sequence[StrategyV2ForwardEvidenceArtifact],
        artifacts: Mapping[str, Sequence[StrategyV2ForwardReplayArtifact]],
        observed_at: datetime,
        complete_through: date | None,
        artifact_resource_cap_exceeded: bool,
    ) -> TrustedSymbolEvidence:
        registration = registrations[0] if len(registrations) == 1 else None
        registration_blockers = list(self._registration_blockers(
            symbol=symbol,
            config_hash=config_hash,
            registrations=registrations,
            source_snapshots=source_snapshots,
        ))
        source_chain_fingerprint = (
            _source_chain_fingerprint(registration, source_snapshots[0])
            if registration is not None and len(source_snapshots) == 1
            else None
        )
        rows = tuple(
            row
            for row in evidence_rows
            if registration is not None
            and row.registration_id == registration.id
        )
        by_day: dict[date, list[StrategyV2ForwardEvidence]] = defaultdict(list)
        for row in rows:
            by_day[row.target_session_date].append(row)
        expected = trusted_assessment_sessions()
        registration_blockers_tuple = tuple(dict.fromkeys(registration_blockers))
        leaves: list[TrustedDailyLeaf] = []
        for session_date in expected:
            day_rows = by_day.get(session_date, [])
            leaves.append(
                self._validated_daily_leaf(
                    symbol=symbol,
                    role=role,
                    config_hash=config_hash,
                    registration=registration,
                    registration_blockers=registration_blockers_tuple,
                    source_chain_fingerprint=source_chain_fingerprint,
                    session_date=session_date,
                    rows=day_rows,
                    links=links,
                    artifacts=artifacts,
                    observed_at=observed_at,
                    complete_through=complete_through,
                    artifact_resource_cap_exceeded=(
                        artifact_resource_cap_exceeded
                    ),
                )
            )
        return TrustedSymbolEvidence(
            symbol=symbol,
            role=role,
            reason=reason,
            config_hash=config_hash,
            registration_id=registration.id if registration is not None else None,
            registration_blockers=registration_blockers_tuple,
            pre_window_rows_excluded=sum(
                row.target_session_date < TRUSTED_ASSESSMENT_WINDOW_START
                for row in rows
            ),
            post_window_rows_excluded=sum(
                row.target_session_date > TRUSTED_ASSESSMENT_WINDOW_END
                for row in rows
            ),
            leaves=tuple(leaves),
            candidate_algorithm_version=(
                registration.candidate_algorithm_version
                if registration is not None
                else None
            ),
            evaluator_digest=(
                registration.evaluator_digest
                if registration is not None
                else None
            ),
            registered_at=(
                _canonical_utc(registration.registered_at)
                if registration is not None
                else None
            ),
            eligible_after=(
                _canonical_utc(registration.eligible_after)
                if registration is not None
                else None
            ),
        )

    def _registration_blockers(
        self,
        *,
        symbol: str,
        config_hash: str,
        registrations: Sequence[StrategyV2ForwardRegistration],
        source_snapshots: Sequence[StrategyV2ShadowVersion],
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if not registrations:
            return ("CANONICAL_REGISTRATION_MISSING",)
        if len(registrations) != 1:
            return ("CANONICAL_REGISTRATION_NOT_UNIQUE",)
        row = registrations[0]
        if row.id is None or isinstance(row.id, bool) or row.id <= 0:
            blockers.append("CANONICAL_REGISTRATION_ID_INVALID")
        if (
            row.symbol != symbol
            or row.market != "US"
            or row.source_config_version != config_hash
            or row.candidate_algorithm_version
            != FORWARD_CANDIDATE_ALGORITHM_VERSION
            or row.evaluator_digest != FROZEN_EVALUATOR_DIGEST
        ):
            blockers.append("CANONICAL_REGISTRATION_IDENTITY_INVALID")
        try:
            spec = _strict_json_object(row.candidate_spec_json)
            if len(source_snapshots) != 1:
                raise TrustedAssessmentError(
                    "frozen source snapshot is missing or ambiguous"
                )
            snapshot = _strict_json_object(source_snapshots[0].config_json)
            source = _validated_source_config(
                spec,
                symbol=symbol,
                config_hash=config_hash,
            )
            if source != snapshot:
                blockers.append("FROZEN_SOURCE_SNAPSHOT_MISMATCH")
            source_digest = hashlib.sha256(
                json.dumps(
                    source,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if source_digest != config_hash:
                blockers.append("FROZEN_SOURCE_CONFIG_DIGEST_INVALID")
            evaluator_spec = dict(spec)
            evaluator_spec.pop("source_config")
            evaluator_spec.pop("source_config_version")
            evaluator_digest = hashlib.sha256(
                json.dumps(
                    evaluator_spec,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            if evaluator_digest != FROZEN_EVALUATOR_DIGEST:
                blockers.append("FROZEN_EVALUATOR_SPEC_INVALID")
            registered_at = _canonical_utc(row.registered_at)
            eligible_after = _canonical_utc(row.eligible_after)
            window_open = _session_open(TRUSTED_ASSESSMENT_WINDOW_START)
            expected_eligible_after = self._legacy._forward_eligible_after(
                row.market,
                registered_at,
            )
            if registered_at >= window_open or eligible_after > window_open:
                blockers.append("CANONICAL_REGISTRATION_NOT_FROZEN_BEFORE_WINDOW")
            if eligible_after != expected_eligible_after:
                blockers.append("CANONICAL_REGISTRATION_BOUNDARY_INVALID")
        except (TrustedAssessmentError, ValueError, TypeError, json.JSONDecodeError):
            blockers.append("CANONICAL_REGISTRATION_SPEC_INVALID")
        return tuple(dict.fromkeys(blockers))

    def _validated_daily_leaf(
        self,
        *,
        symbol: str,
        role: str,
        config_hash: str,
        registration: StrategyV2ForwardRegistration | None,
        registration_blockers: Sequence[str],
        source_chain_fingerprint: str | None,
        session_date: date,
        rows: Sequence[StrategyV2ForwardEvidence],
        links: Sequence[StrategyV2ForwardEvidenceArtifact],
        artifacts: Mapping[str, Sequence[StrategyV2ForwardReplayArtifact]],
        observed_at: datetime,
        complete_through: date | None,
        artifact_resource_cap_exceeded: bool,
    ) -> TrustedDailyLeaf:
        after_cutoff = (
            complete_through is None or session_date > complete_through
        )
        if after_cutoff:
            blockers: list[str] = []
            if rows:
                blockers.append("EVIDENCE_AFTER_SERVER_CUTOFF")
            if len(rows) > 1:
                blockers.append("DUPLICATE_DAILY_EVIDENCE")
            row = rows[0] if len(rows) == 1 else None
            return TrustedDailyLeaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                disposition="PENDING",
                row_present_after_cutoff=bool(rows),
                evidence_id=(
                    _positive_integer_or_none(row.id)
                    if row is not None
                    else None
                ),
                evidence_digest_sha256=(
                    _sha256_or_none(row.evidence_digest_sha256)
                    if row is not None
                    else None
                ),
                blockers=tuple(blockers),
            )

        if not rows:
            return TrustedDailyLeaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                disposition="MISSING",
            )
        if len(rows) != 1:
            return self._invalid_leaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                blocker="DUPLICATE_DAILY_EVIDENCE",
            )
        row = rows[0]
        if registration is None or registration_blockers:
            return self._invalid_leaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                row=row,
                blocker="CANONICAL_REGISTRATION_INVALID",
            )

        try:
            self._validate_evidence_identity(
                row,
                registration=registration,
                session_date=session_date,
                observed_at=observed_at,
            )
        except (TrustedAssessmentError, ValueError, TypeError):
            return self._invalid_leaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                row=row,
                blocker="EVIDENCE_IDENTITY_INVALID",
            )

        if row.disposition == "EXCLUDED":
            if not self._legacy._forward_exclusion_semantics_valid(row):
                return self._invalid_leaf(
                    symbol=symbol,
                    role=role,
                    config_hash=config_hash,
                    session_date=session_date,
                    row=row,
                    blocker="EXCLUSION_SEMANTICS_INVALID",
                )
            return TrustedDailyLeaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                disposition=(
                    "EXCLUDED_STRUCTURAL"
                    if row.structural_failure
                    else "EXCLUDED_NON_STRUCTURAL"
                ),
                exclusion_reason=row.exclusion_reason,
                structural_failure=row.structural_failure,
                evidence_id=row.id,
                evidence_digest_sha256=row.evidence_digest_sha256,
            )
        if row.disposition != "INCLUDED":
            return self._invalid_leaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                row=row,
                blocker="EVIDENCE_DISPOSITION_INVALID",
            )

        return self._included_leaf(
            symbol=symbol,
            role=role,
            config_hash=config_hash,
            registration=registration,
            source_chain_fingerprint=source_chain_fingerprint,
            session_date=session_date,
            row=row,
            links=links,
            artifacts=artifacts,
            artifact_resource_cap_exceeded=(
                artifact_resource_cap_exceeded
            ),
        )

    def _validate_evidence_identity(
        self,
        row: StrategyV2ForwardEvidence,
        *,
        registration: StrategyV2ForwardRegistration,
        session_date: date,
        observed_at: datetime,
    ) -> None:
        if (
            row.id is None
            or isinstance(row.id, bool)
            or row.id <= 0
            or row.registration_id != registration.id
            or row.target_session_date != session_date
            or _canonical_utc(row.target_open_at) != _session_open(session_date)
            or _canonical_utc(row.target_open_at)
            < _canonical_utc(registration.eligible_after)
            or _canonical_utc(row.evaluated_at) > observed_at
            or _canonical_utc(row.created_at) > observed_at
            or not _is_sha256(row.evidence_digest_sha256)
            or self._legacy._forward_evidence_digest(row)
            != row.evidence_digest_sha256
        ):
            raise TrustedAssessmentError("trusted evidence identity is invalid")
        if row.disposition == "INCLUDED":
            session = get_session("US")
            close_at = datetime.combine(
                session_date,
                session.close_time(session_date),
                tzinfo=session.timezone,
            ).astimezone(timezone.utc)
            evaluated_at = _canonical_utc(row.evaluated_at)
            if not (
                close_at + timedelta(minutes=10)
                <= evaluated_at
                < close_at + timedelta(minutes=15)
            ):
                raise TrustedAssessmentError(
                    "included evidence is outside the producer finalization window"
                )

    def _included_leaf(
        self,
        *,
        symbol: str,
        role: str,
        config_hash: str,
        registration: StrategyV2ForwardRegistration,
        source_chain_fingerprint: str | None,
        session_date: date,
        row: StrategyV2ForwardEvidence,
        links: Sequence[StrategyV2ForwardEvidenceArtifact],
        artifacts: Mapping[str, Sequence[StrategyV2ForwardReplayArtifact]],
        artifact_resource_cap_exceeded: bool,
    ) -> TrustedDailyLeaf:
        base = {
            "symbol": symbol,
            "role": role,
            "config_hash": config_hash,
            "session_date": session_date,
            "row": row,
        }
        if not _is_sha256(source_chain_fingerprint):
            return self._invalid_leaf(
                **base,
                blocker="FROZEN_SOURCE_CHAIN_FINGERPRINT_INVALID",
            )
        if (
            row.exclusion_reason
            or row.structural_failure
            or row.target_bars <= 0
            or not _is_sha256(row.target_bars_sha256)
            or not _is_sha256(row.seed_bars_sha256)
            or not _is_sha256(row.baseline_input_sha256)
            or not _is_sha256(row.candidate_input_sha256)
            or row.same_target_bars is not True
            or row.baseline_replay_match is not True
            or row.session_local_invariant is not True
            or row.seed_session_date is None
        ):
            return self._invalid_leaf(
                **base,
                blocker="INCLUDED_EVIDENCE_INVARIANT_INVALID",
            )
        evidence_links = [item for item in links if item.evidence_id == row.id]
        if (
            len(evidence_links) != 1
            or evidence_links[0].role != FORWARD_REPLAY_ARTIFACT_ROLE
        ):
            return self._invalid_leaf(
                **base,
                blocker="REPLAY_ARTIFACT_LINK_NOT_UNIQUE",
            )
        link = evidence_links[0]
        artifact_rows = artifacts.get(link.artifact_sha256, ())
        if len(artifact_rows) != 1:
            return self._invalid_leaf(
                **base,
                artifact_digest_sha256=link.artifact_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_MISSING_OR_DUPLICATE",
            )
        artifact = artifact_rows[0]
        try:
            expected_binding = forward_replay_artifact_binding_sha256(
                evidence_id=row.id,
                evidence_digest_sha256=row.evidence_digest_sha256,
                artifact_digest_sha256=artifact.digest_sha256,
            )
        except (ValueError, TypeError):
            expected_binding = ""
        if (
            link.artifact_sha256 != artifact.digest_sha256
            or link.binding_sha256 != expected_binding
        ):
            return self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_BINDING_INVALID",
            )
        if not self._artifact_metadata_safe(artifact):
            return self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_METADATA_INVALID",
            )
        if artifact_resource_cap_exceeded:
            return TrustedDailyLeaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                disposition="PENDING",
                evidence_id=row.id,
                evidence_digest_sha256=row.evidence_digest_sha256,
                blockers=("VERIFIER_RESOURCE_CAP_EXCEEDED",),
            )
        try:
            coarse_key = self._validation_fingerprint(
                registration=registration,
                source_chain_fingerprint=source_chain_fingerprint,
                row=row,
                link=link,
                artifact=artifact,
                include_payload=False,
            )
        except (TrustedAssessmentError, TypeError, ValueError):
            return self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_FINGERPRINT_INVALID",
            )
        try:
            full_key = self._validation_fingerprint(
                registration=registration,
                source_chain_fingerprint=source_chain_fingerprint,
                row=row,
                link=link,
                artifact=artifact,
                include_payload=True,
            )
        except (TrustedAssessmentError, TypeError, ValueError):
            self.db.expire(artifact, ["payload"])
            return self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_FINGERPRINT_INVALID",
            )
        cached_leaf = self._validation_cache_get(coarse_key, full_key)
        if cached_leaf is not None:
            self.db.expire(artifact, ["payload"])
            return cached_leaf

        payload = self._legacy._validated_forward_replay_artifact_payload(
            row,
            registration,
        )
        if payload is None:
            return self._cache_leaf_and_release_payload(
                coarse_key,
                full_key,
                artifact,
                self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="REPLAY_ARTIFACT_CHAIN_INVALID",
                ),
            )
        if payload.get("capture_mode") != "FULL_REPLAY_VERIFIED":
            return self._cache_leaf_and_release_payload(
                coarse_key,
                full_key,
                artifact,
                self._invalid_leaf(
                **base,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                blocker="SOURCE_TRACE_NOT_PROMOTION_GRADE",
                ),
            )
        try:
            baseline_result = _strict_json_object(row.baseline_result_json)
            candidate_result = _strict_json_object(row.candidate_result_json)
            if (
                _text_sha256(row.baseline_result_json)
                != row.baseline_result_sha256
                or _text_sha256(row.candidate_result_json)
                != row.candidate_result_sha256
            ):
                raise TrustedAssessmentError("result digest is invalid")
            source = _object_mapping(
                payload.get("source_config"),
                field_name="source_config",
            )
            fee_rate, max_entries, quantity, max_holding = (
                _execution_constraints(source, symbol=symbol)
            )
            baseline_replay = _object_mapping(
                payload.get("baseline_replay"),
                field_name="baseline_replay",
            )
            candidate_replay = _object_mapping(
                payload.get("candidate_replay"),
                field_name="candidate_replay",
            )
            baseline_summary = validate_replay_trade_track(
                baseline_replay,
                baseline_result,
                label=f"{symbol}.{session_date.isoformat()}.baseline",
                session_date=session_date,
                expected_fee_rate=fee_rate,
                expected_max_entries_per_day=max_entries,
                expected_virtual_quantity=quantity,
                expected_max_holding_minutes=max_holding,
            )
            candidate_summary = validate_replay_trade_track(
                candidate_replay,
                candidate_result,
                label=f"{symbol}.{session_date.isoformat()}.candidate",
                session_date=session_date,
                expected_fee_rate=fee_rate,
                expected_max_entries_per_day=max_entries,
                expected_virtual_quantity=quantity,
                expected_max_holding_minutes=max_holding,
            )
            daily_binding = trusted_daily_binding_sha256(
                symbol=symbol,
                config_hash=config_hash,
                candidate_algorithm_version=(
                    registration.candidate_algorithm_version
                ),
                evaluator_digest=registration.evaluator_digest,
                registration_id=registration.id,
                registered_at=_canonical_utc(registration.registered_at),
                eligible_after=_canonical_utc(registration.eligible_after),
                evidence_id=row.id,
                session_date=session_date,
                evidence_digest_sha256=row.evidence_digest_sha256,
                baseline_result_sha256=row.baseline_result_sha256,
                candidate_result_sha256=row.candidate_result_sha256,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                baseline_trade_preimage_sha256=(
                    baseline_summary.ordered_trade_preimage_sha256
                ),
                candidate_trade_preimage_sha256=(
                    candidate_summary.ordered_trade_preimage_sha256
                ),
                baseline_summary=baseline_summary,
                candidate_summary=candidate_summary,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            json.JSONDecodeError,
            TrustedAssessmentError,
        ):
            return self._cache_leaf_and_release_payload(
                coarse_key,
                full_key,
                artifact,
                self._invalid_leaf(
                    **base,
                    artifact_digest_sha256=artifact.digest_sha256,
                    artifact_binding_sha256=link.binding_sha256,
                    blocker="REPLAY_PREIMAGE_INVALID",
                ),
            )
        return self._cache_leaf_and_release_payload(
            coarse_key,
            full_key,
            artifact,
            TrustedDailyLeaf(
                symbol=symbol,
                role=role,
                config_hash=config_hash,
                session_date=session_date,
                disposition="INCLUDED",
                evidence_id=row.id,
                evidence_digest_sha256=row.evidence_digest_sha256,
                baseline_result_sha256=row.baseline_result_sha256,
                candidate_result_sha256=row.candidate_result_sha256,
                artifact_digest_sha256=artifact.digest_sha256,
                artifact_binding_sha256=link.binding_sha256,
                daily_binding_sha256=daily_binding,
                baseline=baseline_summary,
                candidate=candidate_summary,
            ),
        )

    def _cache_leaf_and_release_payload(
        self,
        coarse_key: str,
        full_key: str,
        artifact: StrategyV2ForwardReplayArtifact,
        value: TrustedDailyLeaf,
    ) -> TrustedDailyLeaf:
        cached = self._validation_cache_put(coarse_key, full_key, value)
        self.db.expire(artifact, ["payload"])
        return cached

    @staticmethod
    def _artifact_metadata_safe(
        artifact: StrategyV2ForwardReplayArtifact,
    ) -> bool:
        return (
            _is_sha256(artifact.digest_sha256)
            and artifact.schema_version == 1
            and artifact.kind == "STRATEGY_V2_FORWARD_REPLAY"
            and artifact.codec == "zlib"
            and isinstance(artifact.raw_size, int)
            and not isinstance(artifact.raw_size, bool)
            and 0 < artifact.raw_size <= 8 * 1024 * 1024
            and isinstance(artifact.compressed_size, int)
            and not isinstance(artifact.compressed_size, bool)
            and 0 < artifact.compressed_size <= 2 * 1024 * 1024
        )

    def _validation_fingerprint(
        self,
        *,
        registration: StrategyV2ForwardRegistration,
        source_chain_fingerprint: str,
        row: StrategyV2ForwardEvidence,
        link: StrategyV2ForwardEvidenceArtifact,
        artifact: StrategyV2ForwardReplayArtifact,
        include_payload: bool,
    ) -> str:
        artifact_fingerprint = _orm_fingerprint(
            artifact,
            exclude={"payload"},
        )
        if include_payload:
            payload = artifact.payload
            if (
                not isinstance(payload, bytes)
                or len(payload) != artifact.compressed_size
            ):
                raise TrustedAssessmentError(
                    "artifact compressed bytes are invalid"
                )
            artifact_fingerprint["compressed_payload_sha256"] = (
                hashlib.sha256(payload).hexdigest()
            )
        preimage = {
            "cache_contract": (
                "trusted-frozen-assessment-cache-full-v1"
                if include_payload
                else "trusted-frozen-assessment-cache-coarse-v1"
            ),
            "registration_source_sha256": source_chain_fingerprint,
            "evidence": _orm_fingerprint(row),
            "link": _orm_fingerprint(link),
            "artifact": artifact_fingerprint,
        }
        return hashlib.sha256(
            json.dumps(
                preimage,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _validation_cache_get(
        cls,
        coarse_key: str,
        full_key: str,
    ) -> TrustedDailyLeaf | None:
        with cls._validation_cache_lock:
            value = cls._validation_cache.get(coarse_key)
            if value is None or value[0] != full_key:
                return None
            cls._validation_cache.move_to_end(coarse_key)
            return value[1]

    @classmethod
    def _validation_cache_put(
        cls,
        coarse_key: str,
        full_key: str,
        value: TrustedDailyLeaf,
    ) -> TrustedDailyLeaf:
        with cls._validation_cache_lock:
            cls._validation_cache[coarse_key] = (full_key, value)
            cls._validation_cache.move_to_end(coarse_key)
            while len(cls._validation_cache) > cls._VALIDATION_CACHE_MAX:
                cls._validation_cache.popitem(last=False)
        return value

    @staticmethod
    def _invalid_leaf(
        *,
        symbol: str,
        role: str,
        config_hash: str,
        session_date: date,
        blocker: str,
        row: StrategyV2ForwardEvidence | None = None,
        artifact_digest_sha256: str | None = None,
        artifact_binding_sha256: str | None = None,
    ) -> TrustedDailyLeaf:
        return TrustedDailyLeaf(
            symbol=symbol,
            role=role,
            config_hash=config_hash,
            session_date=session_date,
            disposition="INVALID",
            evidence_id=(
                _positive_integer_or_none(row.id)
                if row is not None
                else None
            ),
            evidence_digest_sha256=(
                _sha256_or_none(row.evidence_digest_sha256)
                if row is not None
                else None
            ),
            artifact_digest_sha256=_sha256_or_none(
                artifact_digest_sha256
            ),
            artifact_binding_sha256=_sha256_or_none(
                artifact_binding_sha256
            ),
            blockers=(blocker,),
        )


def _strict_json_object(raw: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise TrustedAssessmentError("trusted JSON payload must be text")
    value = json.loads(raw, object_pairs_hook=_unique_json_object)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TrustedAssessmentError("trusted JSON payload must be an object")
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if raw != canonical:
        raise TrustedAssessmentError("trusted JSON payload is not canonical")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TrustedAssessmentError(
                f"trusted JSON payload contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _canonical_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # SQLAlchemy's UTC datetime adapter returns naive values for SQLite.
        value = value.replace(tzinfo=timezone.utc)
    result = value.astimezone(timezone.utc)
    if result.second or result.microsecond:
        return result
    return result


def _session_open(session_date: date) -> datetime:
    market = get_session("US")
    return datetime.combine(
        session_date,
        market.rth_open,
        tzinfo=market.timezone,
    ).astimezone(timezone.utc)


def _validated_source_config(
    spec: Mapping[str, object],
    *,
    symbol: str,
    config_hash: str,
) -> Mapping[str, object]:
    source = spec.get("source_config")
    if not isinstance(source, Mapping):
        raise TrustedAssessmentError("frozen candidate source config is missing")
    required = {
        "schema_version": 4,
        "candidate_algorithm_version": FORWARD_CANDIDATE_ALGORITHM_VERSION,
        "source_config_version": config_hash,
        "automatic_promotion_allowed": False,
        "order_submission_allowed": False,
        "historical_target_backfill_allowed": False,
        "baseline_scope": "SESSION_LOCAL",
        "evaluation_scope": "FORWARD_OUT_OF_SAMPLE",
    }
    if any(spec.get(key) != value for key, value in required.items()):
        raise TrustedAssessmentError("frozen candidate spec identity is invalid")
    source_required = {
        "symbol": symbol,
        "mode": "SHADOW",
        "short_entries_enabled": False,
        "allow_position_addons": False,
        "order_submission_allowed": False,
    }
    if any(source.get(key) != value for key, value in source_required.items()):
        raise TrustedAssessmentError("frozen source config violates P0 safety")
    return source


def _execution_constraints(
    source: Mapping[str, object],
    *,
    symbol: str,
) -> tuple[float, int, float, int]:
    if (
        source.get("symbol") != symbol
        or source.get("mode") != "SHADOW"
        or source.get("short_entries_enabled") is not False
        or source.get("allow_position_addons") is not False
        or source.get("order_submission_allowed") is not False
    ):
        raise TrustedAssessmentError("frozen execution safety fields are invalid")
    fee_rate_raw = source.get("estimated_fee_rate_us")
    if (
        isinstance(fee_rate_raw, bool)
        or not isinstance(fee_rate_raw, (int, float))
    ):
        raise TrustedAssessmentError("frozen fee rate is invalid")
    fee_rate = float(fee_rate_raw)
    if not 0 < fee_rate < 1:
        raise TrustedAssessmentError("frozen fee rate must be positive")
    max_entries = source.get("max_entries_per_day")
    max_holding = source.get("max_holding_minutes")
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or not 0 < max_entries <= 2
        or isinstance(max_holding, bool)
        or not isinstance(max_holding, int)
        or not 0 < max_holding <= 60
    ):
        raise TrustedAssessmentError("frozen execution limits are invalid")
    # The v1 producer constructs StrategyV2Config with virtual_quantity=1.0;
    # it is an executable semantic constant rather than a mutable config field.
    return fee_rate, max_entries, 1.0, max_holding


def _object_mapping(
    value: object,
    *,
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise TrustedAssessmentError(f"{field_name} must be an object")
    return value


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_or_none(value: object) -> str | None:
    return value if _is_sha256(value) else None


def _positive_integer_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _orm_fingerprint(
    row: object,
    *,
    exclude: set[str] | None = None,
) -> dict[str, object]:
    table = getattr(row, "__table__", None)
    columns = getattr(table, "columns", None)
    if columns is None:
        raise TrustedAssessmentError("cache source is not an ORM row")
    excluded = exclude or set()
    result: dict[str, object] = {}
    for column in columns:
        name = str(column.name)
        if name in excluded:
            continue
        value = getattr(row, name)
        if isinstance(value, datetime):
            result[name] = _canonical_utc(value).isoformat()
        elif isinstance(value, date):
            result[name] = value.isoformat()
        elif isinstance(value, bytes):
            result[name] = hashlib.sha256(value).hexdigest()
        elif value is None or isinstance(value, (str, bool, int, float)):
            result[name] = value
        else:
            raise TrustedAssessmentError(
                f"cache source column {name} has unsupported type"
            )
    return result


def _source_chain_fingerprint(
    registration: StrategyV2ForwardRegistration,
    snapshot: StrategyV2ShadowVersion,
) -> str:
    preimage = {
        "contract": "trusted-frozen-registration-source-chain-v1",
        "registration": _orm_fingerprint(registration),
        "source_snapshot": _orm_fingerprint(snapshot),
    }
    return hashlib.sha256(
        json.dumps(
            preimage,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
