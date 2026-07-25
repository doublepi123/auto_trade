from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import cast

from sqlalchemy.orm import Session

from app.domain.universe_selection.rotation_forward_scorecard import (
    ROTATION_FORWARD_SCORECARD_VERSION,
    RotationForwardCohortEvidence,
    build_rotation_forward_track_score,
    parse_rotation_forward_cohort,
)
from app.domain.universe_selection.rotation_walk_forward import (
    CONCENTRATED_ROTATION_VARIANT,
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_ROTATION_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    RETURN_TO_VARIANCE_ROTATION_VARIANT,
)
from app.models import UniverseSelectionRun
from app.schemas import (
    UniverseRotationForwardCohortResponse,
    UniverseRotationForwardScorecardResponse,
    UniverseRotationForwardTrackResponse,
)


logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = ("COMPLETE", "DEGRADED")
_MAX_SOURCE_RUNS = 800


@dataclass(frozen=True)
class _TrackDefinition:
    parameter_key: str
    variant_name: str


_TRACKS = (
    _TrackDefinition(
        "rotation_forward_snapshot",
        DIVERSIFIED_ROTATION_VARIANT.name,
    ),
    _TrackDefinition(
        "rotation_concentration_challenger_snapshot",
        CONCENTRATED_ROTATION_VARIANT.name,
    ),
    _TrackDefinition(
        "rotation_weighting_challenger_snapshot",
        DIVERSIFIED_INVERSE_VOLATILITY_VARIANT.name,
    ),
    _TrackDefinition(
        "rotation_shrinkage_challenger_snapshot",
        DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT.name,
    ),
    _TrackDefinition(
        "rotation_return_to_variance_challenger_snapshot",
        RETURN_TO_VARIANCE_ROTATION_VARIANT.name,
    ),
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cohort_response(
    evidence: RotationForwardCohortEvidence,
) -> UniverseRotationForwardCohortResponse:
    return UniverseRotationForwardCohortResponse(
        source_run_id=evidence.source_run_id,
        source_as_of_date=evidence.source_as_of_date,
        cohort_month=evidence.cohort_month,
        status=evidence.status,
        signal_date=evidence.signal_date,
        entry_date=evidence.entry_date,
        mark_date=evidence.mark_date,
        target_symbols=list(evidence.target_symbols),
        forward_observation_sessions=(
            evidence.forward_observation_sessions
        ),
        net_return_pct=evidence.net_return_pct,
        qqq_return_pct=evidence.qqq_return_pct,
        dia_return_pct=evidence.dia_return_pct,
        excess_return_vs_qqq_pct=(
            evidence.excess_return_vs_qqq_pct
        ),
        excess_return_vs_dia_pct=(
            evidence.excess_return_vs_dia_pct
        ),
        selection_drift_detected=(
            evidence.selection_drift_detected
        ),
        survivorship_bias=evidence.survivorship_bias,
        blockers=list(evidence.blockers),
    )


class RotationForwardScorecardService:
    """Aggregate preregistered monthly rotation evidence without writes."""

    def __init__(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> None:
        self.db = db
        observed_at = now or datetime.now(timezone.utc)
        self.now = _as_utc(observed_at)

    def get_scorecard(
        self,
    ) -> UniverseRotationForwardScorecardResponse | None:
        runs = (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(
                    _TERMINAL_RUN_STATUSES
                ),
                UniverseSelectionRun.completed_at.is_not(None),
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.completed_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .limit(_MAX_SOURCE_RUNS)
            .all()
        )
        if not runs:
            return None
        latest = runs[0]

        raw_by_track: dict[
            str,
            dict[date, tuple[UniverseSelectionRun, dict[str, object]]],
        ] = {track.variant_name: {} for track in _TRACKS}
        invalid_by_track = {
            track.variant_name: 0 for track in _TRACKS
        }
        definitions_by_key = {
            track.parameter_key: track for track in _TRACKS
        }

        for run in reversed(runs):
            try:
                parameters = json.loads(run.parameters_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(parameters, dict):
                continue
            for parameter_key, track in definitions_by_key.items():
                raw = parameters.get(parameter_key)
                if raw is None:
                    continue
                if not isinstance(raw, dict):
                    invalid_by_track[track.variant_name] += 1
                    continue
                raw_cohort = raw.get("cohort_month")
                if raw_cohort is None:
                    continue
                try:
                    cohort_month = date.fromisoformat(str(raw_cohort))
                except ValueError:
                    invalid_by_track[track.variant_name] += 1
                    continue
                if cohort_month.day != 1:
                    invalid_by_track[track.variant_name] += 1
                    continue
                raw_by_track[track.variant_name][cohort_month] = (
                    run,
                    cast(dict[str, object], raw),
                )

        responses: list[UniverseRotationForwardTrackResponse] = []
        for track in _TRACKS:
            parsed: list[RotationForwardCohortEvidence] = []
            invalid_count = invalid_by_track[track.variant_name]
            for run, raw in raw_by_track[track.variant_name].values():
                try:
                    parsed.append(
                        parse_rotation_forward_cohort(
                            raw,
                            source_run_id=run.id,
                            source_as_of_date=run.as_of_date,
                            expected_variant_name=track.variant_name,
                        )
                    )
                except (TypeError, ValueError):
                    invalid_count += 1
                    logger.warning(
                        "ignored invalid %s in universe run %s",
                        track.parameter_key,
                        run.id,
                    )

            score = build_rotation_forward_track_score(
                variant_name=track.variant_name,
                evidence=parsed,
                as_of_date=latest.as_of_date,
                invalid_evidence_records=invalid_count,
            )
            responses.append(
                UniverseRotationForwardTrackResponse(
                    variant_name=score.variant_name,
                    status=score.status,
                    observed_cohorts=score.observed_cohorts,
                    forward_eligible_cohorts=(
                        score.forward_eligible_cohorts
                    ),
                    completed_cohorts=score.completed_cohorts,
                    minimum_completed_cohorts=(
                        score.minimum_completed_cohorts
                    ),
                    remaining_completed_cohorts=(
                        score.remaining_completed_cohorts
                    ),
                    backfilled_cohorts=score.backfilled_cohorts,
                    incomplete_closed_cohorts=(
                        score.incomplete_closed_cohorts
                    ),
                    selection_drift_cohorts=(
                        score.selection_drift_cohorts
                    ),
                    invalid_evidence_records=(
                        score.invalid_evidence_records
                    ),
                    first_completed_cohort_month=(
                        score.first_completed_cohort_month
                    ),
                    latest_completed_cohort_month=(
                        score.latest_completed_cohort_month
                    ),
                    open_cohort=(
                        _cohort_response(score.open_cohort)
                        if score.open_cohort is not None
                        else None
                    ),
                    compounded_return_pct=(
                        score.compounded_return_pct
                    ),
                    qqq_compounded_return_pct=(
                        score.qqq_compounded_return_pct
                    ),
                    dia_compounded_return_pct=(
                        score.dia_compounded_return_pct
                    ),
                    compounded_excess_vs_qqq_pct=(
                        score.compounded_excess_vs_qqq_pct
                    ),
                    compounded_excess_vs_dia_pct=(
                        score.compounded_excess_vs_dia_pct
                    ),
                    positive_cohort_rate_pct=(
                        score.positive_cohort_rate_pct
                    ),
                    excess_win_rate_vs_qqq_pct=(
                        score.excess_win_rate_vs_qqq_pct
                    ),
                    excess_win_rate_vs_dia_pct=(
                        score.excess_win_rate_vs_dia_pct
                    ),
                    average_cohort_return_pct=(
                        score.average_cohort_return_pct
                    ),
                    worst_cohort_return_pct=(
                        score.worst_cohort_return_pct
                    ),
                    manual_review_ready=score.manual_review_ready,
                    automatic_promotion_allowed=False,
                    blockers=list(score.blockers),
                    warnings=list(score.warnings),
                )
            )

        return UniverseRotationForwardScorecardResponse(
            algorithm_version=ROTATION_FORWARD_SCORECARD_VERSION,
            universe_run_id=latest.id,
            as_of_date=latest.as_of_date,
            generated_at=self.now,
            source_run_count=len(runs),
            tracks=responses,
            automatic_promotion_allowed=False,
        )
