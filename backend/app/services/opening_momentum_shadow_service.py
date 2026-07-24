from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Protocol, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import get_session, is_trading_hours
from app.domain.opening_momentum import (
    ALGORITHM_VERSION,
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
    shadow_round_trip_return_bps,
)
from app.domain.opening_momentum_universe import (
    OPENING_CONTINUATION_UNIVERSE_VERSION,
    OpeningMomentumUniverseCandidate,
    OpeningMomentumUniverseConfig,
    opening_momentum_variant_config_version,
    select_opening_momentum_universe,
)
from app.models import (
    OpeningMomentumShadowRun,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.schemas import (
    OpeningMomentumRankResponse,
    OpeningMomentumShadowConfigResponse,
    OpeningMomentumShadowMetrics,
    OpeningMomentumShadowRunResponse,
    OpeningMomentumShadowStatusResponse,
    OpeningMomentumShadowVariantResponse,
)


logger = logging.getLogger("auto_trade.opening_momentum_shadow")

_CANDLE_COUNT = 500
_BAR_DURATION = timedelta(minutes=1)
_SETTLEMENT_GRACE = timedelta(seconds=5)
_DECISION_WINDOW = timedelta(minutes=5)
_INCUMBENT_SOURCE = "UNIVERSE_SELECTION"
_CONTINUATION_SOURCE = "OPENING_CONTINUATION"
_CONTINUATION_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{OPENING_CONTINUATION_UNIVERSE_VERSION}"
)
_BREADTH_GATE_VERSION = "nonnegative-market-breadth-v1"
_BREADTH_GATE_SOURCE = "OPENING_CONTINUATION_BREADTH_GATE"
_BREADTH_GATE_ALGORITHM_VERSION = (
    f"{_CONTINUATION_ALGORITHM_VERSION}+{_BREADTH_GATE_VERSION}"
)

_VariantName = Literal[
    "INCUMBENT",
    "CONTINUATION_CHALLENGER",
    "BREADTH_GATED_CHALLENGER",
]


class CandleProvider(Protocol):
    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[Any]: ...


@dataclass(frozen=True)
class _Candle:
    timestamp: datetime
    open: float
    close: float


@dataclass(frozen=True)
class _UniverseVariant:
    variant: _VariantName
    algorithm_version: str
    config_version: str
    universe_source: str
    decision_config: OpeningMomentumConfig
    symbols: tuple[str, ...] = ()
    selection_run_id: int | None = None


class OpeningMomentumShadowService:
    """Collect one daily cross-sectional observation without placing orders."""

    def __init__(
        self,
        db: Session,
        candle_provider: CandleProvider | None = None,
        *,
        config: OpeningMomentumConfig | None = None,
    ) -> None:
        self.db = db
        self.candle_provider = candle_provider
        self.config = config or OpeningMomentumConfig()

    def tick(
        self,
        *,
        now: datetime | None = None,
    ) -> OpeningMomentumShadowStatusResponse:
        current = _as_utc(now or datetime.now(timezone.utc))
        open_runs = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(OpeningMomentumShadowRun.status == "OPEN")
            .order_by(
                OpeningMomentumShadowRun.session_date.asc(),
                OpeningMomentumShadowRun.id.asc(),
            )
            .all()
        )
        for open_run in open_runs:
            self._close_if_due(open_run, current)
        if any(open_run.status == "OPEN" for open_run in open_runs):
            return self.get_status()

        if not settings.opening_momentum_shadow_enabled:
            return self.get_status()
        if not is_trading_hours("US", current):
            return self.get_status()

        session = get_session("US")
        local = session.local(current)
        session_open = datetime.combine(
            local.date(),
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        entry_at = session_open + timedelta(
            minutes=(
                self.config.signal_minutes
                + self.config.execution_delay_minutes
            )
        )
        decision_start = entry_at + _BAR_DURATION + _SETTLEMENT_GRACE
        decision_end = decision_start + _DECISION_WINDOW
        if current < decision_start or current > decision_end:
            return self.get_status()

        variants = self._universe_variants(
            session_date=local.date(),
            completed_before=session_open,
        )
        variant_versions = [
            variant.config_version for variant in variants
        ]
        existing_versions = {
            row.config_version
            for row in (
                self.db.query(OpeningMomentumShadowRun)
                .filter(
                    OpeningMomentumShadowRun.session_date
                    == local.date(),
                    OpeningMomentumShadowRun.config_version.in_(
                        variant_versions
                    ),
                )
                .all()
            )
        }
        pending_variants = [
            variant
            for variant in variants
            if variant.config_version not in existing_versions
        ]
        if not pending_variants:
            return self.get_status()
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )

        observations_by_symbol: dict[
            str, OpeningMomentumObservation
        ] = {}
        fetch_errors: dict[str, str] = {}
        signal_at = (
            session_open
            + timedelta(minutes=self.config.signal_minutes)
            - _BAR_DURATION
        )
        expected_signal_bars = {
            session_open + timedelta(minutes=index)
            for index in range(self.config.signal_minutes)
        }
        symbols = tuple(
            dict.fromkeys(
                symbol
                for variant in pending_variants
                for symbol in variant.symbols
            )
        )
        for symbol in symbols:
            try:
                bars = self.candle_provider.get_candlesticks(
                    symbol,
                    "MIN_1",
                    _CANDLE_COUNT,
                )
                by_timestamp = {
                    bar.timestamp: bar
                    for bar in self._coerce_candles(bars)
                }
                missing_signal_bars = (
                    expected_signal_bars - by_timestamp.keys()
                )
                if missing_signal_bars:
                    fetch_errors[symbol] = (
                        "SIGNAL_BARS_MISSING:"
                        f"{len(missing_signal_bars)}"
                    )
                    continue
                opening_bar = by_timestamp.get(session_open)
                signal_bar = by_timestamp.get(signal_at)
                entry_bar = by_timestamp.get(entry_at)
                if opening_bar is None or signal_bar is None:
                    fetch_errors[symbol] = "SIGNAL_BARS_MISSING"
                    continue
                observations_by_symbol[symbol] = (
                    OpeningMomentumObservation(
                        symbol=symbol,
                        session_open=opening_bar.open,
                        signal_close=signal_bar.close,
                        entry_open=(
                            entry_bar.open
                            if entry_bar is not None
                            else None
                        ),
                    )
                )
            except Exception as exc:
                fetch_errors[symbol] = (
                    f"BROKER_ERROR:{type(exc).__name__}"
                )
                logger.warning(
                    "opening momentum candle fetch failed for %s: %s",
                    symbol,
                    exc,
                )

        for variant in pending_variants:
            observations = [
                observations_by_symbol[symbol]
                for symbol in variant.symbols
                if symbol in observations_by_symbol
            ]
            excluded = {
                symbol: fetch_errors[symbol]
                for symbol in variant.symbols
                if symbol in fetch_errors
            }
            decision = evaluate_opening_momentum(
                observations,
                variant.decision_config,
            )
            data_complete = not excluded
            status = (
                "OPEN"
                if decision.action == "ENTER_LONG" and data_complete
                else "SKIPPED"
            )
            if variant.selection_run_id is None:
                reason = "PREOPEN_UNIVERSE_UNAVAILABLE"
            elif not data_complete:
                reason = "DATA_INCOMPLETE"
            else:
                reason = decision.reason
            self.db.add(
                OpeningMomentumShadowRun(
                    session_date=local.date(),
                    algorithm_version=variant.algorithm_version,
                    config_version=variant.config_version,
                    status=status,
                    reason=reason,
                    signal_at=signal_at,
                    observed_at=current,
                    selection_run_id=variant.selection_run_id,
                    universe_source=variant.universe_source,
                    universe_size=decision.universe_size,
                    universe_json=json.dumps(
                        variant.symbols,
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    excluded_symbols_json=json.dumps(
                        excluded,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    ranking_json=json.dumps(
                        [
                            asdict(item)
                            for item in decision.ranking
                        ],
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    candidate_symbol=decision.candidate_symbol,
                    market_return_bps=decision.market_return_bps,
                    candidate_return_bps=(
                        decision.candidate_return_bps
                    ),
                    excess_return_bps=decision.excess_return_bps,
                    entry_at=(
                        entry_at
                        if status == "OPEN"
                        else None
                    ),
                    entry_price=(
                        decision.entry_price
                        if status == "OPEN"
                        else None
                    ),
                    exit_due_at=(
                        entry_at
                        + timedelta(
                            minutes=self.config.holding_minutes
                        )
                        if status == "OPEN"
                        else None
                    ),
                    estimated_cost_bps=(
                        variant.decision_config.round_trip_cost_bps
                    ),
                )
            )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
        return self.get_status()

    def get_status(self) -> OpeningMomentumShadowStatusResponse:
        incumbent_version = self.config.version_hash()
        latest = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(
                OpeningMomentumShadowRun.config_version
                == incumbent_version
            )
            .order_by(
                OpeningMomentumShadowRun.session_date.desc(),
                OpeningMomentumShadowRun.id.desc(),
            )
            .first()
        )
        has_open_run = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(
                OpeningMomentumShadowRun.status == "OPEN"
            )
            .first()
            is not None
        )
        if has_open_run:
            state = "OPEN"
        elif not settings.opening_momentum_shadow_enabled:
            state = "DISABLED"
        elif latest is None:
            state = "WAITING"
        else:
            state = "COLLECTING"
        return OpeningMomentumShadowStatusResponse(
            config=self._config_response(),
            state=state,
            latest=self._run_response(latest) if latest else None,
            metrics=self._metrics(incumbent_version),
            variants=self._variant_responses(),
        )

    def list_runs(
        self,
        *,
        limit: int = 100,
    ) -> list[OpeningMomentumShadowRunResponse]:
        rows = (
            self.db.query(OpeningMomentumShadowRun)
            .order_by(
                OpeningMomentumShadowRun.session_date.desc(),
                OpeningMomentumShadowRun.id.desc(),
            )
            .limit(limit)
            .all()
        )
        return [self._run_response(row) for row in rows]

    def _universe_variants(
        self,
        *,
        session_date: date | None = None,
        completed_before: datetime | None = None,
    ) -> list[_UniverseVariant]:
        identities = self._variant_identities()
        run_query = self.db.query(UniverseSelectionRun).filter(
            UniverseSelectionRun.status == "COMPLETE",
        )
        if session_date is not None:
            run_query = run_query.filter(
                UniverseSelectionRun.as_of_date < session_date,
            )
        if completed_before is not None:
            cutoff = _as_utc(completed_before)
            run_query = run_query.filter(
                UniverseSelectionRun.completed_at.is_not(None),
                UniverseSelectionRun.completed_at <= cutoff,
            )
        run = run_query.order_by(
            UniverseSelectionRun.as_of_date.desc(),
            UniverseSelectionRun.completed_at.desc(),
            UniverseSelectionRun.created_at.desc(),
            UniverseSelectionRun.id.desc(),
        ).first()
        if run is None:
            return [
                _UniverseVariant(
                    variant=identity.variant,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    universe_source="NONE",
                    decision_config=identity.decision_config,
                )
                for identity in identities
            ]
        candidates = (
            self.db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.market == "US",
            )
            .order_by(
                UniverseSelectionCandidate.selected.desc(),
                UniverseSelectionCandidate.rank.asc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.symbol.asc(),
            )
            .all()
        )
        incumbent_symbols = tuple(
            dict.fromkeys(
                row.symbol for row in candidates if row.selected
            )
        )
        variants = [
            _UniverseVariant(
                variant="INCUMBENT",
                algorithm_version=ALGORITHM_VERSION,
                config_version=self.config.version_hash(),
                universe_source=_INCUMBENT_SOURCE,
                decision_config=self.config,
                symbols=incumbent_symbols,
                selection_run_id=run.id,
            )
        ]
        if not settings.opening_momentum_challenger_enabled:
            return variants

        challenger_candidates = [
            OpeningMomentumUniverseCandidate(
                symbol=row.symbol,
                sector=row.sector,
                avg_dollar_volume=_optional_metric(
                    row.metrics_json,
                    "avg_dollar_volume",
                ),
                relative_spread_bps=_optional_metric(
                    row.metrics_json,
                    "relative_spread_bps",
                ),
                opportunity_to_cost_ratio=_optional_metric(
                    row.metrics_json,
                    "opportunity_to_cost_ratio",
                ),
                momentum_5d_pct=_optional_metric(
                    row.metrics_json,
                    "momentum_5d_pct",
                ),
                trend_efficiency_10d=_optional_metric(
                    row.metrics_json,
                    "trend_efficiency_10d",
                ),
                exclusion_reasons=_json_string_tuple(
                    row.exclusion_reasons_json
                ),
            )
            for row in candidates
        ]
        challenger_selection = select_opening_momentum_universe(
            challenger_candidates,
            self._continuation_config(),
        )
        identities_by_variant = {
            identity.variant: identity for identity in identities
        }
        challenger_symbols = tuple(
            row.symbol
            for row in challenger_selection
            if row.selected
        )
        for variant_name in (
            "CONTINUATION_CHALLENGER",
            "BREADTH_GATED_CHALLENGER",
        ):
            identity = identities_by_variant[variant_name]
            variants.append(
                _UniverseVariant(
                    variant=identity.variant,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    universe_source=identity.universe_source,
                    decision_config=identity.decision_config,
                    symbols=challenger_symbols,
                    selection_run_id=run.id,
                )
            )
        return variants

    def _variant_identities(self) -> list[_UniverseVariant]:
        variants = [
            _UniverseVariant(
                variant="INCUMBENT",
                algorithm_version=ALGORITHM_VERSION,
                config_version=self.config.version_hash(),
                universe_source=_INCUMBENT_SOURCE,
                decision_config=self.config,
            )
        ]
        if settings.opening_momentum_challenger_enabled:
            universe_config = self._continuation_config()
            breadth_config = self._breadth_gate_config()
            variants.append(
                _UniverseVariant(
                    variant="CONTINUATION_CHALLENGER",
                    algorithm_version=(
                        _CONTINUATION_ALGORITHM_VERSION
                    ),
                    config_version=(
                        opening_momentum_variant_config_version(
                            self.config.version_hash(),
                            universe_config,
                        )
                    ),
                    universe_source=_CONTINUATION_SOURCE,
                    decision_config=self.config,
                )
            )
            variants.append(
                _UniverseVariant(
                    variant="BREADTH_GATED_CHALLENGER",
                    algorithm_version=(
                        _BREADTH_GATE_ALGORITHM_VERSION
                    ),
                    config_version=(
                        opening_momentum_variant_config_version(
                            (
                                f"{breadth_config.version_hash()}:"
                                f"{_BREADTH_GATE_VERSION}"
                            ),
                            universe_config,
                        )
                    ),
                    universe_source=_BREADTH_GATE_SOURCE,
                    decision_config=breadth_config,
                )
            )
        return variants

    @staticmethod
    def _continuation_config() -> OpeningMomentumUniverseConfig:
        return OpeningMomentumUniverseConfig(
            max_selected=settings.universe_selection_max_symbols,
            max_per_sector=(
                settings.universe_selection_max_per_sector
            ),
        )

    def _breadth_gate_config(self) -> OpeningMomentumConfig:
        return replace(
            self.config,
            minimum_market_return_bps=max(
                0.0,
                self.config.minimum_market_return_bps,
            ),
        )

    def _close_if_due(
        self,
        row: OpeningMomentumShadowRun,
        current: datetime,
    ) -> None:
        if row.exit_due_at is None or row.candidate_symbol is None:
            return
        exit_due_at = _as_utc(row.exit_due_at)
        if current < exit_due_at + _BAR_DURATION + _SETTLEMENT_GRACE:
            return
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )
        bars = self.candle_provider.get_candlesticks(
            row.candidate_symbol,
            "MIN_1",
            _CANDLE_COUNT,
        )
        exit_bar = {
            bar.timestamp: bar
            for bar in self._coerce_candles(bars)
        }.get(exit_due_at)
        if exit_bar is None:
            history_reader = getattr(
                self.candle_provider,
                "get_history_candlesticks_by_offset",
                None,
            )
            if callable(history_reader):
                historical = history_reader(
                    row.candidate_symbol,
                    "MIN_1",
                    10,
                    exit_due_at - _BAR_DURATION,
                )
                if isinstance(historical, list):
                    exit_bar = {
                        bar.timestamp: bar
                        for bar in self._coerce_candles(historical)
                    }.get(exit_due_at)
        if exit_bar is None:
            logger.warning(
                "opening momentum exit bar unavailable for %s at %s",
                row.candidate_symbol,
                exit_due_at.isoformat(),
            )
            return
        if row.entry_price is None:
            raise ValueError(
                "open opening-momentum run has no entry price"
            )
        gross_return_bps, _ = (
            shadow_round_trip_return_bps(
                entry_price=row.entry_price,
                exit_price=exit_bar.open,
                config=self.config,
            )
        )
        net_return_bps = (
            gross_return_bps - float(row.estimated_cost_bps)
        )
        row.status = "CLOSED"
        row.reason = "FIXED_HOLD_EXIT"
        row.exit_at = exit_due_at
        row.exit_price = exit_bar.open
        row.gross_return_bps = gross_return_bps
        row.net_return_bps = net_return_bps
        self.db.add(row)
        self.db.commit()

    def _config_response(
        self,
    ) -> OpeningMomentumShadowConfigResponse:
        return OpeningMomentumShadowConfigResponse(
            enabled=settings.opening_momentum_shadow_enabled,
            algorithm_version=ALGORITHM_VERSION,
            config_version=self.config.version_hash(),
            signal_minutes=self.config.signal_minutes,
            execution_delay_minutes=(
                self.config.execution_delay_minutes
            ),
            holding_minutes=self.config.holding_minutes,
            minimum_universe_size=self.config.minimum_universe_size,
            minimum_market_return_bps=(
                self.config.minimum_market_return_bps
            ),
            minimum_candidate_return_bps=(
                self.config.minimum_candidate_return_bps
            ),
            minimum_excess_return_bps=(
                self.config.minimum_excess_return_bps
            ),
            one_side_fee_rate=self.config.one_side_fee_rate,
            one_side_slippage_bps=(
                self.config.one_side_slippage_bps
            ),
            round_trip_cost_bps=self.config.round_trip_cost_bps,
        )

    def _variant_responses(
        self,
    ) -> list[OpeningMomentumShadowVariantResponse]:
        identities = self._variant_identities()
        rows_by_version: dict[
            str, list[OpeningMomentumShadowRun]
        ] = {}
        for identity in identities:
            rows_by_version[identity.config_version] = (
                self.db.query(OpeningMomentumShadowRun)
                .filter(
                    OpeningMomentumShadowRun.config_version
                    == identity.config_version
                )
                .order_by(
                    OpeningMomentumShadowRun.session_date.asc(),
                    OpeningMomentumShadowRun.id.asc(),
                )
                .all()
            )
        session_sets = [
            {row.session_date for row in rows}
            for rows in rows_by_version.values()
        ]
        comparison_dates = (
            session_sets[0].intersection(*session_sets[1:])
            if len(session_sets) > 1
            else session_sets[0]
            if session_sets
            else set()
        )
        return [
            OpeningMomentumShadowVariantResponse(
                variant=identity.variant,
                universe_source=identity.universe_source,
                algorithm_version=identity.algorithm_version,
                config_version=identity.config_version,
                minimum_market_return_bps=(
                    identity.decision_config.minimum_market_return_bps
                ),
                comparison_sessions=len(comparison_dates),
                latest=(
                    self._run_response(
                        rows_by_version[identity.config_version][-1]
                    )
                    if rows_by_version[identity.config_version]
                    else None
                ),
                metrics=self._metrics(
                    identity.config_version,
                    session_dates=comparison_dates,
                ),
            )
            for identity in identities
        ]

    def _metrics(
        self,
        config_version: str,
        *,
        session_dates: set[date] | None = None,
    ) -> OpeningMomentumShadowMetrics:
        rows = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(
                OpeningMomentumShadowRun.config_version
                == config_version
            )
            .order_by(OpeningMomentumShadowRun.session_date.asc())
            .all()
        )
        if session_dates is not None:
            rows = [
                row
                for row in rows
                if row.session_date in session_dates
            ]
        closed = [row for row in rows if row.status == "CLOSED"]
        net_values = [
            float(row.net_return_bps)
            for row in closed
            if row.net_return_bps is not None
        ]
        wins = sum(value > 0 for value in net_values)
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in net_values:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        gains = sum(value for value in net_values if value > 0)
        losses = -sum(value for value in net_values if value < 0)
        return OpeningMomentumShadowMetrics(
            observed_sessions=len(rows),
            skipped_sessions=sum(
                row.status == "SKIPPED" for row in rows
            ),
            signals=sum(
                row.status in {"OPEN", "CLOSED"} for row in rows
            ),
            open_trades=sum(row.status == "OPEN" for row in rows),
            closed_trades=len(closed),
            wins=wins,
            win_rate=(wins / len(net_values)) if net_values else 0.0,
            mean_net_return_bps=(
                sum(net_values) / len(net_values)
                if net_values
                else 0.0
            ),
            cumulative_net_return_bps=sum(net_values),
            max_drawdown_bps=max_drawdown,
            profit_factor=(
                gains / losses
                if losses > 0
                else None
            ),
        )

    @staticmethod
    def _coerce_candles(values: list[Any]) -> list[_Candle]:
        by_timestamp: dict[datetime, _Candle] = {}
        for value in values:
            try:
                timestamp = _as_utc(getattr(value, "timestamp"))
                open_price = float(getattr(value, "open"))
                close_price = float(getattr(value, "close"))
            except (AttributeError, TypeError, ValueError):
                continue
            if any(
                not math.isfinite(price) or price <= 0
                for price in (open_price, close_price)
            ):
                continue
            by_timestamp[timestamp] = _Candle(
                timestamp=timestamp,
                open=open_price,
                close=close_price,
            )
        return [
            by_timestamp[timestamp]
            for timestamp in sorted(by_timestamp)
        ]

    @staticmethod
    def _run_response(
        row: OpeningMomentumShadowRun,
    ) -> OpeningMomentumShadowRunResponse:
        universe = _json_list(row.universe_json)
        excluded = _json_dict(row.excluded_symbols_json)
        ranking_raw = _json_value(row.ranking_json, [])
        ranking = (
            [
                OpeningMomentumRankResponse.model_validate(item)
                for item in ranking_raw
                if isinstance(item, dict)
            ]
            if isinstance(ranking_raw, list)
            else []
        )
        return OpeningMomentumShadowRunResponse(
            id=row.id,
            session_date=row.session_date,
            algorithm_version=row.algorithm_version,
            config_version=row.config_version,
            status=cast(
                Literal["SKIPPED", "OPEN", "CLOSED"],
                row.status,
            ),
            reason=row.reason,
            signal_at=_as_utc(row.signal_at),
            observed_at=_as_utc(row.observed_at),
            selection_run_id=row.selection_run_id,
            universe_source=row.universe_source,
            universe_size=row.universe_size,
            universe=universe,
            excluded_symbols=excluded,
            ranking=ranking,
            candidate_symbol=row.candidate_symbol,
            market_return_bps=row.market_return_bps,
            candidate_return_bps=row.candidate_return_bps,
            excess_return_bps=row.excess_return_bps,
            entry_at=_optional_utc(row.entry_at),
            entry_price=row.entry_price,
            exit_due_at=_optional_utc(row.exit_due_at),
            exit_at=_optional_utc(row.exit_at),
            exit_price=row.exit_price,
            gross_return_bps=row.gross_return_bps,
            estimated_cost_bps=row.estimated_cost_bps,
            net_return_bps=row.net_return_bps,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _json_value(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _json_list(raw: str) -> list[str]:
    value = _json_value(raw, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_dict(raw: str) -> dict[str, str]:
    value = _json_value(raw, {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
    }


def _json_string_tuple(raw: str) -> tuple[str, ...]:
    value = _json_value(raw, [])
    if not isinstance(value, list):
        return ("DATA_INVALID_EXCLUSION_REASONS",)
    return tuple(str(item) for item in value)


def _optional_metric(raw: str, key: str) -> float | None:
    value = _json_value(raw, {})
    if not isinstance(value, dict):
        return None
    try:
        metric = float(value[key])
    except (KeyError, TypeError, ValueError):
        return None
    return metric if math.isfinite(metric) else None
