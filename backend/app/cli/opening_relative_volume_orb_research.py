from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from app.cli.import_opening_activity import (
    OpeningActivityRecord,
    load_opening_activity_cache,
)
from app.cli.opening_extension_research import (
    RawMinuteBar,
    _load_cache,
    _parse_integer_grid,
    _parse_symbols,
)
from app.core.market_calendar import get_session
from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_stocks_in_play_opening_range_breakout,
)


RESEARCH_VERSION = "relative-volume-orb-causal-research-v1"


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
        if decision.action != "ENTER_LONG" or candidate is None:
            sessions.append(RelativeVolumeOrbSession(
                session_date=session_date,
                status="SKIPPED",
                reason=decision.reason,
                observed_symbols=len(observations),
                ratio_symbols=len(decision_observations),
                top_symbols=top_symbols,
            ))
            continue
        entry_price = decision.entry_price
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
    parser.add_argument("--activity-cache", type=Path, required=True)
    parser.add_argument("--start-date", type=_parse_date, required=True)
    parser.add_argument("--end-date", type=_parse_date, required=True)
    parser.add_argument("--symbols")
    parser.add_argument("--holding-minutes", default="60")
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
                ),
                symbols=requested_symbols,
            )
            reports.append(research_payload(result))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    payload: dict[str, object] = {
        "research_version": RESEARCH_VERSION,
        "automatic_promotion_allowed": False,
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
