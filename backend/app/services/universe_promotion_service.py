from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    StrategyConfig,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistScore,
)
from app.schemas import (
    UniversePromotionReadinessItem,
    UniversePromotionReadinessResponse,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.watchlist_quant_service import (
    QUANT_SCORE_SOURCE,
    list_latest_current_quant_scores,
)
from app.services.watchlist_score_service import WatchlistScoreService

_TERMINAL_RUN_STATUSES = ("COMPLETE", "DEGRADED")
_REVIEW_READY_STATUSES = {"READY_FOR_REVIEW", "MATURE_EVIDENCE"}
_PRIORITY_ALGORITHM_VERSION = "selection-quant-gated-v2"
_MAX_QUANT_WEIGHT = 0.35
_QUANT_NEUTRAL_SCORE = 50.0
_QUANT_DATA_ERROR_PENALTY = -25.0
_QUANT_AVOID_PENALTY = -20.0
_QUANT_WATCH_PENALTY = -10.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quant_priority_adjustment(
    quant: WatchlistScore | None,
    *,
    fresh: bool,
    weight: float,
) -> float:
    if quant is None or not fresh:
        return 0.0
    if quant.source != QUANT_SCORE_SOURCE:
        return _QUANT_DATA_ERROR_PENALTY
    confidence = max(0.0, min(1.0, float(quant.confidence)))
    action = quant.recommended_action.upper()
    if action == "CANDIDATE":
        return round(
            max(0.0, float(quant.score) - _QUANT_NEUTRAL_SCORE)
            * weight,
            2,
        )
    confidence_scale = 0.5 + confidence * 0.5
    penalty = (
        _QUANT_WATCH_PENALTY
        if action == "WATCH"
        else _QUANT_AVOID_PENALTY
    )
    return round(penalty * confidence_scale, 2)


class UniversePromotionService:
    """Assemble a read-only promotion-readiness view for the selected universe."""

    def __init__(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self.db = db
        self.now = observed_at.astimezone(timezone.utc)

    def get_readiness(
        self,
    ) -> UniversePromotionReadinessResponse | None:
        run = self._latest_terminal_run()
        if run is None:
            return None

        selected = (
            self.db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.selected.is_(True),
            )
            .order_by(
                UniverseSelectionCandidate.rank.asc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.symbol.asc(),
            )
            .all()
        )
        strategy = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        trading_symbol = strategy.symbol if strategy is not None else ""
        enabled_shadow_symbols = {
            row.symbol
            for row in self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.enabled.is_(True))
            .all()
        }
        score_service = WatchlistScoreService(self.db)
        quant_scores = {
            row.symbol: row
            for row in list_latest_current_quant_scores(self.db)
        }
        shadow_service = StrategyV2ShadowService(self.db)
        items: list[UniversePromotionReadinessItem] = []
        for candidate in selected:
            if candidate.rank is None:
                raise ValueError(
                    "selected universe candidate must have a rank"
                )
            forward = shadow_service.get_forward_validation(candidate.symbol)
            quant = quant_scores.get(candidate.symbol)
            quant_fresh = (
                quant is not None
                and score_service.is_fresh(
                    quant,
                    self.now,
                )
            )
            quant_confidence = (
                max(0.0, min(1.0, float(quant.confidence)))
                if quant is not None
                else 0.0
            )
            quant_weight = (
                round(_MAX_QUANT_WEIGHT * quant_confidence, 4)
                if quant is not None
                and quant.source == QUANT_SCORE_SOURCE
                and quant_fresh
                else 0.0
            )
            quant_adjustment = _quant_priority_adjustment(
                quant,
                fresh=quant_fresh,
                weight=quant_weight,
            )
            selection_score = float(candidate.score)
            priority_score = round(
                max(
                    0.0,
                    min(
                        100.0,
                        selection_score + quant_adjustment,
                    ),
                ),
                2,
            )
            items.append(
                UniversePromotionReadinessItem(
                    symbol=candidate.symbol,
                    rank=candidate.rank,
                    selection_score=selection_score,
                    priority_rank=1,
                    priority_score=priority_score,
                    quant_weight=quant_weight,
                    quant_adjustment=quant_adjustment,
                    is_trading_target=candidate.symbol == trading_symbol,
                    shadow_enabled=(
                        candidate.symbol in enabled_shadow_symbols
                    ),
                    quant_score=quant.score if quant is not None else None,
                    quant_confidence=(
                        quant.confidence if quant is not None else None
                    ),
                    quant_recommended_action=(
                        quant.recommended_action
                        if quant is not None
                        else ""
                    ),
                    quant_source=(
                        quant.source if quant is not None else ""
                    ),
                    quant_fresh=(
                        quant_fresh
                    ),
                    quant_expires_at=(
                        _as_utc(quant.expires_at)
                        if quant is not None
                        else None
                    ),
                    forward_status=forward.status,
                    included_pairs=forward.included_pairs,
                    minimum_ready_pairs=forward.minimum_ready_pairs,
                    minimum_mature_pairs=forward.minimum_mature_pairs,
                    remaining_ready_pairs=forward.remaining_ready_pairs,
                    remaining_mature_pairs=forward.remaining_mature_pairs,
                    blockers=list(forward.blockers),
                    baseline_metrics=forward.baseline_metrics,
                    candidate_metrics=forward.candidate_metrics,
                    review_ready=(
                        forward.status in _REVIEW_READY_STATUSES
                    ),
                    mature_evidence=(
                        forward.status == "MATURE_EVIDENCE"
                    ),
                )
            )
        items.sort(
            key=lambda item: (
                -item.priority_score,
                item.rank,
                item.symbol,
            )
        )
        items = [
            item.model_copy(update={"priority_rank": priority_rank})
            for priority_rank, item in enumerate(items, start=1)
        ]
        return UniversePromotionReadinessResponse(
            universe_run_id=run.id,
            as_of_date=run.as_of_date,
            generated_at=self.now,
            priority_algorithm_version=_PRIORITY_ALGORITHM_VERSION,
            items=items,
        )

    def _latest_terminal_run(self) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(_TERMINAL_RUN_STATUSES),
                UniverseSelectionRun.completed_at.is_not(None),
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )
