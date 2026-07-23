from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    StrategyConfig,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.schemas import (
    UniversePromotionReadinessItem,
    UniversePromotionReadinessResponse,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.watchlist_score_service import WatchlistScoreService

_TERMINAL_RUN_STATUSES = ("COMPLETE", "DEGRADED")
_REVIEW_READY_STATUSES = {"READY_FOR_REVIEW", "MATURE_EVIDENCE"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
            for row in score_service.list_latest_per_symbol_and_family()
            if score_service.source_family(row.source) == "quant"
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
            items.append(
                UniversePromotionReadinessItem(
                    symbol=candidate.symbol,
                    rank=candidate.rank,
                    selection_score=candidate.score,
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
                        quant is not None
                        and score_service.is_fresh(
                            quant,
                            self.now,
                        )
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
        return UniversePromotionReadinessResponse(
            universe_run_id=run.id,
            as_of_date=run.as_of_date,
            generated_at=self.now,
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
