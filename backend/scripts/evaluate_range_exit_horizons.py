#!/usr/bin/env python3
"""Evaluate range-strategy holding horizons on local OHLC data.

This research CLI is deliberately offline: it imports the pure backtest engine,
reads one explicit JSON file, and optionally writes one explicit JSON report.
It does not import a broker, a database model, or an order service.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.backtest import (  # noqa: E402
    BacktestBar,
    BacktestEngine,
    BacktestEngineParams,
    BacktestResultData,
    BacktestTrade,
)
from app.core.market_calendar import trade_day_for  # noqa: E402


_SCHEMA_VERSION = "range-exit-horizon-research-v1"
_LIMITATIONS = (
    "OHLC bars do not reveal the intrabar price path. Simultaneous threshold "
    "touches follow BacktestEngine's deterministic OHLC rules.",
    "Bar timestamps are normalized to observation/end time. Time-stop and "
    "end-of-day forced exits use the observed bar close, not historical BBO, "
    "so fills are bar-close approximations.",
    "Fresh crossing is a conservative bar-local OHLC approximation; quote-level "
    "crossing age and ordering cannot be reconstructed from candles.",
    "The daily-loss guard approximates the realized-plus-unrealized priority of "
    "the live deterministic BBO reduction path with closed-trade net PnL plus "
    "the open position's gross PnL at the observed "
    "bar close. Historical executable BBO and intrabar breach timing cannot be "
    "reconstructed, and the separate live last-price pre-pause fallback is not "
    "modeled.",
    "A filled DAILY_LOSS reduction is non-auto-resumable in live execution. The "
    "bar input contains no operator-resume event, so replay remains paused for "
    "the rest of the run after that forced exit.",
    "Backtest ANY mode preserves legacy all-hours entry semantics; the live safety "
    "layer still rejects new long entries outside RTH.",
    "RTH_ONLY skips non-RTH fills but does not latch any non-RTH deterministic "
    "reduction (DAILY_LOSS, PRICE_STOP, EOD, or TIME) for execution at the next "
    "open as the live reduction workflow does.",
    "The simulation has no historical bid/ask spread, latency, order queue, "
    "rejections, partial fills, or broker buying-power replay; fixed quantity is "
    "not dynamic full-buying-power sizing.",
    "Results are research evidence only, are not LIVE_EQUIVALENT, and cannot "
    "submit orders or authorize automatic strategy promotion.",
)


@dataclass(frozen=True)
class ResearchConfig:
    symbol: str
    market: str
    buy_low: float
    sell_high: float
    stop_loss_pct: float
    fee_rate: float
    slippage_pct: float
    quantity: float
    horizons: tuple[int, ...]
    bar_timestamp: str
    bar_minutes: int
    opening_warmup_minutes: int = 0
    entry_cutoff_minutes_before_close: int = 0
    flatten_minutes_before_close: int = 0
    max_entries_per_symbol_per_day: int = 0
    entry_crossing_required: bool = False
    trading_session_mode: str = "RTH_ONLY"
    min_profit_amount: float = 0.0
    fixed_fee: float = 0.0
    trailing_stop_pct: float = 0.0
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 3
    initial_cash: float = 100000.0
    discovery_ratio: float = 0.70
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class ChronologicalDaySplit:
    all_dates: tuple[date, ...]
    discovery_dates: tuple[date, ...]
    holdout_dates: tuple[date, ...]
    discovery_bars: tuple[BacktestBar, ...]
    holdout_bars: tuple[BacktestBar, ...]


@dataclass(frozen=True)
class _ClosedTrade:
    net_pnl: float
    entry_notional: float
    exit_date: date
    exit_cause: str


def _parse_timestamp(raw: object, *, row_number: int) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"row {row_number}: timestamp must be a non-empty string")
    value = raw.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"row {row_number}: timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _positive_price(raw: object, *, name: str, row_number: int) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"row {row_number}: {name} must be a number")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"row {row_number}: {name} must be finite and greater than 0")
    return value


def load_range_bars(
    path: Path,
    *,
    bar_timestamp: str,
    bar_minutes: int,
) -> list[BacktestBar]:
    """Load ``[timestamp, open, high, low, close]`` rows.

    BacktestEngine treats timestamps as observation times. Start-stamped input
    is therefore shifted forward by exactly ``bar_minutes``.
    """
    if bar_timestamp not in {"start", "end"}:
        raise ValueError("bar_timestamp must be 'start' or 'end'")
    if bar_minutes <= 0:
        raise ValueError("bar_minutes must be greater than 0")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read input file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"input file is not valid JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("input JSON must be a non-empty array of OHLC rows")

    shift = timedelta(minutes=bar_minutes) if bar_timestamp == "start" else timedelta(0)
    bars: list[BacktestBar] = []
    seen_timestamps: set[datetime] = set()
    for row_number, raw_row in enumerate(payload, start=1):
        if not isinstance(raw_row, list) or len(raw_row) != 5:
            raise ValueError(
                f"row {row_number}: expected [timestamp, open, high, low, close]"
            )
        timestamp = _parse_timestamp(raw_row[0], row_number=row_number) + shift
        open_price = _positive_price(raw_row[1], name="open", row_number=row_number)
        high = _positive_price(raw_row[2], name="high", row_number=row_number)
        low = _positive_price(raw_row[3], name="low", row_number=row_number)
        close = _positive_price(raw_row[4], name="close", row_number=row_number)
        if high < low:
            raise ValueError(f"row {row_number}: high must be greater than or equal to low")
        if high < max(open_price, close):
            raise ValueError(
                f"row {row_number}: high must be greater than or equal to open and close"
            )
        if low > min(open_price, close):
            raise ValueError(
                f"row {row_number}: low must be less than or equal to open and close"
            )
        if timestamp in seen_timestamps:
            raise ValueError(f"row {row_number}: duplicate observation timestamp {timestamp.isoformat()}")
        seen_timestamps.add(timestamp)
        bars.append(BacktestBar(
            timestamp=timestamp,
            open=open_price,
            high=high,
            low=low,
            close=close,
        ))
    return sorted(bars, key=lambda bar: bar.timestamp)


def filter_bars_by_local_date(
    bars: list[BacktestBar] | tuple[BacktestBar, ...],
    *,
    market: str,
    start_date: date | None,
    end_date: date | None,
) -> list[BacktestBar]:
    """Apply inclusive, exchange-local whole-day boundaries."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    filtered = [
        bar
        for bar in bars
        if (start_date is None or trade_day_for(market, bar.timestamp) >= start_date)
        and (end_date is None or trade_day_for(market, bar.timestamp) <= end_date)
    ]
    if not filtered:
        raise ValueError("no bars remain after exchange-local date filtering")
    return sorted(filtered, key=lambda bar: bar.timestamp)


def split_bars_by_local_day(
    bars: list[BacktestBar] | tuple[BacktestBar, ...],
    *,
    market: str,
    discovery_ratio: float,
) -> ChronologicalDaySplit:
    """Create a chronological discovery/holdout split without splitting a day."""
    if not 0 < discovery_ratio < 1:
        raise ValueError("discovery_ratio must be between 0 and 1")
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    all_dates = tuple(sorted({trade_day_for(market, bar.timestamp) for bar in ordered}))
    if len(all_dates) < 2:
        raise ValueError("at least two exchange-local trading days are required")
    split_index = min(max(int(len(all_dates) * discovery_ratio), 1), len(all_dates) - 1)
    discovery_dates = all_dates[:split_index]
    holdout_dates = all_dates[split_index:]
    discovery_set = set(discovery_dates)
    holdout_set = set(holdout_dates)
    return ChronologicalDaySplit(
        all_dates=all_dates,
        discovery_dates=discovery_dates,
        holdout_dates=holdout_dates,
        discovery_bars=tuple(
            bar for bar in ordered if trade_day_for(market, bar.timestamp) in discovery_set
        ),
        holdout_bars=tuple(
            bar for bar in ordered if trade_day_for(market, bar.timestamp) in holdout_set
        ),
    )


def parse_horizons(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            raise ValueError("horizons must be comma-separated positive integers")
        try:
            value = int(candidate)
        except ValueError as exc:
            raise ValueError("horizons must be comma-separated positive integers") from exc
        if not 1 <= value <= 10_080:
            raise ValueError("each horizon must be in [1, 10080] minutes")
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError("horizons must not contain duplicates")
    return tuple(sorted(values))


def _exit_cause(trade: BacktestTrade) -> str:
    action = trade.action.upper()
    for cause in (
        "DAILY_LOSS",
        "PRICE_STOP",
        "STOP_LOSS",
        "EOD_FLATTEN",
        "TIME_STOP",
        "TRAILING_STOP",
    ):
        if action.startswith(cause) or trade.reason.upper().startswith(f"{cause}:"):
            return cause
    if action in {"SELL", "BUY_TO_COVER"}:
        return "TARGET"
    return action


def _closed_trades(result: BacktestResultData, *, market: str) -> list[_ClosedTrade]:
    entry: BacktestTrade | None = None
    closed: list[_ClosedTrade] = []
    for trade in result.trades:
        if trade.state_after in {"long", "short"}:
            entry = trade
            continue
        if trade.state_after != "flat" or trade.net_pnl is None:
            continue
        if entry is None:
            raise RuntimeError("BacktestEngine returned an exit without a matching entry")
        entry_notional = abs(entry.price * entry.quantity)
        if entry_notional <= 0:
            raise RuntimeError("BacktestEngine returned a non-positive entry notional")
        closed.append(_ClosedTrade(
            net_pnl=float(trade.net_pnl),
            entry_notional=entry_notional,
            exit_date=trade_day_for(market, trade.timestamp),
            exit_cause=_exit_cause(trade),
        ))
        entry = None
    return closed


def _finite(value: float) -> float:
    rounded = round(float(value), 10)
    return 0.0 if rounded == 0 else rounded


def _period_metrics(
    result: BacktestResultData,
    *,
    market: str,
    session_dates: tuple[date, ...],
) -> dict[str, object]:
    trades = _closed_trades(result, market=market)
    net_pnls = [trade.net_pnl for trade in trades]
    net_bps = [trade.net_pnl / trade.entry_notional * 10_000 for trade in trades]
    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]
    profit_factor = (
        sum(wins) / abs(sum(losses))
        if losses
        else None
    )
    day_pnl: defaultdict[date, float] = defaultdict(float)
    for trade in trades:
        day_pnl[trade.exit_date] += trade.net_pnl
    positive_days = sum(1 for session_date in session_dates if day_pnl[session_date] > 0)
    cause_counts = Counter(trade.exit_cause for trade in trades)
    peak_equity = result.metrics.initial_cash
    max_drawdown_amount = 0.0
    for point in result.equity_curve:
        peak_equity = max(peak_equity, point.equity)
        max_drawdown_amount = max(max_drawdown_amount, peak_equity - point.equity)
    return {
        "session_days": len(session_dates),
        "positive_days": positive_days,
        "positive_day_ratio": _finite(positive_days / len(session_dates)),
        "total_net_pnl": _finite(sum(net_pnls)),
        "closed_trades": len(trades),
        "win_rate_pct": _finite(len(wins) / len(trades) * 100) if trades else 0.0,
        "profit_factor": _finite(profit_factor) if profit_factor is not None else None,
        "exit_cause_counts": dict(sorted(cause_counts.items())),
        "median_net_pnl": _finite(statistics.median(net_pnls)) if net_pnls else None,
        "median_net_bps": _finite(statistics.median(net_bps)) if net_bps else None,
        "avg_holding_minutes": _finite(result.metrics.avg_holding_minutes),
        "fees_paid": _finite(result.metrics.fees_paid),
        "max_drawdown_amount": _finite(max_drawdown_amount),
        "max_drawdown_pct": _finite(result.metrics.max_drawdown_pct),
        "engine_mark_to_market_total_pnl": _finite(result.metrics.total_pnl),
        "open_position_at_end": result.metrics.final_state != "flat",
    }


def _engine_params(config: ResearchConfig, horizon: int) -> BacktestEngineParams:
    return BacktestEngineParams(
        symbol=config.symbol,
        market=config.market,
        trading_session_mode=config.trading_session_mode,
        buy_low=config.buy_low,
        sell_high=config.sell_high,
        short_selling=False,
        min_profit_amount=config.min_profit_amount,
        max_daily_loss=config.max_daily_loss,
        max_consecutive_losses=config.max_consecutive_losses,
        quantity=config.quantity,
        initial_cash=config.initial_cash,
        fee_rate=config.fee_rate,
        fixed_fee=config.fixed_fee,
        slippage_pct=config.slippage_pct,
        stop_loss_pct=config.stop_loss_pct,
        trailing_stop_pct=config.trailing_stop_pct,
        opening_warmup_minutes=config.opening_warmup_minutes,
        entry_crossing_required=config.entry_crossing_required,
        max_entries_per_symbol_per_day=config.max_entries_per_symbol_per_day,
        max_holding_minutes=horizon,
        entry_cutoff_minutes_before_close=(
            config.entry_cutoff_minutes_before_close
        ),
        flatten_minutes_before_close=config.flatten_minutes_before_close,
    )


def evaluate_range_exit_horizons(
    bars: list[BacktestBar] | tuple[BacktestBar, ...],
    config: ResearchConfig,
    *,
    source_path: Path | None = None,
) -> dict[str, object]:
    """Run every horizon on full/discovery/holdout periods deterministically."""
    if config.market not in {"US", "HK"}:
        raise ValueError("market must be US or HK")
    symbol_suffix = config.symbol.rsplit(".", 1)[-1] if "." in config.symbol else ""
    if symbol_suffix != config.market:
        raise ValueError(
            f"symbol suffix .{symbol_suffix or '?'} does not match market "
            f"{config.market}"
        )
    if config.bar_timestamp not in {"start", "end"}:
        raise ValueError("bar_timestamp must be 'start' or 'end'")
    if config.bar_minutes <= 0:
        raise ValueError("bar_minutes must be greater than 0")
    if not config.horizons:
        raise ValueError("at least one horizon is required")
    source_bars = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    filtered_bars = tuple(filter_bars_by_local_date(
        source_bars,
        market=config.market,
        start_date=config.start_date,
        end_date=config.end_date,
    ))
    split = split_bars_by_local_day(
        filtered_bars,
        market=config.market,
        discovery_ratio=config.discovery_ratio,
    )
    periods = (
        ("full", filtered_bars, split.all_dates),
        ("discovery", split.discovery_bars, split.discovery_dates),
        ("holdout", split.holdout_bars, split.holdout_dates),
    )
    horizon_results: list[dict[str, object]] = []
    for horizon in sorted(config.horizons):
        horizon_payload: dict[str, object] = {"horizon_minutes": horizon}
        for name, period_bars, period_dates in periods:
            result = BacktestEngine(_engine_params(config, horizon)).run(
                list(period_bars),
                include_fee_sensitivity=False,
            )
            horizon_payload[name] = _period_metrics(
                result,
                market=config.market,
                session_dates=period_dates,
            )
        horizon_results.append(horizon_payload)

    return {
        "schema_version": _SCHEMA_VERSION,
        "research_only": True,
        "live_equivalent": False,
        "automatic_promotion_allowed": False,
        "fidelity_mode": "OHLC_BAR_CLOSE_APPROXIMATION",
        "input": {
            "path": str(source_path) if source_path is not None else None,
            "source_bars": len(source_bars),
            "evaluated_bars": len(filtered_bars),
            "bar_timestamp": config.bar_timestamp,
            "bar_minutes": config.bar_minutes,
            "first_observation_at": filtered_bars[0].timestamp.isoformat(),
            "last_observation_at": filtered_bars[-1].timestamp.isoformat(),
        },
        "parameters": {
            "symbol": config.symbol,
            "market": config.market,
            "trading_session_mode": config.trading_session_mode,
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
            "stop_loss_pct": config.stop_loss_pct,
            "trailing_stop_pct": config.trailing_stop_pct,
            "fee_rate": config.fee_rate,
            "fixed_fee": config.fixed_fee,
            "slippage_pct": config.slippage_pct,
            "quantity": config.quantity,
            "horizons": list(sorted(config.horizons)),
            "opening_warmup_minutes": config.opening_warmup_minutes,
            "entry_cutoff_minutes_before_close": (
                config.entry_cutoff_minutes_before_close
            ),
            "flatten_minutes_before_close": config.flatten_minutes_before_close,
            "max_entries_per_symbol_per_day": (
                config.max_entries_per_symbol_per_day
            ),
            "entry_crossing_required": config.entry_crossing_required,
            "min_profit_amount": config.min_profit_amount,
            "max_daily_loss": config.max_daily_loss,
            "max_consecutive_losses": config.max_consecutive_losses,
            "initial_cash": config.initial_cash,
            "start_date": config.start_date.isoformat() if config.start_date else None,
            "end_date": config.end_date.isoformat() if config.end_date else None,
        },
        "split": {
            "method": "chronological_exchange_local_whole_days",
            "discovery_ratio": config.discovery_ratio,
            "all_dates": [value.isoformat() for value in split.all_dates],
            "discovery_dates": [value.isoformat() for value in split.discovery_dates],
            "holdout_dates": [value.isoformat() for value in split.holdout_dates],
            "discovery_end_date": split.discovery_dates[-1].isoformat(),
            "holdout_start_date": split.holdout_dates[0].isoformat(),
        },
        "limitations": list(_LIMITATIONS),
        "horizons": horizon_results,
    }


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than 0")
    return value


def _non_negative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return value


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def _ratio(raw: str) -> float:
    value = _positive_float(raw)
    if value >= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _date_value(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline range exit-horizon research over JSON OHLC rows; never "
            "connects to a broker or database."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market", choices=("US", "HK"), default="US")
    parser.add_argument("--buy-low", type=_positive_float, required=True)
    parser.add_argument("--sell-high", type=_positive_float, required=True)
    parser.add_argument("--stop-loss", type=_non_negative_float, default=0.0,
                        help="fixed stop-loss percent")
    parser.add_argument("--fee", "--fee-rate", dest="fee_rate",
                        type=_non_negative_float, default=0.0,
                        help="simplified per-side notional fee rate")
    parser.add_argument("--fixed-fee", type=_non_negative_float, default=0.0)
    parser.add_argument("--slippage", type=_non_negative_float, default=0.0,
                        help="per-fill slippage percent")
    parser.add_argument("--quantity", type=_positive_float, default=1.0)
    parser.add_argument("--horizons", default="15,30,45,60")
    parser.add_argument("--warmup", type=_non_negative_int, default=0,
                        help="RTH opening warmup minutes")
    parser.add_argument("--cutoff", type=_non_negative_int, default=0,
                        help="entry cutoff minutes before close")
    parser.add_argument("--flatten", type=_non_negative_int, default=0,
                        help="forced flatten minutes before close")
    parser.add_argument("--max-entries", type=_non_negative_int, default=0,
                        help="per-symbol entries per exchange-local day; 0 is unlimited")
    parser.add_argument(
        "--crossing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="require a conservative fresh crossing within the OHLC bar",
    )
    parser.add_argument("--session-mode", choices=("RTH_ONLY", "ANY"),
                        default="RTH_ONLY")
    parser.add_argument("--min-profit", type=_non_negative_float, default=0.0)
    parser.add_argument("--trailing-stop", type=_non_negative_float, default=0.0)
    parser.add_argument(
        "--max-daily-loss",
        type=_non_negative_float,
        default=5000.0,
        help="daily loss limit; 0 disables the guard",
    )
    parser.add_argument("--max-consecutive-losses", type=_positive_int, default=3)
    parser.add_argument("--initial-cash", type=_positive_float, default=100000.0)
    parser.add_argument("--discovery-ratio", type=_ratio, default=0.70)
    parser.add_argument("--start-date", type=_date_value,
                        help="inclusive exchange-local date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=_date_value,
                        help="inclusive exchange-local date (YYYY-MM-DD)")
    parser.add_argument("--bar-timestamp", choices=("start", "end"), required=True)
    parser.add_argument("--bar-minutes", type=_positive_int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        horizons = parse_horizons(str(args.horizons))
        config = ResearchConfig(
            symbol=str(args.symbol).strip().upper(),
            market=str(args.market),
            buy_low=float(args.buy_low),
            sell_high=float(args.sell_high),
            stop_loss_pct=float(args.stop_loss),
            fee_rate=float(args.fee_rate),
            fixed_fee=float(args.fixed_fee),
            slippage_pct=float(args.slippage),
            quantity=float(args.quantity),
            horizons=horizons,
            bar_timestamp=str(args.bar_timestamp),
            bar_minutes=int(args.bar_minutes),
            opening_warmup_minutes=int(args.warmup),
            entry_cutoff_minutes_before_close=int(args.cutoff),
            flatten_minutes_before_close=int(args.flatten),
            max_entries_per_symbol_per_day=int(args.max_entries),
            entry_crossing_required=bool(args.crossing),
            trading_session_mode=str(args.session_mode),
            min_profit_amount=float(args.min_profit),
            trailing_stop_pct=float(args.trailing_stop),
            max_daily_loss=float(args.max_daily_loss),
            max_consecutive_losses=int(args.max_consecutive_losses),
            initial_cash=float(args.initial_cash),
            discovery_ratio=float(args.discovery_ratio),
            start_date=args.start_date,
            end_date=args.end_date,
        )
        input_path = Path(args.input)
        bars = load_range_bars(
            input_path,
            bar_timestamp=config.bar_timestamp,
            bar_minutes=config.bar_minutes,
        )
        report = evaluate_range_exit_horizons(
            bars,
            config,
            source_path=input_path,
        )
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))

    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    if args.output is not None:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
