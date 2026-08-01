from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import (
    get_session,
    market_for_symbol,
    session_status,
)
from app.domain.universe_selection import latest_closed_session_date
from app.domain.universe_selection.rotation_forward import (
    is_last_us_session_of_month,
)
from app.models import (
    OpeningMomentumExecution,
    OpeningMomentumShadowRun,
    LiveExitChallengerRegistration,
    LiveExitChallengerTrade,
    OrderRecord,
    RuntimeState,
    StrategyConfig,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardRegistration,
    StrategyV2ExitChallengerRegistration,
    StrategyV2ExitChallengerTrade,
    StrategyV2PortfolioObservation,
    StrategyV2PortfolioRegistration,
    StrategyV2ShadowDecision,
    StrategyV2ShadowConfig,
    StrategyV2ShadowTrade,
    UniverseSelectionRun,
)
from app.schemas import (
    UniverseObservationHealthComponent,
    UniverseObservationHealthResponse,
)
from app.services.watchlist_quant_service import (
    QUANT_ERROR_SOURCE,
    QUANT_SCORE_SOURCE,
    QUANT_WARMUP_SOURCE,
    build_quant_observation_plan,
    list_latest_current_quant_scores,
)
from app.services import live_exit_challenger_service as live_exit_module
from app.services import (
    strategy_v2_exit_challenger_service as strategy_exit_module,
)
from app.services.live_exit_challenger_service import (
    LIVE_EXIT_ALGORITHM_VERSIONS,
    LiveExitChallengerService,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)
from app.services.opening_momentum_execution_service import (
    OpeningMomentumExecutionService,
)
from app.services.strategy_v2_exit_challenger_service import (
    STRATEGY_V2_EXIT_ALGORITHM_VERSIONS,
    StrategyV2ExitChallengerService,
)
from app.services.strategy_v2_portfolio_service import (
    StrategyV2PortfolioService,
    _CURRENT_ROUTING_ALGORITHM_VERSIONS,
    _routing_spec_for_registration,
)
from app.services.strategy_v2_shadow_service import (
    StrategyV2ShadowService,
)
from app.services.universe_promotion_service import UniversePromotionService


_TERMINAL_UNIVERSE_STATUSES = ("COMPLETE", "DEGRADED")
_PORTFOLIO_WARNING_AGE = timedelta(minutes=45)
_LIVE_PRICE_HEALTHY_AGE = timedelta(minutes=5)
_LIVE_PRICE_WARNING_AGE = timedelta(minutes=15)
_OPENING_SETTLEMENT_GRACE = timedelta(seconds=5)
_OPENING_EXECUTION_STATE_GRACE = timedelta(seconds=60)
_ROTATION_NEXT_REGISTRATION_STATUS_KEYS = (
    "rotation_next_cohort_registration_status",
    "rotation_next_concentration_challenger_registration_status",
    "rotation_next_weighting_challenger_registration_status",
    "rotation_next_shrinkage_challenger_registration_status",
    "rotation_next_return_to_variance_challenger_registration_status",
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    observed = _as_utc(value)
    if observed is None:
        return None
    age = (now - observed).total_seconds()
    return age if age >= 0 else None


def _timestamp_in_future(now: datetime, value: datetime | None) -> bool:
    observed = _as_utc(value)
    return observed is not None and observed > now


def _timestamp_is_after(
    later: datetime | None,
    earlier: datetime | None,
    *,
    inclusive: bool = False,
) -> bool:
    normalized_later = _as_utc(later)
    normalized_earlier = _as_utc(earlier)
    if normalized_later is None or normalized_earlier is None:
        return False
    if inclusive:
        return normalized_later >= normalized_earlier
    return normalized_later > normalized_earlier


def _utc_datetimes(
    values: Iterable[datetime | None],
) -> list[datetime]:
    normalized: list[datetime] = []
    for value in values:
        observed = _as_utc(value)
        if observed is not None:
            normalized.append(observed)
    return normalized


def _bounded_ratio(observed: int, expected: int) -> float | None:
    if expected <= 0:
        return None
    return min(1.0, max(0.0, observed / expected))


def _positive_finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value)) and value > 0


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _session_open_at(market: str, session_date: date) -> datetime:
    session = get_session(market)
    return datetime.combine(
        session_date,
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)


def _session_close_at(market: str, session_date: date) -> datetime:
    session = get_session(market)
    return datetime.combine(
        session_date,
        session.close_time(session_date),
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)


def _registration_eligible_after(registered_at: datetime) -> datetime:
    registered = _as_utc(registered_at)
    assert registered is not None
    return registered.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _freshness_cutoff(
    market: str,
    *,
    now: datetime,
    ttl: timedelta,
) -> datetime:
    """Return a phase-aware cutoff without relaxing during intraday breaks."""
    session = get_session(market)
    phase = session_status(market, now)
    local_day = session.local(now).date()
    if phase == "rth":
        return now - ttl
    if phase == "lunch":
        assert session.lunch_start is not None
        reference = datetime.combine(
            local_day,
            session.lunch_start,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        return max(_session_open_at(market, local_day), reference - ttl)
    if phase == "post":
        reference = _session_close_at(market, local_day)
        return max(_session_open_at(market, local_day), reference - ttl)
    latest_closed = latest_closed_session_date(market=market, now=now)
    return max(
        _session_open_at(market, latest_closed),
        _session_close_at(market, latest_closed) - ttl,
    )


def _completed_session_gap(
    market: str,
    *,
    latest: date,
    expected: date,
) -> int:
    """Count completed market sessions missing after ``latest``."""
    if latest >= expected:
        return 0
    missing = 0
    candidate = latest + timedelta(days=1)
    for _ in range(370):
        if candidate > expected:
            return missing
        if candidate.weekday() < 5 and not is_market_closed(
            market,
            candidate,
        ):
            missing += 1
        candidate += timedelta(days=1)
    raise RuntimeError("unable to count missing market sessions")


def _latest_month_end_due(expected: date) -> date:
    candidate = expected
    for _ in range(40):
        if is_last_us_session_of_month(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("unable to resolve latest US month-end session")


def _portfolio_registration_is_current(
    row: StrategyV2PortfolioRegistration,
) -> bool:
    try:
        spec = _routing_spec_for_registration(row)
    except ValueError:
        return False
    return row.evaluator_digest == StrategyV2PortfolioService._evaluator_digest(
        spec
    )


def _complete_universe_run_is_coherent(
    row: UniverseSelectionRun,
) -> bool:
    try:
        candidate_count = int(row.candidate_count)
        evaluable_count = int(row.evaluable_count)
        selected_count = int(row.selected_count)
        coverage_ratio = float(row.coverage_ratio)
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        candidate_count != row.candidate_count
        or evaluable_count != row.evaluable_count
        or selected_count != row.selected_count
        or candidate_count <= 0
        or evaluable_count <= 0
        or selected_count <= 0
        or selected_count > evaluable_count
        or evaluable_count > candidate_count
        or not math.isfinite(coverage_ratio)
    ):
        return False
    expected_coverage = evaluable_count / candidate_count
    return (
        math.isclose(
            coverage_ratio,
            expected_coverage,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and coverage_ratio >= settings.universe_selection_min_evaluable_ratio
    )


def _live_exit_registration_is_current(
    row: LiveExitChallengerRegistration,
    *,
    market: str,
    slippage_bps: float,
) -> bool:
    for spec in live_exit_module._PROFIT_LOCK_SPECS:
        if row.algorithm_version != spec.algorithm_version:
            continue
        digest = LiveExitChallengerService._evaluator_digest(
            spec,
            slippage_bps=slippage_bps,
        )
        try:
            LiveExitChallengerService._validate_frozen_registration(
                row,
                market=market,
                spec=spec,
                slippage_bps=slippage_bps,
                digest=digest,
            )
        except ValueError:
            return False
        return True
    for spec in live_exit_module._TIME_EXIT_SPECS:
        if row.algorithm_version != spec.algorithm_version:
            continue
        digest = LiveExitChallengerService._time_exit_evaluator_digest(
            spec,
            slippage_bps=slippage_bps,
        )
        try:
            LiveExitChallengerService._validate_frozen_time_exit_registration(
                row,
                market=market,
                spec=spec,
                slippage_bps=slippage_bps,
                digest=digest,
            )
        except ValueError:
            return False
        return True
    return False


def _strategy_exit_registration_is_current(
    row: StrategyV2ExitChallengerRegistration,
    *,
    market: str,
    slippage_bps: float,
) -> bool:
    for spec in strategy_exit_module._PROFIT_LOCK_SPECS:
        if row.algorithm_version != spec.algorithm_version:
            continue
        digest = StrategyV2ExitChallengerService._evaluator_digest(
            spec,
            slippage_bps=slippage_bps,
        )
        try:
            StrategyV2ExitChallengerService._validate_frozen_registration(
                row,
                market=market,
                spec=spec,
                slippage_bps=slippage_bps,
                digest=digest,
            )
        except ValueError:
            return False
        return True
    for spec in strategy_exit_module._TIME_EXIT_SPECS:
        if row.algorithm_version != spec.algorithm_version:
            continue
        digest = StrategyV2ExitChallengerService._time_exit_evaluator_digest(
            spec,
            slippage_bps=slippage_bps,
        )
        try:
            StrategyV2ExitChallengerService._validate_frozen_time_exit_registration(
                row,
                market=market,
                spec=spec,
                slippage_bps=slippage_bps,
                digest=digest,
            )
        except ValueError:
            return False
        return True
    return False


class ResearchObservationHealthService:
    """Report whether read-only research observers are producing evidence."""

    def __init__(
        self,
        db: Session,
        *,
        now: datetime | None = None,
    ) -> None:
        self.db: Session = db
        normalized_now = _as_utc(now or datetime.now(timezone.utc))
        assert normalized_now is not None
        self.now: datetime = normalized_now

    def get_health(self) -> UniverseObservationHealthResponse:
        components = [
            self._universe_component(),
            self._rotation_precommitment_component(),
            self._quant_component(),
            self._diversified_priority_component(),
            self._growth_satellite_component(),
            self._live_interval_component(),
            self._live_exit_challenger_component(),
            self._strategy_v2_exit_challenger_component(),
            self._strategy_v2_forward_component(),
            self._portfolio_component(),
            self._opening_shadow_component(),
            self._opening_execution_component(),
        ]
        active = [
            component
            for component in components
            if component.status != "DISABLED"
        ]
        if any(component.status == "DEGRADED" for component in active):
            status = "DEGRADED"
        elif any(component.status == "WARNING" for component in active):
            status = "WARNING"
        else:
            status = "HEALTHY"
        blockers = [
            f"{component.name}:{blocker}"
            for component in active
            for blocker in component.blockers
        ]
        return UniverseObservationHealthResponse(
            generated_at=self.now,
            status=status,
            components=components,
            blockers=blockers,
        )

    def _universe_component(self) -> UniverseObservationHealthComponent:
        expected = latest_closed_session_date(market="US", now=self.now)
        latest = (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(
                    _TERMINAL_UNIVERSE_STATUSES
                )
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.completed_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )
        if not settings.universe_selection_enabled:
            return UniverseObservationHealthComponent(
                name="UNIVERSE_SELECTION",
                status="DISABLED",
                expected_session_date=expected,
            )
        blockers: list[str] = []
        if latest is None:
            blockers.append("NO_TERMINAL_RUN")
            status = "DEGRADED"
        elif latest.completed_at is None:
            blockers.append("TERMINAL_RUN_COMPLETION_MISSING")
            status = "DEGRADED"
        elif _timestamp_in_future(self.now, latest.completed_at):
            blockers.append("TERMINAL_RUN_COMPLETED_AT_IN_FUTURE")
            status = "DEGRADED"
        elif latest.as_of_date > expected:
            blockers.append("TERMINAL_RUN_SESSION_IN_FUTURE")
            status = "DEGRADED"
        elif latest.status != "COMPLETE":
            blockers.append(f"LATEST_RUN_{latest.status}")
            status = "DEGRADED"
        elif not _complete_universe_run_is_coherent(latest):
            blockers.append("COMPLETE_RUN_COUNTS_OR_COVERAGE_INVALID")
            status = "DEGRADED"
        elif latest.as_of_date < expected:
            session_gap = _completed_session_gap(
                "US",
                latest=latest.as_of_date,
                expected=expected,
            )
            if session_gap > 1:
                blockers.append("MULTIPLE_COMPLETED_SESSIONS_MISSING")
                status = "DEGRADED"
            else:
                blockers.append("LATEST_COMPLETED_SESSION_MISSING")
                status = "WARNING"
        else:
            status = "HEALTHY"
        latest_at = latest.completed_at if latest is not None else None
        coherent = bool(
            latest is not None
            and latest.status == "COMPLETE"
            and _complete_universe_run_is_coherent(latest)
        )
        return UniverseObservationHealthComponent(
            name="UNIVERSE_SELECTION",
            status=status,
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            latest_session_date=(
                latest.as_of_date if latest is not None else None
            ),
            expected_session_date=expected,
            observed_count=(latest.evaluable_count if latest else 0),
            expected_count=(latest.candidate_count if latest else 0),
            coverage_ratio=(
                min(1.0, max(0.0, latest.coverage_ratio))
                if coherent and latest is not None
                else None
            ),
            blockers=blockers,
        )

    def _rotation_precommitment_component(
        self,
    ) -> UniverseObservationHealthComponent:
        expected = latest_closed_session_date(market="US", now=self.now)
        month_end_due = _latest_month_end_due(expected)
        if not settings.universe_selection_enabled:
            return UniverseObservationHealthComponent(
                name="ROTATION_FORWARD_PRECOMMITMENT",
                status="DISABLED",
                expected_session_date=month_end_due,
            )
        earliest = self.db.query(func.min(UniverseSelectionRun.as_of_date)).filter(
            UniverseSelectionRun.status.in_(_TERMINAL_UNIVERSE_STATUSES)
        ).scalar()
        if earliest is None or earliest > month_end_due:
            return UniverseObservationHealthComponent(
                name="ROTATION_FORWARD_PRECOMMITMENT",
                status="HEALTHY",
            )
        latest = (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status.in_(
                    _TERMINAL_UNIVERSE_STATUSES
                ),
                UniverseSelectionRun.as_of_date == month_end_due,
            )
            .order_by(
                UniverseSelectionRun.completed_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )
        blockers: list[str] = []
        parameters: dict[str, object] = {}
        parameters_available = False
        if latest is None:
            blockers.append("MONTH_END_UNIVERSE_RUN_MISSING")
        elif latest.completed_at is None:
            blockers.append("MONTH_END_RUN_COMPLETION_MISSING")
        elif _timestamp_in_future(self.now, latest.completed_at):
            blockers.append("MONTH_END_RUN_COMPLETED_AT_IN_FUTURE")
        elif latest.status != "COMPLETE":
            blockers.append(f"MONTH_END_RUN_{latest.status}")
        else:
            try:
                raw_parameters = json.loads(latest.parameters_json)
            except (TypeError, ValueError):
                raw_parameters = None
            if isinstance(raw_parameters, dict):
                parameters = raw_parameters
                parameters_available = True
            else:
                blockers.append("ROTATION_PARAMETERS_INVALID")
        if parameters_available:
            for key in _ROTATION_NEXT_REGISTRATION_STATUS_KEYS:
                status = parameters.get(key)
                if status != "REGISTERED":
                    blockers.append(
                        f"{key.upper()}_{status or 'MISSING'}"
                    )
        return UniverseObservationHealthComponent(
            name="ROTATION_FORWARD_PRECOMMITMENT",
            status=("DEGRADED" if blockers else "HEALTHY"),
            latest_at=(latest.completed_at if latest else None),
            age_seconds=_age_seconds(
                self.now,
                latest.completed_at if latest else None,
            ),
            latest_session_date=(latest.as_of_date if latest else None),
            expected_session_date=month_end_due,
            observed_count=(
                sum(
                    parameters.get(key) == "REGISTERED"
                    for key in _ROTATION_NEXT_REGISTRATION_STATUS_KEYS
                )
                if parameters_available
                else 0
            ),
            expected_count=len(_ROTATION_NEXT_REGISTRATION_STATUS_KEYS),
            coverage_ratio=(
                sum(
                    parameters.get(key) == "REGISTERED"
                    for key in _ROTATION_NEXT_REGISTRATION_STATUS_KEYS
                )
                / len(_ROTATION_NEXT_REGISTRATION_STATUS_KEYS)
                if parameters_available
                else 0.0
            ),
            blockers=blockers,
        )

    def _quant_component(self) -> UniverseObservationHealthComponent:
        if not settings.watchlist_quant_auto_score_enabled:
            return UniverseObservationHealthComponent(
                name="WATCHLIST_QUANT",
                status="DISABLED",
            )
        plan = build_quant_observation_plan(self.db)
        plan_by_symbol = {
            item.symbol.strip().upper(): item
            for item in plan.items
            if item.symbol.strip()
        }
        expected = len(plan_by_symbol)
        if expected == 0:
            return UniverseObservationHealthComponent(
                name="WATCHLIST_QUANT",
                status="DISABLED",
            )
        rows = [
            row
            for row in list_latest_current_quant_scores(self.db)
            if row.symbol.strip().upper() in plan_by_symbol
        ]
        future_rows = [
            row
            for row in rows
            if _timestamp_in_future(self.now, row.created_at)
        ]
        all_markets_idle = all(
            session_status(
                (item.market or "US").strip().upper(),
                self.now,
            ) in {"pre", "closed"}
            for item in plan_by_symbol.values()
        )

        def is_fresh(row: object) -> bool:
            symbol = str(getattr(row, "symbol", "")).strip().upper()
            item = plan_by_symbol[symbol]
            market = (item.market or "US").strip().upper()
            created_at = _as_utc(getattr(row, "created_at", None))
            expires_at = _as_utc(getattr(row, "expires_at", None))
            if created_at is None or created_at > self.now:
                return False
            if expires_at is None or expires_at <= self.now:
                return False
            cutoff = _freshness_cutoff(
                market,
                now=self.now,
                ttl=timedelta(
                    minutes=settings.watchlist_quant_score_ttl_minutes
                ),
            )
            return created_at >= cutoff

        fresh_rows = [row for row in rows if is_fresh(row)]
        fresh = [
            row
            for row in fresh_rows
            if row.source == QUANT_SCORE_SOURCE
        ]
        warming_up = [
            row
            for row in fresh_rows
            if row.source == QUANT_WARMUP_SOURCE
        ]
        priority_symbols = {
            symbol.strip().upper()
            for symbol in plan.priority_symbols
            if symbol.strip()
        }
        priority_errors = sorted({
            row.symbol.strip().upper()
            for row in fresh_rows
            if (
                row.source == QUANT_ERROR_SOURCE
                and row.symbol.strip().upper() in priority_symbols
            )
        })
        priority_warmups = sorted({
            row.symbol.strip().upper()
            for row in warming_up
            if row.symbol.strip().upper() in priority_symbols
        })
        score_timestamps = [
            observed
            for row in rows
            if (observed := _as_utc(row.created_at)) is not None
            and observed <= self.now
        ]
        latest_at = max(score_timestamps, default=None)
        observed = len({
            row.symbol.strip().upper()
            for row in [*fresh, *warming_up]
        })
        coverage = _bounded_ratio(observed, expected)
        age = _age_seconds(self.now, latest_at)
        blockers = (
            [f"QUANT_TIMESTAMP_IN_FUTURE_{len(future_rows)}"]
            if future_rows
            else []
        )
        blockers.extend([
            f"PRIORITY_QUANT_ERROR_{symbol}"
            for symbol in priority_errors
        ])
        blockers.extend([
            f"PRIORITY_QUANT_WARMUP_{symbol}"
            for symbol in priority_warmups
        ])
        if warming_up:
            blockers.append(f"QUANT_WARMUP_{len(warming_up)}")
        if future_rows:
            status = "DEGRADED"
        elif latest_at is None:
            blockers.append("NO_CURRENT_QUANT_SCORE")
            status = "DEGRADED"
        elif coverage is not None and coverage < 0.5:
            blockers.append("FRESH_SCORE_COVERAGE_BELOW_50_PERCENT")
            status = "WARNING" if all_markets_idle and rows else "DEGRADED"
        elif priority_errors or priority_warmups or warming_up:
            status = "WARNING"
        elif coverage is not None and coverage < 0.8:
            blockers.append("FRESH_SCORE_COVERAGE_BELOW_80_PERCENT")
            status = "WARNING"
        else:
            status = "HEALTHY"
        return UniverseObservationHealthComponent(
            name="WATCHLIST_QUANT",
            status=status,
            latest_at=latest_at,
            age_seconds=age,
            observed_count=observed,
            expected_count=expected,
            coverage_ratio=coverage,
            blockers=blockers,
        )

    def _portfolio_component(self) -> UniverseObservationHealthComponent:
        if not settings.strategy_v2_portfolio_shadow_enabled:
            return UniverseObservationHealthComponent(
                name="PORTFOLIO_ROUTING",
                status="DISABLED",
            )
        strategy = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        expected_versions = set(_CURRENT_ROUTING_ALGORITHM_VERSIONS)
        if strategy is None or not strategy.symbol.strip():
            return UniverseObservationHealthComponent(
                name="PORTFOLIO_ROUTING",
                status="DEGRADED",
                expected_count=len(expected_versions),
                blockers=["PRIMARY_SYMBOL_MISSING"],
            )
        primary_symbol = strategy.symbol.strip().upper()
        market = (strategy.market or market_for_symbol(primary_symbol)).upper()
        registrations = self.db.query(
            StrategyV2PortfolioRegistration
        ).filter(
            StrategyV2PortfolioRegistration.baseline_symbol == primary_symbol,
            StrategyV2PortfolioRegistration.algorithm_version.in_(
                expected_versions
            ),
        ).all()
        present_versions = {row.algorithm_version for row in registrations}
        valid_registrations = [
            row
            for row in registrations
            if _portfolio_registration_is_current(row)
            and not _timestamp_in_future(self.now, row.registered_at)
            and _as_utc(row.eligible_after)
            == _registration_eligible_after(row.registered_at)
        ]
        valid_versions = {
            row.algorithm_version for row in valid_registrations
        }
        blockers: list[str] = []
        missing_versions = expected_versions - present_versions
        invalid_versions = present_versions - valid_versions
        if missing_versions:
            blockers.append(
                f"CURRENT_PORTFOLIO_REGISTRATION_MISSING_"
                f"{len(missing_versions)}"
            )
        if invalid_versions:
            blockers.append(
                f"CURRENT_PORTFOLIO_REGISTRATION_INVALID_"
                f"{len(invalid_versions)}"
            )

        shadow_service = StrategyV2ShadowService(self.db)
        current_pairs = {
            (row.symbol, shadow_service._config_version(row))
            for row in self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.enabled.is_(True))
            .all()
        }
        decisions = [
            row
            for row in self.db.query(StrategyV2ShadowDecision)
            .filter(StrategyV2ShadowDecision.action == "SUBMIT_ENTRY")
            .order_by(
                StrategyV2ShadowDecision.bar_at.asc(),
                StrategyV2ShadowDecision.id.asc(),
            )
            .all()
            if (row.symbol, row.config_version) in current_pairs
        ]
        future_decisions = [
            row
            for row in decisions
            if _timestamp_in_future(self.now, row.bar_at)
            or _timestamp_in_future(self.now, row.observed_at)
        ]
        causal_invalid_decisions = [
            row
            for row in decisions
            if row not in future_decisions
            and (
                not _timestamp_is_after(
                    row.observed_at,
                    row.bar_at,
                    inclusive=True,
                )
                or row.session_date
                != get_session(market_for_symbol(row.symbol)).local(
                    row.bar_at
                ).date()
            )
        ]
        outside_rth_decisions = [
            row
            for row in decisions
            if row not in future_decisions
            and row not in causal_invalid_decisions
            and not get_session(market_for_symbol(row.symbol)).is_rth(
                row.bar_at
            )
        ]
        decisions = [
            row
            for row in decisions
            if row not in future_decisions
            and row not in causal_invalid_decisions
            and row not in outside_rth_decisions
        ]
        if future_decisions:
            blockers.append(
                f"ENTRY_SIGNAL_TIMESTAMP_IN_FUTURE_{len(future_decisions)}"
            )
        if causal_invalid_decisions:
            blockers.append(
                "ENTRY_SIGNAL_CAUSALITY_INVALID_"
                f"{len(causal_invalid_decisions)}"
            )
        if outside_rth_decisions:
            blockers.append(
                f"ENTRY_SIGNAL_OUTSIDE_RTH_{len(outside_rth_decisions)}"
            )

        decision_groups: dict[
            datetime,
            list[StrategyV2ShadowDecision],
        ] = {}
        for decision in decisions:
            signal_at = _as_utc(decision.bar_at)
            if signal_at is not None:
                decision_groups.setdefault(signal_at, []).append(decision)
        group_observed_at = {
            signal_at: max(
                _utc_datetimes(
                    decision.observed_at for decision in signal_decisions
                ),
            )
            for signal_at, signal_decisions in decision_groups.items()
        }

        registration_ids = [row.id for row in valid_registrations]
        candidate_observations = (
            self.db.query(StrategyV2PortfolioObservation)
            .filter(
                StrategyV2PortfolioObservation.registration_id.in_(
                    registration_ids
                )
            )
            .all()
            if registration_ids
            else []
        )
        future_observations = [
            row
            for row in candidate_observations
            if _timestamp_in_future(self.now, row.signal_at)
            or _timestamp_in_future(self.now, row.observed_at)
        ]
        def observation_is_causally_invalid(
            row: StrategyV2PortfolioObservation,
        ) -> bool:
            if not _timestamp_is_after(
                row.observed_at,
                row.signal_at,
                inclusive=True,
            ):
                return True
            signal_at = _as_utc(row.signal_at)
            if signal_at is None or signal_at not in group_observed_at:
                return True
            return not _timestamp_is_after(
                row.observed_at,
                group_observed_at[signal_at],
                inclusive=True,
            )

        causal_invalid_observations = [
            row
            for row in candidate_observations
            if row not in future_observations
            and observation_is_causally_invalid(row)
        ]
        observations = [
            row
            for row in candidate_observations
            if row not in future_observations
            and row not in causal_invalid_observations
        ]
        if future_observations:
            blockers.append(
                "PORTFOLIO_OBSERVATION_TIMESTAMP_IN_FUTURE_"
                f"{len(future_observations)}"
            )
        if causal_invalid_observations:
            blockers.append(
                "PORTFOLIO_OBSERVATION_CAUSALITY_INVALID_"
                f"{len(causal_invalid_observations)}"
            )
        observed_keys = {
            (row.registration_id, _as_utc(row.signal_at))
            for row in observations
        }
        valid_by_version = {
            row.algorithm_version: row for row in valid_registrations
        }
        registrations_by_version = {
            row.algorithm_version: row for row in registrations
        }
        authoritative_eligible_by_version = {
            version: _registration_eligible_after(row.registered_at)
            for version, row in registrations_by_version.items()
            if not _timestamp_in_future(self.now, row.registered_at)
        }
        cohort_eligible_after = min(
            authoritative_eligible_by_version.values(),
            default=None,
        )
        required_groups = [
            (
                version,
                valid_by_version.get(version),
                signal_at,
                group_observed_at[signal_at],
            )
            for version in expected_versions
            for signal_at in decision_groups
            if (
                (eligible_after := authoritative_eligible_by_version.get(
                    version,
                    cohort_eligible_after,
                ))
                is None
                or signal_at >= eligible_after
            )
        ]
        missing_groups = [
            (version, registration, signal_at, observed_at)
            for version, registration, signal_at, observed_at in required_groups
            if registration is None
            or (registration.id, signal_at) not in observed_keys
        ]
        matched_count = len(required_groups) - len(missing_groups)
        if missing_groups:
            oldest_pending_at = min(
                _utc_datetimes(
                    observed_at
                    for _, _, _, observed_at in missing_groups
                ),
                default=None,
            )
            pending_age = _age_seconds(self.now, oldest_pending_at)
            if (
                pending_age is None
                or pending_age > _PORTFOLIO_WARNING_AGE.total_seconds()
            ):
                blockers.append(
                    f"ENTRY_SIGNAL_UNPROCESSED_{len(missing_groups)}"
                )
            else:
                blockers.append(
                    f"ENTRY_SIGNAL_PENDING_{len(missing_groups)}"
                )

        session = get_session(market)
        rth_active = session.is_rth(self.now)
        expected_session = latest_closed_session_date(
            market=market,
            now=self.now,
        )
        current_session = session.local(self.now).date()
        session_signals = [
            row
            for row in decisions
            if session.local(row.bar_at).date()
            == (current_session if rth_active else expected_session)
        ]
        registration_failure = bool(
            missing_versions or invalid_versions or future_decisions
            or causal_invalid_decisions or outside_rth_decisions
            or future_observations or causal_invalid_observations
        )
        if registration_failure:
            status = "DEGRADED"
        elif missing_groups:
            oldest_age = max(
                (
                    _age_seconds(self.now, observed_at) or 0.0
                    for _, _, _, observed_at in missing_groups
                ),
                default=0.0,
            )
            status = (
                "DEGRADED"
                if oldest_age > _PORTFOLIO_WARNING_AGE.total_seconds()
                else "WARNING"
            )
        elif rth_active and not session_signals:
            blockers.append("ROUTING_IDLE_UNOBSERVABLE")
            status = "WARNING"
        else:
            status = "HEALTHY"

        latest_at = max(
            _utc_datetimes(
                row.observed_at for row in observations
            ),
            default=max(
                _utc_datetimes(
                    row.registered_at for row in valid_registrations
                ),
                default=None,
            ),
        )
        latest_signal_session = max(
            (
                session.local(row.bar_at).date()
                for row in decisions
                if any(
                    (
                        registration.id,
                        _as_utc(row.bar_at),
                    ) in observed_keys
                    for registration in valid_registrations
                )
            ),
            default=(expected_session if not rth_active else None),
        )
        return UniverseObservationHealthComponent(
            name="PORTFOLIO_ROUTING",
            status=status,
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            latest_session_date=latest_signal_session,
            expected_session_date=(
                expected_session if not rth_active else current_session
            ),
            observed_count=matched_count,
            expected_count=len(required_groups),
            coverage_ratio=_bounded_ratio(matched_count, len(required_groups)),
            blockers=blockers,
        )

    def _diversified_priority_component(
        self,
    ) -> UniverseObservationHealthComponent:
        readiness = UniversePromotionService(
            self.db,
            now=self.now,
        ).get_readiness()
        if readiness is None or not readiness.items:
            return UniverseObservationHealthComponent(
                name="DIVERSIFIED_PRIORITY_OBSERVATION",
                status="DISABLED",
            )
        selected = [
            item
            for item in readiness.items
            if item.diversified_observation_selected
        ]
        expected = readiness.diversified_observation_limit
        risk_groups = [item.risk_group for item in selected]
        ranks = [item.diversified_observation_rank for item in selected]
        blockers: list[str] = []
        if len(risk_groups) != len(set(risk_groups)):
            blockers.append("DUPLICATE_RISK_GROUP")
        if ranks != list(range(1, len(selected) + 1)):
            blockers.append("NON_CONTIGUOUS_DIVERSIFIED_RANKS")
        invalid = [
            item.symbol
            for item in selected
            if (
                not item.shadow_enabled
                or not item.quant_fresh
                or item.quant_source != QUANT_SCORE_SOURCE
                or item.quant_recommended_action.upper()
                not in {"WATCH", "CANDIDATE"}
            )
        ]
        if invalid:
            blockers.append(
                f"DIVERSIFIED_ELIGIBILITY_INVALID_{len(invalid)}"
            )
        coverage = len(selected) / expected if expected else 1.0
        structural_failure = bool(blockers)
        if structural_failure:
            status = "DEGRADED"
        elif len(selected) < 4:
            blockers.append("DIVERSIFIED_SHORTLIST_BELOW_4")
            status = "WARNING"
        elif len(selected) < expected:
            blockers.append("DIVERSIFIED_SHORTLIST_BELOW_8")
            status = "WARNING"
        else:
            status = "HEALTHY"
        return UniverseObservationHealthComponent(
            name="DIVERSIFIED_PRIORITY_OBSERVATION",
            status=status,
            latest_at=readiness.generated_at,
            age_seconds=_age_seconds(
                self.now,
                readiness.generated_at,
            ),
            observed_count=len(selected),
            expected_count=expected,
            coverage_ratio=coverage,
            blockers=blockers,
        )

    def _growth_satellite_component(
        self,
    ) -> UniverseObservationHealthComponent:
        readiness = UniversePromotionService(self.db, now=self.now).get_readiness()
        if readiness is None or not readiness.items:
            return UniverseObservationHealthComponent(
                name="GROWTH_SATELLITE_OBSERVATION",
                status="DISABLED",
            )
        selected = [
            item
            for item in readiness.items
            if item.growth_satellite_selected
        ]
        expected = readiness.growth_satellite_limit
        ranks = [item.growth_satellite_rank for item in selected]
        blockers: list[str] = []
        if ranks != list(range(1, len(selected) + 1)):
            blockers.append("NON_CONTIGUOUS_SATELLITE_RANKS")
        if set(item.symbol for item in selected).intersection(
            item.symbol
            for item in readiness.items
            if item.diversified_observation_selected
        ):
            blockers.append("CORE_SATELLITE_OVERLAP")
        risk_group_counts: dict[str, int] = {}
        for item in selected:
            risk_group_counts[item.risk_group] = (
                risk_group_counts.get(item.risk_group, 0) + 1
            )
        if any(count > 2 for count in risk_group_counts.values()):
            blockers.append("SATELLITE_RISK_GROUP_CAP_EXCEEDED")
        invalid = [
            item.symbol
            for item in selected
            if (
                item.is_trading_target
                or not item.memberships
                or not item.shadow_enabled
                or not item.quant_fresh
                or item.quant_source != QUANT_SCORE_SOURCE
                or item.quant_recommended_action.upper()
                not in {"WATCH", "CANDIDATE"}
                or item.estimated_round_trip_cost_bps is None
                or item.estimated_round_trip_cost_bps > 20.0
            )
        ]
        if invalid:
            blockers.append(f"SATELLITE_ELIGIBILITY_INVALID_{len(invalid)}")
        structural_failure = bool(blockers)
        if len(selected) < expected:
            blockers.append(
                f"SATELLITE_COVERAGE_MISSING_{expected - len(selected)}"
            )
        return UniverseObservationHealthComponent(
            name="GROWTH_SATELLITE_OBSERVATION",
            status=(
                "DEGRADED"
                if structural_failure
                else "WARNING"
                if blockers
                else "HEALTHY"
            ),
            latest_at=self.now,
            age_seconds=0.0,
            observed_count=len(selected),
            expected_count=expected,
            coverage_ratio=len(selected) / expected,
            blockers=blockers,
        )

    def _strategy_v2_forward_component(
        self,
    ) -> UniverseObservationHealthComponent:
        observed_by_universe = UniversePromotionService(
            self.db,
            now=self.now,
        ).get_observed_symbols()
        expected_symbols = {
            row.symbol
            for row in self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.enabled.is_(True))
            .all()
            if row.universe_managed or row.symbol in observed_by_universe
        }
        if not expected_symbols:
            return UniverseObservationHealthComponent(
                name="STRATEGY_V2_FORWARD",
                status="DISABLED",
            )
        configs = {
            row.symbol: row
            for row in self.db.query(StrategyV2ShadowConfig)
            .filter(
                StrategyV2ShadowConfig.enabled.is_(True),
                StrategyV2ShadowConfig.symbol.in_(expected_symbols),
            )
            .all()
        }
        shadow_service = StrategyV2ShadowService(self.db)
        current_versions = {
            symbol: shadow_service._config_version(config)
            for symbol, config in configs.items()
        }
        evaluator_digest = shadow_service._forward_evaluator_digest()
        evaluator_spec = shadow_service._forward_evaluator_spec()
        candidate_version = str(
            evaluator_spec["candidate_algorithm_version"]
        )
        candidate_rows = self.db.query(
            StrategyV2ForwardRegistration
        ).filter(
            StrategyV2ForwardRegistration.symbol.in_(expected_symbols),
            StrategyV2ForwardRegistration.candidate_algorithm_version
            == candidate_version,
        ).all()
        present_symbols = {
            row.symbol
            for row in candidate_rows
            if row.source_config_version == current_versions.get(row.symbol)
        }
        registrations: list[StrategyV2ForwardRegistration] = []
        for row in candidate_rows:
            expected_market = market_for_symbol(row.symbol)
            registered_at = _as_utc(row.registered_at)
            try:
                expected_eligible_after = (
                    shadow_service._forward_eligible_after(
                        expected_market,
                        row.registered_at,
                    )
                )
            except ValueError:
                continue
            if (
                row.source_config_version
                != current_versions.get(row.symbol)
                or row.market.upper() != expected_market
                or row.evaluator_digest != evaluator_digest
                or not shadow_service._forward_spec_matches(row)
                or registered_at is None
                or registered_at > self.now
                or _as_utc(row.eligible_after) != expected_eligible_after
            ):
                continue
            registrations.append(row)
        registered_symbols = {row.symbol for row in registrations}
        registration_ids = [row.id for row in registrations]
        registration_by_id = {row.id: row for row in registrations}
        candidate_evidence = (
            self.db.query(StrategyV2ForwardEvidence)
            .filter(
                StrategyV2ForwardEvidence.registration_id.in_(
                    registration_ids
                )
            )
            .all()
            if registration_ids
            else []
        )
        due_upper_by_registration: dict[int, date] = {}
        for registration in registrations:
            session = get_session(registration.market)
            latest_closed = latest_closed_session_date(
                market=registration.market,
                now=self.now,
            )
            upper = (
                session.local(self.now).date()
                if shadow_service._in_forward_finalize_window(
                    registration.market,
                    self.now,
                )
                else latest_closed
            )
            due_upper_by_registration[registration.id] = upper
        future_evidence = [
            row
            for row in candidate_evidence
            if _timestamp_in_future(self.now, row.evaluated_at)
            or _timestamp_in_future(self.now, row.target_open_at)
            or row.target_session_date
            > due_upper_by_registration[row.registration_id]
        ]

        def evidence_timing_is_valid(
            row: StrategyV2ForwardEvidence,
        ) -> bool:
            registration = registration_by_id[row.registration_id]
            return (
                _as_utc(row.target_open_at)
                == _session_open_at(
                    registration.market,
                    row.target_session_date,
                )
                and shadow_service._forward_collection_phase(
                    registration.market,
                    row.evaluated_at,
                )
                == "FINALIZE"
                and get_session(registration.market).local(
                    row.evaluated_at
                ).date()
                == row.target_session_date
            )

        timing_invalid_evidence = [
            row
            for row in candidate_evidence
            if row not in future_evidence
            and not evidence_timing_is_valid(row)
        ]
        evidence = [
            row
            for row in candidate_evidence
            if row not in future_evidence
            and row not in timing_invalid_evidence
        ]
        replay_mismatches = [
            row
            for row in evidence
            if (
                row.baseline_replay_match is False
                or row.exclusion_reason == "BASELINE_REPLAY_MISMATCH"
            )
        ]
        structural_failures = [
            row for row in evidence if row.structural_failure
        ]
        validation_statuses = {
            row.id: shadow_service.get_forward_validation(row.symbol).status
            for row in registrations
        }
        blocked_registrations = [
            registration_id
            for registration_id, status in validation_statuses.items()
            if status == "BLOCKED"
        ]
        due_sessions: dict[int, date] = {}
        for row in registrations:
            if validation_statuses.get(row.id) in {
                "BLOCKED",
                "MATURE_EVIDENCE",
            }:
                continue
            due_session = due_upper_by_registration[row.id]
            if _timestamp_is_after(
                _session_open_at(row.market, due_session),
                row.eligible_after,
                inclusive=True,
            ):
                due_sessions[row.id] = due_session
        evidence_keys = {
            (row.registration_id, row.target_session_date)
            for row in evidence
        }
        missing_evidence = [
            registration_id
            for registration_id, target_session in due_sessions.items()
            if (registration_id, target_session) not in evidence_keys
        ]
        blockers: list[str] = []
        missing = sorted(expected_symbols - present_symbols)
        invalid = sorted(present_symbols - registered_symbols)
        if missing:
            blockers.append(
                f"CURRENT_EVALUATOR_REGISTRATION_MISSING_{len(missing)}"
            )
        if invalid:
            blockers.append(
                f"CURRENT_EVALUATOR_REGISTRATION_INVALID_{len(invalid)}"
            )
        if future_evidence:
            blockers.append(
                f"FORWARD_EVIDENCE_TIMESTAMP_IN_FUTURE_"
                f"{len(future_evidence)}"
            )
        if timing_invalid_evidence:
            blockers.append(
                f"FORWARD_EVIDENCE_TIMING_INVALID_"
                f"{len(timing_invalid_evidence)}"
            )
        if blocked_registrations and not (
            replay_mismatches
            or structural_failures
            or future_evidence
            or timing_invalid_evidence
        ):
            blockers.append(
                f"FORWARD_VALIDATION_BLOCKED_"
                f"{len(blocked_registrations)}"
            )
        if replay_mismatches:
            blockers.append(
                f"BASELINE_REPLAY_MISMATCH_{len(replay_mismatches)}"
            )
        other_structural = len(structural_failures) - len({
            row.id for row in replay_mismatches if row.structural_failure
        })
        if other_structural > 0:
            blockers.append(f"STRUCTURAL_FAILURE_{other_structural}")
        if missing_evidence:
            blockers.append(
                f"FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_"
                f"{len(missing_evidence)}"
            )
        if (
            missing
            or invalid
            or future_evidence
            or timing_invalid_evidence
            or blocked_registrations
            or replay_mismatches
            or other_structural > 0
            or missing_evidence
        ):
            status = "DEGRADED"
        else:
            status = "HEALTHY"
        latest_at = max(
            _utc_datetimes(
                row.evaluated_at for row in evidence
            ),
            default=max(
                _utc_datetimes(
                    row.registered_at for row in registrations
                ),
                default=None,
            ),
        )
        failing_registration_ids = {
            row.registration_id
            for row in [
                *future_evidence,
                *timing_invalid_evidence,
                *replay_mismatches,
                *structural_failures,
            ]
        }.union(blocked_registrations).union(missing_evidence)
        healthy_registration_ids = {
            row.id for row in registrations
        } - failing_registration_ids
        observed_count = len(healthy_registration_ids)
        return UniverseObservationHealthComponent(
            name="STRATEGY_V2_FORWARD",
            status=status,
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            latest_session_date=max(
                (row.target_session_date for row in evidence),
                default=None,
            ),
            expected_session_date=(
                max(due_sessions.values()) if due_sessions else None
            ),
            observed_count=observed_count,
            expected_count=len(expected_symbols),
            coverage_ratio=_bounded_ratio(
                observed_count,
                len(expected_symbols),
            ),
            blockers=blockers,
        )

    def _live_interval_component(
        self,
    ) -> UniverseObservationHealthComponent:
        config = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        if (
            config is None
            or not config.symbol
            or config.buy_low <= 0
            or config.stop_loss_pct <= 0
        ):
            return UniverseObservationHealthComponent(
                name="LIVE_INTERVAL_ALIGNMENT",
                status="DISABLED",
            )
        state = self.db.query(RuntimeState).filter(
            RuntimeState.symbol == config.symbol.strip().upper()
        ).first()
        if state is None or state.last_price <= 0:
            return UniverseObservationHealthComponent(
                name="LIVE_INTERVAL_ALIGNMENT",
                status="WARNING",
                expected_count=1,
                blockers=["CURRENT_PRICE_UNAVAILABLE"],
            )
        if _timestamp_in_future(self.now, state.updated_at):
            return UniverseObservationHealthComponent(
                name="LIVE_INTERVAL_ALIGNMENT",
                status="DEGRADED",
                latest_at=state.updated_at,
                expected_count=1,
                blockers=["CURRENT_PRICE_TIMESTAMP_IN_FUTURE"],
            )
        entry_floor = config.buy_low * (
            1 - config.stop_loss_pct / 100
        )
        blockers: list[str] = []
        if (
            state.engine_state == "flat"
            and state.last_price <= entry_floor
        ):
            blockers.append("CURRENT_PRICE_BELOW_LONG_ENTRY_FLOOR")
        market = (config.market or market_for_symbol(config.symbol)).upper()
        session = get_session(market)
        age = _age_seconds(self.now, state.updated_at)
        latest_session = session.local(state.updated_at).date()
        phase = session_status(market, self.now)
        local_day = session.local(self.now).date()
        phase_reference: datetime | None = None
        if phase == "rth":
            expected_session = local_day
            phase_reference = self.now
        elif phase == "lunch":
            expected_session = local_day
            assert session.lunch_start is not None
            phase_reference = datetime.combine(
                local_day,
                session.lunch_start,
                tzinfo=session.timezone,
            ).astimezone(timezone.utc)
        elif phase == "post":
            expected_session = local_day
            phase_reference = _session_close_at(market, local_day)
        else:
            expected_session = latest_closed_session_date(
                market=market,
                now=self.now,
            )

        if phase_reference is not None:
            reference_age = _age_seconds(phase_reference, state.updated_at)
            if latest_session != expected_session:
                blockers.append("CURRENT_PRICE_STALE")
                status = "DEGRADED"
            elif (
                reference_age is None
                or reference_age
                > _LIVE_PRICE_WARNING_AGE.total_seconds()
            ):
                blockers.append("CURRENT_PRICE_STALE")
                status = "DEGRADED"
            elif reference_age > _LIVE_PRICE_HEALTHY_AGE.total_seconds():
                blockers.append("CURRENT_PRICE_LATE")
                status = "WARNING"
            else:
                status = "WARNING" if blockers else "HEALTHY"
        else:
            if latest_session != expected_session:
                blockers.append("CURRENT_PRICE_STALE")
                status = (
                    "DEGRADED"
                    if latest_session > expected_session
                    or _completed_session_gap(
                        market,
                        latest=latest_session,
                        expected=expected_session,
                    ) > 1
                    else "WARNING"
                )
            else:
                observed_at = _as_utc(state.updated_at)
                session_close = _session_close_at(market, expected_session)
                if (
                    observed_at is None
                    or observed_at
                    < session_close - _LIVE_PRICE_WARNING_AGE
                ):
                    blockers.append("CURRENT_PRICE_STALE")
                    status = "DEGRADED"
                elif observed_at < session_close - _LIVE_PRICE_HEALTHY_AGE:
                    blockers.append("CURRENT_PRICE_LATE")
                    status = "WARNING"
                else:
                    status = "WARNING" if blockers else "HEALTHY"
        return UniverseObservationHealthComponent(
            name="LIVE_INTERVAL_ALIGNMENT",
            status=status,
            latest_at=state.updated_at,
            age_seconds=age,
            latest_session_date=latest_session,
            expected_session_date=expected_session,
            observed_count=1,
            expected_count=1,
            coverage_ratio=1.0,
            blockers=blockers,
        )

    def _live_exit_challenger_component(
        self,
    ) -> UniverseObservationHealthComponent:
        if not settings.live_exit_challenger_enabled:
            return UniverseObservationHealthComponent(
                name="LIVE_EXIT_CHALLENGER",
                status="DISABLED",
            )
        config = (
            self.db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        if config is None or not config.symbol:
            return UniverseObservationHealthComponent(
                name="LIVE_EXIT_CHALLENGER",
                status="DEGRADED",
                expected_count=len(LIVE_EXIT_ALGORITHM_VERSIONS),
                blockers=["PRIMARY_SYMBOL_MISSING"],
            )
        normalized = config.symbol.strip().upper()
        shadow_feed = self.db.query(StrategyV2ShadowConfig).filter(
            StrategyV2ShadowConfig.symbol == normalized
        ).first()
        rows = self.db.query(LiveExitChallengerRegistration).filter(
            LiveExitChallengerRegistration.symbol == normalized,
            LiveExitChallengerRegistration.algorithm_version.in_(
                LIVE_EXIT_ALGORITHM_VERSIONS
            ),
        ).all()
        expected = set(LIVE_EXIT_ALGORITHM_VERSIONS)
        present = {row.algorithm_version for row in rows}
        valid_rows = [
            row
            for row in rows
            if _live_exit_registration_is_current(
                row,
                market=config.market,
                slippage_bps=float(
                    settings.entry_round_trip_slippage_bps
                ),
            )
            and not _timestamp_in_future(self.now, row.registered_at)
            and _as_utc(row.eligible_after)
            == _registration_eligible_after(row.registered_at)
        ]
        registered = {row.algorithm_version for row in valid_rows}
        missing = expected - present
        invalid = present - registered
        blockers: list[str] = []
        if shadow_feed is None or not shadow_feed.enabled:
            blockers.append("PRIMARY_LIVE_EXIT_FEED_DISABLED")
        if missing:
            blockers.append(
                f"CURRENT_LIVE_EXIT_REGISTRATION_MISSING_{len(missing)}"
            )
        if invalid:
            blockers.append(
                f"CURRENT_LIVE_EXIT_REGISTRATION_INVALID_{len(invalid)}"
            )

        entry_rows = self.db.query(OrderRecord).filter(
            OrderRecord.symbol == normalized,
            OrderRecord.side == "BUY",
            OrderRecord.status.in_(("FILLED", "PARTIAL_FILLED")),
            OrderRecord.filled_at.isnot(None),
            OrderRecord.filled_at <= self.now,
        ).order_by(OrderRecord.id.asc()).all()
        entries_by_broker_identity: dict[tuple[object, ...], OrderRecord] = {}
        for entry in entry_rows:
            broker_order_id = str(entry.broker_order_id or "").strip()
            identity: tuple[object, ...] = (
                ("BROKER", broker_order_id)
                if broker_order_id
                else (
                    "FILL",
                    _as_utc(entry.filled_at),
                    str(entry.config_version or ""),
                    float(entry.executed_quantity or entry.quantity or 0),
                    float(entry.executed_price or entry.price or 0),
                )
            )
            entries_by_broker_identity[identity] = entry
        eligible_entries = list(entries_by_broker_identity.values())
        required_pairs = [
            (registration, entry)
            for registration in valid_rows
            for entry in eligible_entries
            if entry.filled_at is not None
            and _timestamp_is_after(
                entry.filled_at,
                registration.eligible_after,
                inclusive=True,
            )
        ]
        trade_rows = (
            self.db.query(LiveExitChallengerTrade).filter(
                LiveExitChallengerTrade.registration_id.in_(
                    [row.id for row in valid_rows]
                )
            ).all()
            if valid_rows
            else []
        )
        trades_by_key = {
            (row.registration_id, row.entry_order_id): row
            for row in trade_rows
        }
        baseline_exit_order_ids = {
            row.baseline_exit_order_id
            for row in trade_rows
            if row.baseline_exit_order_id is not None
        }
        baseline_exit_orders = {
            row.id: row
            for row in self.db.query(OrderRecord).filter(
                OrderRecord.id.in_(baseline_exit_order_ids)
            ).all()
        } if baseline_exit_order_ids else {}

        def live_trade_status_is_valid(
            trade: LiveExitChallengerTrade,
            *,
            entry_at: datetime,
            updated_at: datetime,
        ) -> bool:
            challenger_values = (
                trade.challenger_exit_at,
                trade.challenger_exit_price,
                trade.challenger_gross_pnl,
                trade.challenger_estimated_fees,
                trade.challenger_net_pnl,
            )
            pair_values = (
                trade.baseline_exit_order_id,
                trade.baseline_exit_at,
                trade.baseline_exit_price,
                trade.baseline_net_pnl,
                trade.net_pnl_delta,
                trade.paired_at,
            )
            pair_marked = bool(
                any(value is not None for value in pair_values)
                or trade.baseline_exit_reason
            )
            if trade.status == "OPEN":
                return bool(
                    all(value is None for value in challenger_values)
                    and not trade.challenger_exit_reason
                    and not pair_marked
                )
            if trade.status != "CLOSED":
                return False
            challenger_exit_at = _as_utc(trade.challenger_exit_at)
            challenger_exit_price = trade.challenger_exit_price
            challenger_gross_pnl = trade.challenger_gross_pnl
            challenger_estimated_fees = trade.challenger_estimated_fees
            challenger_net_pnl = trade.challenger_net_pnl
            if (
                challenger_exit_at is None
                or challenger_exit_at < entry_at
                or challenger_exit_at > self.now
                or updated_at < challenger_exit_at
                or challenger_exit_price is None
                or not _positive_finite(challenger_exit_price)
                or not trade.challenger_exit_reason
                or challenger_gross_pnl is None
                or not _finite(challenger_gross_pnl)
                or challenger_estimated_fees is None
                or not _finite(challenger_estimated_fees)
                or challenger_estimated_fees < 0
                or challenger_net_pnl is None
                or not _finite(challenger_net_pnl)
            ):
                return False
            baseline_style = trade.challenger_exit_reason.startswith(
                "BASELINE_"
            )
            if (
                not baseline_style
                and trade.challenger_exit_reason
                not in {"PROFIT_LOCK", "TIME_STOP"}
            ):
                return False
            if baseline_style and not pair_marked:
                return False

            baseline_order: OrderRecord | None = None
            baseline_exit_at: datetime | None = None
            baseline_exit_price: float | None = None
            baseline_net_pnl: float | None = None
            net_pnl_delta: float | None = None
            paired_at: datetime | None = None
            if pair_marked:
                if any(value is None for value in pair_values):
                    return False
                baseline_exit_order_id = trade.baseline_exit_order_id
                baseline_exit_price = trade.baseline_exit_price
                baseline_net_pnl = trade.baseline_net_pnl
                net_pnl_delta = trade.net_pnl_delta
                if (
                    baseline_exit_order_id is None
                    or baseline_exit_price is None
                    or baseline_net_pnl is None
                    or net_pnl_delta is None
                ):
                    return False
                baseline_order = baseline_exit_orders.get(
                    baseline_exit_order_id
                )
                baseline_exit_at = _as_utc(trade.baseline_exit_at)
                paired_at = _as_utc(trade.paired_at)
                if (
                    baseline_order is None
                    or baseline_order.side != "SELL"
                    or baseline_order.symbol.strip().upper() != normalized
                    or baseline_order.status != "FILLED"
                    or baseline_order.filled_at is None
                    or baseline_order.net_pnl is None
                    or baseline_exit_at is None
                    or paired_at is None
                    or baseline_exit_at < entry_at
                    or baseline_exit_at > self.now
                    or paired_at < baseline_exit_at
                    or paired_at < challenger_exit_at
                    or paired_at > self.now
                    or updated_at < paired_at
                    or not _positive_finite(baseline_exit_price)
                    or not _finite(baseline_net_pnl)
                    or not _finite(net_pnl_delta)
                    or not trade.baseline_exit_reason
                ):
                    return False

            if baseline_style:
                if (
                    baseline_order is None
                    or baseline_order.filled_at is None
                    or baseline_order.gross_pnl is None
                    or baseline_order.pnl_fee is None
                    or baseline_order.net_pnl is None
                ):
                    return False
                actual_exit_at = _as_utc(baseline_order.filled_at)
                actual_exit_price = float(
                    baseline_order.executed_price
                    if baseline_order.executed_price is not None
                    else baseline_order.price
                )
                actual_gross_pnl = float(baseline_order.gross_pnl)
                actual_fees = float(baseline_order.pnl_fee)
                actual_net_pnl = float(baseline_order.net_pnl)
                if (
                    actual_exit_at is None
                    or not _positive_finite(actual_exit_price)
                    or not math.isfinite(actual_gross_pnl)
                    or not math.isfinite(actual_fees)
                    or actual_fees < 0
                    or not math.isfinite(actual_net_pnl)
                    or challenger_exit_at != actual_exit_at
                    or not math.isclose(
                        challenger_exit_price,
                        actual_exit_price,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    or trade.challenger_exit_reason
                    != f"BASELINE_{baseline_order.exit_cause or 'EXIT'}"
                    or not math.isclose(
                        challenger_gross_pnl,
                        actual_gross_pnl,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    or not math.isclose(
                        challenger_estimated_fees,
                        actual_fees,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    or not math.isclose(
                        challenger_net_pnl,
                        actual_net_pnl,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                ):
                    return False
            else:
                expected_gross = (
                    challenger_exit_price - trade.entry_price
                ) * trade.quantity
                expected_fees = (
                    (trade.entry_price + challenger_exit_price)
                    * trade.quantity
                    * trade.estimated_fee_rate
                )
                if (
                    not math.isclose(
                        challenger_gross_pnl,
                        expected_gross,
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                    or not math.isclose(
                        challenger_estimated_fees,
                        expected_fees,
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                    or not math.isclose(
                        challenger_net_pnl,
                        expected_gross - expected_fees,
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                ):
                    return False

            if not pair_marked:
                return True
            assert baseline_order is not None
            assert baseline_exit_at is not None
            assert baseline_exit_price is not None
            assert baseline_net_pnl is not None
            assert net_pnl_delta is not None
            expected_baseline_net_pnl = baseline_order.net_pnl
            assert expected_baseline_net_pnl is not None
            expected_exit_at = _as_utc(baseline_order.filled_at)
            expected_exit_price = float(
                baseline_order.executed_price
                if baseline_order.executed_price is not None
                else baseline_order.price
            )
            expected_reason = (
                baseline_order.exit_cause
                or baseline_order.exit_reason
                or "EXIT"
            )
            return bool(
                baseline_exit_at == expected_exit_at
                and _positive_finite(expected_exit_price)
                and math.isclose(
                    baseline_exit_price,
                    expected_exit_price,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and math.isclose(
                    baseline_net_pnl,
                    expected_baseline_net_pnl,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and trade.baseline_exit_reason == expected_reason
                and math.isclose(
                    net_pnl_delta,
                    challenger_net_pnl - baseline_net_pnl,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            )

        invalid_evidence = []
        valid_evidence_keys: set[tuple[int, int]] = set()
        for registration, entry in required_pairs:
            trade = trades_by_key.get((registration.id, entry.id))
            if trade is None or entry.filled_at is None:
                continue
            entry_at = _as_utc(trade.entry_at)
            filled_at = _as_utc(entry.filled_at)
            last_bar_at = _as_utc(trade.last_bar_at)
            updated_at = _as_utc(trade.updated_at)
            expected_quantity = float(
                entry.executed_quantity
                if entry.executed_quantity is not None
                else entry.quantity
            )
            expected_price = float(
                entry.executed_price
                if entry.executed_price is not None
                else entry.price
            )
            if (
                entry_at is None
                or filled_at is None
                or last_bar_at is None
                or updated_at is None
                or entry_at > self.now
                or entry_at < filled_at
                or not _timestamp_is_after(
                    entry_at,
                    registration.eligible_after,
                    inclusive=True,
                )
                or (entry_at - filled_at).total_seconds() > 300
                or trade.symbol.strip().upper() != normalized
                or trade.entry_config_version
                != str(entry.config_version or "")
                or not _positive_finite(expected_price)
                or not _positive_finite(expected_quantity)
                or not _positive_finite(trade.entry_price)
                or not _positive_finite(trade.quantity)
                or not _finite(trade.estimated_fee_rate)
                or trade.estimated_fee_rate < 0
                or not math.isclose(
                    trade.entry_price,
                    expected_price,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    trade.quantity,
                    expected_quantity,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or last_bar_at
                < entry_at.replace(second=0, microsecond=0)
                or last_bar_at > self.now
                or updated_at < entry_at
                or updated_at < last_bar_at
                or updated_at > self.now
                or not live_trade_status_is_valid(
                    trade,
                    entry_at=entry_at,
                    updated_at=updated_at,
                )
            ):
                invalid_evidence.append(trade)
                continue
            valid_evidence_keys.add((registration.id, entry.id))
        required_keys = {
            (registration.id, entry.id)
            for registration, entry in required_pairs
        }
        absent_evidence = required_keys - set(trades_by_key)
        unexpected_evidence = set(trades_by_key) - required_keys
        if invalid_evidence:
            blockers.append(
                f"LIVE_EXIT_EVIDENCE_INVALID_{len(invalid_evidence)}"
            )
        if absent_evidence:
            blockers.append(
                f"LIVE_EXIT_EVIDENCE_MISSING_{len(absent_evidence)}"
            )
        if unexpected_evidence:
            blockers.append(
                "LIVE_EXIT_EVIDENCE_UNEXPECTED_"
                f"{len(unexpected_evidence)}"
            )

        if required_pairs:
            observed_count = len(valid_evidence_keys)
            expected_count = len(required_keys)
            latest_at = max(
                _utc_datetimes(
                    trades_by_key[key].updated_at
                    for key in valid_evidence_keys
                ),
                default=max(
                    _utc_datetimes(row.registered_at for row in valid_rows),
                    default=None,
                ),
            )
        else:
            observed_count = len(registered)
            expected_count = len(expected)
            latest_at = max(
                _utc_datetimes(row.registered_at for row in valid_rows),
                default=None,
            )
        return UniverseObservationHealthComponent(
            name="LIVE_EXIT_CHALLENGER",
            status=("DEGRADED" if blockers else "HEALTHY"),
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            observed_count=observed_count,
            expected_count=expected_count,
            coverage_ratio=_bounded_ratio(observed_count, expected_count),
            blockers=blockers,
        )

    def _strategy_v2_exit_challenger_component(
        self,
    ) -> UniverseObservationHealthComponent:
        configs = (
            self.db.query(StrategyV2ShadowConfig)
            .filter(StrategyV2ShadowConfig.enabled.is_(True))
            .all()
        )
        if not configs:
            return UniverseObservationHealthComponent(
                name="STRATEGY_V2_EXIT_CHALLENGER",
                status="DISABLED",
            )
        shadow_service = StrategyV2ShadowService(self.db)
        expected_pairs = {
            (
                config.symbol,
                shadow_service._config_version(config),
                algorithm_version,
            )
            for config in configs
            for algorithm_version in STRATEGY_V2_EXIT_ALGORITHM_VERSIONS
        }
        symbols = {config.symbol for config in configs}
        rows = self.db.query(StrategyV2ExitChallengerRegistration).filter(
            StrategyV2ExitChallengerRegistration.symbol.in_(symbols),
            StrategyV2ExitChallengerRegistration.algorithm_version.in_(
                STRATEGY_V2_EXIT_ALGORITHM_VERSIONS
            ),
        ).all()
        present_pairs = {
            (
                row.symbol,
                row.source_config_version,
                row.algorithm_version,
            )
            for row in rows
        }.intersection(expected_pairs)
        configs_by_pair = {
            (config.symbol, shadow_service._config_version(config)): config
            for config in configs
        }
        valid_rows = [
            row
            for row in rows
            if (
                row.symbol,
                row.source_config_version,
                row.algorithm_version,
            ) in expected_pairs
            and (
                config := configs_by_pair.get((
                    row.symbol,
                    row.source_config_version,
                ))
            )
            is not None
            and _strategy_exit_registration_is_current(
                row,
                market=market_for_symbol(row.symbol),
                slippage_bps=config.slippage_bps,
            )
            and not _timestamp_in_future(self.now, row.registered_at)
            and _as_utc(row.eligible_after)
            == _registration_eligible_after(row.registered_at)
        ]
        registered_pairs = {
            (
                row.symbol,
                row.source_config_version,
                row.algorithm_version,
            )
            for row in valid_rows
        }
        missing = expected_pairs - present_pairs
        invalid = present_pairs - registered_pairs
        latest_at = max(
            _utc_datetimes(row.registered_at for row in valid_rows),
            default=None,
        )
        blockers: list[str] = []
        if missing:
            blockers.append(
                "CURRENT_STRATEGY_V2_EXIT_REGISTRATION_MISSING_"
                f"{len(missing)}"
            )
        if invalid:
            blockers.append(
                "CURRENT_STRATEGY_V2_EXIT_REGISTRATION_INVALID_"
                f"{len(invalid)}"
            )

        baseline_candidates = self.db.query(StrategyV2ShadowTrade).filter(
            StrategyV2ShadowTrade.symbol.in_(symbols)
        ).all()
        entry_decision_ids = {
            row.entry_decision_id
            for row in baseline_candidates
            if row.entry_decision_id is not None
        }
        entry_decisions = {
            row.id: row
            for row in self.db.query(StrategyV2ShadowDecision).filter(
                StrategyV2ShadowDecision.id.in_(entry_decision_ids)
            ).all()
        } if entry_decision_ids else {}

        def baseline_is_valid(row: StrategyV2ShadowTrade) -> bool:
            if (row.symbol, row.config_version) not in configs_by_pair:
                return False
            entry_at = _as_utc(row.entry_at)
            decision = (
                entry_decisions.get(row.entry_decision_id)
                if row.entry_decision_id is not None
                else None
            )
            return bool(
                entry_at is not None
                and entry_at <= self.now
                and row.status in {"OPEN", "CLOSED"}
                and _positive_finite(row.entry_price)
                and _positive_finite(row.quantity)
                and row.estimated_fee_rate is not None
                and math.isfinite(float(row.estimated_fee_rate))
                and row.estimated_fee_rate >= 0
                and decision is not None
                and decision.action == "FILL_ENTRY"
                and decision.symbol == row.symbol
                and decision.config_version == row.config_version
                and _as_utc(decision.bar_at) == entry_at
                and _timestamp_is_after(
                    decision.observed_at,
                    decision.bar_at,
                    inclusive=True,
                )
                and not _timestamp_in_future(self.now, decision.observed_at)
            )

        current_baselines = [
            row
            for row in baseline_candidates
            if (row.symbol, row.config_version) in configs_by_pair
        ]
        eligible_baselines = [
            row for row in current_baselines if baseline_is_valid(row)
        ]
        invalid_baselines = [
            row for row in current_baselines if row not in eligible_baselines
        ]
        if invalid_baselines:
            blockers.append(
                "STRATEGY_V2_EXIT_BASELINE_INVALID_"
                f"{len(invalid_baselines)}"
            )

        required_pairs = [
            (registration, baseline)
            for registration in valid_rows
            for baseline in eligible_baselines
            if baseline.symbol == registration.symbol
            and baseline.config_version
            == registration.source_config_version
            and _timestamp_is_after(
                baseline.entry_at,
                registration.eligible_after,
                inclusive=True,
            )
        ]
        trade_rows = (
            self.db.query(StrategyV2ExitChallengerTrade).filter(
                StrategyV2ExitChallengerTrade.registration_id.in_(
                    [row.id for row in valid_rows]
                )
            ).all()
            if valid_rows
            else []
        )
        trades_by_key = {
            (row.registration_id, row.baseline_trade_id): row
            for row in trade_rows
        }

        def strategy_trade_status_is_valid(
            trade: StrategyV2ExitChallengerTrade,
            baseline: StrategyV2ShadowTrade,
            *,
            entry_at: datetime,
            updated_at: datetime,
        ) -> bool:
            challenger_values = (
                trade.challenger_exit_at,
                trade.challenger_exit_price,
                trade.challenger_gross_pnl,
                trade.challenger_estimated_fees,
                trade.challenger_net_pnl,
            )
            pair_values = (
                trade.baseline_exit_at,
                trade.baseline_exit_price,
                trade.baseline_net_pnl,
                trade.net_pnl_delta,
                trade.paired_at,
            )
            pair_marked = bool(
                any(value is not None for value in pair_values)
                or trade.baseline_exit_reason
            )
            if trade.status == "OPEN":
                return bool(
                    all(value is None for value in challenger_values)
                    and not trade.challenger_exit_reason
                    and not pair_marked
                )
            if trade.status != "CLOSED":
                return False
            challenger_exit_at = _as_utc(trade.challenger_exit_at)
            challenger_exit_price = trade.challenger_exit_price
            challenger_gross_pnl = trade.challenger_gross_pnl
            challenger_estimated_fees = trade.challenger_estimated_fees
            challenger_net_pnl = trade.challenger_net_pnl
            if (
                challenger_exit_at is None
                or challenger_exit_at < entry_at
                or challenger_exit_at > self.now
                or updated_at < challenger_exit_at
                or challenger_exit_price is None
                or not _positive_finite(challenger_exit_price)
                or not trade.challenger_exit_reason
                or challenger_gross_pnl is None
                or not _finite(challenger_gross_pnl)
                or challenger_estimated_fees is None
                or not _finite(challenger_estimated_fees)
                or challenger_estimated_fees < 0
                or challenger_net_pnl is None
                or not _finite(challenger_net_pnl)
            ):
                return False
            expected_gross = (
                challenger_exit_price - trade.entry_price
            ) * trade.quantity
            expected_fees = (
                (trade.entry_price + challenger_exit_price)
                * trade.quantity
                * trade.estimated_fee_rate
            )
            if (
                not math.isclose(
                    challenger_gross_pnl,
                    expected_gross,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    challenger_estimated_fees,
                    expected_fees,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    challenger_net_pnl,
                    expected_gross - expected_fees,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            ):
                return False
            if not pair_marked:
                return True
            if any(value is None for value in pair_values):
                return False
            baseline_exit_price = trade.baseline_exit_price
            baseline_net_pnl = trade.baseline_net_pnl
            net_pnl_delta = trade.net_pnl_delta
            if (
                baseline_exit_price is None
                or baseline_net_pnl is None
                or net_pnl_delta is None
            ):
                return False
            baseline_exit_at = _as_utc(trade.baseline_exit_at)
            paired_at = _as_utc(trade.paired_at)
            expected_baseline_exit_at = _as_utc(baseline.exit_at)
            if (
                baseline.status != "CLOSED"
                or expected_baseline_exit_at is None
                or baseline.exit_price is None
                or baseline.net_pnl is None
                or baseline_exit_at is None
                or paired_at is None
                or baseline_exit_at < entry_at
                or baseline_exit_at > self.now
                or paired_at < baseline_exit_at
                or paired_at < challenger_exit_at
                or paired_at > self.now
                or updated_at < paired_at
                or not _positive_finite(baseline_exit_price)
                or not _finite(baseline_net_pnl)
                or not _finite(net_pnl_delta)
                or not trade.baseline_exit_reason
            ):
                return False
            return bool(
                baseline_exit_at == expected_baseline_exit_at
                and math.isclose(
                    baseline_exit_price,
                    baseline.exit_price,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and trade.baseline_exit_reason == baseline.exit_reason
                and math.isclose(
                    baseline_net_pnl,
                    baseline.net_pnl,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and math.isclose(
                    net_pnl_delta,
                    challenger_net_pnl - baseline_net_pnl,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            )

        valid_evidence_keys: set[tuple[int, int]] = set()
        invalid_evidence: list[StrategyV2ExitChallengerTrade] = []
        for registration, baseline in required_pairs:
            key = (registration.id, baseline.id)
            trade = trades_by_key.get(key)
            if trade is None:
                continue
            entry_at = _as_utc(trade.entry_at)
            baseline_entry_at = _as_utc(baseline.entry_at)
            last_bar_at = _as_utc(trade.last_bar_at)
            updated_at = _as_utc(trade.updated_at)
            expected_fee_rate = baseline.estimated_fee_rate
            if (
                entry_at is None
                or baseline_entry_at is None
                or last_bar_at is None
                or updated_at is None
                or trade.status not in {"OPEN", "CLOSED"}
                or trade.symbol != baseline.symbol
                or trade.source_config_version != baseline.config_version
                or entry_at != baseline_entry_at
                or not _positive_finite(trade.entry_price)
                or not _positive_finite(trade.quantity)
                or not math.isclose(
                    trade.entry_price,
                    baseline.entry_price,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    trade.quantity,
                    baseline.quantity,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or expected_fee_rate is None
                or not _finite(trade.estimated_fee_rate)
                or trade.estimated_fee_rate < 0
                or not math.isclose(
                    trade.estimated_fee_rate,
                    expected_fee_rate,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or last_bar_at < baseline_entry_at - timedelta(microseconds=1)
                or last_bar_at > self.now
                or updated_at < entry_at
                or updated_at < last_bar_at
                or updated_at > self.now
                or not strategy_trade_status_is_valid(
                    trade,
                    baseline,
                    entry_at=entry_at,
                    updated_at=updated_at,
                )
            ):
                invalid_evidence.append(trade)
                continue
            valid_evidence_keys.add(key)

        required_keys = {
            (registration.id, baseline.id)
            for registration, baseline in required_pairs
        }
        missing_evidence = required_keys - set(trades_by_key)
        unexpected_evidence = set(trades_by_key) - required_keys
        if invalid_evidence:
            blockers.append(
                "STRATEGY_V2_EXIT_EVIDENCE_INVALID_"
                f"{len(invalid_evidence)}"
            )
        if missing_evidence:
            blockers.append(
                "STRATEGY_V2_EXIT_EVIDENCE_MISSING_"
                f"{len(missing_evidence)}"
            )
        if unexpected_evidence:
            blockers.append(
                "STRATEGY_V2_EXIT_EVIDENCE_UNEXPECTED_"
                f"{len(unexpected_evidence)}"
            )

        if required_pairs:
            expected_count = len(required_keys)
            observed_count = len(valid_evidence_keys)
            latest_at = max(
                _utc_datetimes(
                    trades_by_key[key].updated_at
                    for key in valid_evidence_keys
                ),
                default=latest_at,
            )
        else:
            expected_count = len(expected_pairs)
            observed_count = len(registered_pairs)
        return UniverseObservationHealthComponent(
            name="STRATEGY_V2_EXIT_CHALLENGER",
            status="DEGRADED" if blockers else "HEALTHY",
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            observed_count=observed_count,
            expected_count=expected_count,
            coverage_ratio=_bounded_ratio(observed_count, expected_count),
            blockers=blockers,
        )

    def _opening_current_session(
        self,
    ) -> tuple[date, datetime] | None:
        session = get_session("US")
        local = session.local(self.now)
        current_day = local.date()
        if (
            local.weekday() < 5
            and not is_market_closed("US", current_day)
        ):
            session_open = datetime.combine(
                current_day,
                session.rth_open,
                tzinfo=session.timezone,
            ).astimezone(timezone.utc)
            return current_day, session_open
        return None

    def _opening_shadow_component(
        self,
    ) -> UniverseObservationHealthComponent:
        if not settings.opening_momentum_shadow_enabled:
            return UniverseObservationHealthComponent(
                name="OPENING_MOMENTUM_SHADOW",
                status="DISABLED",
            )
        observer = OpeningMomentumShadowService(self.db)
        current_session = self._opening_current_session()
        expected: date
        if current_session is not None:
            current_day, session_open = current_session
            current_variants = observer._universe_variants(
                session_date=current_day,
                completed_before=session_open,
            )
            due_variants = [
                variant
                for variant in current_variants
                if (
                    variant.forward_evidence_start_date is None
                    or current_day >= variant.forward_evidence_start_date
                )
                and self.now
                >= observer._variant_entry_at(
                    variant,
                    session_open=session_open,
                )
                + timedelta(minutes=1)
                + _OPENING_SETTLEMENT_GRACE
            ]
        else:
            due_variants = []
        if due_variants and current_session is not None:
            expected = current_session[0]
        else:
            expected = latest_closed_session_date(
                market="US",
                now=self.now,
            )
            expected_open = _session_open_at("US", expected)
            due_variants = [
                variant
                for variant in observer._universe_variants(
                    session_date=expected,
                    completed_before=expected_open,
                )
                if variant.forward_evidence_start_date is None
                or expected >= variant.forward_evidence_start_date
            ]
        expected_by_version = {
            variant.config_version: variant for variant in due_variants
        }
        all_current_versions = {
            variant.config_version
            for variant in observer._variant_identities()
        }
        rows = self.db.query(OpeningMomentumShadowRun).filter(
            OpeningMomentumShadowRun.session_date == expected,
            OpeningMomentumShadowRun.config_version.in_(
                tuple(expected_by_version)
            ),
        ).all()
        current_day = get_session("US").local(self.now).date()
        future_rows = self.db.query(OpeningMomentumShadowRun).filter(
            OpeningMomentumShadowRun.config_version.in_(
                tuple(all_current_versions)
            ),
            OpeningMomentumShadowRun.session_date > current_day,
        ).all()
        future_rows.extend([
            row
            for row in rows
            if _timestamp_in_future(self.now, row.signal_at)
            or _timestamp_in_future(self.now, row.observed_at)
            or _timestamp_in_future(self.now, row.updated_at)
        ])
        future_row_ids = {row.id for row in future_rows}

        def row_matches_variant(row: OpeningMomentumShadowRun) -> bool:
            variant = expected_by_version.get(row.config_version)
            if variant is None or row.id in future_row_ids:
                return False
            session_open = _session_open_at("US", row.session_date)
            expected_entry = observer._variant_entry_at(
                variant,
                session_open=session_open,
            )
            expected_exit_due = expected_entry + timedelta(
                minutes=variant.decision_config.holding_minutes
            )
            if (
                row.algorithm_version != variant.algorithm_version
                or row.universe_source != variant.universe_source
                or row.selection_run_id != variant.selection_run_id
                or row.status not in {"SKIPPED", "OPEN", "CLOSED"}
                or _as_utc(row.signal_at)
                != observer._variant_signal_at(
                    variant,
                    session_open=session_open,
                )
                or not observer._variant_decision_due(
                    variant,
                    session_open=session_open,
                    current=(
                        _as_utc(row.observed_at)
                        or row.observed_at.replace(tzinfo=timezone.utc)
                    ),
                )
                or not _timestamp_is_after(
                    row.updated_at,
                    row.observed_at,
                    inclusive=True,
                )
                or not math.isfinite(float(row.estimated_cost_bps))
                or row.estimated_cost_bps < 0
            ):
                return False
            outcome_fields = (
                row.exit_price,
                row.gross_return_bps,
                row.net_return_bps,
                row.maximum_adverse_excursion_bps,
                row.maximum_favorable_excursion_bps,
            )
            if row.status == "SKIPPED":
                return (
                    row.entry_at is None
                    and row.entry_price is None
                    and row.exit_due_at is None
                    and row.exit_at is None
                    and all(value is None for value in outcome_fields)
                )
            if (
                not str(row.candidate_symbol or "").strip()
                or not _positive_finite(row.entry_price)
                or _as_utc(row.entry_at) != expected_entry
                or _as_utc(row.exit_due_at) != expected_exit_due
            ):
                return False
            if row.status == "OPEN":
                return (
                    row.exit_at is None
                    and all(value is None for value in outcome_fields)
                )
            return (
                row.exit_at is not None
                and _positive_finite(row.exit_price)
                and _finite(row.gross_return_bps)
                and _finite(row.net_return_bps)
                and _finite(row.maximum_adverse_excursion_bps)
                and _finite(row.maximum_favorable_excursion_bps)
                and _timestamp_is_after(
                    row.exit_at,
                    row.entry_at,
                    inclusive=True,
                )
                and _timestamp_is_after(
                    row.exit_due_at,
                    row.exit_at,
                    inclusive=True,
                )
                and _timestamp_is_after(
                    row.updated_at,
                    expected_exit_due
                    + timedelta(minutes=1)
                    + _OPENING_SETTLEMENT_GRACE,
                    inclusive=True,
                )
            )

        open_rows = self.db.query(OpeningMomentumShadowRun).filter(
            OpeningMomentumShadowRun.status == "OPEN"
        ).all()

        def opening_row_is_stranded(
            row: OpeningMomentumShadowRun,
        ) -> bool:
            if (
                not str(row.candidate_symbol or "").strip()
                or row.entry_at is None
                or not _positive_finite(row.entry_price)
                or row.exit_due_at is None
            ):
                return True
            exit_due_at = _as_utc(row.exit_due_at)
            return (
                exit_due_at is not None
                and self.now
                >= exit_due_at + timedelta(minutes=1)
                + _OPENING_SETTLEMENT_GRACE
            )

        stranded_rows = [
            row
            for row in open_rows
            if opening_row_is_stranded(row)
        ]
        stranded_row_ids = {row.id for row in stranded_rows}
        valid_rows = [
            row
            for row in rows
            if row_matches_variant(row) and row.id not in stranded_row_ids
        ]
        observed_versions = {row.config_version for row in valid_rows}
        missing = set(expected_by_version) - observed_versions
        invalid = [
            row
            for row in rows
            if row.config_version not in observed_versions
            and row.id not in future_row_ids
        ]
        blockers: list[str] = []
        if missing:
            blockers.append(f"CURRENT_SHADOW_VARIANT_MISSING_{len(missing)}")
        if invalid:
            blockers.append(f"CURRENT_SHADOW_VARIANT_INVALID_{len(invalid)}")
        if future_rows:
            blockers.append(
                "OPENING_SHADOW_EVIDENCE_IN_FUTURE_"
                f"{len(future_row_ids)}"
            )
        if stranded_rows:
            blockers.append(
                f"OPENING_SHADOW_OPEN_STRANDED_{len(stranded_rows)}"
            )
        latest_at = max(
            _utc_datetimes(row.updated_at for row in valid_rows),
            default=None,
        )
        return UniverseObservationHealthComponent(
            name="OPENING_MOMENTUM_SHADOW",
            status="DEGRADED" if blockers else "HEALTHY",
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            latest_session_date=(expected if valid_rows else None),
            expected_session_date=expected,
            observed_count=len(observed_versions),
            expected_count=(
                len(expected_by_version)
                + len({
                    row.id
                    for row in stranded_rows
                    if row.session_date != expected
                    or row.config_version not in expected_by_version
                })
            ),
            coverage_ratio=_bounded_ratio(
                len(observed_versions),
                len(expected_by_version)
                + len({
                    row.id
                    for row in stranded_rows
                    if row.session_date != expected
                    or row.config_version not in expected_by_version
                }),
            ),
            blockers=blockers,
        )

    def _opening_execution_component(
        self,
    ) -> UniverseObservationHealthComponent:
        if not settings.opening_momentum_execution_enabled:
            return UniverseObservationHealthComponent(
                name="OPENING_MOMENTUM_EXECUTION",
                status="DISABLED",
            )
        observer = OpeningMomentumShadowService(self.db)
        identity = observer.paper_execution_variant_identity()
        current_session = self._opening_current_session()
        expected = latest_closed_session_date(market="US", now=self.now)
        if current_session is not None:
            current_day, session_open = current_session
            signal_ready_at = session_open + timedelta(
                minutes=identity.decision_config.signal_minutes
            ) + _OPENING_SETTLEMENT_GRACE
            if (
                self.now >= signal_ready_at
                and (
                    identity.forward_evidence_start_date is None
                    or current_day >= identity.forward_evidence_start_date
                )
            ):
                expected = current_day
        if (
            identity.forward_evidence_start_date is not None
            and expected < identity.forward_evidence_start_date
        ):
            return UniverseObservationHealthComponent(
                name="OPENING_MOMENTUM_EXECUTION",
                status="HEALTHY",
            )
        session_open = _session_open_at("US", expected)
        variant = observer.paper_execution_variant(
            session_date=expected,
            completed_before=session_open,
        )
        row = self.db.query(OpeningMomentumExecution).filter(
            OpeningMomentumExecution.session_date == expected
        ).order_by(OpeningMomentumExecution.id.desc()).first()
        current_day = get_session("US").local(self.now).date()
        future_rows = self.db.query(OpeningMomentumExecution).filter(
            OpeningMomentumExecution.session_date > current_day,
        ).all()
        valid_identity = bool(
            row is not None
            and variant is not None
            and row.config_version == variant.config_version
            and row.algorithm_version == variant.algorithm_version
            and row.universe_source == variant.universe_source
            and row.selection_run_id == variant.selection_run_id
        )
        blockers: list[str] = []
        if variant is None:
            blockers.append("CURRENT_EXECUTION_VARIANT_UNAVAILABLE")
        if row is None:
            blockers.append("CURRENT_EXECUTION_EVIDENCE_MISSING")
        elif not valid_identity:
            blockers.append("CURRENT_EXECUTION_EVIDENCE_INVALID")

        supported_statuses = {
            "SKIPPED",
            "ARMED",
            "SUBMITTING",
            "SUBMITTED",
            "OPEN",
            "EXITING",
            "CLOSED",
            "FAILED",
            "REJECTED",
            "UNCERTAIN",
            "EXPIRED",
        }
        row_timing_valid = False
        if row is not None and variant is not None and valid_identity:
            signal_at, entry_due_at, entry_deadline_at = (
                OpeningMomentumExecutionService._session_entry_schedule(
                    variant,
                    session_date=expected,
                )
            )
            signal_ready_at = session_open + timedelta(
                minutes=variant.decision_config.signal_minutes
            ) + _OPENING_SETTLEMENT_GRACE
            timestamps = (
                row.signal_at,
                row.armed_at,
                row.requested_at,
                row.entry_filled_at,
                row.exit_filled_at,
                row.updated_at,
            )
            timestamps_not_future = not any(
                _timestamp_in_future(self.now, value)
                for value in timestamps
            )
            missed_window = row.reason in {
                "ENTRY_WINDOW_MISSED",
                "PREOPEN_UNIVERSE_UNAVAILABLE",
            }
            armed_causal = (
                _timestamp_is_after(
                    row.armed_at,
                    signal_ready_at,
                    inclusive=True,
                )
                and (
                    missed_window
                    or _timestamp_is_after(
                        entry_deadline_at,
                        row.armed_at,
                        inclusive=True,
                    )
                )
            )
            requested_causal = (
                row.requested_at is None
                or (
                    _timestamp_is_after(
                        row.requested_at,
                        row.armed_at,
                        inclusive=True,
                    )
                    and _timestamp_is_after(
                        row.requested_at,
                        entry_due_at,
                        inclusive=True,
                    )
                    and _timestamp_is_after(
                        entry_deadline_at,
                        row.requested_at,
                        inclusive=True,
                    )
                )
            )
            fill_anchor = row.requested_at or row.armed_at
            entry_fill_causal = (
                row.entry_filled_at is None
                or (
                    _timestamp_is_after(
                        row.entry_filled_at,
                        fill_anchor,
                        inclusive=True,
                    )
                    and _timestamp_is_after(
                        row.updated_at,
                        row.entry_filled_at,
                        inclusive=True,
                    )
                )
            )
            exit_fill_causal = (
                row.exit_filled_at is None
                or (
                    row.entry_filled_at is not None
                    and _timestamp_is_after(
                        row.exit_filled_at,
                        row.entry_filled_at,
                        inclusive=True,
                    )
                    and _timestamp_is_after(
                        row.updated_at,
                        row.exit_filled_at,
                        inclusive=True,
                    )
                )
            )
            entry_signal_valid = bool(
                str(row.symbol or "").strip()
                and _positive_finite(row.reference_entry_price)
            )
            frozen_config_valid = bool(
                row.max_holding_minutes
                == variant.decision_config.holding_minutes
                and math.isclose(
                    row.stop_loss_pct,
                    float(variant.decision_config.stop_loss_pct or 0),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and math.isclose(
                    row.max_price_deviation_bps,
                    float(
                        settings.opening_momentum_execution_max_price_deviation_bps
                    ),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            )
            status_fields_valid = bool(
                frozen_config_valid
                and (
                    _finite(row.net_pnl)
                    if row.status == "CLOSED"
                    else row.net_pnl is None
                )
            )
            if row.status == "SKIPPED":
                status_fields_valid = bool(
                    status_fields_valid
                    and row.submit_attempts == 0
                    and row.reference_entry_price is None
                    and row.requested_at is None
                    and not row.entry_order_id
                    and not row.exit_order_id
                    and row.entry_filled_at is None
                    and row.entry_price is None
                    and row.quantity is None
                    and row.exit_filled_at is None
                    and row.exit_price is None
                )
            elif row.status == "ARMED":
                status_fields_valid = bool(
                    status_fields_valid
                    and entry_signal_valid
                    and (
                        (
                            row.requested_at is None
                            and row.submit_attempts == 0
                        )
                        or (
                            row.requested_at is not None
                            and row.submit_attempts >= 1
                        )
                    )
                    and not row.entry_order_id
                    and not row.exit_order_id
                    and row.entry_filled_at is None
                    and row.entry_price is None
                    and row.quantity is None
                    and row.exit_filled_at is None
                    and row.exit_price is None
                )
            elif row.status == "SUBMITTING":
                status_fields_valid = bool(
                    status_fields_valid
                    and entry_signal_valid
                    and row.requested_at is not None
                    and row.submit_attempts >= 1
                    and not row.exit_order_id
                    and row.entry_filled_at is None
                    and row.entry_price is None
                    and row.quantity is None
                    and row.exit_filled_at is None
                    and row.exit_price is None
                )
            elif row.status == "SUBMITTED":
                status_fields_valid = bool(
                    status_fields_valid
                    and entry_signal_valid
                    and row.requested_at is not None
                    and row.submit_attempts >= 1
                    and bool(row.entry_order_id)
                    and not row.exit_order_id
                    and row.entry_filled_at is None
                    and row.entry_price is None
                    and row.quantity is None
                    and row.exit_filled_at is None
                    and row.exit_price is None
                )
            elif row.status in {"OPEN", "EXITING"}:
                status_fields_valid = bool(
                    status_fields_valid
                    and entry_signal_valid
                    and row.requested_at is not None
                    and row.submit_attempts >= 1
                    and bool(row.entry_order_id)
                    and row.entry_filled_at is not None
                    and _positive_finite(row.entry_price)
                    and _positive_finite(row.quantity)
                    and (row.status != "OPEN" or not row.exit_order_id)
                    and row.exit_filled_at is None
                    and row.exit_price is None
                )
            elif row.status == "CLOSED":
                status_fields_valid = bool(
                    status_fields_valid
                    and entry_signal_valid
                    and row.requested_at is not None
                    and row.submit_attempts >= 1
                    and bool(row.entry_order_id)
                    and row.entry_filled_at is not None
                    and _positive_finite(row.entry_price)
                    and _positive_finite(row.quantity)
                    and bool(row.exit_order_id)
                    and row.exit_filled_at is not None
                    and _positive_finite(row.exit_price)
                )
            row_timing_valid = bool(
                timestamps_not_future
                and _as_utc(row.signal_at) == signal_at
                and _as_utc(row.entry_due_at) == entry_due_at
                and _as_utc(row.entry_deadline_at) == entry_deadline_at
                and armed_causal
                and _timestamp_is_after(
                    row.updated_at,
                    row.armed_at,
                    inclusive=True,
                )
                and requested_causal
                and entry_fill_causal
                and exit_fill_causal
                and status_fields_valid
            )
            if not timestamps_not_future:
                blockers.append("OPENING_EXECUTION_TIMESTAMP_IN_FUTURE")
            elif not row_timing_valid:
                blockers.append("OPENING_EXECUTION_TIMING_INVALID")
            if row.status in {"FAILED", "REJECTED", "UNCERTAIN", "EXPIRED"}:
                blockers.append(f"OPENING_EXECUTION_{row.status}")
            elif row.status not in supported_statuses:
                blockers.append(
                    f"OPENING_EXECUTION_STATUS_INVALID_{row.status}"
                )

        active_statuses = {
            "ARMED",
            "SUBMITTING",
            "SUBMITTED",
            "OPEN",
            "EXITING",
            "UNCERTAIN",
        }
        active_rows = self.db.query(OpeningMomentumExecution).filter(
            OpeningMomentumExecution.status.in_(active_statuses)
        ).all()

        def execution_row_is_stranded(
            active: OpeningMomentumExecution,
        ) -> bool:
            if (
                active.session_date != expected
                or variant is None
                or active.algorithm_version != variant.algorithm_version
                or active.config_version != variant.config_version
                or active.universe_source != variant.universe_source
                or active.selection_run_id != variant.selection_run_id
            ):
                return True
            deadline = _as_utc(active.entry_deadline_at)
            updated = _as_utc(active.updated_at)
            if deadline is None or updated is None:
                return True
            if active.status == "UNCERTAIN":
                return True
            if active.status == "ARMED":
                return self.now > deadline
            if active.status == "SUBMITTING":
                return self.now > (
                    deadline + _OPENING_EXECUTION_STATE_GRACE
                )
            if active.status == "SUBMITTED":
                return (
                    self.now
                    > deadline + _OPENING_EXECUTION_STATE_GRACE
                )
            if active.status == "OPEN":
                entry_filled_at = _as_utc(active.entry_filled_at)
                if entry_filled_at is None:
                    return (
                        self.now
                        > updated + _OPENING_EXECUTION_STATE_GRACE
                    )
                return self.now > (
                    entry_filled_at
                    + timedelta(minutes=active.max_holding_minutes)
                    + _OPENING_EXECUTION_STATE_GRACE
                )
            if active.status == "EXITING":
                return self.now > updated + _OPENING_EXECUTION_STATE_GRACE
            return False

        stranded_rows = [
            active for active in active_rows if execution_row_is_stranded(active)
        ]
        if stranded_rows:
            blockers.append(
                f"OPENING_EXECUTION_ACTIVE_STATE_STRANDED_{len(stranded_rows)}"
            )
        if future_rows:
            blockers.append(
                f"OPENING_EXECUTION_FUTURE_SESSION_{len(future_rows)}"
            )
        latest_at = row.updated_at if row is not None else None
        old_stranded_count = sum(
            active.id != (row.id if row is not None else None)
            for active in stranded_rows
        )
        evidence_valid = bool(
            valid_identity
            and row_timing_valid
            and row is not None
            and row.status in {"SKIPPED", "ARMED", "SUBMITTING", "SUBMITTED", "OPEN", "EXITING", "CLOSED"}
            and row not in stranded_rows
            and not future_rows
        )
        observed_count = 1 if evidence_valid and not blockers else 0
        expected_count = 1 + old_stranded_count
        return UniverseObservationHealthComponent(
            name="OPENING_MOMENTUM_EXECUTION",
            status="DEGRADED" if blockers else "HEALTHY",
            latest_at=latest_at,
            age_seconds=_age_seconds(self.now, latest_at),
            latest_session_date=(
                row.session_date
                if evidence_valid and row is not None
                else None
            ),
            expected_session_date=expected,
            observed_count=observed_count,
            expected_count=expected_count,
            coverage_ratio=_bounded_ratio(observed_count, expected_count),
            blockers=blockers,
        )
