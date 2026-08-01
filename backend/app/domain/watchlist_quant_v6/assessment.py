from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from app.domain.watchlist_quant_v6.artifact import (
    QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
    QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
    EncodedQuantV6Artifact,
    canonical_decimal,
    canonical_quant_v6_json,
    encode_quant_v6_artifact,
    quant_v6_payload_sha256,
)
from app.domain.watchlist_quant_v6.semantics import (
    BAR_NEXT_OPEN_STRESSED,
    QUANT_V6_ACQUISITION_SPEC,
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QUANT_V6_ALGORITHM_VERSION,
    QUANT_V6_ASSESSMENT_CONTRACT,
    QUANT_V6_ASSESSMENT_SESSIONS,
    QUANT_V6_MIN_COVERED_SESSIONS,
    QUANT_V6_MIN_EDGE_COST_RATIO,
    QUANT_V6_MIN_EVENTS,
    QUANT_V6_MIN_EVENT_SESSIONS,
    QUANT_V6_PAYLOAD_SCHEMA_VERSION,
    QUANT_V6_SEMANTIC_DIGEST,
    QUANT_V6_SESSION_INPUT_CONTRACT,
    SESSION_CLUSTER_T90_BY_DF,
    BarNextOpenStressedEvent,
    QuantV6Bar,
    QuantV6SemanticError,
    QuantV6ThresholdEvidence,
    build_bar_next_open_stressed_session_events,
    quant_v6_consecutive_trading_session_dates,
    quant_v6_fee_rate,
    quant_v6_session_bars_sha256,
    validate_bar_next_open_stressed_event,
    validate_quant_v6_threshold_evidence,
    validate_quant_v6_symbol_market,
)


SESSION_COVERED = "COVERED"
SESSION_MISSING = "MISSING"
_SESSION_STATUSES = frozenset({SESSION_COVERED, SESSION_MISSING})
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class QuantV6AssessmentError(ValueError):
    """Raised when a fixed-window assessment preimage is structurally invalid."""


def _cooperate(checkpoint: Callable[[], None] | None) -> None:
    if checkpoint is not None:
        checkpoint()


def _validated_event_payload(
    event: BarNextOpenStressedEvent,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    _cooperate(checkpoint)
    try:
        validate_bar_next_open_stressed_event(event)
    except QuantV6SemanticError as exc:
        raise QuantV6AssessmentError(
            "event failed canonical replay validation"
        ) from exc
    _cooperate(checkpoint)
    payload = BarNextOpenStressedEvent.canonical_payload(event)
    _cooperate(checkpoint)
    return payload


def _validated_event_artifact_digest(
    event: BarNextOpenStressedEvent,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    payload = _validated_event_payload(event, checkpoint=checkpoint)
    _cooperate(checkpoint)
    digest = quant_v6_payload_sha256(payload)
    _cooperate(checkpoint)
    return digest


@dataclass(frozen=True)
class QuantV6SessionLeaf:
    session_date: date
    status: str
    session_bars: tuple[QuantV6Bar, ...] = ()
    threshold_evidence: QuantV6ThresholdEvidence | None = None
    fee_rate: Decimal | None = None
    events: tuple[BarNextOpenStressedEvent, ...] = ()
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.session_date) is not date:
            raise QuantV6AssessmentError("leaf session_date must be a date")
        if type(self.status) is not str:
            raise QuantV6AssessmentError("leaf status must be canonical text")
        if self.status not in _SESSION_STATUSES:
            raise QuantV6AssessmentError("unsupported session leaf status")
        object.__setattr__(self, "session_bars", tuple(self.session_bars))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if any(type(value) is not QuantV6Bar for value in self.session_bars):
            raise QuantV6AssessmentError(
                "leaf bars contain an unsupported type"
            )
        if self.threshold_evidence is not None and (
            type(self.threshold_evidence) is not QuantV6ThresholdEvidence
        ):
            raise QuantV6AssessmentError(
                "leaf threshold evidence has an unsupported type"
            )
        if any(
            type(value) is not BarNextOpenStressedEvent
            for value in self.events
        ):
            raise QuantV6AssessmentError(
                "leaf events contain an unsupported type"
            )
        if any(
            type(value) is not str
            or not value
            or value != value.strip()
            for value in self.blockers
        ):
            raise QuantV6AssessmentError("leaf blockers must be non-empty canonical text")
        if len(set(self.blockers)) != len(self.blockers):
            raise QuantV6AssessmentError("leaf blockers must be unique")
        if self.status == SESSION_COVERED:
            if not self.session_bars:
                raise QuantV6AssessmentError(
                    "covered leaf requires complete canonical session bars"
                )
            if type(self.threshold_evidence) is not QuantV6ThresholdEvidence:
                raise QuantV6AssessmentError(
                    "covered leaf requires threshold evidence"
                )
            if self.fee_rate is None or isinstance(self.fee_rate, (bool, float)):
                raise QuantV6AssessmentError("covered leaf requires a fee-rate snapshot")
            try:
                normalized_fee = Decimal(self.fee_rate)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise QuantV6AssessmentError("leaf fee rate must be a decimal") from exc
            if not normalized_fee.is_finite() or not Decimal("0") <= normalized_fee < 1:
                raise QuantV6AssessmentError("leaf fee rate must be finite and in [0, 1)")
            try:
                frozen_fee = quant_v6_fee_rate(self.threshold_evidence.market)
            except QuantV6SemanticError as exc:
                raise QuantV6AssessmentError(
                    "covered leaf threshold market is invalid"
                ) from exc
            if normalized_fee != frozen_fee:
                raise QuantV6AssessmentError(
                    "covered leaf fee rate must match frozen market authority"
                )
            object.__setattr__(self, "fee_rate", normalized_fee)
            if self.blockers:
                raise QuantV6AssessmentError("covered leaf cannot contain blockers")
        elif (
            self.session_bars
            or self.threshold_evidence is not None
            or self.fee_rate is not None
            or self.events
        ):
            raise QuantV6AssessmentError(
                "missing leaf cannot contain replay input or events"
            )
        elif not self.blockers:
            raise QuantV6AssessmentError("missing leaf requires an explicit blocker")

    def canonical_replay_input(
        self,
        *,
        symbol: str,
        market: str,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        _cooperate(checkpoint)
        if type(self) is not QuantV6SessionLeaf:
            raise QuantV6AssessmentError("session leaf has an unsupported type")
        if self.status != SESSION_COVERED or self.threshold_evidence is None:
            raise QuantV6AssessmentError(
                "only a covered leaf has a replay-input artifact"
            )
        try:
            validate_quant_v6_threshold_evidence(self.threshold_evidence)
        except QuantV6SemanticError as exc:
            raise QuantV6AssessmentError(
                "session replay threshold evidence is invalid"
            ) from exc
        _cooperate(checkpoint)
        if (
            self.threshold_evidence.symbol != symbol
            or self.threshold_evidence.market != market
            or self.threshold_evidence.target_session_date != self.session_date
        ):
            raise QuantV6AssessmentError(
                "session replay threshold identity does not match the leaf"
            )
        if self.fee_rate != quant_v6_fee_rate(market):
            raise QuantV6AssessmentError(
                "session replay fee rate does not match frozen market authority"
            )
        payload = {
            "acquisition": {
                **dict(QUANT_V6_ACQUISITION_SPEC),
                "spec_digest_sha256": QUANT_V6_ACQUISITION_SPEC_DIGEST,
            },
            "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
            "bar_input_sha256": quant_v6_session_bars_sha256(
                symbol=symbol,
                market=market,
                session_date=self.session_date,
                bars=self.session_bars,
            ),
            "bars": [
                QuantV6Bar.canonical_payload(item)
                for item in self.session_bars
            ],
            "capture_mode": BAR_NEXT_OPEN_STRESSED,
            "contract": QUANT_V6_SESSION_INPUT_CONTRACT,
            "fee_rate": _optional_decimal(self.fee_rate),
            "identity": {
                "market": market,
                "session_date": self.session_date.isoformat(),
                "symbol": symbol,
            },
            "p0": {
                "automatic_promotion_allowed": False,
                "order_submission_allowed": False,
                "position_add_on_allowed": False,
                "short_entry_allowed": False,
            },
            "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
            "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
            "threshold_evidence": (
                QuantV6ThresholdEvidence.canonical_payload(
                    self.threshold_evidence
                )
            ),
        }
        _cooperate(checkpoint)
        return payload

    def encoded_replay_input(
        self,
        *,
        symbol: str,
        market: str,
        checkpoint: Callable[[], None] | None = None,
    ) -> EncodedQuantV6Artifact:
        if type(self) is not QuantV6SessionLeaf:
            raise QuantV6AssessmentError("session leaf has an unsupported type")
        payload = QuantV6SessionLeaf.canonical_replay_input(
            self,
            symbol=symbol,
            market=market,
            checkpoint=checkpoint,
        )
        _cooperate(checkpoint)
        artifact = encode_quant_v6_artifact(
            payload,
            kind=QUANT_V6_SESSION_INPUT_ARTIFACT_KIND,
        )
        _cooperate(checkpoint)
        return artifact

    def canonical_payload(
        self,
        *,
        symbol: str,
        market: str,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        _cooperate(checkpoint)
        if type(self) is not QuantV6SessionLeaf:
            raise QuantV6AssessmentError("session leaf has an unsupported type")
        bar_digest: object = None
        replay_input_digest: str | None = None
        if self.status == SESSION_COVERED:
            replay_input = QuantV6SessionLeaf.canonical_replay_input(
                self,
                symbol=symbol,
                market=market,
                checkpoint=checkpoint,
            )
            bar_digest = replay_input["bar_input_sha256"]
            _cooperate(checkpoint)
            replay_input_digest = quant_v6_payload_sha256(replay_input)
            _cooperate(checkpoint)
        event_artifact_digests: list[str] = []
        for event in self.events:
            _cooperate(checkpoint)
            event_artifact_digests.append(
                _validated_event_artifact_digest(
                    event,
                    checkpoint=checkpoint,
                )
            )
        payload = {
            "bar_input_sha256": bar_digest,
            "blockers": list(self.blockers),
            "event_artifact_sha256": event_artifact_digests,
            "event_count": len(self.events),
            "fee_rate": _optional_decimal(self.fee_rate),
            "replay_input_artifact_sha256": replay_input_digest,
            "session_date": self.session_date.isoformat(),
            "status": self.status,
            "threshold_preimage_sha256": (
                self.threshold_evidence.preimage_digest_sha256
                if self.threshold_evidence is not None
                else None
            ),
        }
        _cooperate(checkpoint)
        return payload


@dataclass(frozen=True)
class QuantV6Assessment:
    symbol: str
    market: str
    leaves: tuple[QuantV6SessionLeaf, ...]
    window_digest_sha256: str
    event_set_digest_sha256: str
    covered_sessions: int
    event_count: int
    event_sessions: int
    median_gross_edge_bps: Decimal | None
    median_cost_bps: Decimal | None
    median_net_return_bps: Decimal | None
    gross_edge_to_cost_ratio: Decimal | None
    session_cluster_lcb_90_bps: Decimal | None
    candidate_thresholds_met: bool
    recommended_action: str
    blockers: tuple[str, ...]
    promotion_eligible: bool = False
    automatic_promotion_allowed: bool = False
    order_submission_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "leaves", tuple(self.leaves))
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if any(type(value) is not QuantV6SessionLeaf for value in self.leaves):
            raise QuantV6AssessmentError(
                "assessment leaves contain an unsupported type"
            )
        if (
            self.promotion_eligible is not False
            or self.automatic_promotion_allowed is not False
            or self.order_submission_allowed is not False
        ):
            raise QuantV6AssessmentError(
                "historical assessment P0 flags must remain false"
            )
        if (
            type(self.recommended_action) is not str
            or self.recommended_action not in {"AVOID", "WATCH"}
        ):
            raise QuantV6AssessmentError(
                "historical assessment action must be AVOID or WATCH"
            )
        if not isinstance(self.candidate_thresholds_met, bool):
            raise QuantV6AssessmentError(
                "candidate_thresholds_met must be boolean"
            )
        if len(self.leaves) != QUANT_V6_ASSESSMENT_SESSIONS:
            raise QuantV6AssessmentError("assessment requires exactly 30 leaves")
        for label, value in (
            ("covered_sessions", self.covered_sessions),
            ("event_count", self.event_count),
            ("event_sessions", self.event_sessions),
        ):
            if type(value) is not int or value < 0:
                raise QuantV6AssessmentError(
                    f"{label} must be a non-negative integer"
                )
        if self.covered_sessions > QUANT_V6_ASSESSMENT_SESSIONS:
            raise QuantV6AssessmentError("covered session count exceeds denominator")
        if self.event_sessions > self.covered_sessions:
            raise QuantV6AssessmentError("event sessions exceed covered sessions")
        if (
            type(self.window_digest_sha256) is not str
            or type(self.event_set_digest_sha256) is not str
            or _SHA256_PATTERN.fullmatch(self.window_digest_sha256) is None
            or _SHA256_PATTERN.fullmatch(self.event_set_digest_sha256) is None
        ):
            raise QuantV6AssessmentError(
                "assessment digests must be lowercase SHA-256"
            )
        if any(
            type(value) is not str
            or not value
            or value != value.strip()
            for value in self.blockers
        ):
            raise QuantV6AssessmentError("assessment blockers must be canonical text")
        if len(set(self.blockers)) != len(self.blockers):
            raise QuantV6AssessmentError("assessment blockers must be unique")
        for label, value in (
            ("median_gross_edge_bps", self.median_gross_edge_bps),
            ("median_cost_bps", self.median_cost_bps),
            ("median_net_return_bps", self.median_net_return_bps),
            ("gross_edge_to_cost_ratio", self.gross_edge_to_cost_ratio),
            ("session_cluster_lcb_90_bps", self.session_cluster_lcb_90_bps),
        ):
            if value is not None and (
                type(value) is not Decimal or not value.is_finite()
            ):
                raise QuantV6AssessmentError(
                    f"{label} must be a finite Decimal or None"
                )

    def canonical_payload(
        self,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        _cooperate(checkpoint)
        if type(self) is not QuantV6Assessment:
            raise QuantV6AssessmentError("assessment has an unsupported type")
        expected = assess_bar_next_open_stressed_window(
            symbol=self.symbol,
            market=self.market,
            leaves=self.leaves,
            checkpoint=checkpoint,
        )
        _cooperate(checkpoint)
        expected_payload = _canonical_assessment_payload(
            expected,
            checkpoint=checkpoint,
        )
        actual_payload = _canonical_assessment_payload(
            self,
            checkpoint=checkpoint,
        )
        _cooperate(checkpoint)
        expected_bytes = canonical_quant_v6_json(expected_payload)
        _cooperate(checkpoint)
        actual_bytes = canonical_quant_v6_json(actual_payload)
        if expected_bytes != actual_bytes:
            raise QuantV6AssessmentError(
                "assessment aggregates or digests failed canonical replay"
            )
        _cooperate(checkpoint)
        return actual_payload

    @property
    def assessment_digest_sha256(self) -> str:
        if type(self) is not QuantV6Assessment:
            raise QuantV6AssessmentError("assessment has an unsupported type")
        return quant_v6_payload_sha256(
            QuantV6Assessment.canonical_payload(self)
        )

    def encoded_artifact(
        self,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> EncodedQuantV6Artifact:
        if type(self) is not QuantV6Assessment:
            raise QuantV6AssessmentError("assessment has an unsupported type")
        payload = QuantV6Assessment.canonical_payload(
            self,
            checkpoint=checkpoint,
        )
        _cooperate(checkpoint)
        artifact = encode_quant_v6_artifact(
            payload,
            kind=QUANT_V6_ASSESSMENT_ARTIFACT_KIND,
        )
        _cooperate(checkpoint)
        return artifact


def _canonical_assessment_payload(
    assessment: QuantV6Assessment,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    leaves: list[dict[str, object]] = []
    for leaf in assessment.leaves:
        _cooperate(checkpoint)
        leaves.append(QuantV6SessionLeaf.canonical_payload(
            leaf,
            symbol=assessment.symbol,
            market=assessment.market,
            checkpoint=checkpoint,
        ))
    _cooperate(checkpoint)
    return {
            "aggregates": {
                "candidate_thresholds_met": assessment.candidate_thresholds_met,
                "covered_sessions": assessment.covered_sessions,
                "event_count": assessment.event_count,
                "event_sessions": assessment.event_sessions,
                "gross_edge_to_cost_ratio": _optional_decimal(
                    assessment.gross_edge_to_cost_ratio
                ),
                "median_cost_bps": _optional_decimal(
                    assessment.median_cost_bps
                ),
                "median_gross_edge_bps": _optional_decimal(
                    assessment.median_gross_edge_bps
                ),
                "median_net_return_bps": _optional_decimal(
                    assessment.median_net_return_bps
                ),
                "session_cluster_lcb_90_bps": _optional_decimal(
                    assessment.session_cluster_lcb_90_bps
                ),
                "session_denominator": QUANT_V6_ASSESSMENT_SESSIONS,
            },
            "algorithm_version": QUANT_V6_ALGORITHM_VERSION,
            "blockers": list(assessment.blockers),
            "capture_mode": BAR_NEXT_OPEN_STRESSED,
            "contract": QUANT_V6_ASSESSMENT_CONTRACT,
            "event_set_digest_sha256": assessment.event_set_digest_sha256,
            "identity": {
                "market": assessment.market,
                "symbol": assessment.symbol,
                "window_digest_sha256": assessment.window_digest_sha256,
            },
            "leaves": leaves,
            "policy": {
                "automatic_promotion_allowed": (
                    assessment.automatic_promotion_allowed
                ),
                "order_submission_allowed": assessment.order_submission_allowed,
                "promotion_eligible": assessment.promotion_eligible,
                "recommended_action": assessment.recommended_action,
                "position_add_on_allowed": False,
                "short_entry_allowed": False,
            },
            "schema_version": QUANT_V6_PAYLOAD_SCHEMA_VERSION,
            "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
            "session_cluster_methodology": {
                "missing_session_treatment": "EXCLUDED_NOT_ZERO",
                "sample": "ALL_COVERED_SESSIONS",
                "sample_sessions": assessment.covered_sessions,
                "zero_event_covered_session_return_bps": "0",
            },
            "thresholds": {
                "coverage_sessions": QUANT_V6_MIN_COVERED_SESSIONS,
                "gross_edge_to_cost_ratio": canonical_decimal(
                    QUANT_V6_MIN_EDGE_COST_RATIO
                ),
                "minimum_event_sessions": QUANT_V6_MIN_EVENT_SESSIONS,
                "minimum_events": QUANT_V6_MIN_EVENTS,
                "median_net_return_bps": "STRICTLY_POSITIVE",
                "session_cluster_lcb_90_bps": "STRICTLY_POSITIVE",
            },
        }


def assess_bar_next_open_stressed_window(
    *,
    symbol: str,
    market: str,
    leaves: Sequence[QuantV6SessionLeaf],
    checkpoint: Callable[[], None] | None = None,
) -> QuantV6Assessment:
    """Assess exactly 30 ordered leaves without shrinking missing sessions."""
    _cooperate(checkpoint)
    try:
        symbol, market = validate_quant_v6_symbol_market(symbol, market)
    except QuantV6SemanticError as exc:
        raise QuantV6AssessmentError(
            "invalid assessment symbol-market identity"
        ) from exc
    normalized_leaves = tuple(leaves)
    if len(normalized_leaves) != QUANT_V6_ASSESSMENT_SESSIONS:
        raise QuantV6AssessmentError("assessment requires exactly 30 session leaves")
    if any(type(leaf) is not QuantV6SessionLeaf for leaf in normalized_leaves):
        raise QuantV6AssessmentError(
            "assessment leaves contain an unsupported type"
        )
    try:
        rebuilt_leaves: list[QuantV6SessionLeaf] = []
        for leaf in normalized_leaves:
            _cooperate(checkpoint)
            rebuilt_leaves.append(QuantV6SessionLeaf(
                session_date=leaf.session_date,
                status=leaf.status,
                session_bars=leaf.session_bars,
                threshold_evidence=leaf.threshold_evidence,
                fee_rate=leaf.fee_rate,
                events=leaf.events,
                blockers=leaf.blockers,
            ))
        normalized_leaves = tuple(rebuilt_leaves)
    except QuantV6AssessmentError as exc:
        raise QuantV6AssessmentError(
            "assessment contains an invalid session leaf"
        ) from exc
    dates = [leaf.session_date for leaf in normalized_leaves]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise QuantV6AssessmentError(
            "assessment leaves must have unique ascending session dates"
        )
    try:
        expected_dates = quant_v6_consecutive_trading_session_dates(
            market,
            dates[0],
            count=QUANT_V6_ASSESSMENT_SESSIONS,
        )
    except QuantV6SemanticError as exc:
        raise QuantV6AssessmentError("assessment window is not calendar-valid") from exc
    if tuple(dates) != expected_dates:
        raise QuantV6AssessmentError(
            "assessment requires 30 consecutive market trading sessions"
        )

    event_keys: set[str] = set()
    artifact_digests: list[str] = []
    covered_events: list[BarNextOpenStressedEvent] = []
    session_returns: list[Decimal] = []
    covered_sessions = 0
    event_sessions = 0
    for leaf in normalized_leaves:
        _cooperate(checkpoint)
        if leaf.status != SESSION_COVERED:
            continue
        covered_sessions += 1
        assert leaf.threshold_evidence is not None
        assert leaf.fee_rate is not None
        try:
            _cooperate(checkpoint)
            expected_events = build_bar_next_open_stressed_session_events(
                symbol=symbol,
                market=market,
                session_date=leaf.session_date,
                bars=leaf.session_bars,
                threshold_evidence=leaf.threshold_evidence,
                fee_rate=leaf.fee_rate,
            )
        except QuantV6SemanticError as exc:
            raise QuantV6AssessmentError(
                "covered leaf failed canonical session replay"
            ) from exc
        _cooperate(checkpoint)
        actual_event_payloads: list[dict[str, object]] = []
        for event in leaf.events:
            _cooperate(checkpoint)
            actual_event_payloads.append(_validated_event_payload(
                event,
                checkpoint=checkpoint,
            ))
        expected_event_payloads: list[dict[str, object]] = []
        for event in expected_events:
            _cooperate(checkpoint)
            expected_event_payloads.append(
                BarNextOpenStressedEvent.canonical_payload(event)
            )
        if len(expected_event_payloads) != len(actual_event_payloads):
            raise QuantV6AssessmentError(
                "covered leaf events do not equal the complete replay event set"
            )
        for expected_payload, actual_payload in zip(
            expected_event_payloads,
            actual_event_payloads,
            strict=True,
        ):
            _cooperate(checkpoint)
            expected_bytes = canonical_quant_v6_json(expected_payload)
            _cooperate(checkpoint)
            actual_bytes = canonical_quant_v6_json(actual_payload)
            if expected_bytes != actual_bytes:
                raise QuantV6AssessmentError(
                    "covered leaf events do not equal the complete replay event set"
                )
        if leaf.events:
            event_sessions += 1
        previous_event: BarNextOpenStressedEvent | None = None
        for event, event_payload in zip(
            leaf.events,
            actual_event_payloads,
            strict=True,
        ):
            _cooperate(checkpoint)
            if event.symbol != symbol or event.market != market:
                raise QuantV6AssessmentError(
                    "event symbol and market must match the assessment"
                )
            if event.session_date != leaf.session_date:
                raise QuantV6AssessmentError(
                    "event session must match its assessment leaf"
                )
            if event.event_key_sha256 in event_keys:
                raise QuantV6AssessmentError("duplicate event identity in assessment")
            if (
                previous_event is not None
                and event.signal_bar.start_at < previous_event.exit_at
            ):
                raise QuantV6AssessmentError(
                    "events must be ordered and non-overlapping within a session"
                )
            event_keys.add(event.event_key_sha256)
            artifact_digests.append(quant_v6_payload_sha256(event_payload))
            covered_events.append(event)
            previous_event = event
        session_returns.append(
            _session_net_return_bps(leaf.events)
            if leaf.events
            else Decimal("0")
        )

    median_gross = _median([event.gross_edge_bps for event in covered_events])
    median_cost = _median([event.cost_bps for event in covered_events])
    median_net = _median([event.net_return_bps for event in covered_events])
    edge_cost_ratio: Decimal | None = None
    if median_gross is not None and median_cost is not None and median_cost > 0:
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            edge_cost_ratio = context.divide(median_gross, median_cost)
    lcb = session_cluster_one_sided_90_lcb(session_returns)

    blockers: list[str] = []
    if covered_sessions < QUANT_V6_MIN_COVERED_SESSIONS:
        blockers.append("INSUFFICIENT_SESSION_COVERAGE")
    if len(covered_events) < QUANT_V6_MIN_EVENTS:
        blockers.append("INSUFFICIENT_EVENTS")
    if event_sessions < QUANT_V6_MIN_EVENT_SESSIONS:
        blockers.append("INSUFFICIENT_EVENT_SESSIONS")
    if median_net is None or median_net <= 0:
        blockers.append("NON_POSITIVE_MEDIAN_NET_RETURN")
    if lcb is None or lcb <= 0:
        blockers.append("NON_POSITIVE_SESSION_CLUSTER_LCB")
    if edge_cost_ratio is None or edge_cost_ratio < QUANT_V6_MIN_EDGE_COST_RATIO:
        blockers.append("INSUFFICIENT_GROSS_EDGE_TO_COST")
    candidate_thresholds_met = not blockers
    # Historical bar evidence is useful for screening but can never become a
    # candidate or promotion input, even if every numerical threshold passes.
    blockers.append("HISTORICAL_CAPTURE_PROMOTION_INELIGIBLE")
    recommended_action = "WATCH" if candidate_thresholds_met else "AVOID"

    window_digest = quant_v6_payload_sha256({
        "market": market,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "session_dates": [value.isoformat() for value in dates],
        "session_denominator": QUANT_V6_ASSESSMENT_SESSIONS,
    })
    event_set_digest = quant_v6_payload_sha256({
        "event_artifact_sha256": artifact_digests,
        "semantic_digest": QUANT_V6_SEMANTIC_DIGEST,
        "window_digest_sha256": window_digest,
    })
    _cooperate(checkpoint)
    return QuantV6Assessment(
        symbol=symbol,
        market=market,
        leaves=normalized_leaves,
        window_digest_sha256=window_digest,
        event_set_digest_sha256=event_set_digest,
        covered_sessions=covered_sessions,
        event_count=len(covered_events),
        event_sessions=event_sessions,
        median_gross_edge_bps=median_gross,
        median_cost_bps=median_cost,
        median_net_return_bps=median_net,
        gross_edge_to_cost_ratio=edge_cost_ratio,
        session_cluster_lcb_90_bps=lcb,
        candidate_thresholds_met=candidate_thresholds_met,
        recommended_action=recommended_action,
        blockers=tuple(blockers),
    )


def session_cluster_one_sided_90_lcb(
    session_returns_bps: Sequence[Decimal | int | str],
) -> Decimal | None:
    """Return an equal-session-weight one-sided 90% Student-t lower bound."""
    if any(isinstance(value, (bool, float)) for value in session_returns_bps):
        raise QuantV6AssessmentError("session returns must be decimals")
    try:
        values = tuple(Decimal(value) for value in session_returns_bps)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise QuantV6AssessmentError("session returns must be decimals") from exc
    if any(not value.is_finite() for value in values):
        raise QuantV6AssessmentError("session returns must be finite")
    count = len(values)
    if count < 2:
        return None
    critical = SESSION_CLUSTER_T90_BY_DF.get(count - 1)
    if critical is None:
        raise QuantV6AssessmentError("session return count exceeds the fixed t table")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        mean = context.divide(sum(values, Decimal("0")), Decimal(count))
        squared = sum(
            ((value - mean) * (value - mean) for value in values),
            Decimal("0"),
        )
        variance = context.divide(squared, Decimal(count - 1))
        standard_error = context.divide(
            context.sqrt(variance),
            context.sqrt(Decimal(count)),
        )
        return mean - critical * standard_error


def _session_net_return_bps(
    events: Sequence[BarNextOpenStressedEvent],
) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        total_notional = sum(
            (event.entry_reference_notional for event in events),
            Decimal("0"),
        )
        if total_notional <= 0:
            raise QuantV6AssessmentError("session entry notional must be positive")
        total_net = sum((event.net_pnl for event in events), Decimal("0"))
        return context.multiply(
            context.divide(total_net, total_notional),
            Decimal("10000"),
        )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return context.divide(ordered[middle - 1] + ordered[middle], Decimal("2"))


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal(value)
