from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, Sequence

from app.cli.import_opening_activity import (
    OpeningActivityRecord,
    load_opening_activity_cache,
)
from app.cli.opening_extension_research import (
    RawMinuteBar,
    _configure_longport_environment,
    _load_cache,
    _merge_bars,
    _parse_integer_grid,
    _parse_symbols,
    _read_cache_payload,
    _save_cache,
)
from app.core.broker import BrokerCandle, BrokerGateway
from app.core.market_calendar import get_session
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_stocks_in_play_opening_range_breakout,
)


RESEARCH_VERSION = "relative-volume-orb-causal-research-v2"
CANDIDATE_PANEL_VERSION = "relative-volume-orb-candidate-panel-v1"
CandidateSelectionMode = Literal[
    "BREAKOUT_DEPTH",
    "ACTIVITY_RATIO",
    "OPENING_RETURN",
]
_CANDIDATE_SELECTION_MODES: tuple[CandidateSelectionMode, ...] = (
    "BREAKOUT_DEPTH",
    "ACTIVITY_RATIO",
    "OPENING_RETURN",
)


class HistoricalCandleProvider(Protocol):
    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]: ...


@dataclass(frozen=True)
class RelativeVolumeOrbResearchConfig:
    signal_minutes: int = 5
    execution_delay_minutes: int = 1
    holding_minutes: int = 60
    lookback_sessions: int = 14
    top_n: int = 5
    minimum_activity_ratio: float = 1.0
    minimum_universe_size: int = 8
    minimum_data_coverage: float = 0.95
    stop_loss_cap_pct: float = 4.0
    round_trip_cost_bps: float = 30.0
    discovery_ratio: float = 0.60
    candidate_selection_mode: CandidateSelectionMode = "BREAKOUT_DEPTH"

    def __post_init__(self) -> None:
        positive_integers = (
            self.signal_minutes,
            self.execution_delay_minutes,
            self.holding_minutes,
            self.lookback_sessions,
            self.top_n,
            self.minimum_universe_size,
        )
        if any(value <= 0 for value in positive_integers):
            raise ValueError("research integer parameters must be positive")
        finite_values = (
            self.minimum_activity_ratio,
            self.minimum_data_coverage,
            self.stop_loss_cap_pct,
            self.round_trip_cost_bps,
            self.discovery_ratio,
        )
        if any(not math.isfinite(value) for value in finite_values):
            raise ValueError("research parameters must be finite")
        if self.minimum_activity_ratio <= 0:
            raise ValueError("minimum activity ratio must be positive")
        if not 0 < self.minimum_data_coverage <= 1:
            raise ValueError("minimum data coverage must be in (0, 1]")
        if not 0 < self.stop_loss_cap_pct <= 20:
            raise ValueError("stop-loss cap must be in (0, 20]")
        if not 0 <= self.round_trip_cost_bps <= 200:
            raise ValueError("round-trip cost must be in [0, 200]")
        if not 0 < self.discovery_ratio < 1:
            raise ValueError("discovery ratio must be in (0, 1)")
        if self.candidate_selection_mode not in _CANDIDATE_SELECTION_MODES:
            raise ValueError("candidate selection mode is unsupported")

    @property
    def required_maximum_offset(self) -> int:
        """Last minute offset required by the production exit semantics."""

        return (
            self.signal_minutes
            + self.execution_delay_minutes
            + self.holding_minutes
        )

    def opening_config(self) -> OpeningMomentumConfig:
        return OpeningMomentumConfig(
            signal_minutes=self.signal_minutes,
            execution_delay_minutes=self.execution_delay_minutes,
            holding_minutes=self.holding_minutes,
            minimum_universe_size=self.minimum_universe_size,
            minimum_market_return_bps=-10_000.0,
            minimum_candidate_return_bps=0.0,
            minimum_excess_return_bps=0.0,
            one_side_fee_rate=0.0005,
            one_side_slippage_bps=10.0,
            stop_loss_pct=self.stop_loss_cap_pct,
        )


@dataclass(frozen=True)
class RelativeVolumeOrbTrade:
    session_date: date
    symbol: str
    activity_ratio: float
    entry_price: float
    stop_price: float
    exit_price: float
    exit_reason: str
    gross_return_bps: float
    net_return_bps: float


@dataclass(frozen=True)
class RelativeVolumeOrbSession:
    session_date: date
    status: str
    reason: str
    observed_symbols: int
    ratio_symbols: int
    top_symbols: tuple[str, ...]
    candidate_symbol: str | None = None
    candidate_activity_ratio: float | None = None


@dataclass(frozen=True)
class RelativeVolumeOrbResearchResult:
    config: RelativeVolumeOrbResearchConfig
    universe: tuple[str, ...]
    sessions: tuple[RelativeVolumeOrbSession, ...]
    trades: tuple[RelativeVolumeOrbTrade, ...]


@dataclass(frozen=True)
class RelativeVolumeOrbCacheExtensionReport:
    required_maximum_offset: int
    candidate_sessions: int
    incomplete_candidate_sessions_before: int
    fetch_requests: int
    fetched_bars: int
    incomplete_candidate_sessions_after: int


@dataclass(frozen=True)
class RelativeVolumeOrbCandidateSignal:
    session_date: date
    symbol: str
    activity_ratio: float
    opening_return_bps: float
    breakout_depth_bps: float
    entry_price: float
    stop_price: float


@dataclass(frozen=True)
class RelativeVolumeOrbCandidateOutcome:
    session_date: date
    symbol: str
    activity_ratio: float
    opening_return_bps: float
    breakout_depth_bps: float
    entry_price: float
    stop_price: float
    exit_price: float
    exit_reason: str
    gross_return_bps: float
    net_return_bps: float


@dataclass(frozen=True)
class RelativeVolumeOrbCandidatePanel:
    config: RelativeVolumeOrbResearchConfig
    universe: tuple[str, ...]
    evaluable_dates: tuple[date, ...]
    candidates: tuple[RelativeVolumeOrbCandidateOutcome, ...]


@dataclass(frozen=True)
class RelativeVolumeOrbPanelCacheExtensionReport:
    required_maximum_offset: int
    candidate_sessions: int
    candidate_paths: int
    incomplete_candidate_paths_before: int
    fetch_requests: int
    fetched_bars: int
    incomplete_candidate_paths_after: int


@dataclass(frozen=True)
class _RelativeVolumeOrbSessionInput:
    session_date: date
    bars_by_symbol: dict[str, dict[int, RawMinuteBar]]
    observations: tuple[OpeningMomentumObservation, ...]
    activity_ratios: dict[str, float]
    top_symbols: tuple[str, ...]
    opening_range_highs: dict[str, float]
    opening_range_lows: dict[str, float]
    observed_symbols: int
    ratio_symbols: int
    data_complete: bool


def load_research_inputs(
    *,
    ohlc_cache_path: Path,
    activity_cache_path: Path,
    start_date: date,
    end_date: date,
    required_maximum_offset: int,
) -> tuple[
    dict[str, tuple[RawMinuteBar, ...]],
    tuple[OpeningActivityRecord, ...],
]:
    """Load both caches and fail when the OHLC horizon cannot settle exits."""

    bars_by_symbol = _load_cache(
        ohlc_cache_path,
        start_date=start_date,
        end_date=end_date,
        retained_minutes_after_open=required_maximum_offset,
    )
    if not bars_by_symbol:
        raise ValueError("opening OHLC research cache is empty")
    activity_records, _ = load_opening_activity_cache(
        activity_cache_path,
        through_date=end_date,
    )
    filtered_activity = tuple(
        item
        for item in activity_records
        if start_date <= item.session_date <= end_date
    )
    if not filtered_activity:
        raise ValueError("opening activity research cache is empty")
    return bars_by_symbol, filtered_activity


def load_seed_research_inputs(
    *,
    ohlc_cache_path: Path,
    activity_cache_path: Path,
    start_date: date,
    end_date: date,
    minimum_required_offset: int,
) -> tuple[
    dict[str, tuple[RawMinuteBar, ...]],
    tuple[OpeningActivityRecord, ...],
]:
    """Load every retained seed minute after checking selection coverage."""

    raw = _read_cache_payload(ohlc_cache_path)
    retained_offset = raw.get("retained_minutes_after_open")
    if not isinstance(retained_offset, int):
        raise ValueError(
            "opening research cache retained-minute metadata is invalid"
        )
    if retained_offset < minimum_required_offset:
        raise ValueError(
            "opening research seed cache covers only "
            f"{retained_offset} minutes after open but "
            f"{minimum_required_offset} are required for selection"
        )
    return load_research_inputs(
        ohlc_cache_path=ohlc_cache_path,
        activity_cache_path=activity_cache_path,
        start_date=start_date,
        end_date=end_date,
        required_maximum_offset=retained_offset,
    )


def _prepare_relative_volume_orb_sessions(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    *,
    config: RelativeVolumeOrbResearchConfig,
    symbols: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[_RelativeVolumeOrbSessionInput, ...]]:
    activity_symbols = {item.symbol for item in activity_records}
    available_symbols = set(bars_by_symbol).intersection(activity_symbols)
    universe = tuple(
        dict.fromkeys(
            symbol.strip().upper()
            for symbol in (
                symbols if symbols is not None else sorted(available_symbols)
            )
        )
    )
    if not universe:
        raise ValueError("relative-volume ORB universe is empty")
    unavailable = set(universe) - available_symbols
    if unavailable:
        raise ValueError(
            "research symbols are missing from a cache: "
            + ", ".join(sorted(unavailable))
        )

    indexed_bars = _index_bars(bars_by_symbol, universe)
    activity_by_date: dict[date, dict[str, float]] = defaultdict(dict)
    for item in activity_records:
        if item.symbol not in universe:
            continue
        current = activity_by_date[item.session_date]
        if item.symbol in current:
            raise ValueError(
                f"duplicate opening activity: {item.session_date} "
                f"{item.symbol}"
            )
        current[item.symbol] = item.volume

    required_observations = max(
        config.minimum_universe_size,
        math.ceil(len(universe) * config.minimum_data_coverage),
    )
    histories: dict[str, list[float]] = defaultdict(list)
    prepared: list[_RelativeVolumeOrbSessionInput] = []
    all_dates = sorted(set(indexed_bars).union(activity_by_date))
    entry_offset = config.signal_minutes + config.execution_delay_minutes
    for session_date in all_dates:
        bars_for_date = indexed_bars.get(session_date, {})
        activity_for_date = activity_by_date.get(session_date, {})
        ratios: dict[str, float] = {}
        observations: list[OpeningMomentumObservation] = []
        opening_range_highs: dict[str, float] = {}
        opening_range_lows: dict[str, float] = {}
        for symbol in universe:
            current_volume = activity_for_date.get(symbol)
            prior = histories[symbol][-config.lookback_sessions :]
            if (
                current_volume is not None
                and len(prior) == config.lookback_sessions
                and sum(prior) > 0
            ):
                ratios[symbol] = current_volume / (
                    sum(prior) / config.lookback_sessions
                )

            bars = bars_for_date.get(symbol, {})
            opening_offsets = range(config.signal_minutes)
            if any(offset not in bars for offset in opening_offsets):
                continue
            signal_bar = bars.get(config.signal_minutes)
            if signal_bar is None:
                continue
            opening_bar = bars[0]
            entry_bar = bars.get(entry_offset)
            observations.append(OpeningMomentumObservation(
                symbol=symbol,
                session_open=opening_bar.open,
                signal_close=signal_bar.close,
                entry_open=entry_bar.open if entry_bar is not None else None,
            ))
            opening_range_highs[symbol] = max(
                _bar_high(bars[offset])
                for offset in opening_offsets
            )
            opening_range_lows[symbol] = min(
                _bar_low(bars[offset])
                for offset in opening_offsets
            )

        # Today's opening volume becomes eligible only on later sessions.
        for symbol, volume in activity_for_date.items():
            histories[symbol].append(volume)

        decision_observations = tuple(
            item for item in observations if item.symbol in ratios
        )
        decision_ratios = {
            item.symbol: ratios[item.symbol]
            for item in decision_observations
        }
        activity_ranking = tuple(
            symbol
            for symbol, ratio in sorted(
                decision_ratios.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if ratio >= config.minimum_activity_ratio
        )
        prepared.append(_RelativeVolumeOrbSessionInput(
            session_date=session_date,
            bars_by_symbol=bars_for_date,
            observations=decision_observations,
            activity_ratios=decision_ratios,
            top_symbols=activity_ranking[: config.top_n],
            opening_range_highs=opening_range_highs,
            opening_range_lows=opening_range_lows,
            observed_symbols=len(observations),
            ratio_symbols=len(decision_observations),
            data_complete=(
                len(decision_observations) >= required_observations
            ),
        ))
    return universe, tuple(prepared)


def _settle_candidate_path(
    *,
    session_date: date,
    symbol: str,
    activity_ratio: float,
    entry_price: float,
    stop_price: float,
    bars: dict[int, RawMinuteBar],
    config: RelativeVolumeOrbResearchConfig,
) -> RelativeVolumeOrbTrade | None:
    entry_offset = config.signal_minutes + config.execution_delay_minutes
    exit_offset = config.required_maximum_offset
    if any(
        offset not in bars
        for offset in range(entry_offset, exit_offset + 1)
    ):
        return None

    exit_price = bars[exit_offset].open
    exit_reason = "FIXED_HOLD_EXIT"
    for offset in range(entry_offset, exit_offset):
        bar = bars[offset]
        if bar.open <= stop_price:
            exit_price = bar.open
            exit_reason = "STOP_LOSS_EXIT"
            break
        if _bar_low(bar) <= stop_price:
            exit_price = stop_price
            exit_reason = "STOP_LOSS_EXIT"
            break
    gross_return_bps = (exit_price / entry_price - 1) * 10_000
    return RelativeVolumeOrbTrade(
        session_date=session_date,
        symbol=symbol,
        activity_ratio=activity_ratio,
        entry_price=entry_price,
        stop_price=stop_price,
        exit_price=exit_price,
        exit_reason=exit_reason,
        gross_return_bps=gross_return_bps,
        net_return_bps=gross_return_bps - config.round_trip_cost_bps,
    )


def evaluate_relative_volume_orb(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> RelativeVolumeOrbResearchResult:
    params = config or RelativeVolumeOrbResearchConfig()
    universe, prepared_sessions = _prepare_relative_volume_orb_sessions(
        bars_by_symbol,
        activity_records,
        config=params,
        symbols=symbols,
    )
    opening_config = params.opening_config()
    sessions: list[RelativeVolumeOrbSession] = []
    trades: list[RelativeVolumeOrbTrade] = []
    for prepared in prepared_sessions:
        session_date = prepared.session_date
        decision_observations = prepared.observations
        decision_ratios = prepared.activity_ratios
        top_symbols = prepared.top_symbols
        opening_range_highs = prepared.opening_range_highs
        opening_range_lows = prepared.opening_range_lows
        bars_for_date = prepared.bars_by_symbol
        if not prepared.data_complete:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason="DATA_INCOMPLETE_OR_WARMUP",
                observed_symbols=prepared.observed_symbols,
                ratio_symbols=prepared.ratio_symbols,
                top_symbols=top_symbols,
            ))
            continue

        decision = evaluate_stocks_in_play_opening_range_breakout(
            decision_observations,
            opening_range_high_by_symbol=opening_range_highs,
            opening_activity_ratio_by_symbol=decision_ratios,
            maximum_stocks_in_play=params.top_n,
            minimum_opening_activity_ratio=(
                params.minimum_activity_ratio
            ),
            config=opening_config,
        )
        candidate = decision.candidate_symbol
        action = decision.action
        reason = decision.reason
        entry_price = decision.entry_price
        if params.candidate_selection_mode != "BREAKOUT_DEPTH":
            selected = _select_alternative_candidate(
                decision_observations,
                top_symbols=top_symbols,
                opening_range_highs=opening_range_highs,
                activity_ratios=decision_ratios,
                mode=params.candidate_selection_mode,
            )
            if selected is None:
                action = "SKIP"
                reason = "OPENING_RANGE_BREAKOUT_MISSING"
                candidate = None
                entry_price = None
            else:
                candidate = selected.symbol
                entry_price = selected.entry_open
                if entry_price is None:
                    action = "SKIP"
                    reason = "ENTRY_BAR_MISSING"
                else:
                    action = "ENTER_LONG"
        if action != "ENTER_LONG" or candidate is None:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason=reason,
                observed_symbols=prepared.observed_symbols,
                ratio_symbols=prepared.ratio_symbols,
                top_symbols=top_symbols,
            ))
            continue
        range_low = opening_range_lows.get(candidate)
        if entry_price is None or range_low is None:
            raise RuntimeError("ORB decision is missing entry evidence")
        stop_price = max(
            range_low,
            entry_price * (1 - params.stop_loss_cap_pct / 100),
        )
        if stop_price >= entry_price:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason="OPENING_RANGE_STOP_INVALID",
                observed_symbols=prepared.observed_symbols,
                ratio_symbols=prepared.ratio_symbols,
                top_symbols=top_symbols,
                candidate_symbol=candidate,
                candidate_activity_ratio=decision_ratios[candidate],
            ))
            continue

        trade = _settle_candidate_path(
            session_date=session_date,
            symbol=candidate,
            activity_ratio=decision_ratios[candidate],
            entry_price=entry_price,
            stop_price=stop_price,
            bars=bars_for_date[candidate],
            config=params,
        )
        if trade is None:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason="EXIT_PATH_INCOMPLETE",
                observed_symbols=prepared.observed_symbols,
                ratio_symbols=prepared.ratio_symbols,
                top_symbols=top_symbols,
                candidate_symbol=candidate,
                candidate_activity_ratio=decision_ratios[candidate],
            ))
            continue
        trades.append(trade)
        sessions.append(RelativeVolumeOrbSession(
            session_date=session_date,
            status="CLOSED",
            reason=trade.exit_reason,
            observed_symbols=prepared.observed_symbols,
            ratio_symbols=prepared.ratio_symbols,
            top_symbols=top_symbols,
            candidate_symbol=candidate,
            candidate_activity_ratio=decision_ratios[candidate],
        ))

    return RelativeVolumeOrbResearchResult(
        config=params,
        universe=universe,
        sessions=tuple(sessions),
        trades=tuple(trades),
    )


def _eligible_candidate_observations(
    observations: Sequence[OpeningMomentumObservation],
    *,
    top_symbols: Sequence[str],
    opening_range_highs: dict[str, float],
) -> tuple[OpeningMomentumObservation, ...]:
    eligible_symbols = set(top_symbols)
    return tuple(
        item
        for item in observations
        if (
            item.symbol in eligible_symbols
            and item.symbol in opening_range_highs
            and item.signal_close > opening_range_highs[item.symbol]
        )
    )


def discover_relative_volume_orb_candidate_signals(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> tuple[
    tuple[str, ...],
    tuple[date, ...],
    tuple[RelativeVolumeOrbCandidateSignal, ...],
]:
    """Return every causally tradeable Top-N confirmed ORB candidate."""

    params = config or RelativeVolumeOrbResearchConfig()
    universe, prepared_sessions = _prepare_relative_volume_orb_sessions(
        bars_by_symbol,
        activity_records,
        config=params,
        symbols=symbols,
    )
    evaluable_dates: list[date] = []
    signals: list[RelativeVolumeOrbCandidateSignal] = []
    for prepared in prepared_sessions:
        if not prepared.data_complete:
            continue
        evaluable_dates.append(prepared.session_date)
        for observation in _eligible_candidate_observations(
            prepared.observations,
            top_symbols=prepared.top_symbols,
            opening_range_highs=prepared.opening_range_highs,
        ):
            entry_price = observation.entry_open
            range_low = prepared.opening_range_lows.get(observation.symbol)
            if entry_price is None or range_low is None:
                continue
            stop_price = max(
                range_low,
                entry_price * (1 - params.stop_loss_cap_pct / 100),
            )
            if stop_price >= entry_price:
                continue
            opening_range_high = prepared.opening_range_highs[
                observation.symbol
            ]
            signals.append(RelativeVolumeOrbCandidateSignal(
                session_date=prepared.session_date,
                symbol=observation.symbol,
                activity_ratio=prepared.activity_ratios[
                    observation.symbol
                ],
                opening_return_bps=observation.opening_return_bps,
                breakout_depth_bps=(
                    observation.signal_close / opening_range_high - 1
                )
                * 10_000,
                entry_price=entry_price,
                stop_price=stop_price,
            ))
    return (
        universe,
        tuple(evaluable_dates),
        tuple(sorted(
            signals,
            key=lambda item: (item.session_date, item.symbol),
        )),
    )


def evaluate_relative_volume_orb_candidate_panel(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> RelativeVolumeOrbCandidatePanel:
    params = config or RelativeVolumeOrbResearchConfig()
    universe, evaluable_dates, signals = (
        discover_relative_volume_orb_candidate_signals(
            bars_by_symbol,
            activity_records,
            config=params,
            symbols=symbols,
        )
    )
    indexed_bars = _index_bars(bars_by_symbol, universe)
    outcomes: list[RelativeVolumeOrbCandidateOutcome] = []
    for signal in signals:
        trade = _settle_candidate_path(
            session_date=signal.session_date,
            symbol=signal.symbol,
            activity_ratio=signal.activity_ratio,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            bars=indexed_bars.get(signal.session_date, {}).get(
                signal.symbol,
                {},
            ),
            config=params,
        )
        if trade is None:
            raise ValueError(
                "candidate panel exit path is incomplete: "
                f"{signal.session_date} {signal.symbol}"
            )
        outcomes.append(RelativeVolumeOrbCandidateOutcome(
            session_date=signal.session_date,
            symbol=signal.symbol,
            activity_ratio=signal.activity_ratio,
            opening_return_bps=signal.opening_return_bps,
            breakout_depth_bps=signal.breakout_depth_bps,
            entry_price=trade.entry_price,
            stop_price=trade.stop_price,
            exit_price=trade.exit_price,
            exit_reason=trade.exit_reason,
            gross_return_bps=trade.gross_return_bps,
            net_return_bps=trade.net_return_bps,
        ))
    return RelativeVolumeOrbCandidatePanel(
        config=params,
        universe=universe,
        evaluable_dates=evaluable_dates,
        candidates=tuple(outcomes),
    )


def _select_alternative_candidate(
    observations: Sequence[OpeningMomentumObservation],
    *,
    top_symbols: Sequence[str],
    opening_range_highs: dict[str, float],
    activity_ratios: dict[str, float],
    mode: CandidateSelectionMode,
) -> OpeningMomentumObservation | None:
    eligible = _eligible_candidate_observations(
        observations,
        top_symbols=top_symbols,
        opening_range_highs=opening_range_highs,
    )
    if not eligible:
        return None
    if mode == "ACTIVITY_RATIO":
        return min(
            eligible,
            key=lambda item: (
                -activity_ratios[item.symbol],
                -(
                    item.signal_close
                    / opening_range_highs[item.symbol]
                    - 1
                ),
                -item.opening_return_bps,
                item.symbol,
            ),
        )
    if mode == "OPENING_RETURN":
        return min(
            eligible,
            key=lambda item: (
                -item.opening_return_bps,
                -(
                    item.signal_close
                    / opening_range_highs[item.symbol]
                    - 1
                ),
                item.symbol,
            ),
        )
    raise ValueError("alternative candidate selection mode is unsupported")


def _fetch_exit_path_gaps(
    extended: dict[str, tuple[RawMinuteBar, ...]],
    gaps: Sequence[tuple[date, str, tuple[int, ...]]],
    provider: HistoricalCandleProvider,
) -> int:
    market_session = get_session("US")
    fetched_bars = 0
    for session_date, symbol, missing_offsets in gaps:
        first_offset = min(missing_offsets)
        last_offset = max(missing_offsets)
        local_open = datetime.combine(
            session_date,
            market_session.rth_open,
            tzinfo=market_session.timezone,
        )
        after = (local_open + timedelta(minutes=first_offset)).astimezone(
            timezone.utc
        )
        response = provider.get_history_candlesticks_by_offset(
            symbol,
            "MIN_1",
            last_offset - first_offset + 1,
            after,
        )
        expected = set(missing_offsets)
        fetched: list[RawMinuteBar] = []
        for candle in response:
            timestamp = candle.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            local = market_session.local(timestamp)
            if local.date() != session_date:
                continue
            offset_seconds = (local - local_open).total_seconds()
            if offset_seconds % 60 != 0:
                continue
            offset = int(offset_seconds // 60)
            if offset not in expected:
                continue
            fetched.append(RawMinuteBar(
                timestamp=timestamp,
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
            ))
        fetched_bars += len(fetched)
        extended[symbol] = _merge_bars(extended[symbol], fetched)
    return fetched_bars


def materialize_candidate_exit_paths(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    provider: HistoricalCandleProvider,
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> tuple[
    dict[str, tuple[RawMinuteBar, ...]],
    RelativeVolumeOrbCacheExtensionReport,
]:
    """Fetch only exit-path minutes for causally selected candidates."""

    params = config or RelativeVolumeOrbResearchConfig()
    extended = {
        symbol: tuple(values) for symbol, values in bars_by_symbol.items()
    }
    before = evaluate_relative_volume_orb(
        extended,
        activity_records,
        config=params,
        symbols=symbols,
    )
    gaps = _candidate_exit_path_gaps(before, extended)
    fetched_bars = _fetch_exit_path_gaps(extended, gaps, provider)

    after = evaluate_relative_volume_orb(
        extended,
        activity_records,
        config=params,
        symbols=symbols,
    )
    remaining = _candidate_exit_path_gaps(after, extended)
    if remaining:
        rendered = ", ".join(
            f"{session_date.isoformat()} {symbol} "
            f"offsets={list(offsets)}"
            for session_date, symbol, offsets in remaining[:10]
        )
        raise ValueError(
            "historical provider did not complete candidate exit paths: "
            + rendered
        )
    return extended, RelativeVolumeOrbCacheExtensionReport(
        required_maximum_offset=params.required_maximum_offset,
        candidate_sessions=sum(
            item.candidate_symbol is not None for item in before.sessions
        ),
        incomplete_candidate_sessions_before=len(gaps),
        fetch_requests=len(gaps),
        fetched_bars=fetched_bars,
        incomplete_candidate_sessions_after=0,
    )


def _candidate_exit_path_gaps(
    result: RelativeVolumeOrbResearchResult,
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
) -> tuple[tuple[date, str, tuple[int, ...]], ...]:
    indexed = _index_bars(bars_by_symbol, result.universe)
    entry_offset = (
        result.config.signal_minutes
        + result.config.execution_delay_minutes
    )
    exit_offset = result.config.required_maximum_offset
    gaps: list[tuple[date, str, tuple[int, ...]]] = []
    for session in result.sessions:
        symbol = session.candidate_symbol
        if session.reason != "EXIT_PATH_INCOMPLETE" or symbol is None:
            continue
        by_offset = indexed.get(session.session_date, {}).get(symbol, {})
        missing = tuple(
            offset
            for offset in range(entry_offset, exit_offset + 1)
            if offset not in by_offset
        )
        if missing:
            gaps.append((session.session_date, symbol, missing))
    return tuple(gaps)


def _candidate_panel_exit_path_gaps(
    signals: Sequence[RelativeVolumeOrbCandidateSignal],
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    *,
    universe: Sequence[str],
    config: RelativeVolumeOrbResearchConfig,
) -> tuple[tuple[date, str, tuple[int, ...]], ...]:
    indexed = _index_bars(bars_by_symbol, universe)
    entry_offset = config.signal_minutes + config.execution_delay_minutes
    exit_offset = config.required_maximum_offset
    gaps: list[tuple[date, str, tuple[int, ...]]] = []
    for signal in signals:
        by_offset = indexed.get(signal.session_date, {}).get(
            signal.symbol,
            {},
        )
        missing = tuple(
            offset
            for offset in range(entry_offset, exit_offset + 1)
            if offset not in by_offset
        )
        if missing:
            gaps.append((signal.session_date, signal.symbol, missing))
    return tuple(gaps)


def materialize_all_candidate_exit_paths(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    provider: HistoricalCandleProvider,
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> tuple[
    dict[str, tuple[RawMinuteBar, ...]],
    RelativeVolumeOrbPanelCacheExtensionReport,
]:
    """Fetch missing exit minutes for every causally eligible candidate."""

    params = config or RelativeVolumeOrbResearchConfig()
    extended = {
        symbol: tuple(values) for symbol, values in bars_by_symbol.items()
    }
    universe, _, before_signals = (
        discover_relative_volume_orb_candidate_signals(
            extended,
            activity_records,
            config=params,
            symbols=symbols,
        )
    )
    gaps = _candidate_panel_exit_path_gaps(
        before_signals,
        extended,
        universe=universe,
        config=params,
    )
    fetched_bars = _fetch_exit_path_gaps(extended, gaps, provider)
    after_universe, _, after_signals = (
        discover_relative_volume_orb_candidate_signals(
            extended,
            activity_records,
            config=params,
            symbols=symbols,
        )
    )
    if after_universe != universe or after_signals != before_signals:
        raise RuntimeError(
            "exit-path materialization changed causal candidate selection"
        )
    remaining = _candidate_panel_exit_path_gaps(
        after_signals,
        extended,
        universe=universe,
        config=params,
    )
    if remaining:
        rendered = ", ".join(
            f"{session_date.isoformat()} {symbol} "
            f"offsets={list(offsets)}"
            for session_date, symbol, offsets in remaining[:10]
        )
        raise ValueError(
            "historical provider did not complete candidate panel exit "
            "paths: "
            + rendered
        )
    return extended, RelativeVolumeOrbPanelCacheExtensionReport(
        required_maximum_offset=params.required_maximum_offset,
        candidate_sessions=len({
            item.session_date for item in before_signals
        }),
        candidate_paths=len(before_signals),
        incomplete_candidate_paths_before=len(gaps),
        fetch_requests=len(gaps),
        fetched_bars=fetched_bars,
        incomplete_candidate_paths_after=0,
    )


def research_payload(
    result: RelativeVolumeOrbResearchResult,
) -> dict[str, object]:
    evaluable_dates = tuple(
        item.session_date
        for item in result.sessions
        if item.reason != "DATA_INCOMPLETE_OR_WARMUP"
    )
    discovery_dates, holdout_dates = _chronological_split(
        evaluable_dates,
        result.config.discovery_ratio,
    )
    discovery_set = set(discovery_dates)
    holdout_set = set(holdout_dates)
    reason_counts = Counter(item.reason for item in result.sessions)
    return {
        "research_version": RESEARCH_VERSION,
        "automatic_promotion_allowed": False,
        "research_design": {
            "causal_activity_baseline": True,
            "fixed_catalog_universe": True,
            "point_in_time_membership": False,
            "selection_uses_holdout": False,
            "candidate_selection_mode": (
                result.config.candidate_selection_mode
            ),
            "production_exit_semantics": True,
            "required_maximum_offset": (
                result.config.required_maximum_offset
            ),
            "discovery_ratio": result.config.discovery_ratio,
        },
        "config": asdict(result.config),
        "universe": {
            "count": len(result.universe),
            "symbols": list(result.universe),
        },
        "sessions": {
            "total": len(result.sessions),
            "evaluable": len(evaluable_dates),
            "reasons": dict(sorted(reason_counts.items())),
            "discovery_dates": [value.isoformat() for value in discovery_dates],
            "holdout_dates": [value.isoformat() for value in holdout_dates],
        },
        "performance": {
            "all": _performance_payload(result.trades),
            "discovery": _performance_payload(tuple(
                item
                for item in result.trades
                if item.session_date in discovery_set
            )),
            "holdout": _performance_payload(tuple(
                item
                for item in result.trades
                if item.session_date in holdout_set
            )),
        },
        "trades": [_trade_payload(item) for item in result.trades],
    }


def _candidate_outcome_trade(
    item: RelativeVolumeOrbCandidateOutcome,
) -> RelativeVolumeOrbTrade:
    return RelativeVolumeOrbTrade(
        session_date=item.session_date,
        symbol=item.symbol,
        activity_ratio=item.activity_ratio,
        entry_price=item.entry_price,
        stop_price=item.stop_price,
        exit_price=item.exit_price,
        exit_reason=item.exit_reason,
        gross_return_bps=item.gross_return_bps,
        net_return_bps=item.net_return_bps,
    )


def _candidate_outcome_sort_key(
    item: RelativeVolumeOrbCandidateOutcome,
    mode: CandidateSelectionMode,
) -> tuple[float, float, float, str]:
    if mode == "BREAKOUT_DEPTH":
        return (
            -item.breakout_depth_bps,
            -item.opening_return_bps,
            0.0,
            item.symbol,
        )
    if mode == "ACTIVITY_RATIO":
        return (
            -item.activity_ratio,
            -item.breakout_depth_bps,
            -item.opening_return_bps,
            item.symbol,
        )
    if mode == "OPENING_RETURN":
        return (
            -item.opening_return_bps,
            -item.breakout_depth_bps,
            0.0,
            item.symbol,
        )
    raise ValueError("candidate outcome selection mode is unsupported")


def _candidate_factor_value(
    item: RelativeVolumeOrbCandidateOutcome,
    mode: CandidateSelectionMode,
) -> float:
    if mode == "BREAKOUT_DEPTH":
        return item.breakout_depth_bps
    if mode == "ACTIVITY_RATIO":
        return item.activity_ratio
    if mode == "OPENING_RETURN":
        return item.opening_return_bps
    raise ValueError("candidate factor mode is unsupported")


def _candidate_factor_diagnostics(
    candidates: Sequence[RelativeVolumeOrbCandidateOutcome],
    *,
    mode: CandidateSelectionMode,
) -> dict[str, object]:
    by_date: dict[date, list[RelativeVolumeOrbCandidateOutcome]] = (
        defaultdict(list)
    )
    for item in candidates:
        by_date[item.session_date].append(item)

    selected: list[RelativeVolumeOrbCandidateOutcome] = []
    regrets: list[float] = []
    winner_hits = 0
    multi_candidate_regrets: list[float] = []
    multi_candidate_winner_hits = 0
    comparable_pairs = 0
    concordant_pairs = 0
    for session_date in sorted(by_date):
        rows = by_date[session_date]
        choice = min(
            rows,
            key=lambda item: _candidate_outcome_sort_key(item, mode),
        )
        selected.append(choice)
        best_return = max(item.net_return_bps for item in rows)
        regret = best_return - choice.net_return_bps
        regrets.append(regret)
        if regret == 0:
            winner_hits += 1
        if len(rows) > 1:
            multi_candidate_regrets.append(regret)
            if regret == 0:
                multi_candidate_winner_hits += 1
        for left_index, left in enumerate(rows):
            for right in rows[left_index + 1 :]:
                factor_delta = (
                    _candidate_factor_value(left, mode)
                    - _candidate_factor_value(right, mode)
                )
                outcome_delta = (
                    left.net_return_bps - right.net_return_bps
                )
                if factor_delta == 0 or outcome_delta == 0:
                    continue
                comparable_pairs += 1
                if factor_delta * outcome_delta > 0:
                    concordant_pairs += 1

    performance = _performance_payload(tuple(
        _candidate_outcome_trade(item) for item in selected
    ))
    performance.update({
        "candidate_sessions": len(selected),
        "multi_candidate_sessions": sum(
            len(rows) > 1 for rows in by_date.values()
        ),
        "winner_hits": winner_hits,
        "winner_hit_rate": (
            winner_hits / len(selected) if selected else 0.0
        ),
        "multi_candidate_winner_hits": multi_candidate_winner_hits,
        "multi_candidate_winner_hit_rate": (
            multi_candidate_winner_hits / len(multi_candidate_regrets)
            if multi_candidate_regrets
            else None
        ),
        "mean_regret_bps": (
            sum(regrets) / len(regrets) if regrets else 0.0
        ),
        "cumulative_regret_bps": sum(regrets),
        "mean_multi_candidate_regret_bps": (
            sum(multi_candidate_regrets) / len(multi_candidate_regrets)
            if multi_candidate_regrets
            else None
        ),
        "comparable_pairs": comparable_pairs,
        "concordant_pairs": concordant_pairs,
        "pairwise_concordance": (
            concordant_pairs / comparable_pairs
            if comparable_pairs
            else None
        ),
        "selected_symbols_by_date": [
            {
                "session_date": item.session_date.isoformat(),
                "symbol": item.symbol,
                "net_return_bps": item.net_return_bps,
            }
            for item in selected
        ],
    })
    return performance


def candidate_panel_payload(
    panel: RelativeVolumeOrbCandidatePanel,
) -> dict[str, object]:
    discovery_dates, holdout_dates = _chronological_split(
        panel.evaluable_dates,
        panel.config.discovery_ratio,
    )
    discovery_set = set(discovery_dates)
    holdout_set = set(holdout_dates)
    by_date: dict[date, list[RelativeVolumeOrbCandidateOutcome]] = (
        defaultdict(list)
    )
    for item in panel.candidates:
        by_date[item.session_date].append(item)

    factor_diagnostics: dict[str, object] = {}
    for mode in _CANDIDATE_SELECTION_MODES:
        factor_diagnostics[mode] = {
            "all": _candidate_factor_diagnostics(
                panel.candidates,
                mode=mode,
            ),
            "discovery": _candidate_factor_diagnostics(
                tuple(
                    item
                    for item in panel.candidates
                    if item.session_date in discovery_set
                ),
                mode=mode,
            ),
            "holdout": _candidate_factor_diagnostics(
                tuple(
                    item
                    for item in panel.candidates
                    if item.session_date in holdout_set
                ),
                mode=mode,
            ),
        }
    return {
        "research_version": CANDIDATE_PANEL_VERSION,
        "automatic_promotion_allowed": False,
        "research_design": {
            "causal_activity_baseline": True,
            "all_tradeable_top_n_breakouts": True,
            "candidate_selection_uses_future": False,
            "outcomes_used_for_diagnostics_only": True,
            "fixed_catalog_universe": True,
            "point_in_time_membership": False,
            "selection_uses_holdout": False,
            "production_exit_semantics": True,
            "required_maximum_offset": (
                panel.config.required_maximum_offset
            ),
            "discovery_ratio": panel.config.discovery_ratio,
        },
        "config": asdict(panel.config),
        "universe": {
            "count": len(panel.universe),
            "symbols": list(panel.universe),
        },
        "sessions": {
            "evaluable": len(panel.evaluable_dates),
            "with_candidates": len(by_date),
            "multi_candidate": sum(
                len(rows) > 1 for rows in by_date.values()
            ),
            "candidate_paths": len(panel.candidates),
            "candidate_count_distribution": dict(sorted(Counter(
                len(by_date.get(session_date, ()))
                for session_date in panel.evaluable_dates
            ).items())),
            "discovery_dates": [
                value.isoformat() for value in discovery_dates
            ],
            "holdout_dates": [
                value.isoformat() for value in holdout_dates
            ],
        },
        "factor_diagnostics": factor_diagnostics,
        "candidates": [
            {
                **asdict(item),
                "session_date": item.session_date.isoformat(),
            }
            for item in panel.candidates
        ],
    }


def _index_bars(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    symbols: Sequence[str],
) -> dict[date, dict[str, dict[int, RawMinuteBar]]]:
    market_session = get_session("US")
    indexed: dict[date, dict[str, dict[int, RawMinuteBar]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for symbol in symbols:
        for bar in bars_by_symbol[symbol]:
            local = market_session.local(bar.timestamp)
            session_open = datetime.combine(
                local.date(),
                market_session.rth_open,
                tzinfo=market_session.timezone,
            )
            offset_seconds = (local - session_open).total_seconds()
            if offset_seconds % 60 != 0:
                continue
            offset = int(offset_seconds // 60)
            if offset < 0:
                continue
            by_offset = indexed[local.date()][symbol]
            if offset in by_offset:
                raise ValueError(
                    f"duplicate OHLC minute: {local.date()} {symbol} "
                    f"offset={offset}"
                )
            by_offset[offset] = bar
    return {
        session_date: {
            symbol: dict(by_offset)
            for symbol, by_offset in by_symbol.items()
        }
        for session_date, by_symbol in indexed.items()
    }


def _bar_high(bar: RawMinuteBar) -> float:
    if bar.high is None:
        raise ValueError("minute bar high is unavailable")
    return bar.high


def _bar_low(bar: RawMinuteBar) -> float:
    if bar.low is None:
        raise ValueError("minute bar low is unavailable")
    return bar.low


def _chronological_split(
    values: Sequence[date],
    ratio: float,
) -> tuple[tuple[date, ...], tuple[date, ...]]:
    ordered = tuple(sorted(set(values)))
    if len(ordered) < 2:
        return ordered, ()
    cutoff = min(len(ordered) - 1, max(1, math.ceil(len(ordered) * ratio)))
    return ordered[:cutoff], ordered[cutoff:]


def _performance_payload(
    trades: Sequence[RelativeVolumeOrbTrade],
) -> dict[str, object]:
    ordered = tuple(sorted(trades, key=lambda item: item.session_date))
    returns = [item.net_return_bps for item in ordered]
    cumulative = sum(returns)
    equity = 0.0
    peak = 0.0
    maximum_drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    positive = [value for value in returns if value > 0]
    negative = [value for value in returns if value < 0]
    ranked = sorted(returns, reverse=True)
    contribution: dict[str, float] = defaultdict(float)
    counts: Counter[str] = Counter()
    for item in ordered:
        contribution[item.symbol] += item.net_return_bps
        counts[item.symbol] += 1
    symbol_contribution = [
        {
            "symbol": symbol,
            "trades": counts[symbol],
            "net_return_bps": value,
        }
        for symbol, value in sorted(
            contribution.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    positive_total = sum(positive)
    return {
        "trades": len(ordered),
        "wins": len(positive),
        "win_rate": len(positive) / len(ordered) if ordered else 0.0,
        "mean_net_return_bps": cumulative / len(ordered) if ordered else 0.0,
        "cumulative_net_return_bps": cumulative,
        "maximum_drawdown_bps": maximum_drawdown,
        "profit_factor": (
            sum(positive) / -sum(negative) if negative else None
        ),
        "stop_exits": sum(
            item.exit_reason == "STOP_LOSS_EXIT" for item in ordered
        ),
        "without_best_1_bps": sum(ranked[1:]) if ranked else 0.0,
        "without_best_3_bps": sum(ranked[3:]) if ranked else 0.0,
        "best_trade_share_of_positive": (
            max(positive) / positive_total if positive_total > 0 else None
        ),
        "symbol_contribution": symbol_contribution,
    }


def _trade_payload(item: RelativeVolumeOrbTrade) -> dict[str, object]:
    return {
        "session_date": item.session_date.isoformat(),
        "symbol": item.symbol,
        "activity_ratio": item.activity_ratio,
        "entry_price": item.entry_price,
        "stop_price": item.stop_price,
        "exit_price": item.exit_price,
        "exit_reason": item.exit_reason,
        "gross_return_bps": item.gross_return_bps,
        "net_return_bps": item.net_return_bps,
    }


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Causally evaluate the exact relative-volume Top-N five-minute "
            "opening-range breakout with production exit semantics."
        ),
    )
    parser.add_argument("--ohlc-cache", type=Path, required=True)
    parser.add_argument(
        "--seed-ohlc-cache",
        type=Path,
        help=(
            "immutable lower-horizon cache used to causally select candidates "
            "and fetch only their missing exit-path minutes"
        ),
    )
    parser.add_argument("--activity-cache", type=Path, required=True)
    parser.add_argument("--start-date", type=_parse_date, required=True)
    parser.add_argument("--end-date", type=_parse_date, required=True)
    parser.add_argument("--symbols")
    parser.add_argument("--holding-minutes", default="60")
    parser.add_argument(
        "--candidate-selection-mode",
        choices=_CANDIDATE_SELECTION_MODES,
        default="BREAKOUT_DEPTH",
    )
    parser.add_argument(
        "--candidate-panel",
        action="store_true",
        help=(
            "materialize and diagnose every tradeable Top-N confirmed "
            "breakout, without changing the selected strategy"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--full", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    start_date = args.start_date
    end_date = args.end_date
    if end_date < start_date:
        parser.error("end date must not precede start date")
    try:
        holding_grid = _parse_integer_grid(
            args.holding_minutes,
            field_name="holding_minutes",
            minimum=1,
            maximum=120,
        )
        requested_symbols = (
            _parse_symbols(args.symbols, field_name="symbols")
            if args.symbols
            else None
        )
        required_offset = max(5 + 1 + value for value in holding_grid)
        if (
            args.seed_ohlc_cache is not None
            and args.seed_ohlc_cache.resolve() == args.ohlc_cache.resolve()
        ):
            raise ValueError(
                "seed OHLC cache must differ from the target OHLC cache"
            )
        cache_extension: (
            RelativeVolumeOrbCacheExtensionReport
            | RelativeVolumeOrbPanelCacheExtensionReport
            | None
        ) = None
        if args.seed_ohlc_cache is None:
            bars_by_symbol, activity_records = load_research_inputs(
                ohlc_cache_path=args.ohlc_cache,
                activity_cache_path=args.activity_cache,
                start_date=start_date,
                end_date=end_date,
                required_maximum_offset=required_offset,
            )
        else:
            seed_path = (
                args.ohlc_cache
                if args.ohlc_cache.exists()
                else args.seed_ohlc_cache
            )
            if args.ohlc_cache.exists():
                bars_by_symbol, activity_records = load_research_inputs(
                    ohlc_cache_path=seed_path,
                    activity_cache_path=args.activity_cache,
                    start_date=start_date,
                    end_date=end_date,
                    required_maximum_offset=required_offset,
                )
            else:
                bars_by_symbol, activity_records = load_seed_research_inputs(
                    ohlc_cache_path=seed_path,
                    activity_cache_path=args.activity_cache,
                    start_date=start_date,
                    end_date=end_date,
                    minimum_required_offset=6,
                )
            _configure_longport_environment()
            broker = BrokerGateway()
            try:
                if args.candidate_panel:
                    bars_by_symbol, cache_extension = (
                        materialize_all_candidate_exit_paths(
                            bars_by_symbol,
                            activity_records,
                            broker,
                            config=RelativeVolumeOrbResearchConfig(
                                holding_minutes=max(holding_grid),
                            ),
                            symbols=requested_symbols,
                        )
                    )
                else:
                    bars_by_symbol, cache_extension = (
                        materialize_candidate_exit_paths(
                            bars_by_symbol,
                            activity_records,
                            broker,
                            config=RelativeVolumeOrbResearchConfig(
                                holding_minutes=max(holding_grid),
                                candidate_selection_mode=(
                                    args.candidate_selection_mode
                                ),
                            ),
                            symbols=requested_symbols,
                        )
                    )
            finally:
                broker.close()
            _save_cache(
                args.ohlc_cache,
                bars_by_symbol,
                start_date=start_date,
                end_date=end_date,
                retained_minutes_after_open=required_offset,
            )
            bars_by_symbol, activity_records = load_research_inputs(
                ohlc_cache_path=args.ohlc_cache,
                activity_cache_path=args.activity_cache,
                start_date=start_date,
                end_date=end_date,
                required_maximum_offset=required_offset,
            )
        reports: list[dict[str, object]] = []
        candidate_panels: list[dict[str, object]] = []
        for holding_minutes in holding_grid:
            result = evaluate_relative_volume_orb(
                bars_by_symbol,
                activity_records,
                config=RelativeVolumeOrbResearchConfig(
                    holding_minutes=holding_minutes,
                    candidate_selection_mode=(
                        args.candidate_selection_mode
                    ),
                ),
                symbols=requested_symbols,
            )
            reports.append(research_payload(result))
            if args.candidate_panel:
                candidate_panels.append(candidate_panel_payload(
                    evaluate_relative_volume_orb_candidate_panel(
                        bars_by_symbol,
                        activity_records,
                        config=RelativeVolumeOrbResearchConfig(
                            holding_minutes=holding_minutes,
                        ),
                        symbols=requested_symbols,
                    )
                ))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    payload: dict[str, object] = {
        "research_version": RESEARCH_VERSION,
        "automatic_promotion_allowed": False,
        "cache_extension": (
            asdict(cache_extension) if cache_extension is not None else None
        ),
        "candidate_selection_mode": args.candidate_selection_mode,
        "requested_holding_minutes": list(holding_grid),
        "reports": reports,
    }
    if args.candidate_panel:
        payload["candidate_panels"] = candidate_panels
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        indent=2 if args.full else None,
        sort_keys=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.tmp")
        temporary.write_text(rendered + "\n", encoding="ascii")
        temporary.replace(args.output)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
