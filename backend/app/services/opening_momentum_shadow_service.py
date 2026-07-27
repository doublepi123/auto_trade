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
    REVERSAL_ALGORITHM_VERSION,
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
    evaluate_opening_reversal,
    opening_path_efficiency,
    shadow_round_trip_return_bps,
)
from app.domain.opening_momentum_comparison import (
    compare_opening_momentum_variants,
)
from app.domain.opening_momentum_policy import (
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS,
    PRODUCTION_MINIMUM_PATH_EFFICIENCY,
    PRODUCTION_POLICY_NAME,
    opening_execution_config,
)
from app.domain.opening_momentum_universe import (
    OPENING_CONTINUATION_UNIVERSE_VERSION,
    OpeningMomentumUniverseCandidate,
    OpeningMomentumUniverseConfig,
    opening_momentum_evidence_config_version,
    opening_momentum_variant_config_version,
    select_opening_momentum_universe,
)
from app.domain.universe_selection import (
    CATALOG_SOURCE_VERSION,
    UNIVERSE_ALGORITHM_VERSION,
)
from app.models import (
    OpeningMomentumShadowRun,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.platform.multiple_testing import (
    holm_adjusted_pvalues,
    one_sample_greater_pvalue,
)
from app.schemas import (
    OpeningMomentumPairedComparisonResponse,
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
_OPENING_CONTEXT_BENCHMARKS = ("QQQ.US", "DIA.US")
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
_LAST_FIVE_GATE_VERSION = "last-five-nonnegative-v1"
_LAST_FIVE_GATE_SOURCE = "OPENING_CONTINUATION_LAST5"
_LAST_FIVE_GATE_ALGORITHM_VERSION = (
    f"{_BREADTH_GATE_ALGORITHM_VERSION}+{_LAST_FIVE_GATE_VERSION}"
)
_LAST_FIVE_ONLY_SOURCE = "OPENING_CONTINUATION_LAST5_ONLY"
_LAST_FIVE_ONLY_ALGORITHM_VERSION = (
    f"{_CONTINUATION_ALGORITHM_VERSION}+{_LAST_FIVE_GATE_VERSION}"
)
_EARLY_BROAD_VERSION = "active-broad-3m-signal-120m-hold-v1"
_EARLY_BROAD_SOURCE = "OPENING_EARLY_BROAD"
_EARLY_BROAD_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_EARLY_BROAD_VERSION}"
)
_EARLY_BROAD_MINIMUM_COVERAGE = 0.95
_EARLY_EXTENSION_COHORT_VERSION = (
    "discovery-top6-positive-delta-min4-20260724-v1"
)
_EXTENSION_MINIMUM_DISPLACEMENT_SESSIONS = 3
_EXTENSION_MINIMUM_OUTPERFORMANCE_RATE = 0.55
_MULTIPLE_TESTING_METHOD = "HOLM_BONFERRONI"
_MULTIPLE_TESTING_SIGNIFICANCE_LEVEL = 0.05
_EXECUTION_BROAD_VERSION = "active-broad-3m-signal-60m-hold-stop1-v1"
_EXECUTION_BROAD_SOURCE = "OPENING_EXECUTION_BROAD"
_EXECUTION_BROAD_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_EXECUTION_BROAD_VERSION}"
)
_EXECUTION_PATH_EFFICIENCY_MINIMUM = (
    PRODUCTION_MINIMUM_PATH_EFFICIENCY
)
_EXECUTION_PATH_EFFICIENCY_VERSION = (
    "active-broad-3m-signal-60m-hold-stop1-path-efficiency-070-v1"
)
_EXECUTION_PATH_EFFICIENCY_SOURCE = (
    "OPENING_EXECUTION_PATH_EFFICIENCY"
)
_EXECUTION_PATH_EFFICIENCY_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_EXECUTION_PATH_EFFICIENCY_VERSION}"
)
_WEAK_BREADTH_PATH_VERSION = (
    "forward-only-max-median0-path-efficiency-070-"
    "precommitted-20260727-v1"
)
_WEAK_BREADTH_PATH_SOURCE = "OPENING_EXECUTION_WEAK_BREADTH_PATH"
_WEAK_BREADTH_PATH_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_WEAK_BREADTH_PATH_VERSION}"
)
_WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS = (
    PRODUCTION_MAXIMUM_MARKET_RETURN_BPS
)
# Post-hoc sensitivity analysis found the adjacent +5 bps ceiling produced
# the same holdout trades as production while accepting three additional
# discovery winners. It remains forward-only and cannot affect paper orders.
_WEAK_BREADTH_RELAXED_MAXIMUM_MARKET_RETURN_BPS = 5.0
_WEAK_BREADTH_RELAXED_VERSION = (
    "forward-only-posthoc-grid-neighbor-max-median5-"
    "path-efficiency-070-20260727-v1"
)
_WEAK_BREADTH_RELAXED_SOURCE = (
    "OPENING_EXECUTION_WEAK_BREADTH_RELAXED"
)
_WEAK_BREADTH_RELAXED_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_WEAK_BREADTH_RELAXED_VERSION}"
)
# Paper execution uses the same frozen identity as the paired shadow variant.
# The broad policy remains the comparison baseline, so every skipped or
# entered session continues to produce a causal counterfactual.
_PAPER_EXECUTION_VARIANT = PRODUCTION_POLICY_NAME
# The active ranking pool is intentionally decoupled from this regime signal.
# Adding one symbol can move a cross-sectional median across zero merely by
# changing the pool parity; the QQQ/DIA average stays comparable as the pool
# evolves. These variants start forward-only evidence after 2026-07-27.
_ETF_REGIME_MAXIMUM_AVERAGE_RETURN_BPS = 0.0
_ETF_REGIME_PATH_VERSION = (
    "forward-only-qqq-dia-average-max0-path-efficiency-070-"
    "precommitted-20260727-v1"
)
_ETF_REGIME_PATH_SOURCE = "OPENING_EXECUTION_ETF_REGIME"
_ETF_REGIME_PATH_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_ETF_REGIME_PATH_VERSION}"
)
_ETF_REGIME_EXTENSION_VERSION = (
    "forward-only-independent-etf-regime-extension-"
    "precommitted-20260727-v1"
)
_ETF_REGIME_EXTENSION_SPECS = (
    (
        "ETF_REGIME_CRWD_CHALLENGER",
        "CRWD.US",
        "OPENING_EXECUTION_ETF_CRWD",
    ),
    (
        "ETF_REGIME_TRV_CHALLENGER",
        "TRV.US",
        "OPENING_EXECUTION_ETF_TRV",
    ),
)
_ETF_REGIME_EXTENSION_VARIANTS = frozenset(
    variant for variant, _, _ in _ETF_REGIME_EXTENSION_SPECS
)
_ETF_REGIME_VARIANTS = (
    "ETF_REGIME_PATH_CHALLENGER",
    *(
        variant
        for variant, _, _ in _ETF_REGIME_EXTENSION_SPECS
    ),
)
# Selected on data through 2026-07-24; only post-deployment rows count as
# forward evidence for the wider catastrophic stop.
_WEAK_BREADTH_WIDE_STOP_PCT = 4.0
_WEAK_BREADTH_WIDE_STOP_VERSION = (
    "forward-only-max-median0-path-efficiency-070-hold60-stop4-"
    "precommitted-20260727-v1"
)
_WEAK_BREADTH_WIDE_STOP_SOURCE = (
    "OPENING_EXECUTION_WEAK_BREADTH_WIDE_STOP"
)
_WEAK_BREADTH_WIDE_STOP_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_WEAK_BREADTH_WIDE_STOP_VERSION}"
)
# Frozen after a three-slice study through 2026-07-24. The rule itself is
# causal: the stop is fixed from the completed three-minute opening range and
# capped at four percent from the delayed entry. Only later rows are forward
# evidence.
_OPENING_RANGE_STOP_MAX_PCT = 4.0
_OPENING_RANGE_STOP_VERSION = (
    "forward-only-opening-range-low-stop-cap4-"
    "precommitted-20260727-v1"
)
_OPENING_RANGE_STOP_SOURCE = "OPENING_EXECUTION_RANGE_STOP"
_OPENING_RANGE_STOP_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_OPENING_RANGE_STOP_VERSION}"
)
_EXECUTION_EXTENSION_COHORT_VERSION = (
    "individual-discovery-top6-positive-delta-min4-stop1-shortlist-v2-"
    "20260724"
)
# An exhaustive discovery-only joint-subset search selected PANW from the
# individual QCOM/PANW/RKLB shortlist. The 30bp cost, tail, and drawdown gates
# rejected every larger subset. This identity remains forward-only because the
# research report was generated before deployment.
_WEAK_BREADTH_INDEX_COHORT_SYMBOLS = (
    "PANW.US",
)
_WEAK_BREADTH_INDEX_COHORT_VERSION = (
    "forward-only-discovery-joint-subset-active-broad-plus-panw-"
    "max-median0-path-efficiency-070-"
    f"{_EXECUTION_EXTENSION_COHORT_VERSION}-20260728-v1"
)
_WEAK_BREADTH_INDEX_COHORT_SOURCE = (
    "OPENING_EXECUTION_WEAK_BREADTH_INDEX_COHORT"
)
_WEAK_BREADTH_INDEX_COHORT_ALGORITHM_VERSION = (
    f"{ALGORITHM_VERSION}+{_WEAK_BREADTH_INDEX_COHORT_VERSION}"
)
_EXECUTION_CRWD_FORWARD_COHORT_VERSION = (
    "forward-only-two-slice-positive-tail-backward-sparse-"
    "precommitted-20260727-v1"
)
_REVERSAL_SOURCE = "OPENING_REVERSAL"
_NON_COMPARABLE_SKIP_REASONS = frozenset(
    {
        "PREOPEN_UNIVERSE_UNAVAILABLE",
        "DATA_INCOMPLETE",
        "BENCHMARK_DATA_INCOMPLETE",
        "ENTRY_BAR_MISSING",
        "INSUFFICIENT_UNIVERSE",
    }
)

_EarlyExtensionVariantName = Literal[
    "EARLY_RKLB_CHALLENGER",
    "EARLY_WDAY_CHALLENGER",
    "EARLY_SNDK_CHALLENGER",
    "EARLY_ALAB_CHALLENGER",
    "EARLY_LITE_CHALLENGER",
    "EARLY_QCOM_CHALLENGER",
]
_ExecutionExtensionVariantName = Literal[
    "EXECUTION_SNDK_CHALLENGER",
    "EXECUTION_INTC_CHALLENGER",
    "EXECUTION_QCOM_CHALLENGER",
    "EXECUTION_RKLB_CHALLENGER",
    "EXECUTION_PANW_CHALLENGER",
    "EXECUTION_CRWD_CHALLENGER",
]
_VariantName = Literal[
    "INCUMBENT",
    "REVERSAL_CHALLENGER",
    "CONTINUATION_CHALLENGER",
    "BREADTH_GATED_CHALLENGER",
    "LAST5_POSITIVE_CHALLENGER",
    "LAST5_ONLY_CHALLENGER",
    "EARLY_BROAD_CHALLENGER",
    "EARLY_RKLB_CHALLENGER",
    "EARLY_WDAY_CHALLENGER",
    "EARLY_SNDK_CHALLENGER",
    "EARLY_ALAB_CHALLENGER",
    "EARLY_LITE_CHALLENGER",
    "EARLY_QCOM_CHALLENGER",
    "EXECUTION_BROAD_CHALLENGER",
    "EXECUTION_PATH_EFFICIENCY_CHALLENGER",
    "WEAK_BREADTH_PATH_CHALLENGER",
    "WEAK_BREADTH_RELAXED_CHALLENGER",
    "WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
    "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
    "ETF_REGIME_PATH_CHALLENGER",
    "ETF_REGIME_CRWD_CHALLENGER",
    "ETF_REGIME_TRV_CHALLENGER",
    "OPENING_RANGE_STOP_CHALLENGER",
    "EXECUTION_SNDK_CHALLENGER",
    "EXECUTION_INTC_CHALLENGER",
    "EXECUTION_QCOM_CHALLENGER",
    "EXECUTION_RKLB_CHALLENGER",
    "EXECUTION_PANW_CHALLENGER",
    "EXECUTION_CRWD_CHALLENGER",
]
_SignalModel = Literal["MOMENTUM", "REVERSAL"]


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
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class _OpeningPathFeatures:
    first_five_return_bps: float
    last_five_return_bps: float
    path_efficiency: float
    max_pullback_bps: float
    opening_range_bps: float


@dataclass(frozen=True)
class _ExitOutcome:
    exited_at: datetime
    price: float
    reason: Literal["FIXED_HOLD_EXIT", "STOP_LOSS_EXIT"]
    maximum_adverse_excursion_bps: float
    maximum_favorable_excursion_bps: float


@dataclass(frozen=True)
class OpeningMomentumExecutionSignal:
    """Causal fixed-stop signal frozen before an order can be submitted."""

    session_date: date
    algorithm_version: str
    config_version: str
    universe_source: str
    selection_run_id: int | None
    action: Literal["ENTER_LONG", "SKIP"]
    reason: str
    symbol: str | None
    signal_at: datetime
    entry_due_at: datetime
    universe_size: int
    market_return_bps: float | None
    candidate_return_bps: float | None
    excess_return_bps: float | None
    reference_entry_price: float | None
    stop_loss_pct: float
    max_holding_minutes: int
    context: dict[str, object]


@dataclass(frozen=True)
class _UniverseVariant:
    variant: _VariantName
    algorithm_version: str
    config_version: str
    universe_source: str
    decision_config: OpeningMomentumConfig
    signal_model: _SignalModel = "MOMENTUM"
    require_nonnegative_last_five: bool = False
    minimum_data_coverage: float = 1.0
    minimum_path_efficiency: float | None = None
    maximum_market_return_bps: float | None = None
    maximum_benchmark_average_return_bps: float | None = None
    opening_range_stop: bool = False
    required_symbols: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    selection_run_id: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_path_efficiency is not None and (
            not math.isfinite(self.minimum_path_efficiency)
            or not 0 <= self.minimum_path_efficiency <= 1
        ):
            raise ValueError(
                "minimum_path_efficiency must be in [0, 1] when set"
            )
        if self.maximum_market_return_bps is not None:
            if not math.isfinite(self.maximum_market_return_bps):
                raise ValueError(
                    "maximum_market_return_bps must be finite when set"
                )
            if (
                self.maximum_market_return_bps
                < self.decision_config.minimum_market_return_bps
            ):
                raise ValueError(
                    "maximum_market_return_bps must not be below the "
                    "minimum market return"
                )
        if (
            self.maximum_benchmark_average_return_bps is not None
            and not math.isfinite(
                self.maximum_benchmark_average_return_bps
            )
        ):
            raise ValueError(
                "maximum_benchmark_average_return_bps must be finite "
                "when set"
            )
        if (
            self.opening_range_stop
            and self.decision_config.stop_loss_pct is None
        ):
            raise ValueError(
                "opening-range stop requires a maximum stop-loss percent"
            )


@dataclass(frozen=True)
class _EarlyExtensionSpec:
    variant: _EarlyExtensionVariantName
    symbol: str

    @property
    def ticker(self) -> str:
        return self.symbol.removesuffix(".US")

    @property
    def version(self) -> str:
        return (
            f"active-broad-plus-{self.ticker.lower()}-3m-signal-"
            f"120m-hold-{_EARLY_EXTENSION_COHORT_VERSION}"
        )

    @property
    def algorithm_version(self) -> str:
        return f"{ALGORITHM_VERSION}+{self.version}"

    @property
    def universe_source(self) -> str:
        return f"OPENING_EARLY_{self.ticker}"


@dataclass(frozen=True)
class _ExecutionExtensionSpec:
    variant: _ExecutionExtensionVariantName
    symbol: str
    cohort_version: str = _EXECUTION_EXTENSION_COHORT_VERSION

    @property
    def ticker(self) -> str:
        return self.symbol.removesuffix(".US")

    @property
    def version(self) -> str:
        return (
            f"active-broad-plus-{self.ticker.lower()}-3m-signal-"
            f"60m-hold-stop1-{self.cohort_version}"
        )

    @property
    def algorithm_version(self) -> str:
        return f"{ALGORITHM_VERSION}+{self.version}"

    @property
    def universe_source(self) -> str:
        return f"OPENING_EXECUTION_{self.ticker}"


# Frozen from the discovery slice only: the six highest positive cumulative
# extension deltas among candidates with at least four actual baseline
# displacements. Holdout results did not determine membership, so observations
# after this deployment remain causal.
_EARLY_EXTENSION_SPECS = (
    _EarlyExtensionSpec("EARLY_RKLB_CHALLENGER", "RKLB.US"),
    _EarlyExtensionSpec("EARLY_WDAY_CHALLENGER", "WDAY.US"),
    _EarlyExtensionSpec("EARLY_SNDK_CHALLENGER", "SNDK.US"),
    _EarlyExtensionSpec("EARLY_ALAB_CHALLENGER", "ALAB.US"),
    _EarlyExtensionSpec("EARLY_LITE_CHALLENGER", "LITE.US"),
    _EarlyExtensionSpec("EARLY_QCOM_CHALLENGER", "QCOM.US"),
)
_EARLY_EXTENSION_VARIANTS = frozenset(
    spec.variant for spec in _EARLY_EXTENSION_SPECS
)

# Frozen from the discovery slice of the 3m/60m/1% stop research grid only.
# The 2026-06-23..2026-07-24 holdout did not determine membership.
_DISCOVERY_EXECUTION_EXTENSION_SPECS = (
    _ExecutionExtensionSpec("EXECUTION_SNDK_CHALLENGER", "SNDK.US"),
    _ExecutionExtensionSpec("EXECUTION_INTC_CHALLENGER", "INTC.US"),
    _ExecutionExtensionSpec("EXECUTION_QCOM_CHALLENGER", "QCOM.US"),
    _ExecutionExtensionSpec("EXECUTION_RKLB_CHALLENGER", "RKLB.US"),
    _ExecutionExtensionSpec("EXECUTION_PANW_CHALLENGER", "PANW.US"),
)

# CRWD was selected after inspecting three historical slices. Its sparse
# backward displacement count is insufficient for promotion, so the unique
# config version below intentionally starts a forward-only evidence series.
_FORWARD_ONLY_EXECUTION_EXTENSION_SPECS = (
    _ExecutionExtensionSpec(
        "EXECUTION_CRWD_CHALLENGER",
        "CRWD.US",
        _EXECUTION_CRWD_FORWARD_COHORT_VERSION,
    ),
)
_EXECUTION_EXTENSION_SPECS = (
    *_DISCOVERY_EXECUTION_EXTENSION_SPECS,
    *_FORWARD_ONLY_EXECUTION_EXTENSION_SPECS,
)
_EXECUTION_EXTENSION_VARIANTS = frozenset(
    spec.variant for spec in _EXECUTION_EXTENSION_SPECS
)


class OpeningMomentumShadowService:
    """Collect paired daily cross-sectional observations without orders."""

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
        settlement_candles: dict[str, dict[datetime, _Candle]] = {}
        for open_run in open_runs:
            self._close_if_due(
                open_run,
                current,
                settlement_candles=settlement_candles,
            )

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
        due_variants = [
            variant
            for variant in pending_variants
            if self._variant_decision_due(
                variant,
                session_open=session_open,
                current=current,
            )
        ]
        if not due_variants:
            return self.get_status()
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )
        self._observe_variants(
            due_variants,
            session_date=local.date(),
            session_open=session_open,
            current=current,
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
        return self.get_status()

    def evaluate_execution_signal(
        self,
        *,
        now: datetime | None = None,
    ) -> OpeningMomentumExecutionSignal | None:
        """Build the paper execution decision from completed signal bars."""
        current = _as_utc(now or datetime.now(timezone.utc))
        if not is_trading_hours("US", current):
            return None
        session = get_session("US")
        local = session.local(current)
        session_open = datetime.combine(
            local.date(),
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        variants = self._universe_variants(
            session_date=local.date(),
            completed_before=session_open,
        )
        execution_identity = self.paper_execution_variant_identity()
        variant = next(
            (
                item
                for item in variants
                if item.variant == execution_identity.variant
            ),
            None,
        )
        if variant is None:
            return None
        config = variant.decision_config
        signal_at = (
            session_open
            + timedelta(minutes=config.signal_minutes)
            - _BAR_DURATION
        )
        signal_ready_at = (
            session_open
            + timedelta(minutes=config.signal_minutes)
            + _SETTLEMENT_GRACE
        )
        entry_due_at = self._variant_entry_at(
            variant,
            session_open=session_open,
        )
        deadline = entry_due_at + timedelta(
            seconds=(
                settings.opening_momentum_execution_max_entry_delay_seconds
            )
        )
        if current < signal_ready_at or current > deadline:
            return None
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum execution candle provider is unavailable"
            )

        expected_signal_bars = {
            session_open + timedelta(minutes=index)
            for index in range(config.signal_minutes)
        }
        observations: list[OpeningMomentumObservation] = []
        path_efficiency_by_symbol: dict[str, float] = {}
        excluded: dict[str, str] = {}
        for symbol in variant.symbols:
            try:
                values = self.candle_provider.get_candlesticks(
                    symbol,
                    "MIN_1",
                    _CANDLE_COUNT,
                )
                by_timestamp = {
                    bar.timestamp: bar
                    for bar in self._coerce_candles(values)
                }
            except Exception as exc:
                excluded[symbol] = (
                    f"BROKER_ERROR:{type(exc).__name__}"
                )
                continue
            missing = expected_signal_bars - by_timestamp.keys()
            if missing:
                excluded[symbol] = (
                    f"SIGNAL_BARS_MISSING:{len(missing)}"
                )
                continue
            opening_bar = by_timestamp.get(session_open)
            signal_bar = by_timestamp.get(signal_at)
            if opening_bar is None or signal_bar is None:
                excluded[symbol] = "SIGNAL_BARS_MISSING"
                continue
            signal_candles = [
                by_timestamp[timestamp]
                for timestamp in sorted(expected_signal_bars)
            ]
            path_efficiency_by_symbol[symbol] = (
                self._opening_path_efficiency(signal_candles)
            )
            observations.append(OpeningMomentumObservation(
                symbol=symbol,
                session_open=opening_bar.open,
                signal_close=signal_bar.close,
                # The BBO at submission owns the actual entry price. This
                # positive value only makes the already-complete signal
                # actionable without reading the future delayed-entry bar.
                entry_open=signal_bar.close,
            ))

        decision = evaluate_opening_momentum(observations, config)
        candidate_path_efficiency = (
            path_efficiency_by_symbol.get(decision.candidate_symbol)
            if decision.candidate_symbol is not None
            else None
        )
        required_observations = max(
            config.minimum_universe_size,
            math.ceil(
                len(variant.symbols)
                * variant.minimum_data_coverage
            ),
        )
        data_complete = (
            bool(variant.symbols)
            and len(observations) >= required_observations
        )
        action: Literal["ENTER_LONG", "SKIP"] = decision.action
        reason = decision.reason
        path_efficiency_gate_failed = (
            variant.minimum_path_efficiency is not None
            and action == "ENTER_LONG"
            and (
                candidate_path_efficiency is None
                or candidate_path_efficiency
                < variant.minimum_path_efficiency
            )
        )
        maximum_market_return_gate_failed = (
            variant.maximum_market_return_bps is not None
            and action == "ENTER_LONG"
            and (
                decision.market_return_bps is None
                or decision.market_return_bps
                > variant.maximum_market_return_bps
            )
        )
        if variant.selection_run_id is None:
            action = "SKIP"
            reason = "PREOPEN_UNIVERSE_UNAVAILABLE"
        elif not data_complete:
            action = "SKIP"
            reason = "DATA_INCOMPLETE"
        elif path_efficiency_gate_failed:
            action = "SKIP"
            reason = "PATH_EFFICIENCY_FILTER"
        elif maximum_market_return_gate_failed:
            action = "SKIP"
            reason = "MAXIMUM_MARKET_RETURN_FILTER"

        return OpeningMomentumExecutionSignal(
            session_date=local.date(),
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            action=action,
            reason=reason,
            # Persist the evaluated candidate even when a gate skips it. The
            # row is inactive in that case, so this improves diagnostics
            # without exposing the symbol to the order registry.
            symbol=decision.candidate_symbol,
            signal_at=signal_at,
            entry_due_at=entry_due_at,
            universe_size=decision.universe_size,
            market_return_bps=decision.market_return_bps,
            candidate_return_bps=decision.candidate_return_bps,
            excess_return_bps=decision.excess_return_bps,
            reference_entry_price=(
                decision.entry_price
                if action == "ENTER_LONG"
                else None
            ),
            stop_loss_pct=float(config.stop_loss_pct or 0),
            max_holding_minutes=config.holding_minutes,
            context={
                "signal_ready_at": signal_ready_at.isoformat(),
                "observed_at": current.isoformat(),
                "required_observations": required_observations,
                "observed_symbols": len(observations),
                "candidate_path_efficiency": (
                    candidate_path_efficiency
                ),
                "candidate_symbol": decision.candidate_symbol,
                "minimum_path_efficiency": (
                    variant.minimum_path_efficiency
                ),
                "maximum_market_return_bps": (
                    variant.maximum_market_return_bps
                ),
                "universe": list(variant.symbols),
                "excluded_symbols": excluded,
                "ranking": [
                    asdict(item) for item in decision.ranking
                ],
            },
        )

    @staticmethod
    def _variant_entry_at(
        variant: _UniverseVariant,
        *,
        session_open: datetime,
    ) -> datetime:
        config = variant.decision_config
        return session_open + timedelta(
            minutes=(
                config.signal_minutes
                + config.execution_delay_minutes
            )
        )

    @classmethod
    def _variant_decision_due(
        cls,
        variant: _UniverseVariant,
        *,
        session_open: datetime,
        current: datetime,
    ) -> bool:
        entry_at = cls._variant_entry_at(
            variant,
            session_open=session_open,
        )
        decision_start = entry_at + _BAR_DURATION + _SETTLEMENT_GRACE
        return decision_start <= current <= (
            decision_start + _DECISION_WINDOW
        )

    def _observe_variants(
        self,
        variants: list[_UniverseVariant],
        *,
        session_date: date,
        session_open: datetime,
        current: datetime,
    ) -> None:
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )
        symbols = tuple(dict.fromkeys(
            symbol
            for variant in variants
            for symbol in variant.symbols
        ))
        bars_by_symbol: dict[str, dict[datetime, _Candle]] = {}
        fetch_errors: dict[str, str] = {}
        for symbol in symbols:
            try:
                values = self.candle_provider.get_candlesticks(
                    symbol,
                    "MIN_1",
                    _CANDLE_COUNT,
                )
                bars_by_symbol[symbol] = {
                    bar.timestamp: bar
                    for bar in self._coerce_candles(values)
                }
            except Exception as exc:
                fetch_errors[symbol] = (
                    f"BROKER_ERROR:{type(exc).__name__}"
                )
                logger.warning(
                    "opening momentum candle fetch failed for %s: %s",
                    symbol,
                    exc,
                )

        benchmark_bars: dict[str, dict[datetime, _Candle]] = {}
        for symbol in _OPENING_CONTEXT_BENCHMARKS:
            try:
                values = self._historical_candles_before(
                    symbol,
                    period="MIN_1",
                    count=_CANDLE_COUNT,
                    before=current,
                )
                if values is not None:
                    benchmark_bars[symbol] = {
                        bar.timestamp: bar for bar in values
                    }
            except Exception as exc:
                logger.warning(
                    "opening context candle fetch failed for %s: %s",
                    symbol,
                    exc,
                )

        previous_close_by_symbol: dict[str, float | None] = {}

        for variant in variants:
            config = variant.decision_config
            signal_at = (
                session_open
                + timedelta(minutes=config.signal_minutes)
                - _BAR_DURATION
            )
            entry_at = self._variant_entry_at(
                variant,
                session_open=session_open,
            )
            expected_signal_bars = {
                session_open + timedelta(minutes=index)
                for index in range(config.signal_minutes)
            }
            observations: list[OpeningMomentumObservation] = []
            path_features_by_symbol: dict[
                str, _OpeningPathFeatures
            ] = {}
            path_efficiency_by_symbol: dict[str, float] = {}
            opening_range_low_by_symbol: dict[str, float] = {}
            excluded = {
                symbol: fetch_errors[symbol]
                for symbol in variant.symbols
                if symbol in fetch_errors
            }
            for symbol in variant.symbols:
                by_timestamp = bars_by_symbol.get(symbol)
                if by_timestamp is None:
                    continue
                missing_signal_bars = (
                    expected_signal_bars - by_timestamp.keys()
                )
                if missing_signal_bars:
                    excluded[symbol] = (
                        "SIGNAL_BARS_MISSING:"
                        f"{len(missing_signal_bars)}"
                    )
                    continue
                opening_bar = by_timestamp.get(session_open)
                signal_bar = by_timestamp.get(signal_at)
                if opening_bar is None or signal_bar is None:
                    excluded[symbol] = "SIGNAL_BARS_MISSING"
                    continue
                observations.append(OpeningMomentumObservation(
                    symbol=symbol,
                    session_open=opening_bar.open,
                    signal_close=signal_bar.close,
                    entry_open=(
                        by_timestamp[entry_at].open
                        if entry_at in by_timestamp
                        else None
                    ),
                ))
                signal_candles = [
                    by_timestamp[timestamp]
                    for timestamp in sorted(expected_signal_bars)
                ]
                path_efficiency_by_symbol[symbol] = (
                    self._opening_path_efficiency(signal_candles)
                )
                opening_range_low_by_symbol[symbol] = min(
                    candle.low for candle in signal_candles
                )
                if len(signal_candles) >= 5:
                    path_features_by_symbol[symbol] = (
                        self._opening_path_features(signal_candles)
                    )

            decision = (
                evaluate_opening_reversal(observations, config)
                if variant.signal_model == "REVERSAL"
                else evaluate_opening_momentum(observations, config)
            )
            candidate_observation = next(
                (
                    item
                    for item in observations
                    if item.symbol == decision.candidate_symbol
                ),
                None,
            )
            previous_close: float | None = None
            if decision.candidate_symbol is not None:
                candidate_symbol = decision.candidate_symbol
                if candidate_symbol not in previous_close_by_symbol:
                    try:
                        daily_candles = self._historical_candles_before(
                            candidate_symbol,
                            period="DAY",
                            count=10,
                            before=session_open,
                        )
                        previous_close_by_symbol[candidate_symbol] = (
                            self._previous_session_close(
                                daily_candles or [],
                                session_date=session_date,
                            )
                            if daily_candles is not None
                            else None
                        )
                    except Exception as exc:
                        logger.warning(
                            "opening context previous-close fetch failed "
                            "for %s: %s",
                            candidate_symbol,
                            exc,
                        )
                        previous_close_by_symbol[candidate_symbol] = None
                previous_close = previous_close_by_symbol[candidate_symbol]
            candidate_overnight_gap_bps = (
                (
                    candidate_observation.session_open
                    / previous_close
                    - 1
                )
                * 10_000
                if (
                    candidate_observation is not None
                    and previous_close is not None
                    and previous_close > 0
                )
                else None
            )
            candidate_prev_close_to_signal_bps = (
                (
                    candidate_observation.signal_close
                    / previous_close
                    - 1
                )
                * 10_000
                if (
                    candidate_observation is not None
                    and previous_close is not None
                    and previous_close > 0
                )
                else None
            )
            benchmark_returns = {
                symbol: self._opening_return_bps(
                    benchmark_bars.get(symbol, {}),
                    expected_signal_bars=expected_signal_bars,
                )
                for symbol in _OPENING_CONTEXT_BENCHMARKS
            }
            benchmark_average_return_bps = (
                sum(cast(float, value) for value in benchmark_returns.values())
                / len(_OPENING_CONTEXT_BENCHMARKS)
                if all(
                    value is not None
                    for value in benchmark_returns.values()
                )
                else None
            )
            path_features = (
                path_features_by_symbol.get(decision.candidate_symbol)
                if decision.candidate_symbol is not None
                else None
            )
            path_efficiency = (
                path_efficiency_by_symbol.get(
                    decision.candidate_symbol
                )
                if decision.candidate_symbol is not None
                else None
            )
            required_observations = max(
                config.minimum_universe_size,
                math.ceil(
                    len(variant.symbols)
                    * variant.minimum_data_coverage
                ),
            )
            data_complete = (
                bool(variant.symbols)
                and len(observations) >= required_observations
                and set(variant.required_symbols).issubset(
                    {item.symbol for item in observations}
                )
            )
            last_five_gate_failed = (
                variant.require_nonnegative_last_five
                and decision.action == "ENTER_LONG"
                and (
                    path_features is None
                    or path_features.last_five_return_bps < 0
                )
            )
            path_efficiency_gate_failed = (
                variant.minimum_path_efficiency is not None
                and decision.action == "ENTER_LONG"
                and (
                    path_efficiency is None
                    or path_efficiency
                    < variant.minimum_path_efficiency
                )
            )
            maximum_market_return_gate_failed = (
                variant.maximum_market_return_bps is not None
                and decision.action == "ENTER_LONG"
                and (
                    decision.market_return_bps is None
                    or decision.market_return_bps
                    > variant.maximum_market_return_bps
                )
            )
            benchmark_data_incomplete = (
                variant.maximum_benchmark_average_return_bps is not None
                and decision.action == "ENTER_LONG"
                and benchmark_average_return_bps is None
            )
            maximum_benchmark_average_gate_failed = (
                variant.maximum_benchmark_average_return_bps is not None
                and decision.action == "ENTER_LONG"
                and benchmark_average_return_bps is not None
                and benchmark_average_return_bps
                > variant.maximum_benchmark_average_return_bps
            )
            opening_range_stop_loss_pct: float | None = None
            if (
                variant.opening_range_stop
                and decision.action == "ENTER_LONG"
                and decision.candidate_symbol is not None
                and decision.entry_price is not None
                and config.stop_loss_pct is not None
            ):
                opening_range_stop_loss_pct = (
                    self._opening_range_stop_loss_pct(
                        opening_range_low=(
                            opening_range_low_by_symbol.get(
                                decision.candidate_symbol
                            )
                        ),
                        entry_price=decision.entry_price,
                        maximum_stop_loss_pct=config.stop_loss_pct,
                    )
                )
            opening_range_stop_invalid = (
                variant.opening_range_stop
                and decision.action == "ENTER_LONG"
                and opening_range_stop_loss_pct is None
            )
            status = (
                "OPEN"
                if (
                    decision.action == "ENTER_LONG"
                    and data_complete
                    and not last_five_gate_failed
                    and not path_efficiency_gate_failed
                    and not maximum_market_return_gate_failed
                    and not benchmark_data_incomplete
                    and not maximum_benchmark_average_gate_failed
                    and not opening_range_stop_invalid
                )
                else "SKIPPED"
            )
            if variant.selection_run_id is None:
                reason = "PREOPEN_UNIVERSE_UNAVAILABLE"
            elif not data_complete:
                reason = "DATA_INCOMPLETE"
            elif last_five_gate_failed:
                reason = "LAST_FIVE_RETURN_FILTER"
            elif path_efficiency_gate_failed:
                reason = "PATH_EFFICIENCY_FILTER"
            elif maximum_market_return_gate_failed:
                reason = "MAXIMUM_MARKET_RETURN_FILTER"
            elif benchmark_data_incomplete:
                reason = "BENCHMARK_DATA_INCOMPLETE"
            elif maximum_benchmark_average_gate_failed:
                reason = "BENCHMARK_AVERAGE_RETURN_FILTER"
            elif opening_range_stop_invalid:
                reason = "OPENING_RANGE_STOP_INVALID"
            else:
                reason = decision.reason
            self.db.add(OpeningMomentumShadowRun(
                session_date=session_date,
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
                    [asdict(item) for item in decision.ranking],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                candidate_symbol=decision.candidate_symbol,
                market_return_bps=decision.market_return_bps,
                candidate_return_bps=decision.candidate_return_bps,
                excess_return_bps=decision.excess_return_bps,
                candidate_first_five_return_bps=(
                    path_features.first_five_return_bps
                    if path_features is not None
                    else None
                ),
                candidate_last_five_return_bps=(
                    path_features.last_five_return_bps
                    if path_features is not None
                    else None
                ),
                candidate_path_efficiency=(
                    path_efficiency
                ),
                candidate_max_pullback_bps=(
                    path_features.max_pullback_bps
                    if path_features is not None
                    else None
                ),
                candidate_opening_range_bps=(
                    path_features.opening_range_bps
                    if path_features is not None
                    else None
                ),
                candidate_overnight_gap_bps=(
                    candidate_overnight_gap_bps
                ),
                candidate_prev_close_to_signal_bps=(
                    candidate_prev_close_to_signal_bps
                ),
                benchmark_qqq_return_bps=(
                    benchmark_returns["QQQ.US"]
                ),
                benchmark_dia_return_bps=(
                    benchmark_returns["DIA.US"]
                ),
                entry_at=entry_at if status == "OPEN" else None,
                entry_price=(
                    decision.entry_price if status == "OPEN" else None
                ),
                exit_due_at=(
                    entry_at + timedelta(minutes=config.holding_minutes)
                    if status == "OPEN"
                    else None
                ),
                estimated_cost_bps=config.round_trip_cost_bps,
                stop_loss_pct=(
                    opening_range_stop_loss_pct
                    if variant.opening_range_stop
                    and status == "OPEN"
                    else config.stop_loss_pct
                ),
            ))

    def get_status(self) -> OpeningMomentumShadowStatusResponse:
        incumbent_version = self._incumbent_config_version()
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
                    signal_model=identity.signal_model,
                    require_nonnegative_last_five=(
                        identity.require_nonnegative_last_five
                    ),
                    minimum_data_coverage=(
                        identity.minimum_data_coverage
                    ),
                    opening_range_stop=(
                        identity.opening_range_stop
                    ),
                    required_symbols=identity.required_symbols,
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
                config_version=self._incumbent_config_version(),
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
        active_broad_symbols = self._active_broad_symbols()
        early_identity = identities_by_variant[
            "EARLY_BROAD_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=early_identity.variant,
            algorithm_version=early_identity.algorithm_version,
            config_version=early_identity.config_version,
            universe_source=early_identity.universe_source,
            decision_config=early_identity.decision_config,
            minimum_data_coverage=(
                early_identity.minimum_data_coverage
            ),
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        for spec in _EARLY_EXTENSION_SPECS:
            identity = identities_by_variant[spec.variant]
            variants.append(_UniverseVariant(
                variant=identity.variant,
                algorithm_version=identity.algorithm_version,
                config_version=identity.config_version,
                universe_source=identity.universe_source,
                decision_config=identity.decision_config,
                minimum_data_coverage=(
                    identity.minimum_data_coverage
                ),
                required_symbols=identity.required_symbols,
                symbols=tuple(dict.fromkeys(
                    active_broad_symbols
                    + identity.required_symbols
                )),
                selection_run_id=run.id,
            ))
        execution_identity = identities_by_variant[
            "EXECUTION_BROAD_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=execution_identity.variant,
            algorithm_version=execution_identity.algorithm_version,
            config_version=execution_identity.config_version,
            universe_source=execution_identity.universe_source,
            decision_config=execution_identity.decision_config,
            minimum_data_coverage=(
                execution_identity.minimum_data_coverage
            ),
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        path_efficiency_identity = identities_by_variant[
            "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=path_efficiency_identity.variant,
            algorithm_version=(
                path_efficiency_identity.algorithm_version
            ),
            config_version=path_efficiency_identity.config_version,
            universe_source=path_efficiency_identity.universe_source,
            decision_config=(
                path_efficiency_identity.decision_config
            ),
            minimum_data_coverage=(
                path_efficiency_identity.minimum_data_coverage
            ),
            minimum_path_efficiency=(
                path_efficiency_identity.minimum_path_efficiency
            ),
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        weak_breadth_path_identity = identities_by_variant[
            "WEAK_BREADTH_PATH_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=weak_breadth_path_identity.variant,
            algorithm_version=(
                weak_breadth_path_identity.algorithm_version
            ),
            config_version=(
                weak_breadth_path_identity.config_version
            ),
            universe_source=(
                weak_breadth_path_identity.universe_source
            ),
            decision_config=(
                weak_breadth_path_identity.decision_config
            ),
            minimum_data_coverage=(
                weak_breadth_path_identity.minimum_data_coverage
            ),
            minimum_path_efficiency=(
                weak_breadth_path_identity.minimum_path_efficiency
            ),
            maximum_market_return_bps=(
                weak_breadth_path_identity.maximum_market_return_bps
            ),
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        weak_breadth_relaxed_identity = identities_by_variant[
            "WEAK_BREADTH_RELAXED_CHALLENGER"
        ]
        variants.append(replace(
            weak_breadth_relaxed_identity,
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        weak_breadth_index_cohort_identity = identities_by_variant[
            "WEAK_BREADTH_INDEX_COHORT_CHALLENGER"
        ]
        variants.append(replace(
            weak_breadth_index_cohort_identity,
            symbols=tuple(dict.fromkeys(
                active_broad_symbols
                + weak_breadth_index_cohort_identity.required_symbols
            )),
            selection_run_id=run.id,
        ))
        for variant_name in _ETF_REGIME_VARIANTS:
            identity = identities_by_variant[
                cast(_VariantName, variant_name)
            ]
            variants.append(replace(
                identity,
                symbols=tuple(dict.fromkeys(
                    active_broad_symbols + identity.required_symbols
                )),
                selection_run_id=run.id,
            ))
        weak_breadth_wide_stop_identity = identities_by_variant[
            "WEAK_BREADTH_WIDE_STOP_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=weak_breadth_wide_stop_identity.variant,
            algorithm_version=(
                weak_breadth_wide_stop_identity.algorithm_version
            ),
            config_version=(
                weak_breadth_wide_stop_identity.config_version
            ),
            universe_source=(
                weak_breadth_wide_stop_identity.universe_source
            ),
            decision_config=(
                weak_breadth_wide_stop_identity.decision_config
            ),
            minimum_data_coverage=(
                weak_breadth_wide_stop_identity.minimum_data_coverage
            ),
            minimum_path_efficiency=(
                weak_breadth_wide_stop_identity.minimum_path_efficiency
            ),
            maximum_market_return_bps=(
                weak_breadth_wide_stop_identity.maximum_market_return_bps
            ),
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        opening_range_stop_identity = identities_by_variant[
            "OPENING_RANGE_STOP_CHALLENGER"
        ]
        variants.append(_UniverseVariant(
            variant=opening_range_stop_identity.variant,
            algorithm_version=(
                opening_range_stop_identity.algorithm_version
            ),
            config_version=opening_range_stop_identity.config_version,
            universe_source=opening_range_stop_identity.universe_source,
            decision_config=(
                opening_range_stop_identity.decision_config
            ),
            minimum_data_coverage=(
                opening_range_stop_identity.minimum_data_coverage
            ),
            opening_range_stop=True,
            symbols=active_broad_symbols,
            selection_run_id=run.id,
        ))
        for spec in _EXECUTION_EXTENSION_SPECS:
            identity = identities_by_variant[spec.variant]
            variants.append(_UniverseVariant(
                variant=identity.variant,
                algorithm_version=identity.algorithm_version,
                config_version=identity.config_version,
                universe_source=identity.universe_source,
                decision_config=identity.decision_config,
                minimum_data_coverage=(
                    identity.minimum_data_coverage
                ),
                required_symbols=identity.required_symbols,
                symbols=tuple(dict.fromkeys(
                    active_broad_symbols
                    + identity.required_symbols
                )),
                selection_run_id=run.id,
            ))
        reversal_identity = identities_by_variant[
            "REVERSAL_CHALLENGER"
        ]
        variants.append(
            _UniverseVariant(
                variant=reversal_identity.variant,
                algorithm_version=(
                    reversal_identity.algorithm_version
                ),
                config_version=reversal_identity.config_version,
                universe_source=reversal_identity.universe_source,
                decision_config=reversal_identity.decision_config,
                signal_model=reversal_identity.signal_model,
                symbols=incumbent_symbols,
                selection_run_id=run.id,
            )
        )
        challenger_symbols = tuple(
            row.symbol
            for row in challenger_selection
            if row.selected
        )
        for variant_name in (
            "CONTINUATION_CHALLENGER",
            "BREADTH_GATED_CHALLENGER",
            "LAST5_POSITIVE_CHALLENGER",
            "LAST5_ONLY_CHALLENGER",
        ):
            identity = identities_by_variant[variant_name]
            variants.append(
                _UniverseVariant(
                    variant=identity.variant,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    universe_source=identity.universe_source,
                    decision_config=identity.decision_config,
                    signal_model=identity.signal_model,
                    require_nonnegative_last_five=(
                        identity.require_nonnegative_last_five
                    ),
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
                config_version=self._incumbent_config_version(),
                universe_source=_INCUMBENT_SOURCE,
                decision_config=self.config,
            )
        ]
        if settings.opening_momentum_challenger_enabled:
            universe_config = self._continuation_config()
            breadth_config = self._breadth_gate_config()
            early_config = self._early_broad_config()
            execution_config = self._execution_broad_config()
            weak_breadth_wide_stop_config = replace(
                execution_config,
                stop_loss_pct=_WEAK_BREADTH_WIDE_STOP_PCT,
            )
            opening_range_stop_config = replace(
                execution_config,
                stop_loss_pct=_OPENING_RANGE_STOP_MAX_PCT,
            )
            variants.append(_UniverseVariant(
                variant="EARLY_BROAD_CHALLENGER",
                algorithm_version=_EARLY_BROAD_ALGORITHM_VERSION,
                config_version=self._evidence_config_version(
                    f"{early_config.version_hash()}:"
                    f"{_EARLY_BROAD_VERSION}"
                ),
                universe_source=_EARLY_BROAD_SOURCE,
                decision_config=early_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
            ))
            for spec in _EARLY_EXTENSION_SPECS:
                variants.append(_UniverseVariant(
                    variant=spec.variant,
                    algorithm_version=spec.algorithm_version,
                    config_version=self._evidence_config_version(
                        f"{early_config.version_hash()}:"
                        f"{spec.version}"
                    ),
                    universe_source=spec.universe_source,
                    decision_config=early_config,
                    minimum_data_coverage=(
                        _EARLY_BROAD_MINIMUM_COVERAGE
                    ),
                    required_symbols=(spec.symbol,),
                ))
            variants.append(self.execution_variant_identity())
            variants.append(_UniverseVariant(
                variant=(
                    "EXECUTION_PATH_EFFICIENCY_CHALLENGER"
                ),
                algorithm_version=(
                    _EXECUTION_PATH_EFFICIENCY_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{execution_config.version_hash()}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_VERSION}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}"
                ),
                universe_source=(
                    _EXECUTION_PATH_EFFICIENCY_SOURCE
                ),
                decision_config=execution_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                minimum_path_efficiency=(
                    _EXECUTION_PATH_EFFICIENCY_MINIMUM
                ),
            ))
            variants.append(self.paper_execution_variant_identity())
            variants.append(_UniverseVariant(
                variant="WEAK_BREADTH_RELAXED_CHALLENGER",
                algorithm_version=(
                    _WEAK_BREADTH_RELAXED_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{execution_config.version_hash()}:"
                    f"{_WEAK_BREADTH_RELAXED_VERSION}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                    f"{_WEAK_BREADTH_RELAXED_MAXIMUM_MARKET_RETURN_BPS:.1f}"
                ),
                universe_source=_WEAK_BREADTH_RELAXED_SOURCE,
                decision_config=execution_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                minimum_path_efficiency=(
                    _EXECUTION_PATH_EFFICIENCY_MINIMUM
                ),
                maximum_market_return_bps=(
                    _WEAK_BREADTH_RELAXED_MAXIMUM_MARKET_RETURN_BPS
                ),
            ))
            variants.append(_UniverseVariant(
                variant="WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
                algorithm_version=(
                    _WEAK_BREADTH_INDEX_COHORT_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{execution_config.version_hash()}:"
                    f"{_WEAK_BREADTH_INDEX_COHORT_VERSION}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                    f"{_WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS:.1f}"
                ),
                universe_source=(
                    _WEAK_BREADTH_INDEX_COHORT_SOURCE
                ),
                decision_config=execution_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                minimum_path_efficiency=(
                    _EXECUTION_PATH_EFFICIENCY_MINIMUM
                ),
                maximum_market_return_bps=(
                    _WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS
                ),
                required_symbols=(
                    _WEAK_BREADTH_INDEX_COHORT_SYMBOLS
                ),
            ))
            variants.append(_UniverseVariant(
                variant="ETF_REGIME_PATH_CHALLENGER",
                algorithm_version=(
                    _ETF_REGIME_PATH_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{execution_config.version_hash()}:"
                    f"{_ETF_REGIME_PATH_VERSION}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                    f"{_ETF_REGIME_MAXIMUM_AVERAGE_RETURN_BPS:.1f}"
                ),
                universe_source=_ETF_REGIME_PATH_SOURCE,
                decision_config=execution_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                minimum_path_efficiency=(
                    _EXECUTION_PATH_EFFICIENCY_MINIMUM
                ),
                maximum_benchmark_average_return_bps=(
                    _ETF_REGIME_MAXIMUM_AVERAGE_RETURN_BPS
                ),
            ))
            for variant_name, symbol, universe_source in (
                _ETF_REGIME_EXTENSION_SPECS
            ):
                variants.append(_UniverseVariant(
                    variant=cast(_VariantName, variant_name),
                    algorithm_version=(
                        f"{_ETF_REGIME_PATH_ALGORITHM_VERSION}+"
                        f"{_ETF_REGIME_EXTENSION_VERSION}+"
                        f"{symbol.removesuffix('.US').lower()}"
                    ),
                    config_version=self._evidence_config_version(
                        f"{execution_config.version_hash()}:"
                        f"{_ETF_REGIME_PATH_VERSION}:"
                        f"{_ETF_REGIME_EXTENSION_VERSION}:"
                        f"{symbol}:"
                        f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                        f"{_ETF_REGIME_MAXIMUM_AVERAGE_RETURN_BPS:.1f}"
                    ),
                    universe_source=universe_source,
                    decision_config=execution_config,
                    minimum_data_coverage=(
                        _EARLY_BROAD_MINIMUM_COVERAGE
                    ),
                    minimum_path_efficiency=(
                        _EXECUTION_PATH_EFFICIENCY_MINIMUM
                    ),
                    maximum_benchmark_average_return_bps=(
                        _ETF_REGIME_MAXIMUM_AVERAGE_RETURN_BPS
                    ),
                    required_symbols=(symbol,),
                ))
            variants.append(_UniverseVariant(
                variant="WEAK_BREADTH_WIDE_STOP_CHALLENGER",
                algorithm_version=(
                    _WEAK_BREADTH_WIDE_STOP_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{weak_breadth_wide_stop_config.version_hash()}:"
                    f"{_WEAK_BREADTH_WIDE_STOP_VERSION}:"
                    f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                    f"{_WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS:.1f}"
                ),
                universe_source=_WEAK_BREADTH_WIDE_STOP_SOURCE,
                decision_config=weak_breadth_wide_stop_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                minimum_path_efficiency=(
                    _EXECUTION_PATH_EFFICIENCY_MINIMUM
                ),
                maximum_market_return_bps=(
                    _WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS
                ),
            ))
            variants.append(_UniverseVariant(
                variant="OPENING_RANGE_STOP_CHALLENGER",
                algorithm_version=(
                    _OPENING_RANGE_STOP_ALGORITHM_VERSION
                ),
                config_version=self._evidence_config_version(
                    f"{opening_range_stop_config.version_hash()}:"
                    f"{_OPENING_RANGE_STOP_VERSION}"
                ),
                universe_source=_OPENING_RANGE_STOP_SOURCE,
                decision_config=opening_range_stop_config,
                minimum_data_coverage=(
                    _EARLY_BROAD_MINIMUM_COVERAGE
                ),
                opening_range_stop=True,
            ))
            for spec in _EXECUTION_EXTENSION_SPECS:
                variants.append(_UniverseVariant(
                    variant=spec.variant,
                    algorithm_version=spec.algorithm_version,
                    config_version=self._evidence_config_version(
                        f"{execution_config.version_hash()}:"
                        f"{spec.version}"
                    ),
                    universe_source=spec.universe_source,
                    decision_config=execution_config,
                    minimum_data_coverage=(
                        _EARLY_BROAD_MINIMUM_COVERAGE
                    ),
                    required_symbols=(spec.symbol,),
                ))
            variants.append(
                _UniverseVariant(
                    variant="REVERSAL_CHALLENGER",
                    algorithm_version=REVERSAL_ALGORITHM_VERSION,
                    config_version=self._evidence_config_version(
                        f"{self.config.version_hash()}:"
                        f"{REVERSAL_ALGORITHM_VERSION}"
                    ),
                    universe_source=_REVERSAL_SOURCE,
                    decision_config=self.config,
                    signal_model="REVERSAL",
                )
            )
            variants.append(
                _UniverseVariant(
                    variant="CONTINUATION_CHALLENGER",
                    algorithm_version=(
                        _CONTINUATION_ALGORITHM_VERSION
                    ),
                    config_version=(
                        opening_momentum_variant_config_version(
                            self._evidence_config_version(
                                self.config.version_hash()
                            ),
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
                            self._evidence_config_version(
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
            variants.append(
                _UniverseVariant(
                    variant="LAST5_POSITIVE_CHALLENGER",
                    algorithm_version=(
                        _LAST_FIVE_GATE_ALGORITHM_VERSION
                    ),
                    config_version=(
                        opening_momentum_variant_config_version(
                            self._evidence_config_version(
                                f"{breadth_config.version_hash()}:"
                                f"{_LAST_FIVE_GATE_VERSION}"
                            ),
                            universe_config,
                        )
                    ),
                    universe_source=_LAST_FIVE_GATE_SOURCE,
                    decision_config=breadth_config,
                    require_nonnegative_last_five=True,
                )
            )
            variants.append(
                _UniverseVariant(
                    variant="LAST5_ONLY_CHALLENGER",
                    algorithm_version=(
                        _LAST_FIVE_ONLY_ALGORITHM_VERSION
                    ),
                    config_version=(
                        opening_momentum_variant_config_version(
                            self._evidence_config_version(
                                f"{self.config.version_hash()}:"
                                f"{_LAST_FIVE_GATE_VERSION}"
                            ),
                            universe_config,
                        )
                    ),
                    universe_source=_LAST_FIVE_ONLY_SOURCE,
                    decision_config=self.config,
                    require_nonnegative_last_five=True,
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

    def _early_broad_config(self) -> OpeningMomentumConfig:
        return replace(
            self.config,
            signal_minutes=3,
            holding_minutes=120,
            minimum_market_return_bps=-50.0,
            minimum_candidate_return_bps=50.0,
            minimum_excess_return_bps=25.0,
        )

    def _execution_broad_config(self) -> OpeningMomentumConfig:
        return opening_execution_config(self.config)

    def execution_variant_identity(self) -> _UniverseVariant:
        config = self._execution_broad_config()
        return _UniverseVariant(
            variant="EXECUTION_BROAD_CHALLENGER",
            algorithm_version=_EXECUTION_BROAD_ALGORITHM_VERSION,
            config_version=self._evidence_config_version(
                f"{config.version_hash()}:{_EXECUTION_BROAD_VERSION}"
            ),
            universe_source=_EXECUTION_BROAD_SOURCE,
            decision_config=config,
            minimum_data_coverage=_EARLY_BROAD_MINIMUM_COVERAGE,
        )

    def paper_execution_variant_identity(self) -> _UniverseVariant:
        config = self._execution_broad_config()
        return _UniverseVariant(
            variant=_PAPER_EXECUTION_VARIANT,
            algorithm_version=_WEAK_BREADTH_PATH_ALGORITHM_VERSION,
            config_version=self._evidence_config_version(
                f"{config.version_hash()}:"
                f"{_WEAK_BREADTH_PATH_VERSION}:"
                f"{_EXECUTION_PATH_EFFICIENCY_MINIMUM:.2f}:"
                f"{_WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS:.1f}"
            ),
            universe_source=_WEAK_BREADTH_PATH_SOURCE,
            decision_config=config,
            minimum_data_coverage=_EARLY_BROAD_MINIMUM_COVERAGE,
            minimum_path_efficiency=_EXECUTION_PATH_EFFICIENCY_MINIMUM,
            maximum_market_return_bps=(
                _WEAK_BREADTH_MAXIMUM_MARKET_RETURN_BPS
            ),
        )

    def _active_broad_symbols(self) -> tuple[str, ...]:
        rows = (
            self.db.query(StrategyV2ShadowConfig)
            .filter(
                StrategyV2ShadowConfig.enabled.is_(True),
                StrategyV2ShadowConfig.opening_momentum_execution_eligible.is_(
                    True
                ),
                StrategyV2ShadowConfig.symbol.like("%.US"),
            )
            .order_by(StrategyV2ShadowConfig.symbol.asc())
            .all()
        )
        return tuple(row.symbol for row in rows)

    def _incumbent_config_version(self) -> str:
        return self._evidence_config_version(
            self.config.version_hash()
        )

    @staticmethod
    def _evidence_config_version(
        opening_config_version: str,
    ) -> str:
        return opening_momentum_evidence_config_version(
            opening_config_version,
            universe_algorithm_version=UNIVERSE_ALGORITHM_VERSION,
            catalog_source_version=CATALOG_SOURCE_VERSION,
        )

    def _close_if_due(
        self,
        row: OpeningMomentumShadowRun,
        current: datetime,
        *,
        settlement_candles: dict[
            str, dict[datetime, _Candle]
        ] | None = None,
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
        candle_cache = (
            settlement_candles
            if settlement_candles is not None
            else {}
        )
        if row.candidate_symbol not in candle_cache:
            raw_bars = self.candle_provider.get_candlesticks(
                row.candidate_symbol,
                "MIN_1",
                _CANDLE_COUNT,
            )
            candle_cache[row.candidate_symbol] = {
                bar.timestamp: bar
                for bar in self._coerce_candles(raw_bars)
            }
        candles_by_timestamp = candle_cache[row.candidate_symbol]
        history_reader = getattr(
            self.candle_provider,
            "get_history_candlesticks_by_offset",
            None,
        )
        needs_stop_path = row.stop_loss_pct is not None
        entry_at = _optional_utc(row.entry_at)
        stop_path_complete = (
            not needs_stop_path
            or entry_at is None
            or self._minute_path_complete(
                candles_by_timestamp,
                start_at=entry_at,
                end_at=exit_due_at,
            )
        )
        if callable(history_reader) and (
            exit_due_at not in candles_by_timestamp
            or not stop_path_complete
        ):
            history_start = (
                entry_at - _BAR_DURATION
                if needs_stop_path and entry_at is not None
                else exit_due_at - _BAR_DURATION
            )
            history_count = 10
            if entry_at is not None:
                history_count = min(
                    1_000,
                    max(
                        history_count,
                        int(
                            (exit_due_at - entry_at).total_seconds()
                            // 60
                        )
                        + 10,
                    ),
                )
            historical = history_reader(
                row.candidate_symbol,
                "MIN_1",
                history_count,
                history_start,
            )
            if isinstance(historical, list):
                candles_by_timestamp.update({
                    bar.timestamp: bar
                    for bar in self._coerce_candles(historical)
                })
        exit_bar = candles_by_timestamp.get(exit_due_at)
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
        entry_at = _optional_utc(row.entry_at)
        if entry_at is None:
            raise ValueError(
                "open opening-momentum run has no entry timestamp"
            )
        outcome = self._exit_outcome(
            tuple(candles_by_timestamp.values()),
            entry_at=entry_at,
            exit_due_at=exit_due_at,
            entry_price=row.entry_price,
            stop_loss_pct=row.stop_loss_pct,
        )
        gross_return_bps, _ = shadow_round_trip_return_bps(
            entry_price=row.entry_price,
            exit_price=outcome.price,
        )
        net_return_bps = (
            gross_return_bps - float(row.estimated_cost_bps)
        )
        row.status = "CLOSED"
        row.reason = outcome.reason
        row.exit_at = outcome.exited_at
        row.exit_price = outcome.price
        row.gross_return_bps = gross_return_bps
        row.net_return_bps = net_return_bps
        row.maximum_adverse_excursion_bps = (
            outcome.maximum_adverse_excursion_bps
        )
        row.maximum_favorable_excursion_bps = (
            outcome.maximum_favorable_excursion_bps
        )
        self.db.add(row)
        self.db.commit()

    @staticmethod
    def _minute_path_complete(
        candles_by_timestamp: dict[datetime, _Candle],
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> bool:
        if end_at < start_at:
            return False
        expected_bars = int(
            (end_at - start_at).total_seconds() // 60
        ) + 1
        return all(
            start_at + timedelta(minutes=offset)
            in candles_by_timestamp
            for offset in range(expected_bars)
        )

    @staticmethod
    def _exit_outcome(
        candles: tuple[_Candle, ...],
        *,
        entry_at: datetime,
        exit_due_at: datetime,
        entry_price: float,
        stop_loss_pct: float | None,
    ) -> _ExitOutcome:
        by_timestamp = {item.timestamp: item for item in candles}
        exit_bar = by_timestamp.get(exit_due_at)
        if exit_bar is None:
            raise ValueError("opening-momentum exit bar is unavailable")
        highest = entry_price
        lowest = entry_price
        stop_price = (
            entry_price * (1 - stop_loss_pct / 100)
            if stop_loss_pct is not None
            else None
        )
        for timestamp in sorted(
            value
            for value in by_timestamp
            if entry_at <= value < exit_due_at
        ):
            bar = by_timestamp[timestamp]
            if stop_price is not None and bar.open <= stop_price:
                highest = max(highest, bar.open)
                lowest = min(lowest, bar.open)
                return _ExitOutcome(
                    exited_at=timestamp,
                    price=bar.open,
                    reason="STOP_LOSS_EXIT",
                    maximum_adverse_excursion_bps=(
                        (lowest / entry_price - 1) * 10_000
                    ),
                    maximum_favorable_excursion_bps=(
                        (highest / entry_price - 1) * 10_000
                    ),
                )
            if stop_price is not None and bar.low <= stop_price:
                highest = max(highest, bar.open)
                lowest = min(lowest, stop_price)
                return _ExitOutcome(
                    exited_at=timestamp,
                    price=stop_price,
                    reason="STOP_LOSS_EXIT",
                    maximum_adverse_excursion_bps=(
                        (lowest / entry_price - 1) * 10_000
                    ),
                    maximum_favorable_excursion_bps=(
                        (highest / entry_price - 1) * 10_000
                    ),
                )
            highest = max(highest, bar.high)
            lowest = min(lowest, bar.low)
        highest = max(highest, exit_bar.open)
        lowest = min(lowest, exit_bar.open)
        return _ExitOutcome(
            exited_at=exit_due_at,
            price=exit_bar.open,
            reason="FIXED_HOLD_EXIT",
            maximum_adverse_excursion_bps=(
                (lowest / entry_price - 1) * 10_000
            ),
            maximum_favorable_excursion_bps=(
                (highest / entry_price - 1) * 10_000
            ),
        )

    def _config_response(
        self,
    ) -> OpeningMomentumShadowConfigResponse:
        return OpeningMomentumShadowConfigResponse(
            enabled=settings.opening_momentum_shadow_enabled,
            algorithm_version=ALGORITHM_VERSION,
            config_version=self._incumbent_config_version(),
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
            stop_loss_pct=self.config.stop_loss_pct,
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
        rows_by_date = {
            config_version: {
                row.session_date: row
                for row in rows
            }
            for config_version, rows in rows_by_version.items()
        }
        identities_by_variant = {
            identity.variant: identity for identity in identities
        }
        incumbent_identity = identities_by_variant["INCUMBENT"]

        responses: list[OpeningMomentumShadowVariantResponse] = []
        paired_deltas_by_variant: dict[str, list[float]] = {}
        for identity in identities:
            is_early_extension = (
                identity.variant in _EARLY_EXTENSION_VARIANTS
            )
            is_execution_extension = (
                identity.variant in _EXECUTION_EXTENSION_VARIANTS
            )
            uses_execution_baseline = (
                is_execution_extension
                or identity.variant in {
                    "EXECUTION_PATH_EFFICIENCY_CHALLENGER",
                    "WEAK_BREADTH_PATH_CHALLENGER",
                    "OPENING_RANGE_STOP_CHALLENGER",
                }
            )
            uses_weak_breadth_baseline = (
                identity.variant in {
                    "WEAK_BREADTH_RELAXED_CHALLENGER",
                    "WEAK_BREADTH_INDEX_COHORT_CHALLENGER",
                    "WEAK_BREADTH_WIDE_STOP_CHALLENGER",
                    "ETF_REGIME_PATH_CHALLENGER",
                }
            )
            uses_etf_regime_baseline = (
                identity.variant in _ETF_REGIME_EXTENSION_VARIANTS
            )
            requires_displacement_evidence = (
                is_early_extension
                or is_execution_extension
                or uses_weak_breadth_baseline
                or uses_etf_regime_baseline
            )
            identity_rows_by_date = rows_by_date[
                identity.config_version
            ]
            comparison_baseline: Literal[
                "INCUMBENT",
                "EARLY_BROAD_CHALLENGER",
                "EXECUTION_BROAD_CHALLENGER",
                "WEAK_BREADTH_PATH_CHALLENGER",
                "ETF_REGIME_PATH_CHALLENGER",
            ] | None
            if identity.variant == "INCUMBENT":
                comparison_baseline = None
            elif is_early_extension:
                comparison_baseline = "EARLY_BROAD_CHALLENGER"
            elif uses_execution_baseline:
                comparison_baseline = "EXECUTION_BROAD_CHALLENGER"
            elif uses_etf_regime_baseline:
                comparison_baseline = "ETF_REGIME_PATH_CHALLENGER"
            elif uses_weak_breadth_baseline:
                comparison_baseline = "WEAK_BREADTH_PATH_CHALLENGER"
            else:
                comparison_baseline = "INCUMBENT"
            if is_early_extension:
                comparison_identity = identities_by_variant[
                    "EARLY_BROAD_CHALLENGER"
                ]
            elif uses_execution_baseline:
                comparison_identity = identities_by_variant[
                    "EXECUTION_BROAD_CHALLENGER"
                ]
            elif uses_etf_regime_baseline:
                comparison_identity = identities_by_variant[
                    "ETF_REGIME_PATH_CHALLENGER"
                ]
            elif uses_weak_breadth_baseline:
                comparison_identity = identities_by_variant[
                    "WEAK_BREADTH_PATH_CHALLENGER"
                ]
            else:
                comparison_identity = incumbent_identity
            comparison_rows_by_date = rows_by_date[
                comparison_identity.config_version
            ]
            comparison_dates_available = set(
                comparison_rows_by_date
            )
            comparison_dates = (
                comparison_dates_available
                if identity.variant == "INCUMBENT"
                else comparison_dates_available.intersection(
                    identity_rows_by_date
                )
            )
            metrics = self._metrics(
                identity.config_version,
                session_dates=comparison_dates,
            )
            comparison_response = None
            if identity.variant != "INCUMBENT":
                resolved_comparison_dates = [
                    session_date
                    for session_date in sorted(comparison_dates)
                    if (
                        self._paired_policy_return(
                            comparison_rows_by_date[session_date]
                        )
                        is not None
                        and self._paired_policy_return(
                            identity_rows_by_date[session_date]
                        )
                        is not None
                    )
                ]
                incumbent_returns = [
                    cast(
                        float,
                        self._paired_policy_return(
                            comparison_rows_by_date[session_date]
                        ),
                    )
                    for session_date in resolved_comparison_dates
                ]
                challenger_returns = [
                    cast(
                        float,
                        self._paired_policy_return(
                            identity_rows_by_date[session_date]
                        ),
                    )
                    for session_date in resolved_comparison_dates
                ]
                paired_deltas_by_variant[identity.variant] = [
                    challenger_return - incumbent_return
                    for incumbent_return, challenger_return in zip(
                        incumbent_returns,
                        challenger_returns,
                        strict=True,
                    )
                ]
                comparison = compare_opening_momentum_variants(
                    incumbent_returns,
                    challenger_returns,
                )
                comparison_response = (
                    OpeningMomentumPairedComparisonResponse.model_validate(
                        asdict(comparison)
                    )
                )
                if requires_displacement_evidence:
                    comparison_response = (
                        self._apply_extension_evidence_gate(
                            comparison_response,
                            comparison_rows_by_date=(
                                comparison_rows_by_date
                            ),
                            challenger_rows_by_date=(
                                identity_rows_by_date
                            ),
                            resolved_dates=(
                                resolved_comparison_dates
                            ),
                        )
                    )
            responses.append(
                OpeningMomentumShadowVariantResponse(
                    variant=identity.variant,
                    universe_source=identity.universe_source,
                    algorithm_version=identity.algorithm_version,
                    config_version=identity.config_version,
                    signal_minutes=(
                        identity.decision_config.signal_minutes
                    ),
                    minimum_market_return_bps=(
                        identity.decision_config.minimum_market_return_bps
                    ),
                    minimum_candidate_return_bps=(
                        identity.decision_config.minimum_candidate_return_bps
                    ),
                    minimum_excess_return_bps=(
                        identity.decision_config.minimum_excess_return_bps
                    ),
                    minimum_data_coverage=(
                        identity.minimum_data_coverage
                    ),
                    minimum_path_efficiency=(
                        identity.minimum_path_efficiency
                    ),
                    maximum_market_return_bps=(
                        identity.maximum_market_return_bps
                    ),
                    maximum_benchmark_average_return_bps=(
                        identity.maximum_benchmark_average_return_bps
                    ),
                    required_symbols=list(
                        identity.required_symbols
                    ),
                    holding_minutes=(
                        identity.decision_config.holding_minutes
                    ),
                    stop_loss_pct=(
                        identity.decision_config.stop_loss_pct
                    ),
                    comparison_sessions=len(comparison_dates),
                    comparison_baseline=comparison_baseline,
                    latest=(
                        self._run_response(
                            rows_by_version[
                                identity.config_version
                            ][-1]
                        )
                        if rows_by_version[identity.config_version]
                        else None
                    ),
                    metrics=metrics,
                    comparison=comparison_response,
                )
            )
        return self._apply_multiple_testing_evidence_gate(
            responses,
            paired_deltas_by_variant=paired_deltas_by_variant,
        )

    @staticmethod
    def _apply_multiple_testing_evidence_gate(
        responses: list[OpeningMomentumShadowVariantResponse],
        *,
        paired_deltas_by_variant: dict[str, list[float]],
    ) -> list[OpeningMomentumShadowVariantResponse]:
        family_indices: dict[str, list[int]] = {}
        for index, response in enumerate(responses):
            if (
                response.comparison is None
                or response.comparison_baseline is None
            ):
                continue
            family_indices.setdefault(
                response.comparison_baseline,
                [],
            ).append(index)

        updated_responses = list(responses)
        for indices in family_indices.values():
            raw_pvalues = [
                one_sample_greater_pvalue(
                    paired_deltas_by_variant.get(
                        responses[index].variant,
                        [],
                    )
                )
                for index in indices
            ]
            adjusted_pvalues = holm_adjusted_pvalues(raw_pvalues)
            family_size = len(indices)
            for index, adjusted_pvalue in zip(
                indices,
                adjusted_pvalues,
                strict=True,
            ):
                response = responses[index]
                comparison = response.comparison
                if comparison is None:
                    continue
                enough_sessions = (
                    comparison.resolved_sessions
                    >= comparison.minimum_promotion_sessions
                )
                evidence_passed = (
                    adjusted_pvalue
                    <= _MULTIPLE_TESTING_SIGNIFICANCE_LEVEL
                    if enough_sessions and adjusted_pvalue is not None
                    else None
                )
                promotion_ready = (
                    comparison.promotion_ready
                    and evidence_passed is True
                )
                recommendation = comparison.recommendation
                if comparison.promotion_ready and not promotion_ready:
                    recommendation = "INCONCLUSIVE"

                updated_comparison = comparison.model_copy(update={
                    "multiple_testing_method": (
                        _MULTIPLE_TESTING_METHOD
                    ),
                    "multiple_testing_family_size": family_size,
                    "multiple_testing_adjusted_pvalue": (
                        adjusted_pvalue
                    ),
                    "multiple_testing_evidence_passed": (
                        evidence_passed
                    ),
                    "promotion_ready": promotion_ready,
                    "recommendation": recommendation,
                })
                updated_responses[index] = response.model_copy(update={
                    "comparison": updated_comparison,
                })
        return updated_responses

    @classmethod
    def _apply_extension_evidence_gate(
        cls,
        comparison: OpeningMomentumPairedComparisonResponse,
        *,
        comparison_rows_by_date: dict[
            date, OpeningMomentumShadowRun
        ],
        challenger_rows_by_date: dict[
            date, OpeningMomentumShadowRun
        ],
        resolved_dates: list[date],
    ) -> OpeningMomentumPairedComparisonResponse:
        displacement_deltas: list[float] = []
        for session_date in resolved_dates:
            baseline_row = comparison_rows_by_date[session_date]
            challenger_row = challenger_rows_by_date[session_date]
            if not cls._policy_displaced(
                baseline_row,
                challenger_row,
            ):
                continue
            baseline_return = cls._paired_policy_return(baseline_row)
            challenger_return = cls._paired_policy_return(
                challenger_row
            )
            if baseline_return is None or challenger_return is None:
                continue
            displacement_deltas.append(
                challenger_return - baseline_return
            )

        displacement_sessions = len(displacement_deltas)
        displacement_outperformance_rate = (
            sum(value > 0 for value in displacement_deltas)
            / displacement_sessions
            if displacement_sessions
            else 0.0
        )
        evidence_gate_passed = (
            displacement_sessions
            >= _EXTENSION_MINIMUM_DISPLACEMENT_SESSIONS
        )
        promotion_ready = (
            comparison.resolved_sessions
            >= comparison.minimum_promotion_sessions
            and evidence_gate_passed
            and comparison.confidence_lower_bps is not None
            and comparison.confidence_lower_bps > 0
            and displacement_outperformance_rate
            >= _EXTENSION_MINIMUM_OUTPERFORMANCE_RATE
            and comparison.risk_guard_passed
        )
        if promotion_ready:
            recommendation = "PROMOTION_CANDIDATE"
        elif not evidence_gate_passed:
            recommendation = "COLLECTING"
        elif (
            comparison.resolved_sessions
            >= comparison.minimum_promotion_sessions
            and comparison.confidence_upper_bps is not None
            and comparison.confidence_upper_bps < 0
        ):
            recommendation = "UNDERPERFORMING"
        elif (
            comparison.resolved_sessions
            < comparison.minimum_promotion_sessions
            and comparison.mean_delta_bps > 0
        ):
            recommendation = "EARLY_LEADER"
        elif comparison.mean_delta_bps < 0:
            recommendation = "LAGGING"
        else:
            recommendation = "INCONCLUSIVE"

        payload = comparison.model_dump()
        payload.update({
            "policy_displacement_sessions": displacement_sessions,
            "minimum_policy_displacement_sessions": (
                _EXTENSION_MINIMUM_DISPLACEMENT_SESSIONS
            ),
            "displacement_outperformance_rate": (
                displacement_outperformance_rate
            ),
            "evidence_gate_passed": evidence_gate_passed,
            "promotion_ready": promotion_ready,
            "recommendation": recommendation,
        })
        return OpeningMomentumPairedComparisonResponse.model_validate(
            payload
        )

    @classmethod
    def _policy_displaced(
        cls,
        baseline: OpeningMomentumShadowRun,
        challenger: OpeningMomentumShadowRun,
    ) -> bool:
        baseline_return = cls._paired_policy_return(baseline)
        challenger_return = cls._paired_policy_return(challenger)
        if baseline_return is None or challenger_return is None:
            return False
        return (
            baseline.status != challenger.status
            or baseline.candidate_symbol
            != challenger.candidate_symbol
            or not math.isclose(
                baseline_return,
                challenger_return,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )

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
    def _paired_policy_return(
        row: OpeningMomentumShadowRun,
    ) -> float | None:
        if row.status == "CLOSED":
            if row.net_return_bps is None:
                return None
            value = float(row.net_return_bps)
            return value if math.isfinite(value) else None
        if (
            row.status == "SKIPPED"
            and row.reason not in _NON_COMPARABLE_SKIP_REASONS
        ):
            return 0.0
        return None

    @staticmethod
    def _opening_return_bps(
        candles_by_timestamp: dict[datetime, _Candle],
        *,
        expected_signal_bars: set[datetime],
    ) -> float | None:
        if (
            not expected_signal_bars
            or not expected_signal_bars.issubset(
                candles_by_timestamp
            )
        ):
            return None
        ordered = sorted(expected_signal_bars)
        opening_price = candles_by_timestamp[ordered[0]].open
        signal_close = candles_by_timestamp[ordered[-1]].close
        return (signal_close / opening_price - 1) * 10_000

    @staticmethod
    def _previous_session_close(
        candles: list[_Candle],
        *,
        session_date: date,
    ) -> float | None:
        market_session = get_session("US")
        completed = [
            candle
            for candle in candles
            if market_session.local(candle.timestamp).date()
            < session_date
        ]
        if not completed:
            return None
        return max(completed, key=lambda candle: candle.timestamp).close

    def _historical_candles_before(
        self,
        symbol: str,
        *,
        period: str,
        count: int,
        before: datetime,
    ) -> list[_Candle] | None:
        if self.candle_provider is None:
            return None
        reader = getattr(
            self.candle_provider,
            "get_forward_adjusted_history_candlesticks_before",
            None,
        )
        if not callable(reader):
            reader = getattr(
                self.candle_provider,
                "get_history_candlesticks_before",
                None,
            )
        if not callable(reader):
            return None
        values = reader(symbol, period, count, before)
        if not isinstance(values, list):
            return []
        return self._coerce_candles(values)

    @staticmethod
    def _opening_path_features(
        candles: list[_Candle],
    ) -> _OpeningPathFeatures:
        if len(candles) < 5:
            raise ValueError(
                "opening path features require at least five candles"
            )
        opening_price = candles[0].open
        signal_close = candles[-1].close
        first_five_return_bps = (
            candles[4].close / opening_price - 1
        ) * 10_000
        last_five_return_bps = (
            signal_close / candles[-5].open - 1
        ) * 10_000

        path_efficiency = (
            OpeningMomentumShadowService._opening_path_efficiency(
                candles
            )
        )

        running_high = candles[0].high
        max_pullback_bps = 0.0
        for candle in candles:
            running_high = max(running_high, candle.high)
            max_pullback_bps = min(
                max_pullback_bps,
                (candle.low / running_high - 1) * 10_000,
            )
        opening_range_bps = (
            max(candle.high for candle in candles)
            - min(candle.low for candle in candles)
        ) / opening_price * 10_000
        return _OpeningPathFeatures(
            first_five_return_bps=first_five_return_bps,
            last_five_return_bps=last_five_return_bps,
            path_efficiency=path_efficiency,
            max_pullback_bps=max_pullback_bps,
            opening_range_bps=opening_range_bps,
        )

    @staticmethod
    def _opening_path_efficiency(candles: list[_Candle]) -> float:
        if not candles:
            raise ValueError(
                "opening path efficiency requires at least one candle"
            )
        return opening_path_efficiency(
            opening_price=candles[0].open,
            closing_prices=tuple(candle.close for candle in candles),
        )

    @staticmethod
    def _opening_range_stop_loss_pct(
        *,
        opening_range_low: float | None,
        entry_price: float,
        maximum_stop_loss_pct: float,
    ) -> float | None:
        if opening_range_low is None:
            return None
        values = (opening_range_low, entry_price, maximum_stop_loss_pct)
        if any(
            not math.isfinite(value) or value <= 0
            for value in values
        ):
            return None
        maximum_stop_price = entry_price * (
            1 - maximum_stop_loss_pct / 100
        )
        stop_price = max(opening_range_low, maximum_stop_price)
        if stop_price >= entry_price:
            return None
        return (1 - stop_price / entry_price) * 100

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
            try:
                high_price = float(getattr(value, "high"))
                low_price = float(getattr(value, "low"))
            except (AttributeError, TypeError, ValueError):
                high_price = max(open_price, close_price)
                low_price = min(open_price, close_price)
            if (
                not math.isfinite(high_price)
                or not math.isfinite(low_price)
                or high_price <= 0
                or low_price <= 0
                or high_price < max(
                    open_price,
                    low_price,
                    close_price,
                )
                or low_price > min(
                    open_price,
                    high_price,
                    close_price,
                )
            ):
                high_price = max(open_price, close_price)
                low_price = min(open_price, close_price)
            by_timestamp[timestamp] = _Candle(
                timestamp=timestamp,
                open=open_price,
                high=high_price,
                low=low_price,
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
            candidate_first_five_return_bps=(
                row.candidate_first_five_return_bps
            ),
            candidate_last_five_return_bps=(
                row.candidate_last_five_return_bps
            ),
            candidate_path_efficiency=(
                row.candidate_path_efficiency
            ),
            candidate_max_pullback_bps=(
                row.candidate_max_pullback_bps
            ),
            candidate_opening_range_bps=(
                row.candidate_opening_range_bps
            ),
            candidate_overnight_gap_bps=(
                row.candidate_overnight_gap_bps
            ),
            candidate_prev_close_to_signal_bps=(
                row.candidate_prev_close_to_signal_bps
            ),
            benchmark_qqq_return_bps=(
                row.benchmark_qqq_return_bps
            ),
            benchmark_dia_return_bps=(
                row.benchmark_dia_return_bps
            ),
            benchmark_average_return_bps=(
                (
                    row.benchmark_qqq_return_bps
                    + row.benchmark_dia_return_bps
                )
                / 2
                if (
                    row.benchmark_qqq_return_bps is not None
                    and row.benchmark_dia_return_bps is not None
                )
                else None
            ),
            entry_at=_optional_utc(row.entry_at),
            entry_price=row.entry_price,
            exit_due_at=_optional_utc(row.exit_due_at),
            exit_at=_optional_utc(row.exit_at),
            exit_price=row.exit_price,
            gross_return_bps=row.gross_return_bps,
            estimated_cost_bps=row.estimated_cost_bps,
            net_return_bps=row.net_return_bps,
            stop_loss_pct=row.stop_loss_pct,
            maximum_adverse_excursion_bps=(
                row.maximum_adverse_excursion_bps
            ),
            maximum_favorable_excursion_bps=(
                row.maximum_favorable_excursion_bps
            ),
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
