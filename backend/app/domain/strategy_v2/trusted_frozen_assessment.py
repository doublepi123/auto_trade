from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.domain.strategy_v2.frozen_disproof_queue import (
    CONTROL_SYMBOL,
    FORWARD_CANDIDATE_ALGORITHM_VERSION,
    FROZEN_EVALUATOR_DIGEST,
    FROZEN_QUEUE_AS_OF_DATE,
    FROZEN_QUEUE_DIGEST,
    FROZEN_QUEUE_ENTRIES,
    MINIMUM_CLOSED_TRADES,
    MINIMUM_EXPECTED_SESSION_COVERAGE_RATIO,
    MINIMUM_FUTURE_TRADE_DAYS,
)


TRUSTED_ASSESSMENT_POLICY_VERSION = (
    "strategy-v2-frozen-forward-disproof-trusted-assessment-v3"
)
TRUSTED_ASSESSMENT_ALGORITHM_VERSION = (
    "strategy-v2-frozen-forward-disproof-trusted-algorithm-v3"
)
TRUSTED_ASSESSMENT_REPORT_SCHEMA_VERSION = (
    "strategy-v2-frozen-forward-disproof-trusted-report-v3"
)
TRUSTED_ASSESSMENT_WINDOW_START = date(2026, 8, 3)
TRUSTED_ASSESSMENT_WINDOW_END = date(2027, 8, 3)
TRUSTED_ASSESSMENT_EXPECTED_SESSIONS = 252
TRUSTED_ASSESSMENT_WINDOW_DIGEST = (
    "3378303933970bc8dfc2bfb310ef3d98623d7e6a906e004353cc243a159621d0"
)
TRUSTED_ASSESSMENT_FINALIZATION_DELAY = timedelta(minutes=15)

TrustedLeafDisposition = Literal[
    "PENDING",
    "MISSING",
    "INCLUDED",
    "EXCLUDED_NON_STRUCTURAL",
    "EXCLUDED_STRUCTURAL",
    "INVALID",
]

_SHA256_PATTERN = "0123456789abcdef"
_ALLOWED_STRATEGY_V2_ACTIONS = frozenset({
    "WAIT",
    "ARM_LONG",
    "CANCEL_ARM",
    "SUBMIT_ENTRY",
    "CANCEL_ENTRY",
    "FILL_ENTRY",
    "EXIT_LONG",
})
_METRIC_FIELDS = frozenset({
    "bars",
    "eligible_bars",
    "breaches",
    "reclaims",
    "entries",
    "exits",
    "closed_trades",
    "win_rate",
    "gross_pnl",
    "fees",
    "net_pnl",
    "max_drawdown",
    "avg_holding_minutes",
    "avg_mae_pct",
    "avg_mfe_pct",
    "comparison_available",
    "live_action_count",
    "action_agreement_rate",
    "net_pnl_delta_vs_live",
})


class TrustedAssessmentError(ValueError):
    """Raised when promotion-grade replay evidence is not self-consistent."""


@dataclass(frozen=True)
class TrustedProducerCutoff:
    observed_at: datetime
    complete_through: date | None
    cutoff_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "authority": "SERVER_CLOCK_AND_STATIC_NYSE_CALENDAR",
            "observed_at": _utc_text(self.observed_at),
            "complete_through": (
                self.complete_through.isoformat()
                if self.complete_through is not None
                else None
            ),
            "cutoff_at": (
                _utc_text(self.cutoff_at)
                if self.cutoff_at is not None
                else None
            ),
            "finalization_delay_minutes": 15,
            "cutoff_provenance_verified": True,
            "caller_cutoff_accepted": False,
        }


@dataclass(frozen=True)
class TrustedTradeSummary:
    closed_trades: int
    gross_pnl: float
    fees: float
    net_pnl: float
    entry_notional: Decimal
    ordered_trade_preimage_sha256: str

    @property
    def net_return_bps(self) -> float | None:
        if self.entry_notional <= 0:
            return None
        value = (
            Decimal(str(self.net_pnl))
            * Decimal(10_000)
            / self.entry_notional
        )
        return round(float(value), 8)

    def to_dict(self) -> dict[str, object]:
        return {
            "closed_trades": self.closed_trades,
            "gross_pnl_decimal": _decimal_text(Decimal(str(self.gross_pnl))),
            "gross_pnl_float_hex": self.gross_pnl.hex(),
            "fees_decimal": _decimal_text(Decimal(str(self.fees))),
            "fees_float_hex": self.fees.hex(),
            "net_pnl_decimal": _decimal_text(Decimal(str(self.net_pnl))),
            "net_pnl_float_hex": self.net_pnl.hex(),
            "closed_trade_entry_notional_decimal": _decimal_text(
                self.entry_notional
            ),
            "net_return_bps": self.net_return_bps,
            "return_preimage_complete": self.entry_notional > 0,
            "ordered_trade_preimage_sha256": (
                self.ordered_trade_preimage_sha256
            ),
        }


@dataclass(frozen=True)
class TrustedDailyLeaf:
    symbol: str
    role: str
    config_hash: str
    session_date: date
    disposition: TrustedLeafDisposition
    exclusion_reason: str = ""
    structural_failure: bool = False
    row_present_after_cutoff: bool = False
    evidence_id: int | None = None
    evidence_digest_sha256: str | None = None
    baseline_result_sha256: str | None = None
    candidate_result_sha256: str | None = None
    artifact_digest_sha256: str | None = None
    artifact_binding_sha256: str | None = None
    daily_binding_sha256: str | None = None
    baseline: TrustedTradeSummary | None = None
    candidate: TrustedTradeSummary | None = None
    blockers: tuple[str, ...] = ()
    leaf_digest_sha256: str = ""

    def canonical_payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "role": self.role,
            "config_hash": self.config_hash,
            "session_date": self.session_date.isoformat(),
            "disposition": self.disposition,
            "exclusion_reason": self.exclusion_reason,
            "structural_failure": self.structural_failure,
            "row_present_after_cutoff": self.row_present_after_cutoff,
            "evidence_id": self.evidence_id,
            "evidence_digest_sha256": self.evidence_digest_sha256,
            "baseline_result_sha256": self.baseline_result_sha256,
            "candidate_result_sha256": self.candidate_result_sha256,
            "artifact_digest_sha256": self.artifact_digest_sha256,
            "artifact_binding_sha256": self.artifact_binding_sha256,
            "daily_binding_sha256": self.daily_binding_sha256,
            "baseline": (
                self.baseline.to_dict() if self.baseline is not None else None
            ),
            "candidate": (
                self.candidate.to_dict() if self.candidate is not None else None
            ),
            "blockers": list(self.blockers),
        }

    def with_digest(self) -> "TrustedDailyLeaf":
        digest = canonical_sha256(self.canonical_payload())
        return TrustedDailyLeaf(
            **{
                **self.__dict__,
                "leaf_digest_sha256": digest,
            }
        )

    def to_dict(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["leaf_digest_sha256"] = (
            self.leaf_digest_sha256
            or canonical_sha256(self.canonical_payload())
        )
        return payload


@dataclass(frozen=True)
class TrustedSymbolEvidence:
    symbol: str
    role: str
    reason: str
    config_hash: str
    registration_id: int | None
    registration_blockers: tuple[str, ...]
    pre_window_rows_excluded: int
    post_window_rows_excluded: int
    leaves: tuple[TrustedDailyLeaf, ...]
    candidate_algorithm_version: str | None = None
    evaluator_digest: str | None = None
    registered_at: datetime | None = None
    eligible_after: datetime | None = None


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TrustedAssessmentError(
            "trusted assessment payload is not canonical JSON"
        ) from exc


def canonical_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def trusted_assessment_sessions() -> tuple[date, ...]:
    sessions: list[date] = []
    current = TRUSTED_ASSESSMENT_WINDOW_START
    while current <= TRUSTED_ASSESSMENT_WINDOW_END:
        if current.weekday() < 5 and not is_market_closed("US", current):
            sessions.append(current)
        current += timedelta(days=1)
    result = tuple(sessions)
    digest = hashlib.sha256(
        "\n".join(item.isoformat() for item in result).encode("ascii")
    ).hexdigest()
    if (
        len(result) != TRUSTED_ASSESSMENT_EXPECTED_SESSIONS
        or not result
        or result[0] != TRUSTED_ASSESSMENT_WINDOW_START
        or result[-1] != TRUSTED_ASSESSMENT_WINDOW_END
        or digest != TRUSTED_ASSESSMENT_WINDOW_DIGEST
    ):
        raise RuntimeError("trusted assessment NYSE window identity drifted")
    return result


def trusted_producer_cutoff(now: datetime) -> TrustedProducerCutoff:
    if now.tzinfo is None:
        raise ValueError("trusted assessment server clock must be timezone-aware")
    observed_at = now.astimezone(timezone.utc)
    session = get_session("US")
    complete_through: date | None = None
    cutoff_at: datetime | None = None
    for session_date in trusted_assessment_sessions():
        close_at = datetime.combine(
            session_date,
            session.close_time(session_date),
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        candidate_cutoff = close_at + TRUSTED_ASSESSMENT_FINALIZATION_DELAY
        if candidate_cutoff > observed_at:
            break
        complete_through = session_date
        cutoff_at = candidate_cutoff
    return TrustedProducerCutoff(
        observed_at=observed_at,
        complete_through=complete_through,
        cutoff_at=cutoff_at,
    )


def trusted_daily_binding_sha256(
    *,
    symbol: str,
    config_hash: str,
    candidate_algorithm_version: str,
    evaluator_digest: str,
    registration_id: int,
    registered_at: datetime,
    eligible_after: datetime,
    evidence_id: int,
    session_date: date,
    evidence_digest_sha256: str,
    baseline_result_sha256: str,
    candidate_result_sha256: str,
    artifact_digest_sha256: str,
    artifact_binding_sha256: str,
    baseline_trade_preimage_sha256: str,
    candidate_trade_preimage_sha256: str,
    baseline_summary: TrustedTradeSummary,
    candidate_summary: TrustedTradeSummary,
) -> str:
    if (
        isinstance(registration_id, bool)
        or not isinstance(registration_id, int)
        or registration_id <= 0
        or isinstance(evidence_id, bool)
        or not isinstance(evidence_id, int)
        or evidence_id <= 0
    ):
        raise TrustedAssessmentError("trusted daily binding ids must be positive")
    if not symbol or not _is_sha256(config_hash) or not candidate_algorithm_version:
        raise TrustedAssessmentError("trusted daily registration identity is invalid")
    for label, digest in (
        ("config", config_hash),
        ("evaluator", evaluator_digest),
        ("evidence", evidence_digest_sha256),
        ("baseline result", baseline_result_sha256),
        ("candidate result", candidate_result_sha256),
        ("artifact", artifact_digest_sha256),
        ("artifact binding", artifact_binding_sha256),
        ("baseline trade preimage", baseline_trade_preimage_sha256),
        ("candidate trade preimage", candidate_trade_preimage_sha256),
    ):
        if not _is_sha256(digest):
            raise TrustedAssessmentError(
                f"trusted daily binding {label} digest is invalid"
            )
    return canonical_sha256({
        "binding_contract": "strategy-v2-trusted-daily-binding-v3",
        "frozen_queue_digest_sha256": FROZEN_QUEUE_DIGEST,
        "assessment_window_digest_sha256": (
            TRUSTED_ASSESSMENT_WINDOW_DIGEST
        ),
        "symbol": symbol,
        "config_hash": config_hash,
        "candidate_algorithm_version": candidate_algorithm_version,
        "evaluator_digest": evaluator_digest,
        "registration_id": registration_id,
        "registered_at": _utc_text(registered_at),
        "eligible_after": _utc_text(eligible_after),
        "evidence_id": evidence_id,
        "session_date": session_date.isoformat(),
        "evidence_digest_sha256": evidence_digest_sha256,
        "baseline_result_sha256": baseline_result_sha256,
        "candidate_result_sha256": candidate_result_sha256,
        "artifact_digest_sha256": artifact_digest_sha256,
        "artifact_binding_sha256": artifact_binding_sha256,
        "baseline_trade_preimage_sha256": (
            baseline_trade_preimage_sha256
        ),
        "candidate_trade_preimage_sha256": (
            candidate_trade_preimage_sha256
        ),
        "baseline_summary": baseline_summary.to_dict(),
        "candidate_summary": candidate_summary.to_dict(),
    })


def validate_replay_trade_track(
    replay: Mapping[str, object],
    result: Mapping[str, object],
    *,
    label: str,
    session_date: date,
    expected_fee_rate: float,
    expected_max_entries_per_day: int,
    expected_virtual_quantity: float,
    expected_max_holding_minutes: int,
) -> TrustedTradeSummary:
    expected_fee_rate = _positive_float(
        expected_fee_rate,
        field_name=f"{label}.expected_fee_rate",
    )
    if (
        isinstance(expected_max_entries_per_day, bool)
        or not isinstance(expected_max_entries_per_day, int)
        or expected_max_entries_per_day <= 0
    ):
        raise TrustedAssessmentError(
            f"{label}.expected_max_entries_per_day must be a positive integer"
        )
    expected_virtual_quantity = _positive_float(
        expected_virtual_quantity,
        field_name=f"{label}.expected_virtual_quantity",
    )
    if (
        isinstance(expected_max_holding_minutes, bool)
        or not isinstance(expected_max_holding_minutes, int)
        or expected_max_holding_minutes <= 0
    ):
        raise TrustedAssessmentError(
            f"{label}.expected_max_holding_minutes must be a positive integer"
        )
    replay_metrics = _mapping(replay.get("metrics"), field_name=f"{label}.metrics")
    result_metrics = _mapping(
        result.get("metrics"),
        field_name=f"{label}.result.metrics",
    )
    if replay_metrics != result_metrics:
        raise TrustedAssessmentError(f"{label} replay metrics do not match result")
    if set(replay_metrics) != _METRIC_FIELDS:
        raise TrustedAssessmentError(f"{label} replay metric shape is invalid")
    trades = _sequence(replay.get("trades"), field_name=f"{label}.trades")
    decisions = _sequence(
        replay.get("decisions"),
        field_name=f"{label}.decisions",
    )
    decision_trades = _decision_trade_pairs(
        decisions,
        label=label,
        session_date=session_date,
    )
    if len(decision_trades) > expected_max_entries_per_day:
        raise TrustedAssessmentError(
            f"{label} exceeds the frozen per-session entry limit"
        )
    result_net_values = _sequence(
        result.get("trade_net_pnl"),
        field_name=f"{label}.result.trade_net_pnl",
    )
    closed_trades = _non_negative_integer(
        replay_metrics.get("closed_trades"),
        field_name=f"{label}.metrics.closed_trades",
    )
    if (
        closed_trades != len(trades)
        or len(result_net_values) != len(trades)
        or len(decision_trades) != len(trades)
    ):
        raise TrustedAssessmentError(f"{label} closed trade count is inconsistent")

    canonical_trades: list[dict[str, object]] = []
    gross_values: list[float] = []
    fee_values: list[float] = []
    net_values: list[float] = []
    entry_notional = Decimal(0)
    previous_entry: datetime | None = None
    previous_exit: datetime | None = None
    for index, raw_trade in enumerate(trades):
        trade = _mapping(raw_trade, field_name=f"{label}.trades[{index}]")
        decision_trade = decision_trades[index]
        entry_at = _aware_datetime(
            trade.get("entry_at"),
            field_name=f"{label}.trades[{index}].entry_at",
        )
        exit_at = _aware_datetime(
            trade.get("exit_at"),
            field_name=f"{label}.trades[{index}].exit_at",
        )
        if exit_at <= entry_at:
            raise TrustedAssessmentError(f"{label} trade exit must follow entry")
        if previous_entry is not None and entry_at < previous_entry:
            raise TrustedAssessmentError(f"{label} trades are not ordered")
        if previous_exit is not None and entry_at < previous_exit:
            raise TrustedAssessmentError(f"{label} trades overlap")
        previous_entry = entry_at
        previous_exit = exit_at
        market_session = get_session("US")
        if (
            not market_session.is_rth(entry_at)
            or not market_session.is_rth(exit_at)
            or market_session.trade_day(entry_at) != session_date
            or market_session.trade_day(exit_at) != session_date
        ):
            raise TrustedAssessmentError(
                f"{label} trade timestamps are outside the target RTH session"
            )

        entry_price_float = _positive_float(
            trade.get("entry_price"),
            field_name=f"{label}.trades[{index}].entry_price",
        )
        exit_price_float = _positive_float(
            trade.get("exit_price"),
            field_name=f"{label}.trades[{index}].exit_price",
        )
        quantity_float = _positive_float(
            trade.get("quantity"),
            field_name=f"{label}.trades[{index}].quantity",
        )
        if quantity_float.hex() != expected_virtual_quantity.hex():
            raise TrustedAssessmentError(
                f"{label} trade quantity does not match frozen virtual quantity"
            )
        fee_rate_float = _positive_float(
            trade.get("estimated_fee_rate"),
            field_name=f"{label}.trades[{index}].estimated_fee_rate",
        )
        if fee_rate_float.hex() != expected_fee_rate.hex():
            raise TrustedAssessmentError(
                f"{label} trade fee rate does not match the frozen source config"
            )
        if trade.get("fee_source") != "ESTIMATED":
            raise TrustedAssessmentError(f"{label} trade fee source is invalid")
        gross_pnl_float = _finite_float(
            trade.get("gross_pnl"),
            field_name=f"{label}.trades[{index}].gross_pnl",
        )
        fees_float = _non_negative_float(
            trade.get("fees"),
            field_name=f"{label}.trades[{index}].fees",
        )
        net_pnl_float = _finite_float(
            trade.get("net_pnl"),
            field_name=f"{label}.trades[{index}].net_pnl",
        )
        expected_gross = (
            exit_price_float - entry_price_float
        ) * quantity_float
        entry_fee = entry_price_float * quantity_float * fee_rate_float
        exit_fee = exit_price_float * quantity_float * fee_rate_float
        expected_fees = entry_fee + exit_fee
        expected_net = expected_gross - expected_fees
        if gross_pnl_float.hex() != expected_gross.hex():
            raise TrustedAssessmentError(f"{label} trade gross PnL is invalid")
        if fees_float.hex() != expected_fees.hex():
            raise TrustedAssessmentError(f"{label} trade fees are invalid")
        if net_pnl_float.hex() != expected_net.hex():
            raise TrustedAssessmentError(f"{label} trade net PnL is invalid")
        result_net_float = _finite_float(
            result_net_values[index],
            field_name=f"{label}.result.trade_net_pnl[{index}]",
        )
        if result_net_float.hex() != net_pnl_float.hex():
            raise TrustedAssessmentError(
                f"{label} result trade PnL is inconsistent"
            )
        holding_minutes_float = _non_negative_float(
            trade.get("holding_minutes"),
            field_name=f"{label}.trades[{index}].holding_minutes",
        )
        expected_holding = (exit_at - entry_at).total_seconds() / 60
        if holding_minutes_float.hex() != expected_holding.hex():
            raise TrustedAssessmentError(
                f"{label} trade holding time is inconsistent"
            )
        if holding_minutes_float > expected_max_holding_minutes:
            raise TrustedAssessmentError(
                f"{label} trade exceeds the frozen maximum holding time"
            )
        mae_pct_float = _finite_float(
            trade.get("mae_pct"),
            field_name=f"{label}.trades[{index}].mae_pct",
        )
        mfe_pct_float = _finite_float(
            trade.get("mfe_pct"),
            field_name=f"{label}.trades[{index}].mfe_pct",
        )
        if mae_pct_float > 0 or mfe_pct_float < 0:
            raise TrustedAssessmentError(
                f"{label} trade excursion direction is invalid for a long trade"
            )

        entry_reason = _required_string(
            trade.get("entry_reason"),
            field_name=f"{label}.trades[{index}].entry_reason",
        )
        exit_reason = _required_string(
            trade.get("exit_reason"),
            field_name=f"{label}.trades[{index}].exit_reason",
        )
        if (
            entry_at != decision_trade["entry_at"]
            or exit_at != decision_trade["exit_at"]
            or entry_reason != decision_trade["entry_reason"]
            or exit_reason != decision_trade["exit_reason"]
            or entry_price_float.hex()
            != decision_trade["entry_price"].hex()
            or exit_price_float.hex()
            != decision_trade["exit_price"].hex()
            or quantity_float.hex() != decision_trade["quantity"].hex()
        ):
            raise TrustedAssessmentError(
                f"{label} trade is not bound to replay decisions"
            )

        entry_price_decimal = Decimal(str(entry_price_float))
        quantity_decimal = Decimal(str(quantity_float))
        notional = entry_price_decimal * quantity_decimal
        entry_notional += notional
        gross_values.append(gross_pnl_float)
        fee_values.append(fees_float)
        net_values.append(net_pnl_float)
        canonical_trades.append({
            "index": index,
            "entry_at": _utc_text(entry_at),
            "exit_at": _utc_text(exit_at),
            "entry_reason": entry_reason,
            "exit_reason": exit_reason,
            "entry_price_float_hex": entry_price_float.hex(),
            "exit_price_float_hex": exit_price_float.hex(),
            "quantity_float_hex": quantity_float.hex(),
            "entry_price_decimal": _decimal_text(entry_price_decimal),
            "exit_price_decimal": _decimal_text(Decimal(str(exit_price_float))),
            "quantity_decimal": _decimal_text(quantity_decimal),
            "entry_notional_decimal": _decimal_text(notional),
            "estimated_fee_rate_float_hex": fee_rate_float.hex(),
            "entry_fee_float_hex": entry_fee.hex(),
            "exit_fee_float_hex": exit_fee.hex(),
            "gross_pnl_float_hex": gross_pnl_float.hex(),
            "fees_float_hex": fees_float.hex(),
            "net_pnl_float_hex": net_pnl_float.hex(),
            "holding_minutes_float_hex": holding_minutes_float.hex(),
            "mae_pct_float_hex": mae_pct_float.hex(),
            "mfe_pct_float_hex": mfe_pct_float.hex(),
        })

    gross_total = sum(gross_values, 0.0)
    fee_total = sum(fee_values, 0.0)
    net_total = sum(net_values, 0.0)
    for metric_name, expected in (
        ("gross_pnl", gross_total),
        ("fees", fee_total),
        ("net_pnl", net_total),
    ):
        actual = _finite_float(
            replay_metrics.get(metric_name),
            field_name=f"{label}.metrics.{metric_name}",
        )
        if actual.hex() != expected.hex():
            raise TrustedAssessmentError(
                f"{label} aggregate {metric_name} is inconsistent"
            )
    wins = sum(value > 0 for value in net_values)
    expected_win_rate = wins / closed_trades if closed_trades else 0.0
    win_rate = _finite_float(
        replay_metrics.get("win_rate"),
        field_name=f"{label}.metrics.win_rate",
    )
    if win_rate.hex() != expected_win_rate.hex():
        raise TrustedAssessmentError(f"{label} aggregate win rate is inconsistent")
    expected_drawdown = _max_drawdown(net_values)
    max_drawdown = _non_negative_float(
        replay_metrics.get("max_drawdown"),
        field_name=f"{label}.metrics.max_drawdown",
    )
    if max_drawdown.hex() != expected_drawdown.hex():
        raise TrustedAssessmentError(
            f"{label} aggregate max drawdown is inconsistent"
        )
    actions = [
        _required_string(
            _mapping(item, field_name=f"{label}.decisions[{index}]").get(
                "action"
            ),
            field_name=f"{label}.decisions[{index}].action",
        )
        for index, item in enumerate(decisions)
    ]
    decision_timestamps: list[str] = []
    eligible_timestamps: set[str] = set()
    for index, raw_decision in enumerate(decisions):
        decision = _mapping(
            raw_decision,
            field_name=f"{label}.decisions[{index}]",
        )
        timestamp = _aware_datetime(
            decision.get("timestamp"),
            field_name=f"{label}.decisions[{index}].timestamp",
        )
        timestamp_text = timestamp.isoformat()
        decision_timestamps.append(timestamp_text)
        gate_passed = decision.get("gate_passed")
        if not isinstance(gate_passed, bool):
            raise TrustedAssessmentError(
                f"{label}.decisions[{index}].gate_passed must be a boolean"
            )
        if gate_passed:
            eligible_timestamps.add(timestamp_text)
    integer_metrics = {
        "bars": len(set(decision_timestamps)),
        "eligible_bars": len(eligible_timestamps),
        "breaches": actions.count("ARM_LONG"),
        "reclaims": actions.count("SUBMIT_ENTRY"),
        "entries": actions.count("FILL_ENTRY"),
        "exits": actions.count("EXIT_LONG"),
    }
    for metric_name, expected in integer_metrics.items():
        actual = _non_negative_integer(
            replay_metrics.get(metric_name),
            field_name=f"{label}.metrics.{metric_name}",
        )
        if actual != expected:
            raise TrustedAssessmentError(
                f"{label} aggregate {metric_name} is inconsistent"
            )
    avg_holding = (
        sum(
            _non_negative_float(
                _mapping(item, field_name=f"{label}.trades[{index}]").get(
                    "holding_minutes"
                ),
                field_name=f"{label}.trades[{index}].holding_minutes",
            )
            for index, item in enumerate(trades)
        )
        / closed_trades
        if closed_trades
        else 0.0
    )
    avg_mae = (
        sum(
            _finite_float(
                _mapping(item, field_name=f"{label}.trades[{index}]").get(
                    "mae_pct"
                ),
                field_name=f"{label}.trades[{index}].mae_pct",
            )
            for index, item in enumerate(trades)
        )
        / closed_trades
        if closed_trades
        else 0.0
    )
    avg_mfe = (
        sum(
            _finite_float(
                _mapping(item, field_name=f"{label}.trades[{index}]").get(
                    "mfe_pct"
                ),
                field_name=f"{label}.trades[{index}].mfe_pct",
            )
            for index, item in enumerate(trades)
        )
        / closed_trades
        if closed_trades
        else 0.0
    )
    for metric_name, expected in (
        ("avg_holding_minutes", avg_holding),
        ("avg_mae_pct", avg_mae),
        ("avg_mfe_pct", avg_mfe),
    ):
        actual = _finite_float(
            replay_metrics.get(metric_name),
            field_name=f"{label}.metrics.{metric_name}",
        )
        if actual.hex() != expected.hex():
            raise TrustedAssessmentError(
                f"{label} aggregate {metric_name} is inconsistent"
            )
    if (
        replay_metrics.get("comparison_available") is not False
        or replay_metrics.get("live_action_count") is not None
        or replay_metrics.get("action_agreement_rate") is not None
        or replay_metrics.get("net_pnl_delta_vs_live") is not None
    ):
        raise TrustedAssessmentError(
            f"{label} live-comparison metrics must retain producer defaults"
        )
    trade_preimage = {
        "preimage_contract": "strategy-v2-ordered-closed-trades-v3",
        "closed_trades": closed_trades,
        "trades": canonical_trades,
        "aggregate": {
            "gross_pnl_float_hex": gross_total.hex(),
            "fees_float_hex": fee_total.hex(),
            "net_pnl_float_hex": net_total.hex(),
            "closed_trade_entry_notional_decimal": _decimal_text(
                entry_notional
            ),
        },
    }
    return TrustedTradeSummary(
        closed_trades=closed_trades,
        gross_pnl=gross_total,
        fees=fee_total,
        net_pnl=net_total,
        entry_notional=entry_notional,
        ordered_trade_preimage_sha256=canonical_sha256(trade_preimage),
    )


def build_trusted_assessment_report(
    symbols: Sequence[TrustedSymbolEvidence],
    *,
    producer_cutoff: TrustedProducerCutoff,
) -> dict[str, object]:
    expected_cutoff = trusted_producer_cutoff(producer_cutoff.observed_at)
    if producer_cutoff != expected_cutoff:
        raise TrustedAssessmentError(
            "trusted assessment producer cutoff is not server-derived"
        )
    expected_dates = trusted_assessment_sessions()
    expected_entries = {
        symbol: (role, reason, config_hash)
        for symbol, role, reason, config_hash in FROZEN_QUEUE_ENTRIES
    }
    by_symbol = {item.symbol: item for item in symbols}
    if set(by_symbol) != set(expected_entries) or len(symbols) != len(by_symbol):
        raise TrustedAssessmentError(
            "trusted assessment must contain the exact frozen cohort"
        )
    symbol_reports: list[dict[str, object]] = []
    normalized_symbols: dict[str, TrustedSymbolEvidence] = {}
    for symbol in sorted(expected_entries):
        item = by_symbol[symbol]
        role, reason, config_hash = expected_entries[symbol]
        if (
            (
                item.registration_id is not None
                and (
                    isinstance(item.registration_id, bool)
                    or not isinstance(item.registration_id, int)
                    or item.registration_id <= 0
                )
            )
            or isinstance(item.pre_window_rows_excluded, bool)
            or not isinstance(item.pre_window_rows_excluded, int)
            or item.pre_window_rows_excluded < 0
            or isinstance(item.post_window_rows_excluded, bool)
            or not isinstance(item.post_window_rows_excluded, int)
            or item.post_window_rows_excluded < 0
            or not _valid_registration_metadata(item)
            or
            item.role != role
            or item.reason != reason
            or item.config_hash != config_hash
            or len(item.leaves) != len(expected_dates)
            or tuple(leaf.session_date for leaf in item.leaves) != expected_dates
            or any(
                leaf.symbol != symbol
                or leaf.role != role
                or leaf.config_hash != config_hash
                for leaf in item.leaves
            )
        ):
            raise TrustedAssessmentError(
                f"trusted assessment symbol evidence identity drifted for {symbol}"
            )
        recomputed_leaves: list[TrustedDailyLeaf] = []
        for leaf in item.leaves:
            after_cutoff = (
                producer_cutoff.complete_through is None
                or leaf.session_date > producer_cutoff.complete_through
            )
            if after_cutoff:
                if (
                    leaf.disposition != "PENDING"
                    or leaf.baseline is not None
                    or leaf.candidate is not None
                    or leaf.daily_binding_sha256 is not None
                    or leaf.artifact_digest_sha256 is not None
                    or leaf.artifact_binding_sha256 is not None
                ):
                    raise TrustedAssessmentError(
                        f"future trusted leaf must remain pending for "
                        f"{symbol} {leaf.session_date.isoformat()}"
                    )
                if (
                    leaf.row_present_after_cutoff
                    and "EVIDENCE_AFTER_SERVER_CUTOFF" not in leaf.blockers
                ):
                    raise TrustedAssessmentError(
                        "future evidence must carry the server-cutoff blocker"
                    )
                if (
                    not leaf.row_present_after_cutoff
                    and (
                        leaf.evidence_id is not None
                        or leaf.evidence_digest_sha256 is not None
                    )
                ):
                    raise TrustedAssessmentError(
                        "pending leaf without a future row cannot carry evidence"
                    )
            elif (
                leaf.disposition == "PENDING"
                and leaf.blockers
                != ("VERIFIER_RESOURCE_CAP_EXCEEDED",)
            ):
                raise TrustedAssessmentError(
                    f"closed trusted leaf cannot remain pending without a "
                    f"verifier resource blocker for "
                    f"{symbol} {leaf.session_date.isoformat()}"
                )
            _validate_leaf_shape(
                leaf,
                after_cutoff=after_cutoff,
                registration_id=item.registration_id,
                registration_blockers=item.registration_blockers,
            )
            if leaf.disposition == "INCLUDED":
                _validate_included_daily_binding(leaf, symbol=item)
            recomputed = leaf.with_digest()
            if (
                leaf.leaf_digest_sha256
                and leaf.leaf_digest_sha256
                != recomputed.leaf_digest_sha256
            ):
                raise TrustedAssessmentError(
                    f"trusted assessment leaf digest mismatch for "
                    f"{symbol} {leaf.session_date.isoformat()}"
                )
            recomputed_leaves.append(recomputed)
        leaves = tuple(recomputed_leaves)
        normalized = TrustedSymbolEvidence(
            **{
                **item.__dict__,
                "leaves": leaves,
            }
        )
        normalized_symbols[symbol] = normalized
        evidence_root = canonical_sha256({
            "symbol": symbol,
            "role": role,
            "reason": reason,
            "config_hash": config_hash,
            "registration_id": item.registration_id,
            "candidate_algorithm_version": item.candidate_algorithm_version,
            "evaluator_digest": item.evaluator_digest,
            "registered_at": (
                _utc_text(item.registered_at)
                if item.registered_at is not None
                else None
            ),
            "eligible_after": (
                _utc_text(item.eligible_after)
                if item.eligible_after is not None
                else None
            ),
            "registration_blockers": list(item.registration_blockers),
            "pre_window_rows_excluded": item.pre_window_rows_excluded,
            "post_window_rows_excluded": item.post_window_rows_excluded,
            "window_digest_sha256": TRUSTED_ASSESSMENT_WINDOW_DIGEST,
            "leaf_digests": [leaf.leaf_digest_sha256 for leaf in leaves],
        })
        symbol_reports.append({
            "symbol": symbol,
            "role": role,
            "reason": reason,
            "config_hash": config_hash,
            "registration_id": item.registration_id,
            "candidate_algorithm_version": item.candidate_algorithm_version,
            "evaluator_digest": item.evaluator_digest,
            "registered_at": (
                _utc_text(item.registered_at)
                if item.registered_at is not None
                else None
            ),
            "eligible_after": (
                _utc_text(item.eligible_after)
                if item.eligible_after is not None
                else None
            ),
            "registration_blockers": list(item.registration_blockers),
            "pre_window_rows_excluded": item.pre_window_rows_excluded,
            "post_window_rows_excluded": item.post_window_rows_excluded,
            "expected_session_count": len(expected_dates),
            "evidence_root_sha256": evidence_root,
            "leaves": [leaf.to_dict() for leaf in leaves],
        })

    control = normalized_symbols[CONTROL_SYMBOL]
    candidates: list[dict[str, object]] = []
    for symbol in sorted(
        value for value in normalized_symbols if value != CONTROL_SYMBOL
    ):
        candidate = normalized_symbols[symbol]
        report = _candidate_report(
            candidate,
            control,
            producer_cutoff=producer_cutoff,
        )
        candidates.append(report)
    all_ready = bool(candidates) and all(
        item["evidence_review_ready"] is True for item in candidates
    )
    report: dict[str, object] = {
        "schema_version": TRUSTED_ASSESSMENT_REPORT_SCHEMA_VERSION,
        "algorithm_version": TRUSTED_ASSESSMENT_ALGORITHM_VERSION,
        "policy_version": TRUSTED_ASSESSMENT_POLICY_VERSION,
        "status": (
            "READY_FOR_MANUAL_DISPROOF_REVIEW"
            if all_ready
            else "INSUFFICIENT_EVIDENCE"
        ),
        "generated_at": _utc_text(producer_cutoff.observed_at),
        "authority_mode": "ONLINE_SERVER_DB_DIRECT_READ",
        "caller_authority_accepted": False,
        "portable_attestation_verified": False,
        "research_only": True,
        "live_equivalent": False,
        "order_submission_allowed": False,
        "automatic_promotion_allowed": False,
        "automatic_disproof_decision_allowed": False,
        "evidence_review_ready": all_ready,
        "promotion_eligible": False,
        "promotion_blockers": [
            *([] if all_ready else ["EVIDENCE_REVIEW_NOT_READY"]),
            "QUANT_CANDIDATE_VETO_NOT_VERIFIED",
            "MANUAL_PROMOTION_REQUIRED",
        ],
        "producer_cutoff": producer_cutoff.to_dict(),
        "freeze": {
            "name": "nasdaq-djia-disproof-2026-07-31",
            "as_of_date": FROZEN_QUEUE_AS_OF_DATE.isoformat(),
            "freeze_digest": FROZEN_QUEUE_DIGEST,
            "candidate_algorithm_version": (
                FORWARD_CANDIDATE_ALGORITHM_VERSION
            ),
            "evaluator_digest": FROZEN_EVALUATOR_DIGEST,
            "control_symbol": CONTROL_SYMBOL,
        },
        "assessment_window": {
            "market": "US",
            "first_expected_session_date": (
                TRUSTED_ASSESSMENT_WINDOW_START.isoformat()
            ),
            "last_expected_session_date": (
                TRUSTED_ASSESSMENT_WINDOW_END.isoformat()
            ),
            "expected_session_count": len(expected_dates),
            "expected_session_dates": [item.isoformat() for item in expected_dates],
            "expected_session_digest": TRUSTED_ASSESSMENT_WINDOW_DIGEST,
            "denominator_is_fixed": True,
            "missing_and_excluded_count_in_denominator": True,
        },
        "evidence_thresholds": {
            "minimum_future_trade_days": MINIMUM_FUTURE_TRADE_DAYS,
            "minimum_closed_trades_per_candidate": MINIMUM_CLOSED_TRADES,
            "minimum_expected_session_coverage_ratio": (
                MINIMUM_EXPECTED_SESSION_COVERAGE_RATIO
            ),
            "full_replay_verified_required": True,
            "source_trace_archive_promotion_grade": False,
            "quant_candidate_required_for_promotion": True,
            "manual_promotion_required": True,
            "thresholds_tunable": False,
        },
        "symbols": symbol_reports,
        "candidates": candidates,
    }
    report["report_digest_sha256"] = canonical_sha256(report)
    return report


def _candidate_report(
    candidate: TrustedSymbolEvidence,
    control: TrustedSymbolEvidence,
    *,
    producer_cutoff: TrustedProducerCutoff,
) -> dict[str, object]:
    expected_count = TRUSTED_ASSESSMENT_EXPECTED_SESSIONS
    complete_through = producer_cutoff.complete_through
    candidate_included = {
        leaf.session_date: leaf
        for leaf in candidate.leaves
        if (
            complete_through is not None
            and leaf.session_date <= complete_through
            and leaf.disposition == "INCLUDED"
            and not leaf.blockers
        )
    }
    control_included = {
        leaf.session_date: leaf
        for leaf in control.leaves
        if (
            complete_through is not None
            and leaf.session_date <= complete_through
            and leaf.disposition == "INCLUDED"
            and not leaf.blockers
        )
    }
    common_dates = tuple(sorted(set(candidate_included) & set(control_included)))
    candidate_baseline = _aggregate_summaries(
        leaf.baseline
        for leaf in candidate_included.values()
        if leaf.baseline is not None
    )
    candidate_track = _aggregate_summaries(
        leaf.candidate
        for leaf in candidate_included.values()
        if leaf.candidate is not None
    )
    candidate_common_values: list[TrustedTradeSummary] = []
    control_common_values: list[TrustedTradeSummary] = []
    for day in common_dates:
        candidate_summary = candidate_included[day].candidate
        control_summary = control_included[day].candidate
        if candidate_summary is not None:
            candidate_common_values.append(candidate_summary)
        if control_summary is not None:
            control_common_values.append(control_summary)
    candidate_common = _aggregate_summaries(candidate_common_values)
    control_common = _aggregate_summaries(control_common_values)

    blockers: list[str] = [
        *candidate.registration_blockers,
        *control.registration_blockers,
    ]
    if producer_cutoff.complete_through != TRUSTED_ASSESSMENT_WINDOW_END:
        blockers.append("FIXED_ASSESSMENT_HORIZON_INCOMPLETE")
    relevant_dates = {
        item
        for item in trusted_assessment_sessions()
        if (
            producer_cutoff.complete_through is not None
            and item <= producer_cutoff.complete_through
        )
    }
    candidate_by_date = {leaf.session_date: leaf for leaf in candidate.leaves}
    control_by_date = {leaf.session_date: leaf for leaf in control.leaves}
    disposition_mismatch = False
    exclusion_reason_mismatch = False
    for session_date in relevant_dates:
        candidate_leaf = candidate_by_date[session_date]
        control_leaf = control_by_date[session_date]
        blockers.extend(candidate_leaf.blockers)
        blockers.extend(control_leaf.blockers)
        if candidate_leaf.structural_failure:
            blockers.append("CANDIDATE_STRUCTURAL_EXCLUSION")
        if control_leaf.structural_failure:
            blockers.append("NVDA_CONTROL_STRUCTURAL_EXCLUSION")
        if candidate_leaf.disposition != control_leaf.disposition:
            disposition_mismatch = True
        elif (
            candidate_leaf.disposition
            in {"EXCLUDED_NON_STRUCTURAL", "EXCLUDED_STRUCTURAL"}
            and candidate_leaf.exclusion_reason != control_leaf.exclusion_reason
        ):
            exclusion_reason_mismatch = True
    if any(leaf.row_present_after_cutoff for leaf in candidate.leaves):
        blockers.append("CANDIDATE_EVIDENCE_AFTER_SERVER_CUTOFF")
    if any(leaf.row_present_after_cutoff for leaf in control.leaves):
        blockers.append("NVDA_EVIDENCE_AFTER_SERVER_CUTOFF")
    if disposition_mismatch:
        blockers.append("EXPECTED_SESSION_DISPOSITION_MISMATCH")
    if exclusion_reason_mismatch:
        blockers.append("EXPECTED_SESSION_EXCLUSION_REASON_MISMATCH")

    candidate_coverage = len(candidate_included) / expected_count
    control_coverage = len(control_included) / expected_count
    paired_coverage = len(common_dates) / expected_count
    if paired_coverage + 1e-12 < MINIMUM_EXPECTED_SESSION_COVERAGE_RATIO:
        blockers.append("EXPECTED_SESSION_COVERAGE_INSUFFICIENT")
    if len(common_dates) < MINIMUM_FUTURE_TRADE_DAYS:
        blockers.append("FUTURE_TRADE_DAYS_INSUFFICIENT")
    if candidate_track.closed_trades < MINIMUM_CLOSED_TRADES:
        blockers.append("CANDIDATE_CLOSED_TRADES_INSUFFICIENT")
    if (
        candidate_track.closed_trades > 0
        and (
            candidate_track.entry_notional <= 0
            or candidate_baseline.entry_notional <= 0
        )
    ):
        blockers.append("WITHIN_SYMBOL_RETURN_BPS_PREIMAGE_MISSING")
    blockers = list(dict.fromkeys(blockers))
    evidence_review_ready = not blockers
    return {
        "symbol": candidate.symbol,
        "role": candidate.role,
        "reason": candidate.reason,
        "config_hash": candidate.config_hash,
        "status": (
            "READY_FOR_MANUAL_DISPROOF_REVIEW"
            if evidence_review_ready
            else "INSUFFICIENT_EVIDENCE"
        ),
        "evidence_review_ready": evidence_review_ready,
        "promotion_eligible": False,
        "promotion_blockers": [
            *(
                []
                if evidence_review_ready
                else ["EVIDENCE_REVIEW_NOT_READY"]
            ),
            "QUANT_CANDIDATE_VETO_NOT_VERIFIED",
            "MANUAL_PROMOTION_REQUIRED",
        ],
        "expected_session_count": expected_count,
        "candidate_included_sessions": len(candidate_included),
        "nvda_included_sessions": len(control_included),
        "paired_included_sessions": len(common_dates),
        "candidate_expected_session_coverage_ratio": round(
            candidate_coverage, 8
        ),
        "nvda_expected_session_coverage_ratio": round(control_coverage, 8),
        "paired_expected_session_coverage_ratio": round(paired_coverage, 8),
        "candidate_closed_trades": candidate_track.closed_trades,
        "remaining_candidate_closed_trades": max(
            0,
            MINIMUM_CLOSED_TRADES - candidate_track.closed_trades,
        ),
        "within_symbol_baseline": candidate_baseline.to_dict(),
        "within_symbol_candidate": candidate_track.to_dict(),
        "candidate_same_window": candidate_common.to_dict(),
        "nvda_same_window_control": control_common.to_dict(),
        "evidence_blockers": blockers,
    }


def _validate_leaf_shape(
    leaf: TrustedDailyLeaf,
    *,
    after_cutoff: bool,
    registration_id: int | None,
    registration_blockers: Sequence[str],
) -> None:
    if leaf.evidence_id is not None and (
        isinstance(leaf.evidence_id, bool)
        or not isinstance(leaf.evidence_id, int)
        or leaf.evidence_id <= 0
    ):
        raise TrustedAssessmentError(
            "trusted assessment evidence id must be a positive integer"
        )
    for label, digest in (
        ("evidence", leaf.evidence_digest_sha256),
        ("baseline result", leaf.baseline_result_sha256),
        ("candidate result", leaf.candidate_result_sha256),
        ("artifact", leaf.artifact_digest_sha256),
        ("artifact binding", leaf.artifact_binding_sha256),
        ("daily binding", leaf.daily_binding_sha256),
    ):
        if digest is not None and not _is_sha256(digest):
            raise TrustedAssessmentError(
                f"trusted assessment {label} digest is invalid"
            )
    if registration_id is None:
        if not registration_blockers:
            raise TrustedAssessmentError(
                "missing trusted registration must carry a blocker"
            )
        if leaf.disposition in {
            "INCLUDED",
            "EXCLUDED_NON_STRUCTURAL",
            "EXCLUDED_STRUCTURAL",
        }:
            raise TrustedAssessmentError(
                "missing trusted registration cannot contribute evidence"
            )
    elif (
        isinstance(registration_id, bool)
        or not isinstance(registration_id, int)
        or registration_id <= 0
    ):
        raise TrustedAssessmentError(
            "trusted assessment registration id must be positive"
        )

    if leaf.disposition == "INCLUDED":
        if (
            after_cutoff
            or leaf.evidence_id is None
            or isinstance(leaf.evidence_id, bool)
            or not isinstance(leaf.evidence_id, int)
            or leaf.evidence_id <= 0
            or leaf.baseline is None
            or leaf.candidate is None
            or leaf.exclusion_reason
            or leaf.structural_failure
            or leaf.row_present_after_cutoff
            or leaf.blockers
            or not _is_sha256(leaf.evidence_digest_sha256 or "")
            or not _is_sha256(leaf.baseline_result_sha256 or "")
            or not _is_sha256(leaf.candidate_result_sha256 or "")
            or not _is_sha256(leaf.artifact_digest_sha256 or "")
            or not _is_sha256(leaf.artifact_binding_sha256 or "")
            or not _is_sha256(leaf.daily_binding_sha256 or "")
        ):
            raise TrustedAssessmentError(
                "included trusted leaf lacks a complete promotion-grade chain"
            )
        _validate_trade_summary(leaf.baseline)
        _validate_trade_summary(leaf.candidate)
        return
    if leaf.disposition in {
        "EXCLUDED_NON_STRUCTURAL",
        "EXCLUDED_STRUCTURAL",
    }:
        expected_structural = leaf.disposition == "EXCLUDED_STRUCTURAL"
        if (
            after_cutoff
            or leaf.evidence_id is None
            or isinstance(leaf.evidence_id, bool)
            or not isinstance(leaf.evidence_id, int)
            or leaf.evidence_id <= 0
            or not _is_sha256(leaf.evidence_digest_sha256 or "")
            or not leaf.exclusion_reason
            or leaf.structural_failure is not expected_structural
            or leaf.row_present_after_cutoff
            or leaf.artifact_digest_sha256 is not None
            or leaf.artifact_binding_sha256 is not None
            or leaf.daily_binding_sha256 is not None
            or leaf.baseline_result_sha256 is not None
            or leaf.candidate_result_sha256 is not None
            or leaf.baseline is not None
            or leaf.candidate is not None
            or leaf.blockers
        ):
            raise TrustedAssessmentError(
                "excluded trusted leaf has inconsistent evidence shape"
            )
        return
    if leaf.disposition == "MISSING":
        if (
            after_cutoff
            or leaf.evidence_id is not None
            or leaf.evidence_digest_sha256 is not None
            or leaf.artifact_digest_sha256 is not None
            or leaf.artifact_binding_sha256 is not None
            or leaf.daily_binding_sha256 is not None
            or leaf.baseline_result_sha256 is not None
            or leaf.candidate_result_sha256 is not None
            or leaf.baseline is not None
            or leaf.candidate is not None
            or leaf.exclusion_reason
            or leaf.structural_failure
            or leaf.row_present_after_cutoff
            or leaf.blockers
        ):
            raise TrustedAssessmentError(
                "missing trusted leaf must not carry evidence"
            )
        return
    if leaf.disposition == "INVALID":
        if (
            after_cutoff
            or not leaf.blockers
            or leaf.baseline is not None
            or leaf.candidate is not None
            or leaf.daily_binding_sha256 is not None
        ):
            raise TrustedAssessmentError(
                "invalid trusted leaf must fail closed without summaries"
            )
        return
    if leaf.disposition == "PENDING":
        if after_cutoff:
            return
        if (
            leaf.blockers != ("VERIFIER_RESOURCE_CAP_EXCEEDED",)
            or leaf.evidence_id is None
            or not _is_sha256(leaf.evidence_digest_sha256 or "")
            or leaf.baseline_result_sha256 is not None
            or leaf.candidate_result_sha256 is not None
            or leaf.artifact_digest_sha256 is not None
            or leaf.artifact_binding_sha256 is not None
            or leaf.daily_binding_sha256 is not None
            or leaf.baseline is not None
            or leaf.candidate is not None
            or leaf.exclusion_reason
            or leaf.structural_failure
            or leaf.row_present_after_cutoff
        ):
            raise TrustedAssessmentError(
                "closed pending trusted leaf must be verifier-resource bounded"
            )
        return
    raise TrustedAssessmentError("trusted leaf disposition is unsupported")


def _valid_registration_metadata(item: TrustedSymbolEvidence) -> bool:
    metadata = (
        item.candidate_algorithm_version,
        item.evaluator_digest,
        item.registered_at,
        item.eligible_after,
    )
    if item.registration_id is None:
        return all(value is None for value in metadata)
    if (
        item.candidate_algorithm_version
        != FORWARD_CANDIDATE_ALGORITHM_VERSION
        or item.evaluator_digest != FROZEN_EVALUATOR_DIGEST
        or item.registered_at is None
        or item.eligible_after is None
        or item.registered_at.tzinfo is None
        or item.eligible_after.tzinfo is None
    ):
        return False
    return item.registered_at < item.eligible_after


def _validate_trade_summary(summary: TrustedTradeSummary | None) -> None:
    if summary is None:
        raise TrustedAssessmentError("trusted trade summary is missing")
    if (
        isinstance(summary.closed_trades, bool)
        or not isinstance(summary.closed_trades, int)
        or summary.closed_trades < 0
        or isinstance(summary.gross_pnl, bool)
        or not isinstance(summary.gross_pnl, float)
        or not math.isfinite(summary.gross_pnl)
        or isinstance(summary.fees, bool)
        or not isinstance(summary.fees, float)
        or not math.isfinite(summary.fees)
        or summary.fees < 0
        or isinstance(summary.net_pnl, bool)
        or not isinstance(summary.net_pnl, float)
        or not math.isfinite(summary.net_pnl)
        or not isinstance(summary.entry_notional, Decimal)
        or not summary.entry_notional.is_finite()
        or summary.entry_notional < 0
        or not _is_sha256(summary.ordered_trade_preimage_sha256)
    ):
        raise TrustedAssessmentError("trusted trade summary shape is invalid")
    if summary.closed_trades == 0:
        if (
            summary.entry_notional != 0
            or summary.gross_pnl.hex() != (0.0).hex()
            or summary.fees.hex() != (0.0).hex()
            or summary.net_pnl.hex() != (0.0).hex()
        ):
            raise TrustedAssessmentError("empty trusted trade summary is invalid")
    elif summary.entry_notional <= 0:
        raise TrustedAssessmentError(
            "closed trusted trades require positive entry notional"
        )
    if not math.isclose(
        summary.gross_pnl - summary.fees,
        summary.net_pnl,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise TrustedAssessmentError("trusted trade summary PnL is inconsistent")


def _validate_included_daily_binding(
    leaf: TrustedDailyLeaf,
    *,
    symbol: TrustedSymbolEvidence,
) -> None:
    if (
        symbol.registration_id is None
        or symbol.candidate_algorithm_version is None
        or symbol.evaluator_digest is None
        or symbol.registered_at is None
        or symbol.eligible_after is None
        or leaf.evidence_id is None
        or leaf.evidence_digest_sha256 is None
        or leaf.baseline_result_sha256 is None
        or leaf.candidate_result_sha256 is None
        or leaf.artifact_digest_sha256 is None
        or leaf.artifact_binding_sha256 is None
        or leaf.daily_binding_sha256 is None
        or leaf.baseline is None
        or leaf.candidate is None
    ):
        raise TrustedAssessmentError(
            "included trusted daily binding preimage is incomplete"
        )
    expected = trusted_daily_binding_sha256(
        symbol=leaf.symbol,
        config_hash=leaf.config_hash,
        candidate_algorithm_version=symbol.candidate_algorithm_version,
        evaluator_digest=symbol.evaluator_digest,
        registration_id=symbol.registration_id,
        registered_at=symbol.registered_at,
        eligible_after=symbol.eligible_after,
        evidence_id=leaf.evidence_id,
        session_date=leaf.session_date,
        evidence_digest_sha256=leaf.evidence_digest_sha256,
        baseline_result_sha256=leaf.baseline_result_sha256,
        candidate_result_sha256=leaf.candidate_result_sha256,
        artifact_digest_sha256=leaf.artifact_digest_sha256,
        artifact_binding_sha256=leaf.artifact_binding_sha256,
        baseline_trade_preimage_sha256=(
            leaf.baseline.ordered_trade_preimage_sha256
        ),
        candidate_trade_preimage_sha256=(
            leaf.candidate.ordered_trade_preimage_sha256
        ),
        baseline_summary=leaf.baseline,
        candidate_summary=leaf.candidate,
    )
    if expected != leaf.daily_binding_sha256:
        raise TrustedAssessmentError("trusted daily binding digest mismatch")


def _aggregate_summaries(
    summaries: Iterable[TrustedTradeSummary],
) -> TrustedTradeSummary:
    values = list(summaries)
    if not values:
        return TrustedTradeSummary(
            closed_trades=0,
            gross_pnl=0.0,
            fees=0.0,
            net_pnl=0.0,
            entry_notional=Decimal(0),
            ordered_trade_preimage_sha256=canonical_sha256({
                "preimage_contract": "strategy-v2-aggregate-trade-preimages-v3",
                "daily_preimage_digests": [],
            }),
        )
    gross_pnl = 0.0
    fees = 0.0
    net_pnl = 0.0
    for item in values:
        gross_pnl += item.gross_pnl
        fees += item.fees
        net_pnl += item.net_pnl
    return TrustedTradeSummary(
        closed_trades=sum(item.closed_trades for item in values),
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=net_pnl,
        entry_notional=sum(
            (item.entry_notional for item in values),
            Decimal(0),
        ),
        ordered_trade_preimage_sha256=canonical_sha256({
            "preimage_contract": "strategy-v2-aggregate-trade-preimages-v3",
            "daily_preimage_digests": [
                item.ordered_trade_preimage_sha256 for item in values
            ],
        }),
    )


def _decision_trade_pairs(
    decisions: Sequence[object],
    *,
    label: str,
    session_date: date,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    open_entry: dict[str, Any] | None = None
    previous_at: datetime | None = None
    market_session = get_session("US")
    for index, raw_decision in enumerate(decisions):
        decision = _mapping(
            raw_decision,
            field_name=f"{label}.decisions[{index}]",
        )
        timestamp = _aware_datetime(
            decision.get("timestamp"),
            field_name=f"{label}.decisions[{index}].timestamp",
        )
        if previous_at is not None and timestamp < previous_at:
            raise TrustedAssessmentError(f"{label} decisions are not ordered")
        previous_at = timestamp
        action = _required_string(
            decision.get("action"),
            field_name=f"{label}.decisions[{index}].action",
        )
        if action not in _ALLOWED_STRATEGY_V2_ACTIONS:
            raise TrustedAssessmentError(
                f"{label} replay contains an unsupported strategy action"
            )
        if (
            not market_session.is_rth(timestamp)
            or market_session.trade_day(timestamp) != session_date
        ):
            raise TrustedAssessmentError(
                f"{label} decision is outside the target RTH session"
            )
        if action not in {"FILL_ENTRY", "EXIT_LONG"}:
            continue
        price = _positive_float(
            decision.get("price"),
            field_name=f"{label}.decisions[{index}].price",
        )
        quantity = _positive_float(
            decision.get("quantity"),
            field_name=f"{label}.decisions[{index}].quantity",
        )
        reason = _required_string(
            decision.get("reason"),
            field_name=f"{label}.decisions[{index}].reason",
        )
        if action == "FILL_ENTRY":
            if open_entry is not None:
                raise TrustedAssessmentError(
                    f"{label} replay decisions contain a position add-on"
                )
            open_entry = {
                "entry_at": timestamp,
                "entry_price": price,
                "quantity": quantity,
                "entry_reason": reason,
            }
            continue
        if open_entry is None:
            raise TrustedAssessmentError(
                f"{label} replay decisions contain an exit without entry"
            )
        if quantity.hex() != open_entry["quantity"].hex():
            raise TrustedAssessmentError(
                f"{label} replay decision exit quantity is inconsistent"
            )
        pairs.append({
            **open_entry,
            "exit_at": timestamp,
            "exit_price": price,
            "exit_reason": reason,
        })
        open_entry = None
    if open_entry is not None:
        raise TrustedAssessmentError(
            f"{label} replay decisions end with an open position"
        )
    return pairs


def _max_drawdown(values: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise TrustedAssessmentError(f"{field_name} must be an object")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TrustedAssessmentError(f"{field_name} must be an array")
    return value


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrustedAssessmentError(f"{field_name} must be a non-empty string")
    return value.strip()


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrustedAssessmentError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _finite_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrustedAssessmentError(f"{field_name} must be a finite float")
    result = float(value)
    if not math.isfinite(result):
        raise TrustedAssessmentError(f"{field_name} must be a finite float")
    return result


def _positive_float(value: object, *, field_name: str) -> float:
    result = _finite_float(value, field_name=field_name)
    if result <= 0:
        raise TrustedAssessmentError(f"{field_name} must be positive")
    return result


def _non_negative_float(value: object, *, field_name: str) -> float:
    result = _finite_float(value, field_name=field_name)
    if result < 0:
        raise TrustedAssessmentError(f"{field_name} must be non-negative")
    return result


def _aware_datetime(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TrustedAssessmentError(f"{field_name} must be an ISO datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrustedAssessmentError(
            f"{field_name} must be an ISO datetime"
        ) from exc
    if result.tzinfo is None:
        raise TrustedAssessmentError(f"{field_name} must include a timezone")
    normalized = result.astimezone(timezone.utc)
    if value != normalized.isoformat():
        raise TrustedAssessmentError(
            f"{field_name} must use canonical UTC ISO format"
        )
    return normalized


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise TrustedAssessmentError("trusted assessment decimal must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise TrustedAssessmentError("trusted assessment datetime must be aware")
    return value.astimezone(timezone.utc).isoformat()


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_PATTERN for character in value)
    )


__all__ = [
    "TRUSTED_ASSESSMENT_ALGORITHM_VERSION",
    "TRUSTED_ASSESSMENT_EXPECTED_SESSIONS",
    "TRUSTED_ASSESSMENT_POLICY_VERSION",
    "TRUSTED_ASSESSMENT_REPORT_SCHEMA_VERSION",
    "TRUSTED_ASSESSMENT_WINDOW_DIGEST",
    "TRUSTED_ASSESSMENT_WINDOW_END",
    "TRUSTED_ASSESSMENT_WINDOW_START",
    "TrustedAssessmentError",
    "TrustedDailyLeaf",
    "TrustedProducerCutoff",
    "TrustedSymbolEvidence",
    "TrustedTradeSummary",
    "build_trusted_assessment_report",
    "canonical_sha256",
    "trusted_assessment_sessions",
    "trusted_daily_binding_sha256",
    "trusted_producer_cutoff",
    "validate_replay_trade_track",
]
