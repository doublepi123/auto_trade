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


def evaluate_relative_volume_orb(
    bars_by_symbol: dict[str, tuple[RawMinuteBar, ...]],
    activity_records: Sequence[OpeningActivityRecord],
    *,
    config: RelativeVolumeOrbResearchConfig | None = None,
    symbols: Sequence[str] | None = None,
) -> RelativeVolumeOrbResearchResult:
    params = config or RelativeVolumeOrbResearchConfig()
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

    opening_config = params.opening_config()
    required_observations = max(
        params.minimum_universe_size,
        math.ceil(len(universe) * params.minimum_data_coverage),
    )
    histories: dict[str, list[float]] = defaultdict(list)
    sessions: list[RelativeVolumeOrbSession] = []
    trades: list[RelativeVolumeOrbTrade] = []
    all_dates = sorted(set(indexed_bars).union(activity_by_date))
    for session_date in all_dates:
        bars_for_date = indexed_bars.get(session_date, {})
        activity_for_date = activity_by_date.get(session_date, {})
        ratios: dict[str, float] = {}
        observations: list[OpeningMomentumObservation] = []
        opening_range_highs: dict[str, float] = {}
        opening_range_lows: dict[str, float] = {}
        entry_offset = (
            params.signal_minutes + params.execution_delay_minutes
        )
        for symbol in universe:
            current_volume = activity_for_date.get(symbol)
            prior = histories[symbol][-params.lookback_sessions :]
            if (
                current_volume is not None
                and len(prior) == params.lookback_sessions
                and sum(prior) > 0
            ):
                ratios[symbol] = current_volume / (
                    sum(prior) / params.lookback_sessions
                )

            bars = bars_for_date.get(symbol, {})
            opening_offsets = range(params.signal_minutes)
            if any(offset not in bars for offset in opening_offsets):
                continue
            signal_bar = bars.get(params.signal_minutes)
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

        # Today's observation becomes eligible only for later sessions.
        for symbol, volume in activity_for_date.items():
            histories[symbol].append(volume)

        decision_observations = [
            item for item in observations if item.symbol in ratios
        ]
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
            if ratio >= params.minimum_activity_ratio
        )
        top_symbols = activity_ranking[: params.top_n]
        if len(decision_observations) < required_observations:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason="DATA_INCOMPLETE_OR_WARMUP",
                observed_symbols=len(observations),
                ratio_symbols=len(decision_observations),
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
                activity_ratios=ratios,
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
                observed_symbols=len(observations),
                ratio_symbols=len(decision_observations),
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
                observed_symbols=len(observations),
                ratio_symbols=len(decision_observations),
                top_symbols=top_symbols,
                candidate_symbol=candidate,
                candidate_activity_ratio=ratios[candidate],
            ))
            continue

        candidate_bars = bars_for_date[candidate]
        exit_offset = params.required_maximum_offset
        if any(
            offset not in candidate_bars
            for offset in range(entry_offset, exit_offset + 1)
        ):
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason="EXIT_PATH_INCOMPLETE",
                observed_symbols=len(observations),
                ratio_symbols=len(decision_observations),
                top_symbols=top_symbols,
                candidate_symbol=candidate,
                candidate_activity_ratio=ratios[candidate],
            ))
            continue

        exit_price = candidate_bars[exit_offset].open
        exit_reason = "FIXED_HOLD_EXIT"
        for offset in range(entry_offset, exit_offset):
            bar = candidate_bars[offset]
            if bar.open <= stop_price:
                exit_price = bar.open
                exit_reason = "STOP_LOSS_EXIT"
                break
            if _bar_low(bar) <= stop_price:
                exit_price = stop_price
                exit_reason = "STOP_LOSS_EXIT"
                break
        gross_return_bps = (exit_price / entry_price - 1) * 10_000
        trades.append(RelativeVolumeOrbTrade(
            session_date=session_date,
            symbol=candidate,
            activity_ratio=ratios[candidate],
            entry_price=entry_price,
            stop_price=stop_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_return_bps=gross_return_bps,
            net_return_bps=(
                gross_return_bps - params.round_trip_cost_bps
            ),
        ))
        sessions.append(RelativeVolumeOrbSession(
            session_date=session_date,
            status="CLOSED",
            reason=exit_reason,
            observed_symbols=len(observations),
            ratio_symbols=len(decision_observations),
            top_symbols=top_symbols,
            candidate_symbol=candidate,
            candidate_activity_ratio=ratios[candidate],
        ))

    return RelativeVolumeOrbResearchResult(
        config=params,
        universe=universe,
        sessions=tuple(sessions),
        trades=tuple(trades),
    )


def _select_alternative_candidate(
    observations: Sequence[OpeningMomentumObservation],
    *,
    top_symbols: Sequence[str],
    opening_range_highs: dict[str, float],
    activity_ratios: dict[str, float],
    mode: CandidateSelectionMode,
) -> OpeningMomentumObservation | None:
    eligible = tuple(
        item
        for item in observations
        if (
            item.symbol in top_symbols
            and item.symbol in opening_range_highs
            and item.signal_close > opening_range_highs[item.symbol]
        )
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
        cache_extension: RelativeVolumeOrbCacheExtensionReport | None = None
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
        reports = []
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
