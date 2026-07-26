from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from functools import reduce
from operator import mul
from typing import Literal, Mapping, Sequence, cast

from app.domain.universe_selection.rotation_forward import (
    ROTATION_FORWARD_VERSION,
    is_last_us_session_of_month,
)
from app.domain.universe_selection.selector import (
    ROTATION_ALGORITHM_VERSION,
)


ROTATION_FORWARD_SCORECARD_VERSION = "rotation-forward-scorecard-v1"
MINIMUM_COMPLETED_FORWARD_COHORTS = 3
MINIMUM_EXCESS_WIN_RATE_PCT = 60.0
RotationForwardTrackStatus = Literal[
    "NOT_REGISTERED",
    "AWAITING_PRECOMMITMENT",
    "COLLECTING",
    "DATA_BLOCKED",
    "PERFORMANCE_BLOCKED",
    "READY_FOR_MANUAL_REVIEW",
]
RotationForwardEvidenceMode = Literal[
    "FORWARD_PRECOMMITTED",
    "BACKFILLED_AFTER_ENTRY",
]

_FORWARD_STATUSES = {"FORWARD_OPEN", "FORWARD_CASH"}
_BACKFILLED_STATUSES = {"BACKFILLED_OPEN", "BACKFILLED_CASH"}
_SNAPSHOT_STATUSES = {
    *_FORWARD_STATUSES,
    *_BACKFILLED_STATUSES,
    "DATA_INCOMPLETE",
}
_EVIDENCE_MODES = {
    "FORWARD_PRECOMMITTED",
    "BACKFILLED_AFTER_ENTRY",
}
_DATA_BLOCKERS = {
    "FORWARD_COHORT_DATA_INCOMPLETE",
    "FORWARD_EVIDENCE_INVALID",
    "FORWARD_SELECTION_DRIFT",
}


def _required_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean")


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_date(value: object, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        result = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number")
    return result


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in value
    ):
        raise ValueError(f"{field_name} must be a string list")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _metric(value: float) -> float:
    return round(value, 6)


@dataclass(frozen=True)
class RotationForwardCohortEvidence:
    source_run_id: int
    source_as_of_date: date
    cohort_month: date
    variant_name: str
    status: str
    evidence_mode: RotationForwardEvidenceMode
    signal_date: date
    entry_date: date
    mark_date: date
    registered_as_of_date: date
    forward_eligible: bool
    selection_drift_detected: bool
    target_symbols: tuple[str, ...]
    forward_observation_sessions: int
    net_return_pct: float | None
    qqq_return_pct: float | None
    dia_return_pct: float | None
    excess_return_vs_qqq_pct: float | None
    excess_return_vs_dia_pct: float | None
    survivorship_bias: bool
    blockers: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return (
            self.forward_eligible
            and self.evidence_mode == "FORWARD_PRECOMMITTED"
            and self.status in _FORWARD_STATUSES
            and not self.selection_drift_detected
            and self.forward_observation_sessions > 0
            and is_last_us_session_of_month(self.mark_date)
            and all(
                value is not None
                for value in (
                    self.net_return_pct,
                    self.qqq_return_pct,
                    self.dia_return_pct,
                    self.excess_return_vs_qqq_pct,
                    self.excess_return_vs_dia_pct,
                )
            )
        )


@dataclass(frozen=True)
class RotationForwardTrackScore:
    variant_name: str
    status: RotationForwardTrackStatus
    observed_cohorts: int
    forward_eligible_cohorts: int
    completed_cohorts: int
    minimum_completed_cohorts: int
    remaining_completed_cohorts: int
    backfilled_cohorts: int
    incomplete_closed_cohorts: int
    selection_drift_cohorts: int
    invalid_evidence_records: int
    first_completed_cohort_month: date | None
    latest_completed_cohort_month: date | None
    open_cohort: RotationForwardCohortEvidence | None
    diagnostic_cohort: RotationForwardCohortEvidence | None
    compounded_return_pct: float | None
    qqq_compounded_return_pct: float | None
    dia_compounded_return_pct: float | None
    compounded_excess_vs_qqq_pct: float | None
    compounded_excess_vs_dia_pct: float | None
    positive_cohort_rate_pct: float | None
    excess_win_rate_vs_qqq_pct: float | None
    excess_win_rate_vs_dia_pct: float | None
    average_cohort_return_pct: float | None
    worst_cohort_return_pct: float | None
    manual_review_ready: bool
    automatic_promotion_allowed: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def parse_rotation_forward_cohort(
    payload: Mapping[str, object],
    *,
    source_run_id: int,
    source_as_of_date: date,
    expected_variant_name: str,
) -> RotationForwardCohortEvidence:
    if source_run_id < 1:
        raise ValueError("source_run_id must be positive")
    algorithm_version = _required_string(
        payload.get("algorithm_version"),
        field_name="algorithm_version",
    )
    if algorithm_version != ROTATION_FORWARD_VERSION:
        raise ValueError("rotation forward algorithm version is invalid")
    rotation_algorithm_version = _required_string(
        payload.get("rotation_algorithm_version"),
        field_name="rotation_algorithm_version",
    )
    if rotation_algorithm_version != ROTATION_ALGORITHM_VERSION:
        raise ValueError("rotation selection algorithm version is invalid")
    cohort_month = _required_date(
        payload.get("cohort_month"),
        field_name="cohort_month",
    )
    if cohort_month.day != 1:
        raise ValueError("cohort_month must be the first of month")
    variant_name = _required_string(
        payload.get("variant_name"),
        field_name="variant_name",
    )
    if variant_name != expected_variant_name:
        raise ValueError("rotation forward variant does not match its track")
    status = _required_string(payload.get("status"), field_name="status")
    if status not in _SNAPSHOT_STATUSES:
        raise ValueError("rotation forward status is invalid")
    raw_evidence_mode = _required_string(
        payload.get("evidence_mode"),
        field_name="evidence_mode",
    )
    if raw_evidence_mode not in _EVIDENCE_MODES:
        raise ValueError("rotation forward evidence mode is invalid")
    evidence_mode = cast(
        RotationForwardEvidenceMode,
        raw_evidence_mode,
    )
    signal_date = _required_date(
        payload.get("signal_date"),
        field_name="signal_date",
    )
    entry_date = _required_date(
        payload.get("entry_date"),
        field_name="entry_date",
    )
    mark_date = _required_date(
        payload.get("mark_date"),
        field_name="mark_date",
    )
    registered_as_of_date = _required_date(
        payload.get("registered_as_of_date"),
        field_name="registered_as_of_date",
    )
    if signal_date >= cohort_month:
        raise ValueError("signal_date must precede cohort_month")
    if registered_as_of_date < signal_date:
        raise ValueError("registration cannot precede the signal")
    if entry_date < cohort_month or (
        entry_date.year,
        entry_date.month,
    ) != (cohort_month.year, cohort_month.month):
        raise ValueError("entry_date must be within cohort_month")
    if mark_date < entry_date or (
        mark_date.year,
        mark_date.month,
    ) != (cohort_month.year, cohort_month.month):
        raise ValueError("mark_date must be within cohort_month")
    if mark_date > source_as_of_date:
        raise ValueError("mark_date cannot follow its source run")
    if registered_as_of_date > source_as_of_date:
        raise ValueError("registration cannot follow its source run")

    forward_eligible = _required_bool(
        payload.get("forward_eligible"),
        field_name="forward_eligible",
    )
    if forward_eligible != (
        evidence_mode == "FORWARD_PRECOMMITTED"
        and registered_as_of_date == signal_date
    ):
        raise ValueError("forward eligibility is inconsistent")
    if (
        not forward_eligible
        and registered_as_of_date == signal_date
    ):
        raise ValueError("backfilled registration must follow the signal")
    if (
        status in _FORWARD_STATUSES
        and not forward_eligible
    ):
        raise ValueError("forward status requires eligible evidence")
    if status in _BACKFILLED_STATUSES and forward_eligible:
        raise ValueError("backfilled status requires ineligible evidence")

    selection_drift_detected = _required_bool(
        payload.get("selection_drift_detected"),
        field_name="selection_drift_detected",
    )
    survivorship_bias = _required_bool(
        payload.get("survivorship_bias"),
        field_name="survivorship_bias",
    )
    if _required_bool(
        payload.get("order_execution_allowed"),
        field_name="order_execution_allowed",
    ):
        raise ValueError("forward scorecard evidence must be read-only")
    if _required_bool(
        payload.get("automatic_promotion_allowed"),
        field_name="automatic_promotion_allowed",
    ):
        raise ValueError("automatic promotion must remain disabled")

    target_symbols = _string_tuple(
        payload.get("target_symbols"),
        field_name="target_symbols",
    )
    forward_observation_sessions = _non_negative_integer(
        payload.get("forward_observation_sessions"),
        field_name="forward_observation_sessions",
    )
    if status.endswith("_OPEN") and not target_symbols:
        raise ValueError("open rotation evidence requires target symbols")
    if status.endswith("_CASH") and target_symbols:
        raise ValueError("cash rotation evidence cannot have target symbols")
    if status in _BACKFILLED_STATUSES and forward_observation_sessions:
        raise ValueError("backfilled evidence cannot claim forward sessions")

    net_return = _optional_number(
        payload.get("net_liquidation_return_pct"),
        field_name="net_liquidation_return_pct",
    )
    qqq_return = _optional_number(
        payload.get("qqq_return_pct"),
        field_name="qqq_return_pct",
    )
    dia_return = _optional_number(
        payload.get("dia_return_pct"),
        field_name="dia_return_pct",
    )
    excess_qqq = _optional_number(
        payload.get("excess_return_vs_qqq_pct"),
        field_name="excess_return_vs_qqq_pct",
    )
    excess_dia = _optional_number(
        payload.get("excess_return_vs_dia_pct"),
        field_name="excess_return_vs_dia_pct",
    )
    for field_name, value in (
        ("net_liquidation_return_pct", net_return),
        ("qqq_return_pct", qqq_return),
        ("dia_return_pct", dia_return),
    ):
        if value is not None and value <= -100:
            raise ValueError(f"{field_name} must be greater than -100")
    if status.endswith("_CASH") and (
        net_return is None
        or not math.isclose(net_return, 0.0, abs_tol=1e-9)
    ):
        raise ValueError("cash rotation return must be zero")
    if (
        net_return is not None
        and qqq_return is not None
        and (
            excess_qqq is None
            or not math.isclose(
                excess_qqq,
                net_return - qqq_return,
                abs_tol=1e-6,
            )
        )
    ):
        raise ValueError("QQQ excess return is inconsistent")
    if (
        (net_return is None or qqq_return is None)
        and excess_qqq is not None
    ):
        raise ValueError("QQQ excess return requires both returns")
    if (
        net_return is not None
        and dia_return is not None
        and (
            excess_dia is None
            or not math.isclose(
                excess_dia,
                net_return - dia_return,
                abs_tol=1e-6,
            )
        )
    ):
        raise ValueError("DIA excess return is inconsistent")
    if (
        (net_return is None or dia_return is None)
        and excess_dia is not None
    ):
        raise ValueError("DIA excess return requires both returns")

    return RotationForwardCohortEvidence(
        source_run_id=source_run_id,
        source_as_of_date=source_as_of_date,
        cohort_month=cohort_month,
        variant_name=variant_name,
        status=status,
        evidence_mode=evidence_mode,
        signal_date=signal_date,
        entry_date=entry_date,
        mark_date=mark_date,
        registered_as_of_date=registered_as_of_date,
        forward_eligible=forward_eligible,
        selection_drift_detected=selection_drift_detected,
        target_symbols=target_symbols,
        forward_observation_sessions=forward_observation_sessions,
        net_return_pct=net_return,
        qqq_return_pct=qqq_return,
        dia_return_pct=dia_return,
        excess_return_vs_qqq_pct=excess_qqq,
        excess_return_vs_dia_pct=excess_dia,
        survivorship_bias=survivorship_bias,
        blockers=_string_tuple(
            payload.get("blockers"),
            field_name="blockers",
        ),
    )


def _compounded_return(values: Sequence[float]) -> float:
    growth = reduce(
        mul,
        (1.0 + value / 100.0 for value in values),
        1.0,
    )
    return _metric((growth - 1.0) * 100.0)


def build_rotation_forward_track_score(
    *,
    variant_name: str,
    evidence: Sequence[RotationForwardCohortEvidence],
    as_of_date: date,
    invalid_evidence_records: int = 0,
    minimum_completed_cohorts: int = (
        MINIMUM_COMPLETED_FORWARD_COHORTS
    ),
    minimum_excess_win_rate_pct: float = (
        MINIMUM_EXCESS_WIN_RATE_PCT
    ),
) -> RotationForwardTrackScore:
    if not variant_name:
        raise ValueError("variant_name is required")
    if invalid_evidence_records < 0:
        raise ValueError("invalid_evidence_records must not be negative")
    if minimum_completed_cohorts < 1:
        raise ValueError("minimum_completed_cohorts must be positive")
    if not 0 <= minimum_excess_win_rate_pct <= 100:
        raise ValueError("minimum_excess_win_rate_pct is invalid")

    latest_by_cohort: dict[date, RotationForwardCohortEvidence] = {}
    for item in evidence:
        if item.variant_name != variant_name:
            raise ValueError("evidence variant does not match score track")
        current = latest_by_cohort.get(item.cohort_month)
        item_key = (
            item.mark_date,
            item.source_as_of_date,
            item.source_run_id,
        )
        current_key = (
            current.mark_date,
            current.source_as_of_date,
            current.source_run_id,
        ) if current is not None else None
        if current_key is None or item_key > current_key:
            latest_by_cohort[item.cohort_month] = item

    observed = tuple(
        latest_by_cohort[key]
        for key in sorted(latest_by_cohort)
    )
    eligible = tuple(
        item
        for item in observed
        if item.forward_eligible
        and item.evidence_mode == "FORWARD_PRECOMMITTED"
    )
    completed = tuple(item for item in eligible if item.complete)
    as_of_month = as_of_date.replace(day=1)
    open_items = tuple(
        item
        for item in eligible
        if not item.complete and item.cohort_month == as_of_month
    )
    open_cohort = open_items[-1] if open_items else None
    diagnostic_items = tuple(
        item for item in observed if not item.forward_eligible
    )
    diagnostic_cohort = (
        diagnostic_items[-1] if diagnostic_items else None
    )
    backfilled_count = len(diagnostic_items)
    drift_count = sum(
        item.selection_drift_detected for item in eligible
    )
    incomplete_closed_count = sum(
        not item.complete
        and (
            item.cohort_month < as_of_month
            or is_last_us_session_of_month(item.mark_date)
        )
        for item in eligible
    )

    returns = tuple(
        float(item.net_return_pct)
        for item in completed
        if item.net_return_pct is not None
    )
    qqq_returns = tuple(
        float(item.qqq_return_pct)
        for item in completed
        if item.qqq_return_pct is not None
    )
    dia_returns = tuple(
        float(item.dia_return_pct)
        for item in completed
        if item.dia_return_pct is not None
    )

    compounded_return = (
        _compounded_return(returns) if returns else None
    )
    qqq_compounded = (
        _compounded_return(qqq_returns) if qqq_returns else None
    )
    dia_compounded = (
        _compounded_return(dia_returns) if dia_returns else None
    )
    compounded_excess_qqq = (
        _metric(compounded_return - qqq_compounded)
        if compounded_return is not None
        and qqq_compounded is not None
        else None
    )
    compounded_excess_dia = (
        _metric(compounded_return - dia_compounded)
        if compounded_return is not None
        and dia_compounded is not None
        else None
    )
    count = len(completed)
    positive_rate = (
        _metric(sum(value > 0 for value in returns) / count * 100)
        if count
        else None
    )
    qqq_win_rate = (
        _metric(
            sum(
                item.excess_return_vs_qqq_pct is not None
                and item.excess_return_vs_qqq_pct > 0
                for item in completed
            )
            / count
            * 100
        )
        if count
        else None
    )
    dia_win_rate = (
        _metric(
            sum(
                item.excess_return_vs_dia_pct is not None
                and item.excess_return_vs_dia_pct > 0
                for item in completed
            )
            / count
            * 100
        )
        if count
        else None
    )

    blockers: list[str] = []
    if count < minimum_completed_cohorts:
        blockers.append("FORWARD_COMPLETED_COHORTS_INSUFFICIENT")
    if invalid_evidence_records:
        blockers.append("FORWARD_EVIDENCE_INVALID")
    if drift_count:
        blockers.append("FORWARD_SELECTION_DRIFT")
    if incomplete_closed_count:
        blockers.append("FORWARD_COHORT_DATA_INCOMPLETE")
    elif any(item.status == "DATA_INCOMPLETE" for item in eligible):
        blockers.append("FORWARD_COHORT_DATA_INCOMPLETE")
    if compounded_return is not None and compounded_return <= 0:
        blockers.append("FORWARD_COMPOUNDED_RETURN_NON_POSITIVE")
    if (
        compounded_excess_qqq is not None
        and compounded_excess_qqq <= 0
    ):
        blockers.append("FORWARD_EXCESS_VS_QQQ_NON_POSITIVE")
    if (
        compounded_excess_dia is not None
        and compounded_excess_dia <= 0
    ):
        blockers.append("FORWARD_EXCESS_VS_DIA_NON_POSITIVE")
    if qqq_win_rate is not None and (
        qqq_win_rate < minimum_excess_win_rate_pct
    ):
        blockers.append("FORWARD_WIN_RATE_VS_QQQ_INSUFFICIENT")
    if dia_win_rate is not None and (
        dia_win_rate < minimum_excess_win_rate_pct
    ):
        blockers.append("FORWARD_WIN_RATE_VS_DIA_INSUFFICIENT")

    warnings: list[str] = []
    if backfilled_count:
        warnings.append("BACKFILLED_COHORTS_EXCLUDED")
    if any(item.survivorship_bias for item in completed):
        warnings.append("SURVIVORSHIP_BIAS")

    manual_review_ready = not blockers
    if any(blocker in _DATA_BLOCKERS for blocker in blockers):
        status = "DATA_BLOCKED"
    elif not observed:
        status = "NOT_REGISTERED"
    elif not eligible:
        status = "AWAITING_PRECOMMITMENT"
    elif count < minimum_completed_cohorts:
        status = "COLLECTING"
    elif manual_review_ready:
        status = "READY_FOR_MANUAL_REVIEW"
    else:
        status = "PERFORMANCE_BLOCKED"

    return RotationForwardTrackScore(
        variant_name=variant_name,
        status=status,
        observed_cohorts=len(observed),
        forward_eligible_cohorts=len(eligible),
        completed_cohorts=count,
        minimum_completed_cohorts=minimum_completed_cohorts,
        remaining_completed_cohorts=max(
            0,
            minimum_completed_cohorts - count,
        ),
        backfilled_cohorts=backfilled_count,
        incomplete_closed_cohorts=incomplete_closed_count,
        selection_drift_cohorts=drift_count,
        invalid_evidence_records=invalid_evidence_records,
        first_completed_cohort_month=(
            completed[0].cohort_month if completed else None
        ),
        latest_completed_cohort_month=(
            completed[-1].cohort_month if completed else None
        ),
        open_cohort=open_cohort,
        diagnostic_cohort=diagnostic_cohort,
        compounded_return_pct=compounded_return,
        qqq_compounded_return_pct=qqq_compounded,
        dia_compounded_return_pct=dia_compounded,
        compounded_excess_vs_qqq_pct=compounded_excess_qqq,
        compounded_excess_vs_dia_pct=compounded_excess_dia,
        positive_cohort_rate_pct=positive_rate,
        excess_win_rate_vs_qqq_pct=qqq_win_rate,
        excess_win_rate_vs_dia_pct=dia_win_rate,
        average_cohort_return_pct=(
            _metric(sum(returns) / count) if count else None
        ),
        worst_cohort_return_pct=(min(returns) if returns else None),
        manual_review_ready=manual_review_ready,
        automatic_promotion_allowed=False,
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
