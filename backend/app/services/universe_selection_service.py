from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Collection, Protocol, Sequence, cast

from sqlalchemy import and_, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerCandle
from app.domain.strategy_v2 import RISK_GROUP_RELATIVE_MIN_PEERS
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    CONCENTRATED_ROTATION_VARIANT,
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_ROTATION_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    RETURN_TO_VARIANCE_ROTATION_VARIANT,
    DEFAULT_ROTATION_VARIANTS,
    INDEX_CANDIDATE_CATALOG,
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_ALGORITHM_VERSION,
    ROTATION_BENCHMARK_SYMBOLS,
    ROTATION_FORWARD_VERSION,
    ROTATION_WALK_FORWARD_VERSION,
    UNIVERSE_ALGORITHM_VERSION,
    CandidateInput,
    CandidateSelection,
    DailyBar,
    IndexCandidate,
    RotationCohortRegistration,
    UniverseSelectionConfig,
    build_rotation_cohort_registration,
    completed_daily_bars,
    evaluate_rotation_walk_forward,
    evaluate_rotation_forward,
    is_last_us_session_of_month,
    latest_closed_session_date,
    liquidity_spread_proxy_bps,
    latest_complete_session_date,
    risk_group_for_sector,
    next_cohort_month,
    rotation_cohort_month,
    select_candidates,
    unavailable_rotation_forward_snapshot,
)
from app.models import (
    OrderRecord,
    StrategyConfig,
    StrategyV2ShadowConfig,
    TrackedEntry,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistItem,
    WatchlistScore,
)
from app.schemas import StrategyV2ShadowConfigUpdate
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

logger = logging.getLogger("auto_trade.universe_selection_service")

_WATCHLIST_SOURCE = "universe"
_DAILY_BAR_COUNT = 35
_MAX_RESEARCH_DAILY_BARS = 1000
_ROTATION_EVALUATION_HISTORY_BARS = 1000
_ROTATION_EVALUATION_VALIDATION_PERIODS = 12
_ROTATION_EXPANDING_MIN_TRAINING_PERIODS = 12
_ROTATION_EXPANDING_FOLD_PERIODS = 12
_LIVE_ORDER_STATUSES = ("SUBMITTED", "PARTIAL_FILLED")
_REFRESH_LOCK = threading.Lock()
_RUN_WAIT_POLL_SECONDS = 0.05
_RUN_CLAIM_LEASE_SECONDS = 300.0
_RUN_WAIT_TIMEOUT_SECONDS = _RUN_CLAIM_LEASE_SECONDS + 30.0
_CLAIM_PREFIX = "refresh-claim:"
_EXPLORATION_ALGORITHM_VERSION = (
    "risk-group-and-refined-sector-peer-benchmark-v3"
)
_PEER_DOLLAR_VOLUME_RATIO = 0.75
_EXPLORATION_ELIGIBLE_REASONS = frozenset(
    {"SECTOR_CAP", "BELOW_SELECTION_CUTOFF"}
)
_PEER_ONLY_ELIGIBLE_REASONS = frozenset(
    {"DOLLAR_VOLUME_BELOW_MINIMUM"}
)


class UniverseMarketDataProvider(Protocol):
    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]: ...


def _research_candlesticks(
    broker: UniverseMarketDataProvider,
    symbol: str,
    period: str,
    count: int,
) -> list[BrokerCandle]:
    adjusted_reader = getattr(
        broker,
        "get_forward_adjusted_candlesticks",
        None,
    )
    if callable(adjusted_reader):
        return cast(
            list[BrokerCandle],
            adjusted_reader(symbol, period, count),
        )
    return broker.get_candlesticks(symbol, period, count)


@dataclass(frozen=True)
class UniverseRefreshResult:
    run: UniverseSelectionRun
    items: tuple[UniverseSelectionCandidate, ...]
    exploration_symbols: tuple[str, ...] = ()
    added_symbols: tuple[str, ...] = ()
    removed_symbols: tuple[str, ...] = ()
    retained_symbols: tuple[str, ...] = ()
    shadow_enabled_symbols: tuple[str, ...] = ()
    shadow_disabled_symbols: tuple[str, ...] = ()
    shadow_failed_symbols: tuple[str, ...] = ()
    applied: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _RunClaim:
    run_id: int
    token: str
    started_at: datetime


@dataclass(frozen=True)
class ObservationPoolOverrides:
    already_observed_symbols: frozenset[str]
    unobservable_symbols: frozenset[str]


def observation_pool_overrides(
    db: Session,
) -> ObservationPoolOverrides:
    """Return durable observers and explicit operator shadow opt-outs."""
    with db.no_autoflush:
        strategy = (
            db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        unmanaged = (
            db.query(StrategyV2ShadowConfig)
            .filter(
                StrategyV2ShadowConfig.universe_managed.is_(False),
            )
            .all()
        )

    already_observed = {
        row.symbol.strip().upper()
        for row in unmanaged
        if row.enabled and row.symbol.strip()
    }
    unobservable = {
        row.symbol.strip().upper()
        for row in unmanaged
        if not row.enabled and row.symbol.strip()
    }
    if strategy is not None and strategy.symbol.strip():
        primary_symbol = strategy.symbol.strip().upper()
        # The main runner observes its trading target independently of the
        # per-symbol shadow toggle.
        already_observed.add(primary_symbol)
        unobservable.discard(primary_symbol)
    return ObservationPoolOverrides(
        already_observed_symbols=frozenset(already_observed),
        unobservable_symbols=frozenset(unobservable),
    )


def minimum_peer_observation_dollar_volume(
    selection_minimum: float,
) -> float:
    if not math.isfinite(selection_minimum) or selection_minimum <= 0:
        raise ValueError(
            "selection minimum dollar volume must be positive"
        )
    return selection_minimum * _PEER_DOLLAR_VOLUME_RATIO


def _candidate_avg_dollar_volume(
    item: UniverseSelectionCandidate,
) -> float | None:
    try:
        decoded = json.loads(item.metrics_json)
        value = float(decoded["avg_dollar_volume"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def select_exploration_candidates(
    items: Sequence[UniverseSelectionCandidate],
    *,
    max_symbols: int,
    max_per_sector: int,
    already_observed_symbols: Collection[str] = (),
    unobservable_symbols: Collection[str] = (),
    minimum_risk_group_peers: int = RISK_GROUP_RELATIVE_MIN_PEERS,
    minimum_peer_dollar_volume: float | None = None,
) -> list[UniverseSelectionCandidate]:
    """Choose broad and refined-sector peers before diversified research."""
    if max_symbols < 0:
        raise ValueError("exploration max_symbols must not be negative")
    if max_per_sector < 1:
        raise ValueError("exploration max_per_sector must be positive")
    if minimum_risk_group_peers < 1:
        raise ValueError(
            "exploration minimum_risk_group_peers must be positive"
        )
    if (
        minimum_peer_dollar_volume is not None
        and (
            not math.isfinite(minimum_peer_dollar_volume)
            or minimum_peer_dollar_volume <= 0
        )
    ):
        raise ValueError(
            "exploration minimum_peer_dollar_volume must be positive"
        )
    if max_symbols == 0:
        return []

    observed_symbols = {
        symbol.strip().upper()
        for symbol in already_observed_symbols
        if symbol.strip()
    }
    blocked_symbols = {
        symbol.strip().upper()
        for symbol in unobservable_symbols
        if symbol.strip()
    }
    items_by_symbol = {
        item.symbol.strip().upper(): item
        for item in items
        if item.symbol.strip()
    }
    planned_observed_symbols = {
        item.symbol.strip().upper()
        for item in items
        if (
            item.selected
            and item.symbol.strip().upper() not in blocked_symbols
        )
    } | observed_symbols
    group_counts = Counter(
        risk_group_for_sector(items_by_symbol[symbol].sector)
        for symbol in planned_observed_symbols
        if symbol in items_by_symbol
    )
    sector_counts = Counter(
        items_by_symbol[symbol].sector.strip()
        for symbol in planned_observed_symbols
        if symbol in items_by_symbol
        and items_by_symbol[symbol].sector.strip()
    )
    selected_groups = {
        risk_group_for_sector(item.sector)
        for item in items
        if item.selected
    }

    eligible: list[UniverseSelectionCandidate] = []
    peer_only_eligible: list[
        tuple[float, UniverseSelectionCandidate]
    ] = []
    for item in items:
        normalized_symbol = item.symbol.strip().upper()
        score = float(item.score)
        if (
            item.selected
            or normalized_symbol in blocked_symbols
        ):
            continue
        try:
            decoded = json.loads(item.exclusion_reasons_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(decoded, list)
            or not decoded
            or any(not isinstance(reason, str) for reason in decoded)
        ):
            continue
        reasons = set(decoded)
        if (
            reasons.issubset(_EXPLORATION_ELIGIBLE_REASONS)
            and math.isfinite(score)
            and score > 0
        ):
            eligible.append(item)
            continue
        if (
            minimum_peer_dollar_volume is None
            or reasons != _PEER_ONLY_ELIGIBLE_REASONS
        ):
            continue
        avg_dollar_volume = _candidate_avg_dollar_volume(item)
        if (
            avg_dollar_volume is not None
            and avg_dollar_volume >= minimum_peer_dollar_volume
        ):
            peer_only_eligible.append((avg_dollar_volume, item))

    eligible.sort(key=lambda item: (-float(item.score), item.symbol))
    peer_only_eligible.sort(
        key=lambda pair: (-pair[0], pair[1].symbol)
    )
    sector_capacity = Counter(sector_counts)
    for item in eligible:
        normalized_symbol = item.symbol.strip().upper()
        sector = item.sector.strip()
        if (
            not sector
            or normalized_symbol in planned_observed_symbols
        ):
            continue
        sector_capacity[sector] += 1
    refined_peer_sectors = {
        sector
        for sector, capacity in sector_capacity.items()
        if (
            risk_group_for_sector(sector) in selected_groups
            and risk_group_for_sector(sector) != sector
            and capacity >= minimum_risk_group_peers
        )
    }
    selected: list[UniverseSelectionCandidate] = []
    selected_symbols: set[str] = set()
    peer_target = max(
        max_per_sector,
        minimum_risk_group_peers,
    )

    # First make selected risk groups usable by the cross-symbol residual
    # evaluator. These are observers only and do not relax the live selection
    # concentration limit.
    for item in eligible:
        normalized_symbol = item.symbol.strip().upper()
        if normalized_symbol in observed_symbols:
            continue
        risk_group = risk_group_for_sector(item.sector)
        if (
            risk_group not in selected_groups
            or group_counts.get(risk_group, 0) >= peer_target
        ):
            continue
        selected.append(item)
        selected_symbols.add(normalized_symbol)
        group_counts[risk_group] = (
            group_counts.get(risk_group, 0) + 1
        )
        sector = item.sector.strip()
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= max_symbols:
            return selected

    # A narrow liquidity fallback may complete a selected group's observation
    # baseline. These candidates remain ineligible for formal selection and
    # are never used by the broad exploration phase.
    for _, item in peer_only_eligible:
        normalized_symbol = item.symbol.strip().upper()
        if normalized_symbol in observed_symbols:
            continue
        risk_group = risk_group_for_sector(item.sector)
        if (
            risk_group not in selected_groups
            or group_counts.get(risk_group, 0) >= peer_target
        ):
            continue
        selected.append(item)
        selected_symbols.add(normalized_symbol)
        group_counts[risk_group] = (
            group_counts.get(risk_group, 0) + 1
        )
        sector = item.sector.strip()
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= max_symbols:
            return selected

    # Durable manual observers and the active trading target retain an
    # exploration role when they pass the exploration gates, but they do not
    # consume peer-filling work because they were counted above.
    for item in eligible:
        normalized_symbol = item.symbol.strip().upper()
        if (
            normalized_symbol not in observed_symbols
            or normalized_symbol in selected_symbols
        ):
            continue
        selected.append(item)
        selected_symbols.add(normalized_symbol)
        if len(selected) >= max_symbols:
            return selected

    # Broad risk groups can hide distinct industries. Complete a nested
    # industry atomically so a tight observer budget never leaves a thin,
    # unusable reference set.
    refined_batches: list[
        tuple[
            float,
            str,
            int,
            list[UniverseSelectionCandidate],
        ]
    ] = []
    for sector in refined_peer_sectors:
        needed = (
            minimum_risk_group_peers
            - sector_counts.get(sector, 0)
        )
        if needed <= 0:
            continue
        candidates = [
            item
            for item in eligible
            if (
                item.sector.strip() == sector
                and item.symbol.strip().upper()
                not in selected_symbols
                and item.symbol.strip().upper()
                not in observed_symbols
            )
        ]
        if len(candidates) < needed:
            continue
        refined_batches.append(
            (
                -float(candidates[0].score),
                sector,
                needed,
                candidates,
            )
        )
    refined_batches.sort(key=lambda batch: (batch[0], batch[1]))
    for _, sector, needed, candidates in refined_batches:
        if max_symbols - len(selected) < needed:
            continue
        for item in candidates[:needed]:
            normalized_symbol = item.symbol.strip().upper()
            selected.append(item)
            selected_symbols.add(normalized_symbol)
            sector_counts[sector] = (
                sector_counts.get(sector, 0) + 1
            )
            risk_group = risk_group_for_sector(sector)
            group_counts[risk_group] = (
                group_counts.get(risk_group, 0) + 1
            )

    # Spend any remaining observer budget on broad research while retaining
    # the normal per-risk-group diversification cap.
    for item in eligible:
        if len(selected) >= max_symbols:
            break
        normalized_symbol = item.symbol.strip().upper()
        if normalized_symbol in selected_symbols:
            continue
        risk_group = risk_group_for_sector(item.sector)
        group_cap = (
            peer_target
            if risk_group in selected_groups
            else max_per_sector
        )
        if group_counts.get(risk_group, 0) >= group_cap:
            continue
        selected.append(item)
        selected_symbols.add(normalized_symbol)
        group_counts[risk_group] = (
            group_counts.get(risk_group, 0) + 1
        )
        sector = item.sector.strip()
        if sector:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= max_symbols:
            break
    return selected


def selection_config_from_settings(
    *,
    round_trip_fee_bps: float | None = None,
) -> UniverseSelectionConfig:
    return UniverseSelectionConfig(
        max_selected=settings.universe_selection_max_symbols,
        max_per_sector=settings.universe_selection_max_per_sector,
        min_price=settings.universe_selection_min_price,
        min_avg_dollar_volume=(
            settings.universe_selection_min_avg_dollar_volume
        ),
        max_relative_spread_bps=settings.universe_selection_max_spread_bps,
        min_realized_vol_20d=settings.universe_selection_min_realized_vol,
        max_realized_vol_20d=settings.universe_selection_max_realized_vol,
        min_atr_pct_14d=settings.universe_selection_min_atr_pct,
        max_atr_pct_14d=settings.universe_selection_max_atr_pct,
        round_trip_fee_bps=(
            10.0
            if round_trip_fee_bps is None
            else round_trip_fee_bps
        ),
        round_trip_slippage_bps=settings.entry_round_trip_slippage_bps,
    )


def _active_round_trip_fee_bps(db: Session) -> float | None:
    with db.no_autoflush:
        config = (
            db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
    fee_rate = getattr(config, "fee_rate_us", None)
    if fee_rate is None:
        return None
    value = float(fee_rate)
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "ignoring invalid US fee rate for universe selection: %r",
            fee_rate,
        )
        return None
    return round(value * 2 * 10_000, 10)


class UniverseSelectionService:
    def __init__(
        self,
        db: Session,
        broker: UniverseMarketDataProvider,
        *,
        catalog: Sequence[IndexCandidate] = INDEX_CANDIDATE_CATALOG,
        config: UniverseSelectionConfig | None = None,
        minimum_evaluable_ratio: float | None = None,
        minimum_residency_days: int | None = None,
        exploration_max_symbols: int | None = None,
        apply_to_watchlist: bool | None = None,
        enable_shadow: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        if not catalog:
            raise ValueError("universe catalog must not be empty")
        self.db = db
        self.broker = broker
        self.catalog = tuple(catalog)
        self.config = config or selection_config_from_settings(
            round_trip_fee_bps=_active_round_trip_fee_bps(db),
        )
        required_rotation_bars = max(
            self.config.rotation_lookback_bars + 1,
            self.config.rotation_sma_bars,
        )
        if required_rotation_bars > _MAX_RESEARCH_DAILY_BARS:
            raise ValueError(
                "rotation history exceeds broker daily-bar limit"
            )
        self.minimum_evaluable_ratio = (
            settings.universe_selection_min_evaluable_ratio
            if minimum_evaluable_ratio is None
            else minimum_evaluable_ratio
        )
        self.minimum_residency_days = (
            settings.universe_selection_min_residency_days
            if minimum_residency_days is None
            else minimum_residency_days
        )
        if not 0 < self.minimum_evaluable_ratio <= 1:
            raise ValueError("minimum_evaluable_ratio must be in (0, 1]")
        if self.minimum_residency_days < 1:
            raise ValueError("minimum_residency_days must be positive")
        self.exploration_max_symbols = (
            settings.universe_selection_exploration_max_symbols
            if exploration_max_symbols is None
            else exploration_max_symbols
        )
        if self.exploration_max_symbols < 0:
            raise ValueError(
                "exploration_max_symbols must not be negative"
            )
        self.apply_to_watchlist = (
            settings.universe_selection_apply_to_watchlist
            if apply_to_watchlist is None
            else apply_to_watchlist
        )
        self.enable_shadow = (
            settings.universe_selection_enable_shadow
            if enable_shadow is None
            else enable_shadow
        )
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self.now = observed_at.astimezone(timezone.utc)

    def latest_run(self) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )

    def items_for_run(
        self,
        run_id: int,
    ) -> list[UniverseSelectionCandidate]:
        return (
            self.db.query(UniverseSelectionCandidate)
            .filter(UniverseSelectionCandidate.run_id == run_id)
            .order_by(
                UniverseSelectionCandidate.selected.desc(),
                UniverseSelectionCandidate.rank.asc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.symbol.asc(),
            )
            .all()
        )

    def _rotation_registration_for_month(
        self,
        cohort_month: date,
        *,
        available_as_of_date: date,
        variant_name: str = DIVERSIFIED_ROTATION_VARIANT.name,
        parameter_keys: tuple[str, ...] = (
            "rotation_cohort_registration",
            "rotation_next_cohort_registration",
        ),
    ) -> RotationCohortRegistration | None:
        runs = (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(
                    ("COMPLETE", "DEGRADED")
                ),
                UniverseSelectionRun.completed_at.is_not(None),
                UniverseSelectionRun.as_of_date
                <= available_as_of_date,
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.completed_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .limit(400)
            .all()
        )
        for run in runs:
            try:
                raw_parameters = json.loads(run.parameters_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_parameters, dict):
                continue
            for key in parameter_keys:
                raw_registration = raw_parameters.get(key)
                if not isinstance(raw_registration, dict):
                    continue
                try:
                    registration = (
                        RotationCohortRegistration.from_dict(
                            cast(
                                dict[str, object],
                                raw_registration,
                            )
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    logger.warning(
                        "ignored invalid %s in universe run %s",
                        key,
                        run.id,
                    )
                    continue
                if (
                    registration.cohort_month == cohort_month
                    and registration.registered_as_of_date
                    <= available_as_of_date
                    and registration.rotation_algorithm_version
                    == ROTATION_ALGORITHM_VERSION
                    and registration.variant_name
                    == variant_name
                ):
                    return registration
        return None

    @staticmethod
    def _disable_rotation(
        selections: Sequence[CandidateSelection],
        *,
        reason: str,
    ) -> list[CandidateSelection]:
        result: list[CandidateSelection] = []
        for row in selections:
            evidence = replace(
                row.rotation,
                algorithm_version=ROTATION_ALGORITHM_VERSION,
                momentum_pct=None,
                sma_price=None,
                above_sma=None,
                eligible=False,
                selected=False,
                rank=None,
                score=0.0,
                exclusion_reasons=tuple(
                    dict.fromkeys(
                        (
                            *row.rotation.exclusion_reasons,
                            reason,
                        )
                    )
                ),
            )
            result.append(replace(row, rotation=evidence))
        return result

    @classmethod
    def _merge_fixed_rotation(
        cls,
        selections: Sequence[CandidateSelection],
        fixed_selections: Sequence[CandidateSelection],
    ) -> list[CandidateSelection]:
        fixed_by_symbol = {
            row.candidate.symbol: row.rotation
            for row in fixed_selections
        }
        result: list[CandidateSelection] = []
        for row in selections:
            evidence = fixed_by_symbol.get(row.candidate.symbol)
            if evidence is None:
                result.extend(
                    cls._disable_rotation(
                        (row,),
                        reason="ROTATION_MONTHLY_SIGNAL_UNAVAILABLE",
                    )
                )
            else:
                result.append(replace(row, rotation=evidence))
        return result

    def refresh(
        self,
        *,
        apply_to_watchlist: bool | None = None,
    ) -> UniverseRefreshResult:
        with _REFRESH_LOCK:
            return self._refresh_locked(
                apply_to_watchlist=apply_to_watchlist,
            )

    def _refresh_locked(
        self,
        *,
        apply_to_watchlist: bool | None,
    ) -> UniverseRefreshResult:
        should_apply = (
            self.apply_to_watchlist
            if apply_to_watchlist is None
            else apply_to_watchlist
        )
        parameters = self._parameters()
        algorithm_version = self._algorithm_version(parameters)
        expected_as_of_date = self._consensus_as_of_date({})
        existing = self._run_for_identity(
            as_of_date=expected_as_of_date,
            algorithm_version=algorithm_version,
        )
        if existing is not None and existing.status == "COMPLETE":
            items = self.items_for_run(existing.id)
            return self._result_for_existing(
                existing,
                items,
                should_apply=should_apply,
            )

        claim = self._claim_run(
            as_of_date=expected_as_of_date,
            algorithm_version=algorithm_version,
            parameters=parameters,
        )
        if claim is None:
            resolution = self._wait_for_winner(
                as_of_date=expected_as_of_date,
                algorithm_version=algorithm_version,
                parameters=parameters,
            )
            if isinstance(resolution, _RunClaim):
                claim = resolution
            else:
                winner, items = resolution
                return self._result_for_existing(
                    winner,
                    items,
                    should_apply=should_apply,
                )

        try:
            (
                selections,
                as_of_date,
                rotation_parameters,
            ) = self._evaluate_catalog(
                expected_as_of_date=expected_as_of_date,
            )
            published_parameters = {
                **parameters,
                **rotation_parameters,
            }
            evaluable_count = sum(item.evaluable for item in selections)
            selected_count = sum(item.selected for item in selections)
            candidate_count = len(selections)
            coverage_ratio = (
                evaluable_count / candidate_count
                if candidate_count
                else 0.0
            )
            errors: list[str] = []
            if coverage_ratio < self.minimum_evaluable_ratio:
                errors.append(
                    "evaluable coverage below minimum: "
                    f"{coverage_ratio:.1%} < "
                    f"{self.minimum_evaluable_ratio:.1%}"
                )
            if selected_count == 0:
                errors.append("no eligible candidates selected")
            status = "DEGRADED" if errors else "COMPLETE"

            published = self._publish_claim(
                claim,
                selections=selections,
                status=status,
                candidate_count=candidate_count,
                evaluable_count=evaluable_count,
                selected_count=selected_count,
                coverage_ratio=coverage_ratio,
                parameters=published_parameters,
                error="; ".join(errors),
            )
        except Exception as exc:
            self._release_failed_claim(claim, exc)
            raise
        if published is None:
            resolution = self._wait_for_winner(
                as_of_date=as_of_date,
                algorithm_version=algorithm_version,
                parameters=parameters,
            )
            if isinstance(resolution, _RunClaim):
                # The original owner lost its lease and the intervening owner
                # also disappeared. This caller already has complete T-1
                # evidence, so publish it under the newly acquired token.
                published = self._publish_claim(
                    resolution,
                    selections=selections,
                    status=status,
                    candidate_count=candidate_count,
                    evaluable_count=evaluable_count,
                    selected_count=selected_count,
                    coverage_ratio=coverage_ratio,
                    parameters=published_parameters,
                    error="; ".join(errors),
                )
                if published is None:
                    raise RuntimeError(
                        "universe selection takeover claim was lost"
                    )
            else:
                winner, items = resolution
                return self._result_for_existing(
                    winner,
                    items,
                    should_apply=should_apply,
                )
        run, rows = published

        if status != "COMPLETE":
            return UniverseRefreshResult(
                run=run,
                items=tuple(rows),
                reason=run.error,
            )
        return self._result_for_existing(
            run,
            rows,
            should_apply=should_apply,
        )

    def _run_for_identity(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
    ) -> UniverseSelectionRun | None:
        return (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.as_of_date == as_of_date,
                UniverseSelectionRun.algorithm_version
                == algorithm_version,
                UniverseSelectionRun.source_version
                == CATALOG_SOURCE_VERSION,
            )
            .first()
        )

    def _claim_run(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
        parameters: dict[str, object],
    ) -> _RunClaim | None:
        # A preceding identity read may hold a WAL snapshot. End it before
        # the atomic UPSERT so SQLite never has to upgrade a stale reader into
        # the single writer (which can fail with SQLITE_BUSY_SNAPSHOT).
        self.db.rollback()
        token = f"{_CLAIM_PREFIX}{uuid.uuid4().hex}"
        parameters_json = json.dumps(
            parameters,
            sort_keys=True,
            separators=(",", ":"),
        )
        claim_started_at = datetime.now(timezone.utc)
        stale_before = claim_started_at - timedelta(
            seconds=_RUN_CLAIM_LEASE_SECONDS,
        )
        insert = sqlite_insert(UniverseSelectionRun).values(
            as_of_date=as_of_date,
            algorithm_version=algorithm_version,
            source_version=CATALOG_SOURCE_VERSION,
            status="RUNNING",
            candidate_count=0,
            evaluable_count=0,
            selected_count=0,
            coverage_ratio=0.0,
            parameters_json=parameters_json,
            error=token,
            started_at=claim_started_at,
            completed_at=None,
            created_at=self.now,
        )
        claimed_id = self.db.execute(
            insert.on_conflict_do_update(
                index_elements=[
                    UniverseSelectionRun.as_of_date,
                    UniverseSelectionRun.algorithm_version,
                    UniverseSelectionRun.source_version,
                ],
                set_={
                    "status": "RUNNING",
                    "parameters_json": parameters_json,
                    "error": token,
                    "started_at": claim_started_at,
                    "completed_at": None,
                },
                where=or_(
                    UniverseSelectionRun.status == "DEGRADED",
                    and_(
                        UniverseSelectionRun.status == "RUNNING",
                        UniverseSelectionRun.started_at < stale_before,
                    ),
                ),
            ).returning(UniverseSelectionRun.id)
        ).scalar_one_or_none()
        # Releasing the SQLite writer lock here lets the market-data fetch run
        # without blocking unrelated persistence. The opaque token protects
        # the later publication transaction from a stale owner.
        self.db.commit()
        if claimed_id is None:
            return None
        return _RunClaim(
            run_id=claimed_id,
            token=token,
            started_at=claim_started_at,
        )

    def _wait_for_winner(
        self,
        *,
        as_of_date: date,
        algorithm_version: str,
        parameters: dict[str, object],
    ) -> (
        tuple[
            UniverseSelectionRun,
            list[UniverseSelectionCandidate],
        ]
        | _RunClaim
    ):
        deadline = time.monotonic() + _RUN_WAIT_TIMEOUT_SECONDS
        while True:
            self.db.rollback()
            self.db.expire_all()
            run = self._run_for_identity(
                as_of_date=as_of_date,
                algorithm_version=algorithm_version,
            )
            if run is not None and run.status != "RUNNING":
                return run, self.items_for_run(run.id)
            if run is not None and self._claim_is_stale(run):
                takeover = self._claim_run(
                    as_of_date=as_of_date,
                    algorithm_version=algorithm_version,
                    parameters=parameters,
                )
                if takeover is not None:
                    return takeover
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out waiting for universe selection refresh"
                )
            time.sleep(_RUN_WAIT_POLL_SECONDS)

    @staticmethod
    def _claim_is_stale(run: UniverseSelectionRun) -> bool:
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        age_seconds = (
            datetime.now(timezone.utc) - started_at
        ).total_seconds()
        return age_seconds >= _RUN_CLAIM_LEASE_SECONDS

    def _publish_claim(
        self,
        claim: _RunClaim,
        *,
        selections: Sequence[CandidateSelection],
        status: str,
        candidate_count: int,
        evaluable_count: int,
        selected_count: int,
        coverage_ratio: float,
        parameters: dict[str, object],
        error: str,
    ) -> tuple[
        UniverseSelectionRun,
        list[UniverseSelectionCandidate],
    ] | None:
        completed_at = max(
            datetime.now(timezone.utc),
            claim.started_at,
        )
        claimed_id = self.db.execute(
            update(UniverseSelectionRun)
            .where(
                UniverseSelectionRun.id == claim.run_id,
                UniverseSelectionRun.status == "RUNNING",
                UniverseSelectionRun.error == claim.token,
            )
            .values(
                status=status,
                candidate_count=candidate_count,
                evaluable_count=evaluable_count,
                selected_count=selected_count,
                coverage_ratio=coverage_ratio,
                parameters_json=json.dumps(
                    parameters,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                error=error,
                completed_at=completed_at,
            )
            .execution_options(synchronize_session=False)
            .returning(UniverseSelectionRun.id)
        ).scalar_one_or_none()
        if claimed_id != claim.run_id:
            self.db.rollback()
            return None

        # Run metadata and its complete candidate evidence are published in
        # the same SQLite write transaction, so readers never observe a
        # terminal winner paired with another attempt's rows.
        self.db.query(UniverseSelectionCandidate).filter(
            UniverseSelectionCandidate.run_id == claim.run_id,
        ).delete(synchronize_session="fetch")
        rows = [
            self._candidate_row(claim.run_id, selection)
            for selection in selections
        ]
        self.db.add_all(rows)
        self.db.commit()
        run = self.db.get(UniverseSelectionRun, claim.run_id)
        if run is None:
            raise RuntimeError("published universe selection run disappeared")
        return run, self.items_for_run(claim.run_id)

    def _release_failed_claim(
        self,
        claim: _RunClaim,
        exc: Exception,
    ) -> None:
        self.db.rollback()
        try:
            self.db.execute(
                update(UniverseSelectionRun)
                .where(
                    UniverseSelectionRun.id == claim.run_id,
                    UniverseSelectionRun.status == "RUNNING",
                    UniverseSelectionRun.error == claim.token,
                )
                .values(
                    status="DEGRADED",
                    error=(
                        "refresh failed before publication: "
                        f"{type(exc).__name__}"
                    ),
                    completed_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "failed to release universe selection claim %s",
                claim.run_id,
            )

    def _evaluate_catalog(
        self,
        *,
        expected_as_of_date: date,
    ) -> tuple[
        list[CandidateSelection],
        date,
        dict[str, object],
    ]:
        complete_by_symbol: dict[str, Sequence[DailyBar]] = {}
        spread_by_symbol: dict[str, float] = {}
        latest_by_symbol: dict[str, date] = {}
        errors_by_symbol: dict[str, list[str]] = {}
        daily_bar_count = min(
            _MAX_RESEARCH_DAILY_BARS,
            max(
                _DAILY_BAR_COUNT,
                self.config.rotation_lookback_bars + 1,
                self.config.rotation_sma_bars,
                _ROTATION_EVALUATION_HISTORY_BARS,
            ),
        )
        for candidate in self.catalog:
            data_errors: list[str] = []
            try:
                raw_bars = _research_candlesticks(
                    self.broker,
                    candidate.symbol,
                    "DAY",
                    daily_bar_count,
                )
                bars = completed_daily_bars(
                    raw_bars,
                    market=candidate.market,
                    now=self.now,
                )
                latest = latest_complete_session_date(
                    raw_bars,
                    market=candidate.market,
                    now=self.now,
                )
                if latest is None:
                    data_errors.append("DATA_NO_COMPLETED_DAILY_BAR")
                else:
                    latest_by_symbol[candidate.symbol] = latest
                spread_proxy = liquidity_spread_proxy_bps(bars)
                if spread_proxy is None:
                    data_errors.append("DATA_INVALID_SPREAD_PROXY")
                else:
                    spread_by_symbol[candidate.symbol] = spread_proxy
            except Exception as exc:
                bars = []
                data_errors.append(
                    f"DATA_DAILY_BARS_{type(exc).__name__.upper()}"
                )
                logger.warning(
                    "universe daily bars failed for %s: %s",
                    candidate.symbol,
                    exc,
                    exc_info=True,
                )
            complete_by_symbol[candidate.symbol] = bars
            errors_by_symbol[candidate.symbol] = data_errors

        inputs: list[CandidateInput] = []
        for candidate in self.catalog:
            data_errors = list(
                errors_by_symbol.get(candidate.symbol, ())
            )
            latest = latest_by_symbol.get(candidate.symbol)
            if latest is not None and latest != expected_as_of_date:
                data_errors.append("DATA_STALE_SESSION_DATE")
            inputs.append(
                CandidateInput(
                    candidate=candidate,
                    completed_daily_bars=complete_by_symbol.get(
                        candidate.symbol,
                        [],
                    ),
                    bid=None,
                    ask=None,
                    estimated_spread_bps=spread_by_symbol.get(
                        candidate.symbol,
                    ),
                    data_errors=tuple(data_errors),
                )
            )
        selections = select_candidates(inputs, self.config)
        benchmark_bars: dict[str, Sequence[DailyBar]] = {}
        benchmark_errors: list[str] = []
        for symbol in ROTATION_BENCHMARK_SYMBOLS:
            try:
                raw_bars = _research_candlesticks(
                    self.broker,
                    symbol,
                    "DAY",
                    daily_bar_count,
                )
                bars = completed_daily_bars(
                    raw_bars,
                    market="US",
                    now=self.now,
                )
                latest = latest_complete_session_date(
                    raw_bars,
                    market="US",
                    now=self.now,
                )
                if latest != expected_as_of_date:
                    benchmark_errors.append(
                        f"{symbol}:DATA_STALE_SESSION_DATE"
                    )
                benchmark_bars[symbol] = bars
            except Exception as exc:
                benchmark_errors.append(
                    f"{symbol}:DATA_DAILY_BARS_"
                    f"{type(exc).__name__.upper()}"
                )
                logger.warning(
                    "rotation benchmark daily bars failed for %s: %s",
                    symbol,
                    exc,
                    exc_info=True,
                )
        membership_history_metadata = (
            INDEX_MEMBERSHIP_HISTORY.metadata(self.catalog)
        )
        if benchmark_errors:
            rotation_evaluation: dict[str, object] = {
                "algorithm_version": ROTATION_WALK_FORWARD_VERSION,
                "status": "BENCHMARK_DATA_UNAVAILABLE",
                "benchmark_symbols": list(
                    ROTATION_BENCHMARK_SYMBOLS
                ),
                "data_scope": "CURRENT_CONSTITUENTS_ONLY",
                "survivorship_bias": True,
                "validation_periods": (
                    _ROTATION_EVALUATION_VALIDATION_PERIODS
                ),
                "expanding_validation_min_training_periods": (
                    _ROTATION_EXPANDING_MIN_TRAINING_PERIODS
                ),
                "expanding_validation_fold_periods": (
                    _ROTATION_EXPANDING_FOLD_PERIODS
                ),
                "selected_variant": None,
                "selected_variant_validation_passed": False,
                "validated_challenger_variant": None,
                "automatic_promotion_allowed": False,
                "promotion_blockers": [
                    "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE",
                    "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
                    "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
                ],
                "errors": benchmark_errors,
                "variants": [],
                "selected_variant_periods": [],
                "validated_challenger_periods": [],
            }
            rotation_snapshot = (
                unavailable_rotation_forward_snapshot(
                    "BENCHMARK_DATA_UNAVAILABLE",
                    blocker=(
                        "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE"
                    ),
                )
            )
            rotation_registration = None
            rotation_concentration_challenger_snapshot = (
                unavailable_rotation_forward_snapshot(
                    "BENCHMARK_DATA_UNAVAILABLE",
                    blocker=(
                        "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE"
                    ),
                    variant=CONCENTRATED_ROTATION_VARIANT,
                )
            )
            rotation_concentration_challenger_registration = None
            rotation_weighting_challenger_snapshot = (
                unavailable_rotation_forward_snapshot(
                    "BENCHMARK_DATA_UNAVAILABLE",
                    blocker=(
                        "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE"
                    ),
                    variant=(
                        DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
                    ),
                )
            )
            rotation_weighting_challenger_registration = None
            rotation_shrinkage_challenger_snapshot = (
                unavailable_rotation_forward_snapshot(
                    "BENCHMARK_DATA_UNAVAILABLE",
                    blocker=(
                        "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE"
                    ),
                    variant=(
                        DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
                    ),
                )
            )
            rotation_shrinkage_challenger_registration = None
            rotation_return_to_variance_challenger_snapshot = (
                unavailable_rotation_forward_snapshot(
                    "BENCHMARK_DATA_UNAVAILABLE",
                    blocker=(
                        "ROTATION_BENCHMARK_HISTORY_UNAVAILABLE"
                    ),
                    variant=(
                        RETURN_TO_VARIANCE_ROTATION_VARIANT
                    ),
                )
            )
            rotation_return_to_variance_challenger_registration = (
                None
            )
            rotation_point_in_time_sensitivity: dict[
                str,
                object,
            ] = {
                "status": "BENCHMARK_DATA_UNAVAILABLE",
                "membership_history": (
                    membership_history_metadata
                ),
                "evaluation": None,
                "errors": list(benchmark_errors),
            }
            selections = self._disable_rotation(
                selections,
                reason="ROTATION_MONTHLY_SIGNAL_UNAVAILABLE",
            )
        else:
            try:
                rotation_evaluation = (
                    evaluate_rotation_walk_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        variants=DEFAULT_ROTATION_VARIANTS,
                        validation_periods=(
                            _ROTATION_EVALUATION_VALIDATION_PERIODS
                        ),
                        expanding_validation_min_training_periods=(
                            _ROTATION_EXPANDING_MIN_TRAINING_PERIODS
                        ),
                        expanding_validation_fold_periods=(
                            _ROTATION_EXPANDING_FOLD_PERIODS
                        ),
                    ).to_dict()
                )
            except Exception as exc:
                logger.exception("rotation walk-forward evaluation failed")
                rotation_evaluation = {
                    "algorithm_version": (
                        ROTATION_WALK_FORWARD_VERSION
                    ),
                    "status": "EVALUATION_FAILED",
                    "benchmark_symbols": list(
                        ROTATION_BENCHMARK_SYMBOLS
                    ),
                    "data_scope": "CURRENT_CONSTITUENTS_ONLY",
                    "survivorship_bias": True,
                    "validation_periods": (
                        _ROTATION_EVALUATION_VALIDATION_PERIODS
                    ),
                    "expanding_validation_min_training_periods": (
                        _ROTATION_EXPANDING_MIN_TRAINING_PERIODS
                    ),
                    "expanding_validation_fold_periods": (
                        _ROTATION_EXPANDING_FOLD_PERIODS
                    ),
                    "selected_variant": None,
                    "selected_variant_validation_passed": False,
                    "validated_challenger_variant": None,
                    "automatic_promotion_allowed": False,
                    "promotion_blockers": [
                        "ROTATION_EVALUATION_FAILED",
                        "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
                        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
                    ],
                    "errors": [type(exc).__name__],
                    "variants": [],
                    "selected_variant_periods": [],
                    "validated_challenger_periods": [],
                }
            try:
                point_in_time_evaluation = (
                    evaluate_rotation_walk_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        variants=DEFAULT_ROTATION_VARIANTS,
                        validation_periods=(
                            _ROTATION_EVALUATION_VALIDATION_PERIODS
                        ),
                        expanding_validation_min_training_periods=(
                            _ROTATION_EXPANDING_MIN_TRAINING_PERIODS
                        ),
                        expanding_validation_fold_periods=(
                            _ROTATION_EXPANDING_FOLD_PERIODS
                        ),
                        membership_history=(
                            INDEX_MEMBERSHIP_HISTORY
                        ),
                    ).to_dict()
                )
                rotation_point_in_time_sensitivity = {
                    "status": point_in_time_evaluation["status"],
                    "membership_history": (
                        membership_history_metadata
                    ),
                    "evaluation": point_in_time_evaluation,
                    "errors": [],
                }
            except Exception as exc:
                logger.exception(
                    "rotation point-in-time sensitivity failed"
                )
                rotation_point_in_time_sensitivity = {
                    "status": "EVALUATION_FAILED",
                    "membership_history": (
                        membership_history_metadata
                    ),
                    "evaluation": None,
                    "errors": [type(exc).__name__],
                }
            cohort_month = rotation_cohort_month(
                benchmark_bars,
                as_of_date=expected_as_of_date,
            )
            try:
                frozen_registration = (
                    self._rotation_registration_for_month(
                        cohort_month,
                        available_as_of_date=expected_as_of_date,
                    )
                    if cohort_month is not None
                    else None
                )
                forward_evaluation = evaluate_rotation_forward(
                    candidates=self.catalog,
                    bars_by_symbol=complete_by_symbol,
                    benchmark_bars_by_symbol=benchmark_bars,
                    base_config=self.config,
                    as_of_date=expected_as_of_date,
                    frozen_registration=frozen_registration,
                )
                rotation_snapshot = forward_evaluation.snapshot
                rotation_registration = (
                    forward_evaluation.registration
                )
                if forward_evaluation.selections:
                    selections = self._merge_fixed_rotation(
                        selections,
                        forward_evaluation.selections,
                    )
                else:
                    selections = self._disable_rotation(
                        selections,
                        reason=(
                            "ROTATION_MONTHLY_SIGNAL_UNAVAILABLE"
                        ),
                    )
            except Exception:
                logger.exception("rotation forward evaluation failed")
                rotation_snapshot = (
                    unavailable_rotation_forward_snapshot(
                        "EVALUATION_FAILED",
                        blocker="ROTATION_FORWARD_EVALUATION_FAILED",
                    )
                )
                rotation_registration = None
                selections = self._disable_rotation(
                    selections,
                    reason="ROTATION_MONTHLY_SIGNAL_UNAVAILABLE",
                )
            try:
                frozen_concentration_challenger_registration = (
                    self._rotation_registration_for_month(
                        cohort_month,
                        available_as_of_date=expected_as_of_date,
                        variant_name=(
                            CONCENTRATED_ROTATION_VARIANT.name
                        ),
                        parameter_keys=(
                            "rotation_concentration_challenger_registration",
                            "rotation_next_concentration_challenger_registration",
                        ),
                    )
                    if cohort_month is not None
                    else None
                )
                concentration_challenger_evaluation = (
                    evaluate_rotation_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        as_of_date=expected_as_of_date,
                        frozen_registration=(
                            frozen_concentration_challenger_registration
                        ),
                        variant=CONCENTRATED_ROTATION_VARIANT,
                    )
                )
                rotation_concentration_challenger_snapshot = (
                    concentration_challenger_evaluation.snapshot
                )
                rotation_concentration_challenger_registration = (
                    concentration_challenger_evaluation.registration
                )
            except Exception:
                logger.exception(
                    "rotation concentration challenger evaluation failed"
                )
                rotation_concentration_challenger_snapshot = (
                    unavailable_rotation_forward_snapshot(
                        "EVALUATION_FAILED",
                        blocker=(
                            "ROTATION_CONCENTRATION_CHALLENGER_"
                            "EVALUATION_FAILED"
                        ),
                        variant=CONCENTRATED_ROTATION_VARIANT,
                    )
                )
                rotation_concentration_challenger_registration = None
            try:
                frozen_weighting_challenger_registration = (
                    self._rotation_registration_for_month(
                        cohort_month,
                        available_as_of_date=expected_as_of_date,
                        variant_name=(
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT.name
                        ),
                        parameter_keys=(
                            "rotation_weighting_challenger_registration",
                            "rotation_next_weighting_challenger_registration",
                        ),
                    )
                    if cohort_month is not None
                    else None
                )
                weighting_challenger_evaluation = (
                    evaluate_rotation_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        as_of_date=expected_as_of_date,
                        frozen_registration=(
                            frozen_weighting_challenger_registration
                        ),
                        variant=(
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
                        ),
                    )
                )
                rotation_weighting_challenger_snapshot = (
                    weighting_challenger_evaluation.snapshot
                )
                rotation_weighting_challenger_registration = (
                    weighting_challenger_evaluation.registration
                )
            except Exception:
                logger.exception(
                    "rotation weighting challenger evaluation failed"
                )
                rotation_weighting_challenger_snapshot = (
                    unavailable_rotation_forward_snapshot(
                        "EVALUATION_FAILED",
                        blocker=(
                            "ROTATION_WEIGHTING_CHALLENGER_"
                            "EVALUATION_FAILED"
                        ),
                        variant=(
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
                        ),
                    )
                )
                rotation_weighting_challenger_registration = None
            try:
                frozen_shrinkage_challenger_registration = (
                    self._rotation_registration_for_month(
                        cohort_month,
                        available_as_of_date=expected_as_of_date,
                        variant_name=(
                            DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT.name
                        ),
                        parameter_keys=(
                            "rotation_shrinkage_challenger_registration",
                            "rotation_next_shrinkage_challenger_registration",
                        ),
                    )
                    if cohort_month is not None
                    else None
                )
                shrinkage_challenger_evaluation = (
                    evaluate_rotation_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        as_of_date=expected_as_of_date,
                        frozen_registration=(
                            frozen_shrinkage_challenger_registration
                        ),
                        variant=(
                            DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_shrinkage_challenger_snapshot = (
                    shrinkage_challenger_evaluation.snapshot
                )
                rotation_shrinkage_challenger_registration = (
                    shrinkage_challenger_evaluation.registration
                )
            except Exception:
                logger.exception(
                    "rotation shrinkage challenger evaluation failed"
                )
                rotation_shrinkage_challenger_snapshot = (
                    unavailable_rotation_forward_snapshot(
                        "EVALUATION_FAILED",
                        blocker=(
                            "ROTATION_SHRINKAGE_CHALLENGER_"
                            "EVALUATION_FAILED"
                        ),
                        variant=(
                            DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_shrinkage_challenger_registration = None
            try:
                frozen_return_to_variance_registration = (
                    self._rotation_registration_for_month(
                        cohort_month,
                        available_as_of_date=expected_as_of_date,
                        variant_name=(
                            RETURN_TO_VARIANCE_ROTATION_VARIANT.name
                        ),
                        parameter_keys=(
                            "rotation_return_to_variance_challenger_registration",
                            "rotation_next_return_to_variance_challenger_registration",
                        ),
                    )
                    if cohort_month is not None
                    else None
                )
                return_to_variance_evaluation = (
                    evaluate_rotation_forward(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        benchmark_bars_by_symbol=benchmark_bars,
                        base_config=self.config,
                        as_of_date=expected_as_of_date,
                        frozen_registration=(
                            frozen_return_to_variance_registration
                        ),
                        variant=(
                            RETURN_TO_VARIANCE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_return_to_variance_challenger_snapshot = (
                    return_to_variance_evaluation.snapshot
                )
                rotation_return_to_variance_challenger_registration = (
                    return_to_variance_evaluation.registration
                )
            except Exception:
                logger.exception(
                    "rotation return-to-variance challenger "
                    "evaluation failed"
                )
                rotation_return_to_variance_challenger_snapshot = (
                    unavailable_rotation_forward_snapshot(
                        "EVALUATION_FAILED",
                        blocker=(
                            "ROTATION_RETURN_TO_VARIANCE_CHALLENGER_"
                            "EVALUATION_FAILED"
                        ),
                        variant=(
                            RETURN_TO_VARIANCE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_return_to_variance_challenger_registration = (
                    None
                )
        rotation_evaluation["as_of_date"] = (
            expected_as_of_date.isoformat()
        )
        rotation_evaluation["history_bars_requested"] = daily_bar_count
        raw_point_in_time_evaluation = (
            rotation_point_in_time_sensitivity.get("evaluation")
        )
        if isinstance(raw_point_in_time_evaluation, dict):
            raw_point_in_time_evaluation["as_of_date"] = (
                expected_as_of_date.isoformat()
            )
            raw_point_in_time_evaluation[
                "history_bars_requested"
            ] = daily_bar_count
        rotation_parameters: dict[str, object] = {
            "rotation_evaluation": rotation_evaluation,
            "rotation_point_in_time_sensitivity": (
                rotation_point_in_time_sensitivity
            ),
            "rotation_forward_snapshot": (
                rotation_snapshot.to_dict()
            ),
            "rotation_cohort_registration": (
                rotation_registration.to_dict()
                if rotation_registration is not None
                else None
            ),
            "rotation_next_cohort_registration_status": "NOT_DUE",
            "rotation_next_cohort_registration": None,
            "rotation_concentration_challenger_snapshot": (
                rotation_concentration_challenger_snapshot.to_dict()
            ),
            "rotation_concentration_challenger_registration": (
                rotation_concentration_challenger_registration.to_dict()
                if rotation_concentration_challenger_registration
                is not None
                else None
            ),
            "rotation_next_concentration_challenger_registration_status": (
                "NOT_DUE"
            ),
            "rotation_next_concentration_challenger_registration": None,
            "rotation_weighting_challenger_snapshot": (
                rotation_weighting_challenger_snapshot.to_dict()
            ),
            "rotation_weighting_challenger_registration": (
                rotation_weighting_challenger_registration.to_dict()
                if rotation_weighting_challenger_registration
                is not None
                else None
            ),
            "rotation_next_weighting_challenger_registration_status": (
                "NOT_DUE"
            ),
            "rotation_next_weighting_challenger_registration": None,
            "rotation_shrinkage_challenger_snapshot": (
                rotation_shrinkage_challenger_snapshot.to_dict()
            ),
            "rotation_shrinkage_challenger_registration": (
                rotation_shrinkage_challenger_registration.to_dict()
                if rotation_shrinkage_challenger_registration
                is not None
                else None
            ),
            "rotation_next_shrinkage_challenger_registration_status": (
                "NOT_DUE"
            ),
            "rotation_next_shrinkage_challenger_registration": None,
            "rotation_return_to_variance_challenger_snapshot": (
                rotation_return_to_variance_challenger_snapshot.to_dict()
            ),
            "rotation_return_to_variance_challenger_registration": (
                rotation_return_to_variance_challenger_registration.to_dict()
                if rotation_return_to_variance_challenger_registration
                is not None
                else None
            ),
            "rotation_next_return_to_variance_challenger_registration_status": (
                "NOT_DUE"
            ),
            "rotation_next_return_to_variance_challenger_registration": (
                None
            ),
        }
        if (
            not benchmark_errors
            and is_last_us_session_of_month(
                expected_as_of_date
            )
        ):
            evaluable_count = sum(
                row.evaluable for row in selections
            )
            coverage_ratio = (
                evaluable_count / len(selections)
                if selections
                else 0.0
            )
            if coverage_ratio < self.minimum_evaluable_ratio:
                rotation_parameters[
                    "rotation_next_cohort_registration_status"
                ] = "BLOCKED_INSUFFICIENT_COVERAGE"
                rotation_parameters[
                    "rotation_next_concentration_challenger_registration_status"
                ] = "BLOCKED_INSUFFICIENT_COVERAGE"
                rotation_parameters[
                    "rotation_next_weighting_challenger_registration_status"
                ] = "BLOCKED_INSUFFICIENT_COVERAGE"
                rotation_parameters[
                    "rotation_next_shrinkage_challenger_registration_status"
                ] = "BLOCKED_INSUFFICIENT_COVERAGE"
                rotation_parameters[
                    "rotation_next_return_to_variance_challenger_registration_status"
                ] = "BLOCKED_INSUFFICIENT_COVERAGE"
            else:
                next_registration = (
                    build_rotation_cohort_registration(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        base_config=self.config,
                        cohort_month=next_cohort_month(
                            expected_as_of_date
                        ),
                        signal_date=expected_as_of_date,
                        registered_as_of_date=expected_as_of_date,
                    )
                )
                rotation_parameters[
                    "rotation_next_cohort_registration_status"
                ] = "REGISTERED"
                rotation_parameters[
                    "rotation_next_cohort_registration"
                ] = next_registration.to_dict()
                next_concentration_challenger_registration = (
                    build_rotation_cohort_registration(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        base_config=self.config,
                        cohort_month=next_cohort_month(
                            expected_as_of_date
                        ),
                        signal_date=expected_as_of_date,
                        registered_as_of_date=expected_as_of_date,
                        variant=CONCENTRATED_ROTATION_VARIANT,
                    )
                )
                rotation_parameters[
                    "rotation_next_concentration_challenger_registration_status"
                ] = "REGISTERED"
                rotation_parameters[
                    "rotation_next_concentration_challenger_registration"
                ] = (
                    next_concentration_challenger_registration.to_dict()
                )
                next_weighting_challenger_registration = (
                    build_rotation_cohort_registration(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        base_config=self.config,
                        cohort_month=next_cohort_month(
                            expected_as_of_date
                        ),
                        signal_date=expected_as_of_date,
                        registered_as_of_date=expected_as_of_date,
                        variant=(
                            DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
                        ),
                    )
                )
                rotation_parameters[
                    "rotation_next_weighting_challenger_registration_status"
                ] = "REGISTERED"
                rotation_parameters[
                    "rotation_next_weighting_challenger_registration"
                ] = (
                    next_weighting_challenger_registration.to_dict()
                )
                next_shrinkage_challenger_registration = (
                    build_rotation_cohort_registration(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        base_config=self.config,
                        cohort_month=next_cohort_month(
                            expected_as_of_date
                        ),
                        signal_date=expected_as_of_date,
                        registered_as_of_date=expected_as_of_date,
                        variant=(
                            DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_parameters[
                    "rotation_next_shrinkage_challenger_registration_status"
                ] = "REGISTERED"
                rotation_parameters[
                    "rotation_next_shrinkage_challenger_registration"
                ] = (
                    next_shrinkage_challenger_registration.to_dict()
                )
                next_return_to_variance_registration = (
                    build_rotation_cohort_registration(
                        candidates=self.catalog,
                        bars_by_symbol=complete_by_symbol,
                        base_config=self.config,
                        cohort_month=next_cohort_month(
                            expected_as_of_date
                        ),
                        signal_date=expected_as_of_date,
                        registered_as_of_date=expected_as_of_date,
                        variant=(
                            RETURN_TO_VARIANCE_ROTATION_VARIANT
                        ),
                    )
                )
                rotation_parameters[
                    "rotation_next_return_to_variance_challenger_registration_status"
                ] = "REGISTERED"
                rotation_parameters[
                    "rotation_next_return_to_variance_challenger_registration"
                ] = next_return_to_variance_registration.to_dict()
        return selections, expected_as_of_date, rotation_parameters

    def _consensus_as_of_date(
        self,
        latest_by_symbol: dict[str, date],
    ) -> date:
        if latest_by_symbol:
            counts = Counter(latest_by_symbol.values())
            return max(counts, key=lambda value: (counts[value], value))
        return latest_closed_session_date(
            market="US",
            now=self.now,
        )

    def _candidate_row(
        self,
        run_id: int,
        selection: CandidateSelection,
        *,
        row: UniverseSelectionCandidate | None = None,
    ) -> UniverseSelectionCandidate:
        candidate_row = row or UniverseSelectionCandidate()
        candidate_row.run_id = run_id
        candidate_row.symbol = selection.candidate.symbol
        candidate_row.market = selection.candidate.market
        candidate_row.alias = selection.candidate.alias
        candidate_row.sector = selection.candidate.sector
        candidate_row.memberships_json = json.dumps(
            selection.candidate.memberships,
            separators=(",", ":"),
        )
        candidate_row.selected = selection.selected
        candidate_row.rank = selection.rank
        candidate_row.score = round(selection.score, 6)
        metrics = asdict(selection.metrics)
        metrics["rotation"] = asdict(selection.rotation)
        candidate_row.metrics_json = json.dumps(
            metrics,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_row.exclusion_reasons_json = json.dumps(
            selection.exclusion_reasons,
            separators=(",", ":"),
        )
        candidate_row.created_at = self.now
        return candidate_row

    def _result_for_existing(
        self,
        run: UniverseSelectionRun,
        items: Sequence[UniverseSelectionCandidate],
        *,
        should_apply: bool,
    ) -> UniverseRefreshResult:
        if run.status != "COMPLETE":
            return UniverseRefreshResult(
                run=run,
                items=tuple(items),
                reason=run.error or "selection run is not complete",
            )
        observation_overrides = observation_pool_overrides(self.db)
        exploration = select_exploration_candidates(
            items,
            max_symbols=self.exploration_max_symbols,
            max_per_sector=self.config.max_per_sector,
            already_observed_symbols=(
                observation_overrides.already_observed_symbols
            ),
            unobservable_symbols=(
                observation_overrides.unobservable_symbols
            ),
            minimum_peer_dollar_volume=(
                minimum_peer_observation_dollar_volume(
                    self.config.min_avg_dollar_volume
                )
            ),
        )
        exploration_symbols = tuple(
            item.symbol for item in exploration
        )
        if not should_apply:
            return UniverseRefreshResult(
                run=run,
                items=tuple(items),
                exploration_symbols=exploration_symbols,
                reason="watchlist application disabled",
            )
        selected = [item for item in items if item.selected]
        observed = [*selected, *exploration]
        added, removed, retained = self._reconcile_watchlist(observed)
        shadow_enabled, shadow_disabled, shadow_failures = (
            self._sync_observation_shadows(
                observed_symbols=(
                    {item.symbol for item in observed}
                    | set(
                        observation_overrides.already_observed_symbols
                    )
                ),
            )
        )
        reason = "candidate and exploration watchlist reconciled"
        if shadow_failures:
            reason += "; shadow sync failed for " + ", ".join(
                shadow_failures
            )
        return UniverseRefreshResult(
            run=run,
            items=tuple(items),
            exploration_symbols=exploration_symbols,
            added_symbols=tuple(added),
            removed_symbols=tuple(removed),
            retained_symbols=tuple(retained),
            shadow_enabled_symbols=tuple(shadow_enabled),
            shadow_disabled_symbols=tuple(shadow_disabled),
            shadow_failed_symbols=tuple(shadow_failures),
            applied=True,
            reason=reason,
        )

    def _reconcile_watchlist(
        self,
        selected: Sequence[UniverseSelectionCandidate],
    ) -> tuple[list[str], list[str], list[str]]:
        existing_rows = self.db.query(WatchlistItem).all()
        existing = {row.symbol: row for row in existing_rows}
        primary = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        primary_symbol = primary.symbol if primary is not None else ""
        selected_symbols = {item.symbol for item in selected}
        added: list[str] = []
        retained: list[str] = []
        for candidate in selected:
            row = existing.get(candidate.symbol)
            if row is None:
                inserted_id = self.db.execute(
                    sqlite_insert(WatchlistItem)
                    .values(
                        symbol=candidate.symbol,
                        market=candidate.market,
                        alias=candidate.alias,
                        source=_WATCHLIST_SOURCE,
                        is_active=candidate.symbol == primary_symbol,
                        created_at=self.now,
                    )
                    .on_conflict_do_nothing(index_elements=["symbol"])
                    .returning(WatchlistItem.id)
                ).scalar_one_or_none()
                row = (
                    self.db.query(WatchlistItem)
                    .filter(WatchlistItem.symbol == candidate.symbol)
                    .one()
                )
                existing[candidate.symbol] = row
                if inserted_id is not None:
                    added.append(candidate.symbol)
                else:
                    retained.append(candidate.symbol)
            else:
                retained.append(candidate.symbol)
            row.is_active = candidate.symbol == primary_symbol
            if row.source == _WATCHLIST_SOURCE:
                row.market = candidate.market
                row.alias = candidate.alias

        residency_cutoff = self.now - timedelta(
            days=self.minimum_residency_days
        )
        removed: list[str] = []
        for row in existing_rows:
            row.is_active = row.symbol == primary_symbol
            if row.source != _WATCHLIST_SOURCE:
                continue
            if row.symbol in selected_symbols:
                continue
            created_at = row.created_at
            if created_at is None:
                created_at = self.now
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (
                row.is_active
                or row.symbol == primary_symbol
                or created_at > residency_cutoff
                or self._has_live_exposure(row.symbol)
            ):
                retained.append(row.symbol)
                continue
            removed.append(row.symbol)
            self.db.query(WatchlistScore).filter(
                WatchlistScore.symbol == row.symbol,
            ).delete(synchronize_session=False)
            self.db.delete(row)
        self.db.commit()
        return (
            sorted(added),
            sorted(removed),
            sorted(set(retained)),
        )

    def _has_live_exposure(self, symbol: str) -> bool:
        tracked = (
            self.db.query(TrackedEntry)
            .filter(
                TrackedEntry.symbol == symbol,
                TrackedEntry.quantity > 0,
            )
            .first()
        )
        if tracked is not None:
            return True
        live_order = (
            self.db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.status.in_(_LIVE_ORDER_STATUSES),
            )
            .first()
        )
        return live_order is not None

    def _sync_observation_shadows(
        self,
        *,
        observed_symbols: set[str],
    ) -> tuple[list[str], list[str], list[str]]:
        if not self.enable_shadow:
            return [], [], []
        enabled: list[str] = []
        disabled: list[str] = []
        failures: list[str] = []
        service = StrategyV2ShadowService(self.db)
        for symbol in sorted(observed_symbols):
            try:
                row = (
                    self.db.query(StrategyV2ShadowConfig)
                    .filter(StrategyV2ShadowConfig.symbol == symbol)
                    .first()
                )
                created_for_universe = row is None
                if row is None:
                    service.get_config(symbol)
                    row = (
                        self.db.query(StrategyV2ShadowConfig)
                        .filter(StrategyV2ShadowConfig.symbol == symbol)
                        .one()
                    )
                was_enabled = row.enabled
                if row.enabled and not row.universe_managed:
                    continue
                if (
                    not row.enabled
                    and not created_for_universe
                    and not row.universe_managed
                ):
                    # Existing disabled unmanaged configs are explicit
                    # operator opt-outs. Never silently re-enable them.
                    continue
                row.universe_managed = True
                self.db.add(row)
                service.ensure_universe_managed_enabled(symbol)
                if not was_enabled:
                    enabled.append(symbol)
            except Exception:
                self.db.rollback()
                logger.exception(
                    "failed to enable Strategy v2 shadow for %s",
                    symbol,
                )
                failures.append(f"enable:{symbol}")
        managed_rows = (
            self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.universe_managed.is_(True))
            .all()
        )
        for managed in managed_rows:
            symbol = managed.symbol
            if symbol in observed_symbols:
                continue
            try:
                if managed.enabled:
                    service.update_config(
                        StrategyV2ShadowConfigUpdate(enabled=False),
                        symbol=symbol,
                        preserve_universe_management=True,
                    )
                    disabled.append(symbol)
            except Exception:
                self.db.rollback()
                logger.exception(
                    "failed to disable retired Strategy v2 shadow for %s",
                    symbol,
                )
                failures.append(f"disable:{symbol}")
        return enabled, disabled, failures

    def _parameters(self) -> dict[str, object]:
        membership_history_metadata = (
            INDEX_MEMBERSHIP_HISTORY.metadata(self.catalog)
        )
        return {
            **asdict(self.config),
            "catalog_size": len(self.catalog),
            "rotation_algorithm_version": ROTATION_ALGORITHM_VERSION,
            "rotation_walk_forward_algorithm_version": (
                ROTATION_WALK_FORWARD_VERSION
            ),
            "rotation_forward_algorithm_version": (
                ROTATION_FORWARD_VERSION
            ),
            "rotation_forward_variant": asdict(
                DIVERSIFIED_ROTATION_VARIANT
            ),
            "rotation_concentration_challenger_variant": asdict(
                CONCENTRATED_ROTATION_VARIANT
            ),
            "rotation_weighting_challenger_variant": asdict(
                DIVERSIFIED_INVERSE_VOLATILITY_VARIANT
            ),
            "rotation_shrinkage_challenger_variant": asdict(
                DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT
            ),
            "rotation_return_to_variance_challenger_variant": asdict(
                RETURN_TO_VARIANCE_ROTATION_VARIANT
            ),
            "rotation_forward_registration_policy": (
                "previous-month-final-session-signal/"
                "next-month-first-session-open/equal-weight/"
                "estimated-round-trip-cost"
            ),
            "rotation_concentration_challenger_registration_policy": (
                "same-12-1-signal/top6/two-per-risk-group/"
                "equal-weight/estimated-round-trip-cost"
            ),
            "rotation_weighting_challenger_registration_policy": (
                "same-frozen-top8/inverse-20d-volatility/"
                "25pct-position-cap/cash-residual/"
                "estimated-round-trip-cost"
            ),
            "rotation_shrinkage_challenger_registration_policy": (
                "same-frozen-top8/75pct-equal/"
                "25pct-inverse-20d-volatility/"
                "15pct-inverse-leg-position-cap/"
                "estimated-round-trip-cost"
            ),
            "rotation_return_to_variance_challenger_registration_policy": (
                "same-12-1-formation-window/"
                "rank-return-divided-by-formation-variance/"
                "top8-one-per-risk-group/equal-weight/"
                "estimated-round-trip-cost"
            ),
            "rotation_walk_forward_history_bars": (
                _ROTATION_EVALUATION_HISTORY_BARS
            ),
            "rotation_walk_forward_validation_periods": (
                _ROTATION_EVALUATION_VALIDATION_PERIODS
            ),
            "rotation_expanding_min_training_periods": (
                _ROTATION_EXPANDING_MIN_TRAINING_PERIODS
            ),
            "rotation_expanding_fold_periods": (
                _ROTATION_EXPANDING_FOLD_PERIODS
            ),
            "rotation_walk_forward_benchmarks": list(
                ROTATION_BENCHMARK_SYMBOLS
            ),
            "rotation_walk_forward_variants": [
                asdict(variant)
                for variant in DEFAULT_ROTATION_VARIANTS
            ],
            "rotation_point_in_time_membership_history": (
                membership_history_metadata
            ),
            "exploration_algorithm_version": (
                _EXPLORATION_ALGORITHM_VERSION
            ),
            "exploration_max_symbols": self.exploration_max_symbols,
            "exploration_min_risk_group_peers": (
                RISK_GROUP_RELATIVE_MIN_PEERS
            ),
            "exploration_refined_sector_peer_target": (
                RISK_GROUP_RELATIVE_MIN_PEERS
            ),
            "exploration_min_peer_dollar_volume": (
                minimum_peer_observation_dollar_volume(
                    self.config.min_avg_dollar_volume
                )
            ),
            "minimum_evaluable_ratio": self.minimum_evaluable_ratio,
            "minimum_residency_days": self.minimum_residency_days,
        }

    @staticmethod
    def _algorithm_version(parameters: dict[str, object]) -> str:
        encoded = json.dumps(
            parameters,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        digest = hashlib.sha256(encoded).hexdigest()[:12]
        return f"{UNIVERSE_ALGORITHM_VERSION}-{digest}"
