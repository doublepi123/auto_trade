from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import trade_day_for
from app.domain.universe_selection.catalog import risk_group_for_sector
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
    QUANT_WARMUP_SOURCE,
    list_latest_current_quant_scores,
)
from app.services.watchlist_score_service import WatchlistScoreService
from app.services.universe_selection_service import (
    minimum_peer_observation_dollar_volume,
    observation_pool_overrides,
    select_exploration_candidates,
    validated_inverse_volatility_observation_symbols,
    validated_point_in_time_shrinkage_observation_symbols,
)

_TERMINAL_RUN_STATUSES = ("COMPLETE", "DEGRADED")
_REVIEW_READY_STATUSES = {"READY_FOR_REVIEW", "MATURE_EVIDENCE"}
_PRIORITY_ALGORITHM_VERSION = (
    "selection-exploration-quant-core-satellite-observation-v7"
)
_DIVERSIFIED_OBSERVATION_LIMIT = 8
_GROWTH_SATELLITE_LIMIT = 4
_GROWTH_SATELLITE_MAX_PER_RISK_GROUP = 2
_GROWTH_SATELLITE_MAX_COST_BPS = 20.0
_MAX_QUANT_WEIGHT = 0.35
_QUANT_NEUTRAL_SCORE = 50.0
_QUANT_DATA_ERROR_PENALTY = -25.0
_QUANT_MISSING_PENALTY = -25.0
_QUANT_STALE_PENALTY = -15.0
_QUANT_AVOID_PENALTY = -20.0
_QUANT_WATCH_PENALTY = -10.0
_MIN_FORWARD_REVIEW_TRADES = 5
_MIN_FORWARD_MATURE_TRADES = 20


def _candidate_memberships(
    candidate: UniverseSelectionCandidate,
) -> list[Literal["NASDAQ_100", "DJIA"]]:
    try:
        raw = json.loads(candidate.memberships_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    memberships: list[Literal["NASDAQ_100", "DJIA"]] = []
    for membership in raw:
        if membership == "NASDAQ_100":
            memberships.append("NASDAQ_100")
        elif membership == "DJIA":
            memberships.append("DJIA")
    return memberships


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
    if quant is None:
        return _QUANT_MISSING_PENALTY
    if quant.source != QUANT_SCORE_SOURCE:
        return _QUANT_DATA_ERROR_PENALTY
    if not fresh:
        return _QUANT_STALE_PENALTY
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


def _promotion_blockers(
    *,
    forward_blockers: list[str],
    forward_status: str,
    shadow_enabled: bool,
    quant: WatchlistScore | None,
    quant_fresh: bool,
    baseline_closed_trades: int,
    baseline_net_pnl: float,
    candidate_closed_trades: int,
    candidate_net_pnl: float,
) -> list[str]:
    blockers = list(forward_blockers)
    if not shadow_enabled:
        blockers.append("SHADOW_DISABLED")
    if quant is None:
        blockers.append("QUANT_SCORE_MISSING")
    elif quant.source == QUANT_WARMUP_SOURCE:
        blockers.append("QUANT_SCORE_WARMING_UP")
    elif quant.source != QUANT_SCORE_SOURCE:
        blockers.append("QUANT_SCORE_DATA_ERROR")
    elif not quant_fresh:
        blockers.append("QUANT_SCORE_STALE")
    elif quant.recommended_action.upper() != "CANDIDATE":
        blockers.append("QUANT_ACTION_NOT_CANDIDATE")

    if forward_status in _REVIEW_READY_STATUSES:
        required_trades = (
            _MIN_FORWARD_MATURE_TRADES
            if forward_status == "MATURE_EVIDENCE"
            else _MIN_FORWARD_REVIEW_TRADES
        )
        if candidate_closed_trades < required_trades:
            blockers.append("FORWARD_CANDIDATE_TRADES_INSUFFICIENT")
        else:
            if candidate_net_pnl <= 0:
                blockers.append("FORWARD_CANDIDATE_NET_PNL_NON_POSITIVE")
            if (
                baseline_closed_trades > 0
                and candidate_net_pnl <= baseline_net_pnl
            ):
                blockers.append("FORWARD_CANDIDATE_NOT_BETTER_THAN_BASELINE")
    return list(dict.fromkeys(blockers))


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
        context = self._observation_context()
        if context is None:
            return None
        (
            run,
            observed,
            exploration_symbols,
            trading_symbol,
            _,
        ) = context
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
        for candidate in observed:
            if candidate.selected and candidate.rank is None:
                raise ValueError(
                    "selected universe candidate must have a rank"
                )
            universe_role = (
                "SELECTED"
                if candidate.selected
                else (
                    "EXPLORATION"
                    if candidate.symbol in exploration_symbols
                    else "TRADING_TARGET"
                )
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
            shadow_enabled = candidate.symbol in enabled_shadow_symbols
            blockers = _promotion_blockers(
                forward_blockers=list(forward.blockers),
                forward_status=forward.status,
                shadow_enabled=shadow_enabled,
                quant=quant,
                quant_fresh=quant_fresh,
                baseline_closed_trades=(
                    forward.baseline_metrics.closed_trades
                ),
                baseline_net_pnl=forward.baseline_metrics.net_pnl,
                candidate_closed_trades=(
                    forward.candidate_metrics.closed_trades
                ),
                candidate_net_pnl=forward.candidate_metrics.net_pnl,
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
                    memberships=_candidate_memberships(candidate),
                    sector=candidate.sector,
                    risk_group=risk_group_for_sector(candidate.sector),
                    universe_role=universe_role,
                    rank=candidate.rank,
                    selection_score=selection_score,
                    priority_rank=1,
                    priority_score=priority_score,
                    quant_weight=quant_weight,
                    quant_adjustment=quant_adjustment,
                    is_trading_target=candidate.symbol == trading_symbol,
                    shadow_enabled=shadow_enabled,
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
                    estimated_round_trip_cost_bps=(
                        quant.estimated_round_trip_cost_bps
                        if quant is not None
                        else None
                    ),
                    forward_status=forward.status,
                    included_pairs=forward.included_pairs,
                    minimum_ready_pairs=forward.minimum_ready_pairs,
                    minimum_mature_pairs=forward.minimum_mature_pairs,
                    remaining_ready_pairs=forward.remaining_ready_pairs,
                    remaining_mature_pairs=forward.remaining_mature_pairs,
                    blockers=blockers,
                    baseline_metrics=forward.baseline_metrics,
                    candidate_metrics=forward.candidate_metrics,
                    review_ready=(
                        forward.status in _REVIEW_READY_STATUSES
                        and not blockers
                    ),
                    mature_evidence=(
                        forward.status == "MATURE_EVIDENCE"
                    ),
                )
            )
        items.sort(
            key=lambda item: (
                -item.priority_score,
                item.rank or 10_000,
                item.symbol,
            )
        )
        items = [
            item.model_copy(update={"priority_rank": priority_rank})
            for priority_rank, item in enumerate(items, start=1)
        ]
        diversified_ranks: dict[str, int] = {}
        represented_risk_groups: set[str] = set()
        for item in items:
            if len(diversified_ranks) >= _DIVERSIFIED_OBSERVATION_LIMIT:
                break
            if (
                not item.shadow_enabled
                or not item.quant_fresh
                or item.quant_source != QUANT_SCORE_SOURCE
                or item.quant_recommended_action.upper()
                not in {"WATCH", "CANDIDATE"}
                or not item.risk_group
                or item.risk_group in represented_risk_groups
            ):
                continue
            represented_risk_groups.add(item.risk_group)
            diversified_ranks[item.symbol] = len(diversified_ranks) + 1
        items = [
            item.model_copy(update={
                "diversified_observation_selected": (
                    item.symbol in diversified_ranks
                ),
                "diversified_observation_rank": diversified_ranks.get(
                    item.symbol
                ),
            })
            for item in items
        ]
        satellite_ranks: dict[str, int] = {}
        satellite_risk_group_counts: dict[str, int] = {}
        for item in items:
            if len(satellite_ranks) >= _GROWTH_SATELLITE_LIMIT:
                break
            cost_bps = item.estimated_round_trip_cost_bps
            if (
                item.diversified_observation_selected
                or item.is_trading_target
                or not item.memberships
                or not item.shadow_enabled
                or not item.quant_fresh
                or item.quant_source != QUANT_SCORE_SOURCE
                or item.quant_recommended_action.upper()
                not in {"WATCH", "CANDIDATE"}
                or not item.risk_group
                or cost_bps is None
                or cost_bps > _GROWTH_SATELLITE_MAX_COST_BPS
                or satellite_risk_group_counts.get(item.risk_group, 0)
                >= _GROWTH_SATELLITE_MAX_PER_RISK_GROUP
            ):
                continue
            satellite_risk_group_counts[item.risk_group] = (
                satellite_risk_group_counts.get(item.risk_group, 0) + 1
            )
            satellite_ranks[item.symbol] = len(satellite_ranks) + 1
        items = [
            item.model_copy(update={
                "growth_satellite_selected": item.symbol in satellite_ranks,
                "growth_satellite_rank": satellite_ranks.get(item.symbol),
            })
            for item in items
        ]
        return UniversePromotionReadinessResponse(
            universe_run_id=run.id,
            as_of_date=run.as_of_date,
            generated_at=self.now,
            priority_algorithm_version=_PRIORITY_ALGORITHM_VERSION,
            diversified_observation_limit=_DIVERSIFIED_OBSERVATION_LIMIT,
            growth_satellite_limit=_GROWTH_SATELLITE_LIMIT,
            items=items,
        )

    def get_observed_symbols(self) -> frozenset[str]:
        """Return all current daily and frozen-rotation observations."""
        context = self._observation_context()
        if context is None:
            return frozenset()
        return frozenset(
            candidate.symbol for candidate in context[1]
        ) | context[4]

    def _observation_context(
        self,
    ) -> tuple[
        UniverseSelectionRun,
        list[UniverseSelectionCandidate],
        set[str],
        str,
        frozenset[str],
    ] | None:
        run = self._latest_terminal_run()
        if run is None:
            return None
        candidates = (
            self.db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == run.id,
            )
            .order_by(
                UniverseSelectionCandidate.selected.desc(),
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
        observation_overrides = observation_pool_overrides(self.db)
        rotation_observation_symbols = (
            validated_inverse_volatility_observation_symbols(
                run,
                candidates,
                session_date=trade_day_for("US", self.now),
            )
            | validated_point_in_time_shrinkage_observation_symbols(
                run,
                candidates,
                session_date=trade_day_for("US", self.now),
            )
        ) - observation_overrides.unobservable_symbols
        exploration_symbols = {
            candidate.symbol
            for candidate in select_exploration_candidates(
                candidates,
                max_symbols=(
                    settings.universe_selection_exploration_max_symbols
                ),
                max_per_sector=(
                    settings.universe_selection_max_per_sector
                ),
                top_score_challengers=(
                    settings
                    .universe_selection_exploration_top_score_challengers
                ),
                already_observed_symbols=(
                    observation_overrides.already_observed_symbols
                ),
                challenger_excluded_symbols=(
                    observation_overrides.challenger_excluded_symbols
                ),
                unobservable_symbols=(
                    observation_overrides.exploration_excluded_symbols
                ),
                minimum_peer_dollar_volume=(
                    minimum_peer_observation_dollar_volume(
                        settings.universe_selection_min_avg_dollar_volume
                    )
                ),
            )
        }
        observed = [
            candidate
            for candidate in candidates
            if (
                candidate.selected
                or candidate.symbol in exploration_symbols
                or candidate.symbol == trading_symbol
            )
        ]
        return (
            run,
            observed,
            exploration_symbols,
            trading_symbol,
            rotation_observation_symbols,
        )

    def _latest_terminal_run(self) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(_TERMINAL_RUN_STATUSES),
                UniverseSelectionRun.completed_at.is_not(None),
                UniverseSelectionRun.completed_at <= self.now,
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )
