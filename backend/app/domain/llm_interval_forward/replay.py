from __future__ import annotations

import inspect
import math
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache
from types import ModuleType
from typing import Literal, cast

import app.core.backtest as backtest_module
import app.core.fees as fees_module
import app.core.holiday_calendar as holiday_calendar_module
import app.core.market_calendar as market_calendar_module
import app.domain.llm_interval_forward.contract as contract_module
import app.domain.llm_interval_forward.artifact as artifact_module
from app.core.backtest import BacktestBar, BacktestEngine, BacktestEngineParams

from .artifact import (
    INTERVAL_FORWARD_ARTIFACT_KIND,
    INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION,
    EncodedIntervalForwardArtifact,
    IntervalForwardArtifactError,
    decode_interval_forward_artifact,
    encode_interval_forward_artifact,
)

from .contract import (
    BBO_COVERAGE,
    DATA_FIDELITY,
    ENTRY_CROSSING_SEMANTICS,
    FEE_MODEL_FIDELITY,
    MAX_CANONICAL_CONTAINER_ITEMS,
    MAX_EXPECTED_SESSION_OBSERVATIONS,
    MAX_PRICE,
    MAX_VOLUME,
    TIMESTAMP_SEMANTICS,
    FrozenExecutionPolicy,
    FrozenIntervalBand,
    FrozenSessionSlot,
    IntervalForwardContractError,
    ProposalObservation,
    canonical_decimal_text,
    canonical_sha256,
    content_sha256,
    counterfactual_policy_without_confidence,
    freeze_session_slot,
    full_session_observation_schedule,
    observation_schedule_sha256,
    resolve_session_slot,
    validate_symbol,
)


EVALUATOR_ALGORITHM_VERSION = "llm-interval-paired-ohlc-evaluator-v1"
FORWARD_BAR_SCHEMA_VERSION = "llm-interval-forward-bar-v1"
REPLAY_ARTIFACT_KIND = INTERVAL_FORWARD_ARTIFACT_KIND
COMMON_ENVIRONMENT_SCHEMA_VERSION = "llm-interval-common-environment-v1"
ARM_RESULT_SCHEMA_VERSION = "llm-interval-forward-arm-result-v1"
DAILY_LEAF_SCHEMA_VERSION = "llm-interval-forward-daily-leaf-v1"
ASSESSMENT_SCHEMA_VERSION = "llm-interval-forward-assessment-v1"
ASSESSMENT_POLICY_VERSION = "fixed-60-session-paired-diagnostic-v1"

FIXED_HORIZON_SESSIONS = 60
MINIMUM_INCLUDED_SESSIONS = 20
MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM = 50
CONFIDENCE_MULTIPLIER = Decimal("2.1")
MAX_DAILY_DELTA_CONCENTRATION = Decimal("0.25")
ARTIFACT_DEADLINE_DELAY = timedelta(hours=6)
PERMANENT_LIMITATIONS = (
    "BBO_NONE",
    "OHLC_CROSSING_APPROXIMATION",
    "LIVE_PARITY_UNPROVEN",
    "CONFIGURED_FEE_ESTIMATE_ONLY",
    "MANUAL_REVIEW_REQUIRED",
    "SERVER_SOURCE_AUTHORITY_REQUIRED",
)
ASSESSMENT_BLOCKER_CODES = frozenset({
    "FIXED_HORIZON_NOT_COMPLETE",
    "MISSING_SESSIONS",
    "INVALID_SESSIONS",
    "INSUFFICIENT_INCLUDED_SESSIONS",
    "INSUFFICIENT_BASELINE_CLOSED_ROUND_TRIPS",
    "INSUFFICIENT_CANDIDATE_CLOSED_ROUND_TRIPS",
    "CANDIDATE_NET_NOT_POSITIVE",
    "DELTA_CONFIDENCE_LOWER_NOT_POSITIVE",
    "DAILY_DELTA_CONCENTRATION_TOO_HIGH",
    "PAIRED_COVERAGE_INCOMPLETE",
})

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_TEN_THOUSAND = Decimal("10000")
_METRIC_QUANTUM = Decimal("0.00000001")
_MAX_ABS_METRIC = Decimal("1000000000000000000")
_FROZEN_REPLAY_DECIMAL_PRECISION = 50

LeafDisposition = Literal["PENDING", "MISSING", "INCLUDED", "INVALID"]
ArmName = Literal["baseline", "candidate"]
_LONG_EXIT_ACTIONS = frozenset({
    "SELL",
    "STOP_LOSS_SELL",
    "TRAILING_STOP_SELL",
    "EOD_FLATTEN_SELL",
    "TIME_STOP_SELL",
    "DAILY_LOSS_SELL",
})


class IntervalForwardReplayError(IntervalForwardContractError):
    """Raised when source bars or replay output are not trustworthy."""


def _decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise IntervalForwardReplayError(f"{field_name} must be a finite Decimal")
    try:
        canonical_decimal_text(value)
    except IntervalForwardContractError as exc:
        raise IntervalForwardReplayError(str(exc)) from exc
    return value


def _metric(value: Decimal | float | int) -> Decimal:
    if isinstance(value, bool):
        raise IntervalForwardReplayError("metric must be numeric")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise IntervalForwardReplayError("metric must be finite")
    if result.copy_abs() > _MAX_ABS_METRIC:
        raise IntervalForwardReplayError("metric exceeds the frozen maximum")
    try:
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return result.quantize(_METRIC_QUANTUM)
    except ArithmeticError as exc:
        raise IntervalForwardReplayError("metric cannot be quantized") from exc


def _metric_calculation(
    calculation: Callable[[], Decimal | float | int],
) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            return _metric(calculation())
    except ArithmeticError as exc:
        raise IntervalForwardReplayError(
            "metric calculation failed under the frozen context"
        ) from exc


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except IntervalForwardContractError as exc:
        raise IntervalForwardReplayError(str(exc)) from exc


def _utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IntervalForwardReplayError(
            f"{field_name} must be timezone-aware UTC"
        )
    if value.utcoffset() != timedelta(0):
        raise IntervalForwardReplayError(f"{field_name} must use UTC")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, field_name="timestamp").isoformat().replace("+00:00", "Z")


def _sha(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IntervalForwardReplayError(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


@dataclass(frozen=True, kw_only=True)
class ForwardBar:
    timestamp: datetime
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_sha256: str

    def __post_init__(self) -> None:
        timestamp = _utc(self.timestamp, field_name="bar.timestamp")
        if timestamp.second != 0 or timestamp.microsecond != 0:
            raise IntervalForwardReplayError(
                "bar timestamp must align to a complete minute"
            )
        observed_at = _utc(self.observed_at, field_name="bar.observed_at")
        if observed_at < timestamp:
            raise IntervalForwardReplayError(
                "bar observed_at cannot precede its observation timestamp"
            )
        opened = _decimal(self.open, field_name="bar.open")
        high = _decimal(self.high, field_name="bar.high")
        low = _decimal(self.low, field_name="bar.low")
        close = _decimal(self.close, field_name="bar.close")
        volume = _decimal(self.volume, field_name="bar.volume")
        if min(opened, high, low, close) <= _ZERO:
            raise IntervalForwardReplayError("OHLC prices must be positive")
        if max(opened, high, low, close) > MAX_PRICE:
            raise IntervalForwardReplayError("OHLC prices exceed the frozen maximum")
        if low > high or not low <= opened <= high or not low <= close <= high:
            raise IntervalForwardReplayError("bar OHLC relationship is invalid")
        if volume < _ZERO:
            raise IntervalForwardReplayError("bar volume must be non-negative")
        if volume > MAX_VOLUME:
            raise IntervalForwardReplayError("bar volume exceeds the frozen maximum")
        _sha(self.source_sha256, field_name="bar.source_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": FORWARD_BAR_SCHEMA_VERSION,
            "timestamp": _iso(self.timestamp),
            "observed_at": _iso(self.observed_at),
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class ReplayRoundTrip:
    entry_at: datetime
    exit_at: datetime
    exit_action: str
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    modeled_fees: Decimal
    net_pnl: Decimal
    gross_bps: Decimal
    fee_bps: Decimal
    net_bps: Decimal
    holding_minutes: Decimal

    def __post_init__(self) -> None:
        entry_at = _utc(self.entry_at, field_name="round_trip.entry_at")
        exit_at = _utc(self.exit_at, field_name="round_trip.exit_at")
        if exit_at <= entry_at:
            raise IntervalForwardReplayError(
                "round-trip exit must follow its entry"
            )
        if self.exit_action not in _LONG_EXIT_ACTIONS:
            raise IntervalForwardReplayError("round-trip exit action is invalid")
        entry_price = _decimal(
            self.entry_price,
            field_name="round_trip.entry_price",
        )
        exit_price = _decimal(
            self.exit_price,
            field_name="round_trip.exit_price",
        )
        if (
            entry_price <= _ZERO
            or exit_price <= _ZERO
            or entry_price > MAX_PRICE
            or exit_price > MAX_PRICE
        ):
            raise IntervalForwardReplayError("round-trip prices are outside bounds")
        for field_name in (
            "gross_pnl",
            "modeled_fees",
            "net_pnl",
            "gross_bps",
            "fee_bps",
            "net_bps",
            "holding_minutes",
        ):
            _decimal(getattr(self, field_name), field_name=f"round_trip.{field_name}")
        if self.modeled_fees < _ZERO or self.fee_bps < _ZERO:
            raise IntervalForwardReplayError("round-trip fees must be non-negative")
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            expected_gross = _metric(exit_price - entry_price)
            expected_fees = _metric(self.modeled_fees)
            expected_net = _metric(expected_gross - expected_fees)
            expected_gross_bps = _metric(
                expected_gross / entry_price * _TEN_THOUSAND
            )
            expected_fee_bps = _metric(
                expected_fees / entry_price * _TEN_THOUSAND
            )
            expected_net_bps = _metric(
                expected_net / entry_price * _TEN_THOUSAND
            )
            expected_holding = _metric(
                Decimal(str((exit_at - entry_at).total_seconds()))
                / Decimal("60")
            )
        if (
            self.gross_pnl != expected_gross
            or self.modeled_fees != expected_fees
            or self.net_pnl != expected_net
            or self.gross_bps != expected_gross_bps
            or self.fee_bps != expected_fee_bps
            or self.net_bps != expected_net_bps
            or self.holding_minutes != expected_holding
        ):
            raise IntervalForwardReplayError(
                "round-trip derived metrics are inconsistent"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "entry_at": _iso(self.entry_at),
            "exit_at": _iso(self.exit_at),
            "exit_action": self.exit_action,
            "entry_price": _decimal_text(self.entry_price),
            "exit_price": _decimal_text(self.exit_price),
            "gross_pnl": _decimal_text(self.gross_pnl),
            "modeled_fees": _decimal_text(self.modeled_fees),
            "net_pnl": _decimal_text(self.net_pnl),
            "gross_bps": _decimal_text(self.gross_bps),
            "fee_bps": _decimal_text(self.fee_bps),
            "net_bps": _decimal_text(self.net_bps),
            "holding_minutes": _decimal_text(self.holding_minutes),
        }


@dataclass(frozen=True, kw_only=True)
class VariantSessionResult:
    arm: ArmName
    band: FrozenIntervalBand
    common_environment_sha256: str
    arm_params_sha256: str
    arm_input_sha256: str
    closed_round_trips: int
    gross_bps: Decimal
    modeled_fee_bps: Decimal
    net_bps: Decimal
    max_drawdown_bps: Decimal
    final_state: Literal["flat"]
    skipped_signal_counts: tuple[tuple[str, int], ...]
    round_trips: tuple[ReplayRoundTrip, ...]
    ordered_trade_preimage_sha256: str
    result_digest_sha256: str = field(default="")

    def __post_init__(self) -> None:
        if self.arm not in {"baseline", "candidate"}:
            raise IntervalForwardReplayError("arm is invalid")
        for field_name in (
            "common_environment_sha256",
            "arm_params_sha256",
            "arm_input_sha256",
            "ordered_trade_preimage_sha256",
        ):
            _sha(getattr(self, field_name), field_name=field_name)
        if (
            isinstance(self.closed_round_trips, bool)
            or not isinstance(self.closed_round_trips, int)
            or self.closed_round_trips < 0
            or self.closed_round_trips != len(self.round_trips)
        ):
            raise IntervalForwardReplayError("closed round-trip count is invalid")
        if self.closed_round_trips > 1:
            raise IntervalForwardReplayError(
                "v1 permits at most one round trip per arm and session"
            )
        for field_name in (
            "gross_bps",
            "modeled_fee_bps",
            "net_bps",
            "max_drawdown_bps",
        ):
            _decimal(getattr(self, field_name), field_name=field_name)
        if self.modeled_fee_bps < _ZERO or self.max_drawdown_bps < _ZERO:
            raise IntervalForwardReplayError("fee and drawdown metrics must be non-negative")
        if self.final_state != "flat":
            raise IntervalForwardReplayError("included arm must finish flat")
        if tuple(sorted(self.skipped_signal_counts)) != self.skipped_signal_counts:
            raise IntervalForwardReplayError("skipped counts must be sorted")
        categories: set[str] = set()
        for category, count in self.skipped_signal_counts:
            if (
                not isinstance(category, str)
                or not category
                or len(category) > 32
                or not all(character.isupper() or character == "_" for character in category)
            ):
                raise IntervalForwardReplayError("skipped category is invalid")
            if category in categories:
                raise IntervalForwardReplayError("skipped categories must be unique")
            categories.add(category)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise IntervalForwardReplayError("skipped count is invalid")
            if count > MAX_EXPECTED_SESSION_OBSERVATIONS:
                raise IntervalForwardReplayError(
                    "skipped count exceeds the session maximum"
                )
        previous_exit: datetime | None = None
        for round_trip in self.round_trips:
            if previous_exit is not None and round_trip.entry_at <= previous_exit:
                raise IntervalForwardReplayError("round trips overlap or are unordered")
            previous_exit = round_trip.exit_at
        expected_preimage = canonical_sha256({
            "schema_version": "llm-interval-trade-preimage-v1",
            "round_trips": [item.to_payload() for item in self.round_trips],
        })
        if expected_preimage != self.ordered_trade_preimage_sha256:
            raise IntervalForwardReplayError("ordered trade preimage digest mismatch")
        expected_gross = _metric_calculation(
            lambda: sum(
                (item.gross_bps for item in self.round_trips),
                _ZERO,
            )
        )
        expected_fees = _metric_calculation(
            lambda: sum(
                (item.fee_bps for item in self.round_trips),
                _ZERO,
            )
        )
        expected_net = _metric_calculation(
            lambda: sum(
                (item.net_bps for item in self.round_trips),
                _ZERO,
            )
        )
        expected_drawdown = _max_drawdown_bps(
            [item.net_bps for item in self.round_trips]
        )
        if (
            self.gross_bps != expected_gross
            or self.modeled_fee_bps != expected_fees
            or self.net_bps != expected_net
            or self.max_drawdown_bps != expected_drawdown
        ):
            raise IntervalForwardReplayError("arm aggregate metrics are inconsistent")
        expected_arm_params = _arm_params_sha256(
            arm=self.arm,
            band=self.band,
            common_environment_sha256=self.common_environment_sha256,
        )
        if self.arm_params_sha256 != expected_arm_params:
            raise IntervalForwardReplayError("arm parameter digest mismatch")
        expected_arm_input = canonical_sha256({
            "schema_version": "llm-interval-arm-input-v1",
            "common_environment_sha256": self.common_environment_sha256,
            "arm_params_sha256": self.arm_params_sha256,
        })
        if self.arm_input_sha256 != expected_arm_input:
            raise IntervalForwardReplayError("arm input digest mismatch")
        expected_digest = canonical_sha256(self._preimage_payload())
        if not self.result_digest_sha256:
            object.__setattr__(self, "result_digest_sha256", expected_digest)
        elif self.result_digest_sha256 != expected_digest:
            raise IntervalForwardReplayError("arm result digest mismatch")

    def _preimage_payload(self) -> dict[str, object]:
        return {
            "schema_version": ARM_RESULT_SCHEMA_VERSION,
            "arm": self.arm,
            "band": self.band.to_payload(),
            "common_environment_sha256": self.common_environment_sha256,
            "arm_params_sha256": self.arm_params_sha256,
            "arm_input_sha256": self.arm_input_sha256,
            "closed_round_trips": self.closed_round_trips,
            "gross_bps": _decimal_text(self.gross_bps),
            "modeled_fee_bps": _decimal_text(self.modeled_fee_bps),
            "net_bps": _decimal_text(self.net_bps),
            "max_drawdown_bps": _decimal_text(self.max_drawdown_bps),
            "final_state": self.final_state,
            "skipped_signal_counts": [
                {"category": category, "count": count}
                for category, count in self.skipped_signal_counts
            ],
            "round_trips": [item.to_payload() for item in self.round_trips],
            "ordered_trade_preimage_sha256": self.ordered_trade_preimage_sha256,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._preimage_payload(),
            "result_digest_sha256": self.result_digest_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class PairedSessionLeaf:
    symbol: str
    target_session_date: date
    disposition: LeafDisposition
    reason: str
    registration_digest_sha256: str
    evaluator_digest_sha256: str
    expected_observation_count: int
    observed_observation_count: int
    finalized_at: datetime
    source_artifact_sha256: str | None = None
    common_environment_sha256: str | None = None
    baseline: VariantSessionResult | None = None
    candidate: VariantSessionResult | None = None
    delta_net_bps: Decimal | None = None
    permanent_limitations: tuple[str, ...] = PERMANENT_LIMITATIONS
    order_submission_allowed: Literal[False] = False
    live_config_mutation_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    promotion_eligible: Literal[False] = False
    leaf_digest_sha256: str = field(default="")

    def __post_init__(self) -> None:
        try:
            validate_symbol(self.symbol)
        except IntervalForwardContractError as exc:
            raise IntervalForwardReplayError("leaf symbol is invalid") from exc
        if type(self.target_session_date) is not date:
            raise IntervalForwardReplayError("leaf target date must be a date")
        if self.disposition not in {"PENDING", "MISSING", "INCLUDED", "INVALID"}:
            raise IntervalForwardReplayError("leaf disposition is invalid")
        if not self.reason or len(self.reason.encode("utf-8")) > 256:
            raise IntervalForwardReplayError("leaf reason must not be empty")
        _sha(
            self.registration_digest_sha256,
            field_name="registration_digest_sha256",
        )
        _sha(self.evaluator_digest_sha256, field_name="evaluator_digest_sha256")
        for field_name in (
            "expected_observation_count",
            "observed_observation_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntervalForwardReplayError(f"{field_name} is invalid")
            if value > MAX_EXPECTED_SESSION_OBSERVATIONS:
                raise IntervalForwardReplayError(
                    f"{field_name} exceeds the frozen maximum"
                )
        _utc(self.finalized_at, field_name="finalized_at")
        market: Literal["US", "HK"] = "HK" if self.symbol.endswith(".HK") else "US"
        schedule = full_session_observation_schedule(
            market,
            self.target_session_date,
        )
        if self.expected_observation_count != len(schedule):
            raise IntervalForwardReplayError(
                "leaf expected observation count does not match the calendar"
            )
        deadline = schedule[-1] + ARTIFACT_DEADLINE_DELAY
        if self.disposition == "PENDING" and self.finalized_at >= deadline:
            raise IntervalForwardReplayError("pending leaf passed its fixed deadline")
        if self.disposition == "MISSING" and self.finalized_at < deadline:
            raise IntervalForwardReplayError("missing leaf precedes its fixed deadline")
        if self.disposition == "INCLUDED" and self.finalized_at < schedule[-1]:
            raise IntervalForwardReplayError("included leaf predates session completion")
        if self.permanent_limitations != PERMANENT_LIMITATIONS:
            raise IntervalForwardReplayError("permanent limitations cannot change")
        for field_name in (
            "order_submission_allowed",
            "live_config_mutation_allowed",
            "automatic_promotion_allowed",
            "promotion_eligible",
        ):
            if getattr(self, field_name) is not False:
                raise IntervalForwardReplayError(f"{field_name} must remain false")

        if self.disposition == "INCLUDED":
            if (
                self.source_artifact_sha256 is None
                or self.common_environment_sha256 is None
                or self.baseline is None
                or self.candidate is None
                or self.delta_net_bps is None
            ):
                raise IntervalForwardReplayError(
                    "included leaf requires the complete paired evidence chain"
                )
            _sha(self.source_artifact_sha256, field_name="source_artifact_sha256")
            _sha(
                self.common_environment_sha256,
                field_name="common_environment_sha256",
            )
            if self.observed_observation_count != self.expected_observation_count:
                raise IntervalForwardReplayError(
                    "included leaf requires complete observation coverage"
                )
            if (
                self.baseline.common_environment_sha256
                != self.common_environment_sha256
                or self.candidate.common_environment_sha256
                != self.common_environment_sha256
            ):
                raise IntervalForwardReplayError(
                    "paired arms do not share the common environment"
                )
            if self.baseline.arm != "baseline" or self.candidate.arm != "candidate":
                raise IntervalForwardReplayError("paired arm labels are inconsistent")
            baseline = cast(VariantSessionResult, self.baseline)
            candidate = cast(VariantSessionResult, self.candidate)
            expected_delta = _metric_calculation(
                lambda: candidate.net_bps - baseline.net_bps
            )
            if self.delta_net_bps != expected_delta:
                raise IntervalForwardReplayError("paired delta is inconsistent")
        elif any(
            value is not None
            for value in (
                self.source_artifact_sha256,
                self.common_environment_sha256,
                self.baseline,
                self.candidate,
                self.delta_net_bps,
            )
        ):
            raise IntervalForwardReplayError(
                "non-included leaf cannot carry partial replay results"
            )
        expected_digest = canonical_sha256(self._preimage_payload())
        if not self.leaf_digest_sha256:
            object.__setattr__(self, "leaf_digest_sha256", expected_digest)
        elif self.leaf_digest_sha256 != expected_digest:
            raise IntervalForwardReplayError("daily leaf digest mismatch")

    def _preimage_payload(self) -> dict[str, object]:
        return {
            "schema_version": DAILY_LEAF_SCHEMA_VERSION,
            "symbol": self.symbol,
            "target_session_date": self.target_session_date.isoformat(),
            "disposition": self.disposition,
            "reason": self.reason,
            "registration_digest_sha256": self.registration_digest_sha256,
            "evaluator_digest_sha256": self.evaluator_digest_sha256,
            "expected_observation_count": self.expected_observation_count,
            "observed_observation_count": self.observed_observation_count,
            "finalized_at": _iso(self.finalized_at),
            "source_artifact_sha256": self.source_artifact_sha256,
            "common_environment_sha256": self.common_environment_sha256,
            "baseline": None if self.baseline is None else self.baseline.to_payload(),
            "candidate": None if self.candidate is None else self.candidate.to_payload(),
            "delta_net_bps": (
                None
                if self.delta_net_bps is None
                else _decimal_text(self.delta_net_bps)
            ),
            "permanent_limitations": list(self.permanent_limitations),
            "order_submission_allowed": self.order_submission_allowed,
            "live_config_mutation_allowed": self.live_config_mutation_allowed,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "promotion_eligible": self.promotion_eligible,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._preimage_payload(),
            "leaf_digest_sha256": self.leaf_digest_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class PairedLeafVerificationInput:
    """Server-frozen inputs required to re-verify one persisted daily leaf."""

    slot: FrozenSessionSlot
    proposal: ProposalObservation
    causal_proposals: tuple[ProposalObservation, ...]
    source_artifact: EncodedIntervalForwardArtifact | None

    def __post_init__(self) -> None:
        if type(self.slot) is not FrozenSessionSlot:
            raise IntervalForwardReplayError(
                "verification slot must use the exact frozen type"
            )
        if type(self.proposal) is not ProposalObservation:
            raise IntervalForwardReplayError(
                "verification proposal must use the exact frozen type"
            )
        if type(self.causal_proposals) is not tuple or not self.causal_proposals:
            raise IntervalForwardReplayError(
                "verification requires the complete causal proposal tuple"
            )
        if len(self.causal_proposals) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise IntervalForwardReplayError(
                "causal proposal tuple exceeds the frozen maximum"
            )
        registration_digests: set[str] = set()
        for causal_proposal in self.causal_proposals:
            if type(causal_proposal) is not ProposalObservation:
                raise IntervalForwardReplayError(
                    "causal proposal must use the exact frozen type"
                )
            if (
                causal_proposal.execution_policy.symbol != self.slot.symbol
                or causal_proposal.target_session_date
                != self.slot.target_session_date
                or causal_proposal.registered_at > self.slot.occupied_at
            ):
                raise IntervalForwardReplayError(
                    "causal proposal lies outside the frozen slot cutoff"
                )
            if causal_proposal.registration_digest_sha256 in registration_digests:
                raise IntervalForwardReplayError(
                    "causal proposal registrations must be unique"
                )
            registration_digests.add(
                causal_proposal.registration_digest_sha256
            )
        if (
            self.source_artifact is not None
            and type(self.source_artifact) is not EncodedIntervalForwardArtifact
        ):
            raise IntervalForwardReplayError(
                "verification artifact must use the exact encoded type"
            )
        rebuilt_slot = freeze_session_slot(
            self.causal_proposals,
            symbol=self.slot.symbol,
            target_session_date=self.slot.target_session_date,
            occupied_at=self.slot.occupied_at,
        )
        if (
            rebuilt_slot is None
            or rebuilt_slot.to_payload() != self.slot.to_payload()
        ):
            raise IntervalForwardReplayError(
                "verification slot failed causal registration replay"
            )
        resolved = resolve_session_slot(rebuilt_slot, self.causal_proposals)
        if (
            resolved is None
            or resolved.to_payload() != self.proposal.to_payload()
        ):
            raise IntervalForwardReplayError(
                "verification proposal is not the frozen session-slot winner"
            )
        if self.proposal.evaluator_digest_sha256 != evaluator_digest_sha256():
            raise IntervalForwardReplayError(
                "verification proposal evaluator does not match the frozen evaluator"
            )

    def verify(self, leaf: PairedSessionLeaf) -> PairedSessionLeaf:
        if type(leaf) is not PairedSessionLeaf:
            raise IntervalForwardReplayError(
                "daily leaf must use the exact frozen type"
            )
        if (
            leaf.symbol != self.proposal.execution_policy.symbol
            or leaf.target_session_date != self.proposal.target_session_date
            or leaf.registration_digest_sha256
            != self.proposal.registration_digest_sha256
            or leaf.evaluator_digest_sha256
            != self.proposal.evaluator_digest_sha256
        ):
            raise IntervalForwardReplayError(
                "daily leaf does not match its frozen verification inputs"
            )
        if leaf.disposition in {"INCLUDED", "INVALID"}:
            if self.source_artifact is None:
                raise IntervalForwardReplayError(
                    "replayed leaf requires its encoded source artifact"
                )
            bars = decode_source_artifact(
                self.proposal,
                self.source_artifact,
            )
            rebuilt = replay_paired_session(
                self.proposal,
                bars,
                finalized_at=leaf.finalized_at,
            )
        else:
            if self.source_artifact is not None:
                raise IntervalForwardReplayError(
                    "absent leaf cannot carry a source artifact"
                )
            rebuilt = absent_session_leaf(
                self.proposal,
                as_of=leaf.finalized_at,
                observed_count=leaf.observed_observation_count,
            )
        if rebuilt.to_payload() != leaf.to_payload():
            raise IntervalForwardReplayError(
                "daily leaf failed deterministic source replay"
            )
        return leaf


def source_artifact_payload(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
) -> dict[str, object]:
    if len(bars) > MAX_EXPECTED_SESSION_OBSERVATIONS:
        raise IntervalForwardReplayError("SOURCE_ARTIFACT_RESOURCE_LIMIT_EXCEEDED")
    return {
        "kind": REPLAY_ARTIFACT_KIND,
        "schema_version": INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION,
        "symbol": proposal.execution_policy.symbol,
        "target_session_date": proposal.target_session_date.isoformat(),
        "timestamp_semantics": TIMESTAMP_SEMANTICS,
        "data_fidelity": DATA_FIDELITY,
        "bbo_coverage": BBO_COVERAGE,
        "bars": [item.to_payload() for item in bars],
    }


def encode_source_artifact(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
) -> EncodedIntervalForwardArtifact:
    try:
        return encode_interval_forward_artifact(
            source_artifact_payload(proposal, bars)
        )
    except IntervalForwardArtifactError as exc:
        raise IntervalForwardReplayError(
            "SOURCE_ARTIFACT_ENCODING_FAILED"
        ) from exc


def _parse_canonical_timestamp(value: object, *, field_name: str) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > 40
        or not value.endswith("Z")
    ):
        raise IntervalForwardReplayError(f"{field_name} is not canonical UTC text")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise IntervalForwardReplayError(
            f"{field_name} is not a valid timestamp"
        ) from exc
    if _iso(parsed) != value:
        raise IntervalForwardReplayError(f"{field_name} is not canonical UTC text")
    return parsed


def _parse_canonical_decimal(value: object, *, field_name: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 256:
        raise IntervalForwardReplayError(f"{field_name} must be decimal text")
    try:
        parsed = Decimal(value)
    except ArithmeticError as exc:
        raise IntervalForwardReplayError(f"{field_name} is invalid") from exc
    if _decimal_text(parsed) != value:
        raise IntervalForwardReplayError(f"{field_name} is not canonical decimal text")
    return parsed


def bars_from_source_artifact_payload(
    proposal: ProposalObservation,
    payload: Mapping[str, object],
) -> tuple[ForwardBar, ...]:
    """Strictly rebuild typed bars from one bounded-decoded source payload."""
    expected_keys = {
        "kind",
        "schema_version",
        "symbol",
        "target_session_date",
        "timestamp_semantics",
        "data_fidelity",
        "bbo_coverage",
        "bars",
    }
    if set(payload) != expected_keys:
        raise IntervalForwardReplayError("source artifact fields are not exact")
    if (
        payload.get("kind") != REPLAY_ARTIFACT_KIND
        or payload.get("schema_version")
        != INTERVAL_FORWARD_PAYLOAD_SCHEMA_VERSION
        or payload.get("symbol") != proposal.execution_policy.symbol
        or payload.get("target_session_date")
        != proposal.target_session_date.isoformat()
        or payload.get("timestamp_semantics") != TIMESTAMP_SEMANTICS
        or payload.get("data_fidelity") != DATA_FIDELITY
        or payload.get("bbo_coverage") != BBO_COVERAGE
    ):
        raise IntervalForwardReplayError("source artifact identity mismatch")
    raw_bars = payload.get("bars")
    if not isinstance(raw_bars, list):
        raise IntervalForwardReplayError("source artifact bars must be a list")
    if len(raw_bars) > MAX_EXPECTED_SESSION_OBSERVATIONS:
        raise IntervalForwardReplayError("SOURCE_ARTIFACT_RESOURCE_LIMIT_EXCEEDED")
    expected_bar_keys = {
        "schema_version",
        "timestamp",
        "observed_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source_sha256",
    }
    bars: list[ForwardBar] = []
    for index, raw_bar in enumerate(raw_bars):
        if not isinstance(raw_bar, dict) or set(raw_bar) != expected_bar_keys:
            raise IntervalForwardReplayError(
                f"source artifact bar {index} fields are not exact"
            )
        if raw_bar.get("schema_version") != FORWARD_BAR_SCHEMA_VERSION:
            raise IntervalForwardReplayError(
                f"source artifact bar {index} schema drift"
            )
        source_sha256 = raw_bar.get("source_sha256")
        if not isinstance(source_sha256, str):
            raise IntervalForwardReplayError(
                f"source artifact bar {index} digest is invalid"
            )
        bars.append(ForwardBar(
            timestamp=_parse_canonical_timestamp(
                raw_bar.get("timestamp"),
                field_name=f"bar[{index}].timestamp",
            ),
            observed_at=_parse_canonical_timestamp(
                raw_bar.get("observed_at"),
                field_name=f"bar[{index}].observed_at",
            ),
            open=_parse_canonical_decimal(
                raw_bar.get("open"),
                field_name=f"bar[{index}].open",
            ),
            high=_parse_canonical_decimal(
                raw_bar.get("high"),
                field_name=f"bar[{index}].high",
            ),
            low=_parse_canonical_decimal(
                raw_bar.get("low"),
                field_name=f"bar[{index}].low",
            ),
            close=_parse_canonical_decimal(
                raw_bar.get("close"),
                field_name=f"bar[{index}].close",
            ),
            volume=_parse_canonical_decimal(
                raw_bar.get("volume"),
                field_name=f"bar[{index}].volume",
            ),
            source_sha256=source_sha256,
        ))
    values = tuple(bars)
    if source_artifact_payload(proposal, values) != dict(payload):
        raise IntervalForwardReplayError(
            "source artifact does not round-trip canonically"
        )
    return values


def decode_source_artifact(
    proposal: ProposalObservation,
    artifact: EncodedIntervalForwardArtifact,
) -> tuple[ForwardBar, ...]:
    try:
        payload = decode_interval_forward_artifact(
            digest_sha256=artifact.digest_sha256,
            schema_version=artifact.schema_version,
            kind=artifact.kind,
            codec=artifact.codec,
            raw_size=artifact.raw_size,
            compressed_size=artifact.compressed_size,
            payload=artifact.payload,
        )
    except IntervalForwardArtifactError as exc:
        raise IntervalForwardReplayError("source artifact decoding failed") from exc
    return bars_from_source_artifact_payload(proposal, payload)


def _validate_complete_bars(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
    *,
    finalized_at: datetime,
) -> tuple[ForwardBar, ...]:
    cutoff = _utc(finalized_at, field_name="finalized_at")
    if len(bars) > MAX_EXPECTED_SESSION_OBSERVATIONS:
        raise IntervalForwardReplayError("SOURCE_ARTIFACT_RESOURCE_LIMIT_EXCEEDED")
    values = tuple(bars)
    if len(values) != proposal.expected_observation_count:
        raise IntervalForwardReplayError("INCOMPLETE_OBSERVATION_SCHEDULE")
    expected = full_session_observation_schedule(
        proposal.execution_policy.market,
        proposal.target_session_date,
    )
    actual = tuple(item.timestamp for item in values)
    if actual != expected:
        raise IntervalForwardReplayError("OBSERVATION_SCHEDULE_MISMATCH")
    if observation_schedule_sha256(actual) != proposal.observation_schedule_sha256:
        raise IntervalForwardReplayError("OBSERVATION_SCHEDULE_DIGEST_MISMATCH")
    if cutoff < expected[-1]:
        raise IntervalForwardReplayError("FINALIZED_BEFORE_SESSION_COMPLETE")
    previous_observed: datetime | None = None
    for item in values:
        observed = _utc(item.observed_at, field_name="bar.observed_at")
        if observed > cutoff:
            raise IntervalForwardReplayError("BAR_OBSERVED_AFTER_FINALIZATION")
        if observed <= proposal.registered_at:
            raise IntervalForwardReplayError("PRE_REGISTRATION_BAR_FORBIDDEN")
        if previous_observed is not None and observed <= previous_observed:
            raise IntervalForwardReplayError(
                "BAR_OBSERVATION_TIMES_NOT_STRICTLY_INCREASING"
            )
        previous_observed = observed
    return values


def _common_environment_sha256(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
    *,
    source_artifact_sha256: str,
    finalized_at: datetime,
) -> str:
    return canonical_sha256({
        "schema_version": COMMON_ENVIRONMENT_SCHEMA_VERSION,
        "registration_digest_sha256": proposal.registration_digest_sha256,
        "execution_policy": proposal.execution_policy.to_payload(),
        "execution_policy_sha256": proposal.execution_policy.digest_sha256,
        "evaluator_digest_sha256": proposal.evaluator_digest_sha256,
        "source_artifact_sha256": source_artifact_sha256,
        "observation_schedule_sha256": proposal.observation_schedule_sha256,
        "expected_observation_count": proposal.expected_observation_count,
        "finalized_at": _iso(finalized_at),
        "data_fidelity": DATA_FIDELITY,
        "bbo_coverage": BBO_COVERAGE,
        "entry_crossing_semantics": ENTRY_CROSSING_SEMANTICS,
        "fee_model_fidelity": FEE_MODEL_FIDELITY,
        "timestamp_semantics": TIMESTAMP_SEMANTICS,
        "bar_source_digests": [item.source_sha256 for item in bars],
    })


def _arm_params_sha256(
    *,
    arm: ArmName,
    band: FrozenIntervalBand,
    common_environment_sha256: str,
) -> str:
    return canonical_sha256({
        "schema_version": "llm-interval-arm-params-v1",
        "arm": arm,
        "band": band.to_payload(),
        "band_sha256": band.digest_sha256,
        "common_environment_sha256": common_environment_sha256,
    })


def _build_variant_session_result(
    *,
    arm: ArmName,
    band: FrozenIntervalBand,
    common_environment_sha256: str,
    round_trips: Sequence[ReplayRoundTrip],
    skipped_signal_counts: Sequence[tuple[str, int]] = (),
) -> VariantSessionResult:
    """Rebuild one arm exclusively from its ordered closed round trips."""
    values = tuple(round_trips)
    counts = tuple(sorted(skipped_signal_counts))
    gross_bps = _metric_calculation(
        lambda: sum((item.gross_bps for item in values), _ZERO)
    )
    fee_bps = _metric_calculation(
        lambda: sum((item.fee_bps for item in values), _ZERO)
    )
    net_bps = _metric_calculation(
        lambda: sum((item.net_bps for item in values), _ZERO)
    )
    params_digest = _arm_params_sha256(
        arm=arm,
        band=band,
        common_environment_sha256=common_environment_sha256,
    )
    input_digest = canonical_sha256({
        "schema_version": "llm-interval-arm-input-v1",
        "common_environment_sha256": common_environment_sha256,
        "arm_params_sha256": params_digest,
    })
    trade_preimage = canonical_sha256({
        "schema_version": "llm-interval-trade-preimage-v1",
        "round_trips": [item.to_payload() for item in values],
    })
    return VariantSessionResult(
        arm=arm,
        band=band,
        common_environment_sha256=common_environment_sha256,
        arm_params_sha256=params_digest,
        arm_input_sha256=input_digest,
        closed_round_trips=len(values),
        gross_bps=gross_bps,
        modeled_fee_bps=fee_bps,
        net_bps=net_bps,
        max_drawdown_bps=_max_drawdown_bps([item.net_bps for item in values]),
        final_state="flat",
        skipped_signal_counts=counts,
        round_trips=values,
        ordered_trade_preimage_sha256=trade_preimage,
    )


def _round_trips_from_engine(
    result: backtest_module.BacktestResultData,
    *,
    policy: FrozenExecutionPolicy,
    band: FrozenIntervalBand,
    allowed_timestamps: frozenset[datetime],
) -> tuple[ReplayRoundTrip, ...]:
    entry: backtest_module.BacktestTrade | None = None
    output: list[ReplayRoundTrip] = []
    entry_count = 0
    previous_timestamp: datetime | None = None
    for event in result.trades:
        if (
            not math.isfinite(event.price)
            or not math.isfinite(event.quantity)
            or not math.isfinite(event.fee)
        ):
            raise IntervalForwardReplayError("NON_FINITE_ENGINE_TRADE")
        event_timestamp = _utc(
            event.timestamp,
            field_name="engine trade timestamp",
        )
        if event_timestamp not in allowed_timestamps:
            raise IntervalForwardReplayError("ENGINE_TRADE_OUTSIDE_FROZEN_SCHEDULE")
        if previous_timestamp is not None and event_timestamp <= previous_timestamp:
            raise IntervalForwardReplayError("ENGINE_TRADES_NOT_STRICTLY_ORDERED")
        previous_timestamp = event_timestamp
        event_price = _metric(event.price)
        event_fee = _metric(event.fee)
        if event_price <= _ZERO or event_price > MAX_PRICE or event_fee < _ZERO:
            raise IntervalForwardReplayError("ENGINE_TRADE_VALUE_OUTSIDE_BOUNDS")
        if not math.isclose(event.quantity, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise IntervalForwardReplayError("VIRTUAL_QUANTITY_DRIFT")
        expected_event_fee = _metric_calculation(
            lambda: (
                event_price * policy.one_side_fee_rate
                + policy.fixed_fee_per_share_per_order
            )
        )
        if event_fee != expected_event_fee:
            raise IntervalForwardReplayError("ENGINE_FEE_REBUILD_MISMATCH")
        if event.action in {"SELL_SHORT", "BUY_TO_COVER"} or event.action.endswith(
            "_COVER"
        ):
            raise IntervalForwardReplayError("SHORT_ACTION_FORBIDDEN")
        if event.action == "BUY":
            if entry is not None:
                raise IntervalForwardReplayError("POSITION_ADDON_FORBIDDEN")
            if event.state_after != "long":
                raise IntervalForwardReplayError("ENGINE_ENTRY_STATE_DRIFT")
            expected_entry_price = _metric_calculation(
                lambda: (
                    band.buy_low
                    * (
                        _ONE
                        + policy.per_side_slippage_bps / _TEN_THOUSAND
                    )
                )
            )
            if event_price != expected_entry_price:
                raise IntervalForwardReplayError("ENGINE_ENTRY_FILL_MISMATCH")
            entry = event
            entry_count += 1
            if entry_count > policy.max_entries_per_symbol_per_day:
                raise IntervalForwardReplayError("DAILY_ENTRY_CAP_EXCEEDED")
            continue
        if event.action in _LONG_EXIT_ACTIONS:
            if entry is None:
                raise IntervalForwardReplayError("EXIT_WITHOUT_ENTRY")
            if event.state_after != "flat":
                raise IntervalForwardReplayError("ENGINE_EXIT_STATE_DRIFT")
            if event.action == "SELL":
                expected_exit_price = _metric_calculation(
                    lambda: (
                        band.sell_high
                        * (
                            _ONE
                            - policy.per_side_slippage_bps / _TEN_THOUSAND
                        )
                    )
                )
                if event_price != expected_exit_price:
                    raise IntervalForwardReplayError("ENGINE_EXIT_FILL_MISMATCH")
            entry_price = _metric(entry.price)
            exit_price = event_price
            entry_fee = _metric(entry.fee)
            gross_pnl = _metric_calculation(lambda: exit_price - entry_price)
            modeled_fees = _metric_calculation(lambda: entry_fee + event_fee)
            net_pnl = _metric_calculation(lambda: gross_pnl - modeled_fees)
            if (
                event.gross_pnl is None
                or event.total_fees is None
                or event.net_pnl is None
                or event.holding_minutes is None
            ):
                raise IntervalForwardReplayError("ENGINE_EXIT_AUDIT_FIELDS_MISSING")
            if (
                _metric(event.gross_pnl) != gross_pnl
                or _metric(event.total_fees) != modeled_fees
                or _metric(event.net_pnl) != net_pnl
                or _metric(event.pnl) != net_pnl
            ):
                raise IntervalForwardReplayError("ENGINE_PNL_REBUILD_MISMATCH")
            with localcontext() as context:
                context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
                context.rounding = ROUND_HALF_EVEN
                gross_bps = _metric(
                    gross_pnl / entry_price * _TEN_THOUSAND
                )
                fee_bps = _metric(
                    modeled_fees / entry_price * _TEN_THOUSAND
                )
                net_bps = _metric(net_pnl / entry_price * _TEN_THOUSAND)
                holding_minutes = _metric(
                    Decimal(
                        str(
                            (
                                event_timestamp - entry.timestamp
                            ).total_seconds()
                        )
                    )
                    / Decimal("60")
                )
            if _metric(event.holding_minutes) != holding_minutes:
                raise IntervalForwardReplayError("ENGINE_HOLDING_TIME_MISMATCH")
            output.append(ReplayRoundTrip(
                entry_at=entry.timestamp,
                exit_at=event_timestamp,
                exit_action=event.action,
                entry_price=entry_price,
                exit_price=exit_price,
                gross_pnl=gross_pnl,
                modeled_fees=modeled_fees,
                net_pnl=net_pnl,
                gross_bps=gross_bps,
                fee_bps=fee_bps,
                net_bps=net_bps,
                holding_minutes=holding_minutes,
            ))
            entry = None
            continue
        raise IntervalForwardReplayError("UNSUPPORTED_ENGINE_ACTION")
    if entry is not None or result.metrics.final_state != "flat":
        raise IntervalForwardReplayError("FINAL_STATE_NOT_FLAT")
    if result.metrics.closed_trade_count != len(output):
        raise IntervalForwardReplayError("ENGINE_CLOSED_TRADE_COUNT_MISMATCH")
    if len(output) > policy.max_entries_per_symbol_per_day:
        raise IntervalForwardReplayError("DAILY_ENTRY_CAP_EXCEEDED")
    expected_total_pnl = _metric_calculation(
        lambda: sum((item.net_pnl for item in output), _ZERO)
    )
    expected_total_fees = _metric_calculation(
        lambda: sum((item.modeled_fees for item in output), _ZERO)
    )
    if (
        _metric(result.metrics.total_pnl) != expected_total_pnl
        or _metric(result.metrics.fees_paid) != expected_total_fees
    ):
        raise IntervalForwardReplayError("ENGINE_AGGREGATE_REBUILD_MISMATCH")
    return tuple(output)


def _max_drawdown_bps(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        cumulative = _ZERO
        peak = _ZERO
        maximum = _ZERO
        for value in values:
            cumulative += value
            peak = max(peak, cumulative)
            maximum = max(maximum, peak - cumulative)
        return _metric(maximum)


def _run_arm(
    *,
    arm: ArmName,
    band: FrozenIntervalBand,
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
    common_environment_sha256: str,
) -> VariantSessionResult:
    policy = proposal.execution_policy
    decision = counterfactual_policy_without_confidence(
        reference_price=proposal.reference_price,
        band=band,
        policy=policy,
    )
    if arm == "candidate" and not decision.allowed:
        raise IntervalForwardReplayError("CANDIDATE_POLICY_RECHECK_FAILED")
    with localcontext() as context:
        context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        slippage_pct = float(policy.per_side_slippage_bps / _HUNDRED)
    params = BacktestEngineParams(
        symbol=policy.symbol,
        buy_low=float(band.buy_low),
        sell_high=float(band.sell_high),
        short_selling=False,
        min_profit_amount=float(policy.minimum_profit_per_share),
        max_daily_loss=float(policy.max_daily_loss_per_share),
        max_drawdown_amount=float(policy.max_drawdown_per_share),
        max_consecutive_losses=policy.max_consecutive_losses,
        quantity=1.0,
        initial_cash=max(100_000.0, float(proposal.reference_price) * 10_000.0),
        fee_rate=float(policy.one_side_fee_rate),
        fixed_fee=float(policy.fixed_fee_per_share_per_order),
        slippage_pct=slippage_pct,
        stop_loss_pct=float(policy.stop_loss_pct),
        trailing_stop_pct=float(policy.trailing_stop_pct),
        market=policy.market,
        trading_session_mode=policy.trading_session_mode,
        opening_warmup_minutes=policy.opening_warmup_minutes,
        entry_crossing_required=True,
        max_entries_per_symbol_per_day=policy.max_entries_per_symbol_per_day,
        max_holding_minutes=policy.max_holding_minutes,
        entry_cutoff_minutes_before_close=policy.entry_cutoff_minutes_before_close,
        flatten_minutes_before_close=policy.flatten_minutes_before_close,
    )
    engine_bars = [
        BacktestBar(
            timestamp=item.timestamp,
            open=float(item.open),
            high=float(item.high),
            low=float(item.low),
            close=float(item.close),
            volume=float(item.volume),
        )
        for item in bars
    ]
    try:
        result = BacktestEngine(params).run(
            engine_bars,
            include_fee_sensitivity=False,
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise IntervalForwardReplayError("ENGINE_REPLAY_FAILED") from exc
    round_trips = _round_trips_from_engine(
        result,
        policy=policy,
        band=band,
        allowed_timestamps=frozenset(item.timestamp for item in bars),
    )
    skipped_counts = Counter(item.category or "UNCLASSIFIED" for item in result.skipped_signals)
    return _build_variant_session_result(
        arm=arm,
        band=band,
        common_environment_sha256=common_environment_sha256,
        round_trips=round_trips,
        skipped_signal_counts=tuple(skipped_counts.items()),
    )


def _non_included_leaf(
    proposal: ProposalObservation,
    *,
    disposition: Literal["PENDING", "MISSING", "INVALID"],
    reason: str,
    observed_count: int,
    finalized_at: datetime,
) -> PairedSessionLeaf:
    return PairedSessionLeaf(
        symbol=proposal.execution_policy.symbol,
        target_session_date=proposal.target_session_date,
        disposition=disposition,
        reason=reason,
        registration_digest_sha256=proposal.registration_digest_sha256,
        evaluator_digest_sha256=proposal.evaluator_digest_sha256,
        expected_observation_count=proposal.expected_observation_count,
        observed_observation_count=observed_count,
        finalized_at=finalized_at,
    )


def absent_session_leaf(
    proposal: ProposalObservation,
    *,
    as_of: datetime,
    observed_count: int = 0,
) -> PairedSessionLeaf:
    """Create a denominator-preserving PENDING or terminal MISSING leaf."""
    now = _utc(as_of, field_name="as_of")
    schedule = full_session_observation_schedule(
        proposal.execution_policy.market,
        proposal.target_session_date,
    )
    deadline = schedule[-1] + ARTIFACT_DEADLINE_DELAY
    disposition: Literal["PENDING", "MISSING"] = (
        "PENDING" if now < deadline else "MISSING"
    )
    reason = (
        "AWAITING_COMPLETE_SOURCE_ARTIFACT"
        if disposition == "PENDING"
        else "SOURCE_ARTIFACT_MISSING_AT_FIXED_DEADLINE"
    )
    return _non_included_leaf(
        proposal,
        disposition=disposition,
        reason=reason,
        observed_count=observed_count,
        finalized_at=now,
    )


def replay_paired_session(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
    *,
    finalized_at: datetime,
) -> PairedSessionLeaf:
    """Replay baseline and candidate against one identical complete OHLC artifact."""
    cutoff = _utc(finalized_at, field_name="finalized_at")
    bounded_observed_count = min(
        len(bars),
        MAX_EXPECTED_SESSION_OBSERVATIONS,
    )
    current_evaluator = evaluator_digest_sha256()
    if proposal.evaluator_digest_sha256 != current_evaluator:
        return _non_included_leaf(
            proposal,
            disposition="INVALID",
            reason="EVALUATOR_DRIFT",
            observed_count=bounded_observed_count,
            finalized_at=cutoff,
        )
    try:
        trusted_bars = _validate_complete_bars(
            proposal,
            bars,
            finalized_at=cutoff,
        )
        encoded_artifact = encode_source_artifact(proposal, trusted_bars)
        source_digest = encoded_artifact.digest_sha256
        common_digest = _common_environment_sha256(
            proposal,
            trusted_bars,
            source_artifact_sha256=source_digest,
            finalized_at=cutoff,
        )
        baseline = _run_arm(
            arm="baseline",
            band=proposal.baseline_band,
            proposal=proposal,
            bars=trusted_bars,
            common_environment_sha256=common_digest,
        )
        candidate = _run_arm(
            arm="candidate",
            band=proposal.effective_candidate_band,
            proposal=proposal,
            bars=trusted_bars,
            common_environment_sha256=common_digest,
        )
    except IntervalForwardReplayError as exc:
        return _non_included_leaf(
            proposal,
            disposition="INVALID",
            reason=str(exc),
            observed_count=bounded_observed_count,
            finalized_at=cutoff,
        )
    return PairedSessionLeaf(
        symbol=proposal.execution_policy.symbol,
        target_session_date=proposal.target_session_date,
        disposition="INCLUDED",
        reason="COMPLETE_PAIRED_DIAGNOSTIC_REPLAY",
        registration_digest_sha256=proposal.registration_digest_sha256,
        evaluator_digest_sha256=proposal.evaluator_digest_sha256,
        expected_observation_count=proposal.expected_observation_count,
        observed_observation_count=len(trusted_bars),
        finalized_at=cutoff,
        source_artifact_sha256=source_digest,
        common_environment_sha256=common_digest,
        baseline=baseline,
        candidate=candidate,
        delta_net_bps=_metric_calculation(
            lambda: candidate.net_bps - baseline.net_bps
        ),
    )


@dataclass(frozen=True, kw_only=True)
class PairedReplayBundle:
    """One atomically persistable leaf plus its exact encoded source artifact."""

    proposal: ProposalObservation
    leaf: PairedSessionLeaf
    source_artifact: EncodedIntervalForwardArtifact

    def __post_init__(self) -> None:
        if type(self.proposal) is not ProposalObservation:
            raise IntervalForwardReplayError(
                "paired replay proposal must use the exact frozen type"
            )
        if type(self.leaf) is not PairedSessionLeaf:
            raise IntervalForwardReplayError(
                "paired replay leaf must use the exact frozen type"
            )
        if type(self.source_artifact) is not EncodedIntervalForwardArtifact:
            raise IntervalForwardReplayError(
                "paired replay artifact must use the exact encoded type"
            )
        bars = decode_source_artifact(self.proposal, self.source_artifact)
        rebuilt = replay_paired_session(
            self.proposal,
            bars,
            finalized_at=self.leaf.finalized_at,
        )
        if rebuilt.to_payload() != self.leaf.to_payload():
            raise IntervalForwardReplayError(
                "paired replay bundle leaf and artifact are inconsistent"
            )


def replay_paired_session_bundle(
    proposal: ProposalObservation,
    bars: Sequence[ForwardBar],
    *,
    finalized_at: datetime,
) -> PairedReplayBundle:
    if len(bars) > MAX_EXPECTED_SESSION_OBSERVATIONS:
        raise IntervalForwardReplayError("SOURCE_ARTIFACT_RESOURCE_LIMIT_EXCEEDED")
    values = tuple(bars)
    source_artifact = encode_source_artifact(proposal, values)
    leaf = replay_paired_session(
        proposal,
        values,
        finalized_at=finalized_at,
    )
    return PairedReplayBundle(
        proposal=proposal,
        leaf=leaf,
        source_artifact=source_artifact,
    )


@dataclass(frozen=True, kw_only=True)
class IntervalForwardAssessment:
    symbol: str
    expected_session_dates: tuple[date, ...]
    assessment_cutoff: datetime
    evaluator_digest_sha256: str
    expected_sessions: int
    included_sessions: int
    pending_sessions: int
    missing_sessions: int
    invalid_sessions: int
    paired_coverage_ratio: Decimal
    baseline_closed_round_trips: int
    candidate_closed_round_trips: int
    baseline_net_bps: Decimal
    candidate_net_bps: Decimal
    cumulative_delta_bps: Decimal
    mean_session_delta_bps: Decimal
    confidence_lower_bps: Decimal | None
    confidence_upper_bps: Decimal | None
    baseline_max_drawdown_bps: Decimal
    candidate_max_drawdown_bps: Decimal
    max_daily_delta_concentration: Decimal
    horizon_complete: bool
    human_review_discussion_eligible: bool
    blockers: tuple[str, ...]
    permanent_limitations: tuple[str, ...] = PERMANENT_LIMITATIONS
    order_submission_allowed: Literal[False] = False
    live_config_mutation_allowed: Literal[False] = False
    automatic_promotion_allowed: Literal[False] = False
    promotion_eligible: Literal[False] = False
    evidence_root_sha256: str = ""
    report_digest_sha256: str = field(default="")

    def __post_init__(self) -> None:
        try:
            validate_symbol(self.symbol)
        except IntervalForwardContractError as exc:
            raise IntervalForwardReplayError("assessment symbol is invalid") from exc
        if len(self.expected_session_dates) != FIXED_HORIZON_SESSIONS:
            raise IntervalForwardReplayError("assessment horizon must remain fixed")
        if self.expected_sessions != len(self.expected_session_dates):
            raise IntervalForwardReplayError("expected session count is inconsistent")
        if len(set(self.expected_session_dates)) != len(self.expected_session_dates):
            raise IntervalForwardReplayError("expected sessions contain duplicates")
        if tuple(sorted(self.expected_session_dates)) != self.expected_session_dates:
            raise IntervalForwardReplayError("expected sessions must be sorted")
        market: Literal["US", "HK"] = (
            "HK" if self.symbol.endswith(".HK") else "US"
        )
        if self.expected_session_dates != fixed_assessment_session_dates(
            market,
            self.expected_session_dates[0],
        ):
            raise IntervalForwardReplayError(
                "assessment sessions are not the frozen consecutive horizon"
            )
        _utc(self.assessment_cutoff, field_name="assessment_cutoff")
        _sha(self.evaluator_digest_sha256, field_name="evaluator_digest_sha256")
        if self.evaluator_digest_sha256 != evaluator_digest_sha256():
            raise IntervalForwardReplayError("assessment evaluator digest drift")
        for field_name in (
            "expected_sessions",
            "included_sessions",
            "pending_sessions",
            "missing_sessions",
            "invalid_sessions",
            "baseline_closed_round_trips",
            "candidate_closed_round_trips",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntervalForwardReplayError(f"{field_name} is invalid")
        if sum((
            self.included_sessions,
            self.pending_sessions,
            self.missing_sessions,
            self.invalid_sessions,
        )) != self.expected_sessions:
            raise IntervalForwardReplayError("assessment disposition counts mismatch")
        for field_name in (
            "paired_coverage_ratio",
            "baseline_net_bps",
            "candidate_net_bps",
            "cumulative_delta_bps",
            "mean_session_delta_bps",
            "baseline_max_drawdown_bps",
            "candidate_max_drawdown_bps",
            "max_daily_delta_concentration",
        ):
            _decimal(getattr(self, field_name), field_name=field_name)
        if not _ZERO <= self.paired_coverage_ratio <= _ONE:
            raise IntervalForwardReplayError("paired coverage ratio is invalid")
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            expected_coverage = _metric(
                Decimal(self.included_sessions) / Decimal(self.expected_sessions)
            )
        if self.paired_coverage_ratio != expected_coverage:
            raise IntervalForwardReplayError("paired coverage ratio is inconsistent")
        if (
            self.baseline_closed_round_trips > self.included_sessions
            or self.candidate_closed_round_trips > self.included_sessions
        ):
            raise IntervalForwardReplayError(
                "assessment round-trip count exceeds its session count"
            )
        if self.cumulative_delta_bps != _metric_calculation(
            lambda: self.candidate_net_bps - self.baseline_net_bps
        ):
            raise IntervalForwardReplayError("assessment cumulative delta is inconsistent")
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            expected_mean = (
                _metric(
                    self.cumulative_delta_bps / Decimal(self.included_sessions)
                )
                if self.included_sessions
                else _ZERO
            )
        if self.mean_session_delta_bps != expected_mean:
            raise IntervalForwardReplayError("assessment mean delta is inconsistent")
        if (
            self.baseline_max_drawdown_bps < _ZERO
            or self.candidate_max_drawdown_bps < _ZERO
            or not _ZERO <= self.max_daily_delta_concentration <= _ONE
        ):
            raise IntervalForwardReplayError("assessment risk metrics are invalid")
        if (self.confidence_lower_bps is None) != (
            self.confidence_upper_bps is None
        ):
            raise IntervalForwardReplayError("assessment confidence interval is partial")
        if self.confidence_lower_bps is not None:
            lower = _decimal(
                self.confidence_lower_bps,
                field_name="confidence_lower_bps",
            )
            upper = _decimal(
                self.confidence_upper_bps,
                field_name="confidence_upper_bps",
            )
            if lower > upper:
                raise IntervalForwardReplayError(
                    "assessment confidence interval is inverted"
                )
        if len(set(self.blockers)) != len(self.blockers) or any(
            blocker not in ASSESSMENT_BLOCKER_CODES for blocker in self.blockers
        ):
            raise IntervalForwardReplayError("assessment blockers are invalid")
        if type(self.horizon_complete) is not bool or type(
            self.human_review_discussion_eligible
        ) is not bool:
            raise IntervalForwardReplayError("assessment boolean field is invalid")
        expected_blockers: list[str] = []
        if not self.horizon_complete:
            expected_blockers.append("FIXED_HORIZON_NOT_COMPLETE")
        if self.missing_sessions:
            expected_blockers.append("MISSING_SESSIONS")
        if self.invalid_sessions:
            expected_blockers.append("INVALID_SESSIONS")
        if self.included_sessions < MINIMUM_INCLUDED_SESSIONS:
            expected_blockers.append("INSUFFICIENT_INCLUDED_SESSIONS")
        if (
            self.baseline_closed_round_trips
            < MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM
        ):
            expected_blockers.append(
                "INSUFFICIENT_BASELINE_CLOSED_ROUND_TRIPS"
            )
        if (
            self.candidate_closed_round_trips
            < MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM
        ):
            expected_blockers.append(
                "INSUFFICIENT_CANDIDATE_CLOSED_ROUND_TRIPS"
            )
        if self.candidate_net_bps <= _ZERO:
            expected_blockers.append("CANDIDATE_NET_NOT_POSITIVE")
        if self.confidence_lower_bps is None or self.confidence_lower_bps <= _ZERO:
            expected_blockers.append("DELTA_CONFIDENCE_LOWER_NOT_POSITIVE")
        if self.max_daily_delta_concentration > MAX_DAILY_DELTA_CONCENTRATION:
            expected_blockers.append("DAILY_DELTA_CONCENTRATION_TOO_HIGH")
        if self.included_sessions != self.expected_sessions:
            expected_blockers.append("PAIRED_COVERAGE_INCOMPLETE")
        if self.blockers != tuple(expected_blockers):
            raise IntervalForwardReplayError(
                "assessment blockers do not match the frozen policy"
            )
        if self.horizon_complete != (self.pending_sessions == 0):
            raise IntervalForwardReplayError(
                "assessment horizon completion is inconsistent"
            )
        if self.human_review_discussion_eligible != (not self.blockers):
            raise IntervalForwardReplayError(
                "assessment eligibility and blockers are inconsistent"
            )
        if (
            self.human_review_discussion_eligible
            and (not self.horizon_complete or self.candidate_net_bps <= _ZERO)
        ):
            raise IntervalForwardReplayError(
                "eligible assessment violates the frozen success conditions"
            )
        if self.permanent_limitations != PERMANENT_LIMITATIONS:
            raise IntervalForwardReplayError("assessment limitations cannot change")
        for field_name in (
            "order_submission_allowed",
            "live_config_mutation_allowed",
            "automatic_promotion_allowed",
            "promotion_eligible",
        ):
            if getattr(self, field_name) is not False:
                raise IntervalForwardReplayError(f"{field_name} must remain false")
        _sha(self.evidence_root_sha256, field_name="evidence_root_sha256")
        expected_digest = canonical_sha256(self._preimage_payload())
        if not self.report_digest_sha256:
            object.__setattr__(self, "report_digest_sha256", expected_digest)
        elif self.report_digest_sha256 != expected_digest:
            raise IntervalForwardReplayError("assessment report digest mismatch")

    def _preimage_payload(self) -> dict[str, object]:
        def decimal_or_none(value: Decimal | None) -> str | None:
            return None if value is None else _decimal_text(value)

        return {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "policy_version": ASSESSMENT_POLICY_VERSION,
            "symbol": self.symbol,
            "expected_session_dates": [
                item.isoformat() for item in self.expected_session_dates
            ],
            "assessment_cutoff": _iso(self.assessment_cutoff),
            "evaluator_digest_sha256": self.evaluator_digest_sha256,
            "expected_sessions": self.expected_sessions,
            "included_sessions": self.included_sessions,
            "pending_sessions": self.pending_sessions,
            "missing_sessions": self.missing_sessions,
            "invalid_sessions": self.invalid_sessions,
            "paired_coverage_ratio": _decimal_text(self.paired_coverage_ratio),
            "baseline_closed_round_trips": self.baseline_closed_round_trips,
            "candidate_closed_round_trips": self.candidate_closed_round_trips,
            "baseline_net_bps": _decimal_text(self.baseline_net_bps),
            "candidate_net_bps": _decimal_text(self.candidate_net_bps),
            "cumulative_delta_bps": _decimal_text(self.cumulative_delta_bps),
            "mean_session_delta_bps": _decimal_text(self.mean_session_delta_bps),
            "confidence_lower_bps": decimal_or_none(self.confidence_lower_bps),
            "confidence_upper_bps": decimal_or_none(self.confidence_upper_bps),
            "baseline_max_drawdown_bps": _decimal_text(
                self.baseline_max_drawdown_bps
            ),
            "candidate_max_drawdown_bps": _decimal_text(
                self.candidate_max_drawdown_bps
            ),
            "max_daily_delta_concentration": _decimal_text(
                self.max_daily_delta_concentration
            ),
            "horizon_complete": self.horizon_complete,
            "human_review_discussion_eligible": (
                self.human_review_discussion_eligible
            ),
            "blockers": list(self.blockers),
            "permanent_limitations": list(self.permanent_limitations),
            "order_submission_allowed": self.order_submission_allowed,
            "live_config_mutation_allowed": self.live_config_mutation_allowed,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "promotion_eligible": self.promotion_eligible,
            "evidence_root_sha256": self.evidence_root_sha256,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            **self._preimage_payload(),
            "report_digest_sha256": self.report_digest_sha256,
        }


def fixed_assessment_session_dates(
    market: Literal["US", "HK"],
    first_target_session_date: date,
) -> tuple[date, ...]:
    """Freeze 60 consecutive open exchange sessions without cherry-picking."""
    if (
        first_target_session_date.weekday() >= 5
        or holiday_calendar_module.is_market_closed(
            market,
            first_target_session_date,
        )
    ):
        raise IntervalForwardReplayError("first assessment date is not an open session")
    values: list[date] = []
    cursor = first_target_session_date
    while len(values) < FIXED_HORIZON_SESSIONS:
        if not (
            holiday_calendar_module.COVERAGE_START_YEAR
            <= cursor.year
            <= holiday_calendar_module.COVERAGE_END_YEAR
        ):
            raise IntervalForwardReplayError(
                "assessment horizon exceeds holiday-calendar coverage"
            )
        if cursor.weekday() < 5 and not holiday_calendar_module.is_market_closed(
            market,
            cursor,
        ):
            values.append(cursor)
        cursor += timedelta(days=1)
    return tuple(values)


def assess_paired_sessions(
    *,
    symbol: str,
    expected_session_dates: Sequence[date],
    leaves: Sequence[PairedSessionLeaf],
    verification_inputs: Sequence[PairedLeafVerificationInput],
    assessment_cutoff: datetime,
) -> IntervalForwardAssessment:
    """Re-verify every source chain before computing fixed-horizon metrics."""
    inputs_by_date: dict[date, PairedLeafVerificationInput] = {}
    for verification_input in verification_inputs:
        if type(verification_input) is not PairedLeafVerificationInput:
            raise IntervalForwardReplayError(
                "verification input must use the exact frozen type"
            )
        target_date = verification_input.proposal.target_session_date
        if target_date in inputs_by_date:
            raise IntervalForwardReplayError("duplicate leaf verification input")
        inputs_by_date[target_date] = verification_input
    if len(inputs_by_date) != len(leaves):
        raise IntervalForwardReplayError(
            "every daily leaf requires one source verification input"
        )
    verified: list[PairedSessionLeaf] = []
    for leaf in leaves:
        if type(leaf) is not PairedSessionLeaf:
            raise IntervalForwardReplayError(
                "daily leaf must use the exact frozen type"
            )
        verification_input = inputs_by_date.get(leaf.target_session_date)
        if verification_input is None:
            raise IntervalForwardReplayError(
                "daily leaf is missing its source verification input"
            )
        verified.append(
            PairedLeafVerificationInput.verify(verification_input, leaf)
        )
    return _assess_verified_paired_leaves(
        symbol=symbol,
        expected_session_dates=expected_session_dates,
        leaves=verified,
        assessment_cutoff=assessment_cutoff,
    )


def _assess_verified_paired_leaves(
    *,
    symbol: str,
    expected_session_dates: Sequence[date],
    leaves: Sequence[PairedSessionLeaf],
    assessment_cutoff: datetime,
) -> IntervalForwardAssessment:
    try:
        validate_symbol(symbol)
    except IntervalForwardContractError as exc:
        raise IntervalForwardReplayError("assessment symbol is invalid") from exc
    expected = tuple(expected_session_dates)
    if len(expected) != FIXED_HORIZON_SESSIONS:
        raise IntervalForwardReplayError(
            f"fixed assessment requires {FIXED_HORIZON_SESSIONS} sessions"
        )
    if tuple(sorted(expected)) != expected or len(set(expected)) != len(expected):
        raise IntervalForwardReplayError(
            "expected sessions must be sorted and unique"
        )
    market: Literal["US", "HK"] = "HK" if symbol.endswith(".HK") else "US"
    if expected != fixed_assessment_session_dates(market, expected[0]):
        raise IntervalForwardReplayError(
            "expected sessions must be consecutive frozen exchange sessions"
        )
    expected_set = set(expected)
    by_date: dict[date, PairedSessionLeaf] = {}
    for leaf in leaves:
        if type(leaf) is not PairedSessionLeaf:
            raise IntervalForwardReplayError(
                "daily leaf must use the exact frozen type"
            )
        if leaf.symbol != symbol:
            raise IntervalForwardReplayError("leaf symbol does not match assessment")
        if leaf.target_session_date in by_date:
            raise IntervalForwardReplayError("duplicate daily leaf")
        if leaf.target_session_date not in expected_set:
            raise IntervalForwardReplayError("leaf lies outside the fixed horizon")
        by_date[leaf.target_session_date] = leaf
    if set(by_date) != expected_set:
        raise IntervalForwardReplayError(
            "every fixed-horizon session requires a denominator leaf"
        )
    ordered = [by_date[item] for item in expected]
    current_evaluator = evaluator_digest_sha256()
    evaluator_digests = {item.evaluator_digest_sha256 for item in ordered}
    if evaluator_digests != {current_evaluator}:
        raise IntervalForwardReplayError(
            "assessment leaves must share the current frozen evaluator"
        )
    cutoff = _utc(assessment_cutoff, field_name="assessment_cutoff")
    if any(item.finalized_at > cutoff for item in ordered):
        raise IntervalForwardReplayError(
            "assessment cutoff precedes a daily leaf finalization"
        )
    counts = Counter(item.disposition for item in ordered)
    included = [item for item in ordered if item.disposition == "INCLUDED"]
    baseline_values = [
        cast(VariantSessionResult, item.baseline).net_bps for item in included
    ]
    candidate_values = [
        cast(VariantSessionResult, item.candidate).net_bps for item in included
    ]
    deltas = [cast(Decimal, item.delta_net_bps) for item in included]
    baseline_trades = sum(
        cast(VariantSessionResult, item.baseline).closed_round_trips
        for item in included
    )
    candidate_trades = sum(
        cast(VariantSessionResult, item.candidate).closed_round_trips
        for item in included
    )
    baseline_net = _metric_calculation(lambda: sum(baseline_values, _ZERO))
    candidate_net = _metric_calculation(lambda: sum(candidate_values, _ZERO))
    cumulative_delta = _metric_calculation(lambda: sum(deltas, _ZERO))
    with localcontext() as context:
        context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        mean_delta = (
            _metric(cumulative_delta / Decimal(len(deltas)))
            if deltas
            else _ZERO
        )
    ci_lower: Decimal | None = None
    ci_upper: Decimal | None = None
    if len(deltas) >= 2:
        with localcontext() as context:
            context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            count = Decimal(len(deltas))
            center = sum(deltas, _ZERO) / count
            squared_deviations = sum(
                ((item - center) ** 2 for item in deltas),
                _ZERO,
            )
            sample_variance = squared_deviations / Decimal(len(deltas) - 1)
            standard_error = (sample_variance / count).sqrt()
            ci_lower = _metric(
                center - CONFIDENCE_MULTIPLIER * standard_error
            )
            ci_upper = _metric(
                center + CONFIDENCE_MULTIPLIER * standard_error
            )
    with localcontext() as context:
        context.prec = _FROZEN_REPLAY_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        absolute_delta = sum((abs(item) for item in deltas), _ZERO)
        concentration = (
            _ZERO
            if absolute_delta == _ZERO
            else _metric(max(abs(item) for item in deltas) / absolute_delta)
        )
    horizon_complete = counts["PENDING"] == 0
    blockers: list[str] = []
    if not horizon_complete:
        blockers.append("FIXED_HORIZON_NOT_COMPLETE")
    if counts["MISSING"]:
        blockers.append("MISSING_SESSIONS")
    if counts["INVALID"]:
        blockers.append("INVALID_SESSIONS")
    if len(included) < MINIMUM_INCLUDED_SESSIONS:
        blockers.append("INSUFFICIENT_INCLUDED_SESSIONS")
    if baseline_trades < MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM:
        blockers.append("INSUFFICIENT_BASELINE_CLOSED_ROUND_TRIPS")
    if candidate_trades < MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM:
        blockers.append("INSUFFICIENT_CANDIDATE_CLOSED_ROUND_TRIPS")
    if candidate_net <= _ZERO:
        blockers.append("CANDIDATE_NET_NOT_POSITIVE")
    if ci_lower is None or ci_lower <= _ZERO:
        blockers.append("DELTA_CONFIDENCE_LOWER_NOT_POSITIVE")
    if concentration > MAX_DAILY_DELTA_CONCENTRATION:
        blockers.append("DAILY_DELTA_CONCENTRATION_TOO_HIGH")
    if len(included) != len(expected):
        blockers.append("PAIRED_COVERAGE_INCOMPLETE")
    eligible = not blockers
    evidence_root = canonical_sha256({
        "schema_version": "llm-interval-forward-evidence-root-v1",
        "symbol": symbol,
        "evaluator_digest_sha256": current_evaluator,
        "expected_session_dates": [item.isoformat() for item in expected],
        "leaf_digests": [item.leaf_digest_sha256 for item in ordered],
    })
    return IntervalForwardAssessment(
        symbol=symbol,
        expected_session_dates=expected,
        assessment_cutoff=cutoff,
        evaluator_digest_sha256=current_evaluator,
        expected_sessions=len(expected),
        included_sessions=len(included),
        pending_sessions=counts["PENDING"],
        missing_sessions=counts["MISSING"],
        invalid_sessions=counts["INVALID"],
        paired_coverage_ratio=_metric_calculation(
            lambda: Decimal(len(included)) / Decimal(len(expected))
        ),
        baseline_closed_round_trips=baseline_trades,
        candidate_closed_round_trips=candidate_trades,
        baseline_net_bps=baseline_net,
        candidate_net_bps=candidate_net,
        cumulative_delta_bps=cumulative_delta,
        mean_session_delta_bps=mean_delta,
        confidence_lower_bps=ci_lower,
        confidence_upper_bps=ci_upper,
        baseline_max_drawdown_bps=_max_drawdown_bps(baseline_values),
        candidate_max_drawdown_bps=_max_drawdown_bps(candidate_values),
        max_daily_delta_concentration=concentration,
        horizon_complete=horizon_complete,
        human_review_discussion_eligible=eligible,
        blockers=tuple(blockers),
        evidence_root_sha256=evidence_root,
    )


def _canonical_source_sha256(value: ModuleType) -> str:
    """Hash repository source text without interpreter-specific AST serialization."""
    source = inspect.getsource(value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in source.split("\n"))
    return content_sha256(f"{normalized.rstrip()}\n")


def evaluator_manifest() -> dict[str, object]:
    source_modules: Mapping[str, ModuleType] = {
        "artifact_module": artifact_module,
        "backtest_module": backtest_module,
        "fees_module": fees_module,
        "holiday_calendar_module": holiday_calendar_module,
        "interval_contract_module": contract_module,
        "interval_replay_module": sys.modules[__name__],
        "market_calendar_module": market_calendar_module,
    }
    return {
        "algorithm_version": EVALUATOR_ALGORITHM_VERSION,
        "semantic_constants": {
            "artifact_deadline_delay_seconds": int(
                ARTIFACT_DEADLINE_DELAY.total_seconds()
            ),
            "assessment_policy_version": ASSESSMENT_POLICY_VERSION,
            "bbo_coverage": BBO_COVERAGE,
            "confidence_multiplier": _decimal_text(
                Decimal(str(CONFIDENCE_MULTIPLIER))
            ),
            "data_fidelity": DATA_FIDELITY,
            "entry_crossing_semantics": ENTRY_CROSSING_SEMANTICS,
            "fee_model_fidelity": FEE_MODEL_FIDELITY,
            "fixed_horizon_sessions": FIXED_HORIZON_SESSIONS,
            "max_daily_delta_concentration": _decimal_text(
                MAX_DAILY_DELTA_CONCENTRATION
            ),
            "minimum_closed_round_trips_per_arm": (
                MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM
            ),
            "minimum_included_sessions": MINIMUM_INCLUDED_SESSIONS,
            "permanent_limitations": list(PERMANENT_LIMITATIONS),
            "timestamp_semantics": TIMESTAMP_SEMANTICS,
        },
        "source_sha256": {
            key: _canonical_source_sha256(value)
            for key, value in sorted(source_modules.items())
        },
    }


@lru_cache(maxsize=1)
def evaluator_digest_sha256() -> str:
    return canonical_sha256(evaluator_manifest())


__all__ = [
    "ARTIFACT_DEADLINE_DELAY",
    "ASSESSMENT_POLICY_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "COMMON_ENVIRONMENT_SCHEMA_VERSION",
    "EVALUATOR_ALGORITHM_VERSION",
    "FIXED_HORIZON_SESSIONS",
    "ForwardBar",
    "IntervalForwardAssessment",
    "IntervalForwardReplayError",
    "MAX_DAILY_DELTA_CONCENTRATION",
    "MINIMUM_CLOSED_ROUND_TRIPS_PER_ARM",
    "MINIMUM_INCLUDED_SESSIONS",
    "PERMANENT_LIMITATIONS",
    "PairedSessionLeaf",
    "PairedLeafVerificationInput",
    "PairedReplayBundle",
    "ReplayRoundTrip",
    "VariantSessionResult",
    "absent_session_leaf",
    "assess_paired_sessions",
    "bars_from_source_artifact_payload",
    "decode_source_artifact",
    "encode_source_artifact",
    "evaluator_digest_sha256",
    "evaluator_manifest",
    "fixed_assessment_session_dates",
    "replay_paired_session",
    "replay_paired_session_bundle",
    "source_artifact_payload",
]
