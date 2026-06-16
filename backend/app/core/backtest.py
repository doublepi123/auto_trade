from __future__ import annotations

import csv
import io
import itertools
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional

@dataclass(frozen=True)
class BacktestBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class BacktestEngineParams:
    symbol: str = ""
    buy_low: float = 0.0
    sell_high: float = 0.0
    short_selling: bool = False
    min_profit_amount: float = 0.0
    max_daily_loss: float = 5000.0
    max_consecutive_losses: int = 3
    quantity: float = 1.0
    initial_cash: float = 100000.0
    fee_rate: float = 0.0
    fixed_fee: float = 0.0
    slippage_pct: float = 0.0
    stop_loss_pct: float = 0.0


@dataclass(frozen=True)
class BacktestTrade:
    timestamp: datetime
    action: str
    price: float
    quantity: float
    fee: float
    pnl: float
    state_after: str
    reason: str
    holding_minutes: float | None = None


@dataclass(frozen=True)
class BacktestSkippedSignal:
    timestamp: datetime
    action: str
    price: float
    reason: str
    state: str
    category: str | None = None


@dataclass(frozen=True)
class BacktestEquityPoint:
    timestamp: datetime
    close: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float
    position: str


@dataclass(frozen=True)
class BacktestMetrics:
    initial_cash: float
    final_equity: float
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
    trade_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_holding_minutes: float
    fees_paid: float
    skipped_signals: int
    final_state: str
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    calmar_ratio: Optional[float] = None
    profit_factor: Optional[float] = None
    profit_loss_ratio: Optional[float] = None



@dataclass(frozen=True)
class BacktestFeeSensitivityPoint:
    fee_rate: float
    total_pnl: float
    total_return_pct: float
    max_drawdown_pct: float
@dataclass(frozen=True)
class BacktestResultData:
    metrics: BacktestMetrics
    equity_curve: list[BacktestEquityPoint]
    trades: list[BacktestTrade]
    skipped_signals: list[BacktestSkippedSignal]
    fee_sensitivity: list[BacktestFeeSensitivityPoint]


@dataclass(frozen=True)
class _OpenPosition:
    side: str
    quantity: float
    entry_price: float
    entry_at: datetime
    entry_fee: float


class BacktestEngine:
    def __init__(self, params: BacktestEngineParams) -> None:
        self.params: BacktestEngineParams = params
        self._validate_params()

    def run(self, bars: list[BacktestBar], *, include_fee_sensitivity: bool = True) -> BacktestResultData:
        ordered_bars = sorted(bars, key=lambda item: item.timestamp)
        if not ordered_bars:
            raise ValueError("at least one price bar is required")

        position: _OpenPosition | None = None
        trades: list[BacktestTrade] = []
        skipped: list[BacktestSkippedSignal] = []
        equity_curve: list[BacktestEquityPoint] = []
        closed_trade_pnls: list[float] = []
        holding_minutes: list[float] = []

        realized_pnl = 0.0
        fees_paid = 0.0
        daily_pnl = 0.0
        current_day = ordered_bars[0].timestamp.date()
        consecutive_losses = 0
        paused_reason = ""
        peak_equity = self.params.initial_cash
        max_drawdown_pct = 0.0

        for bar in ordered_bars:
            closed_position_this_bar = False
            if bar.timestamp.date() != current_day:
                current_day = bar.timestamp.date()
                daily_pnl = 0.0
                consecutive_losses = 0
                if paused_reason.startswith("daily loss limit") or paused_reason.startswith("consecutive loss"):
                    paused_reason = ""

            if position is not None:
                exit_result = self._try_exit_position(bar, position)
                if exit_result is not None:
                    action, price, exit_fee, net_pnl, reason, require_min_profit = exit_result
                    if require_min_profit and net_pnl < self.params.min_profit_amount:
                        skipped.append(BacktestSkippedSignal(
                            timestamp=bar.timestamp,
                            action=action,
                            price=price,
                            reason=(
                                f"net profit {net_pnl:.2f} is below "
                                f"min_profit_amount {self.params.min_profit_amount:.2f}"
                            ),
                            state=position.side,
                            category="FEE",
                        ))
                    else:
                        gross_pnl = self._gross_exit_pnl(position, price)
                        realized_pnl += gross_pnl - exit_fee
                        fees_paid += exit_fee
                        daily_pnl += net_pnl
                        closed_trade_pnls.append(net_pnl)
                        held_minutes = (bar.timestamp - position.entry_at).total_seconds() / 60
                        holding_minutes.append(held_minutes)
                        state_after = "flat"
                        trades.append(BacktestTrade(
                            timestamp=bar.timestamp,
                            action=action,
                            price=price,
                            quantity=position.quantity,
                            fee=exit_fee,
                            pnl=net_pnl,
                            state_after=state_after,
                            reason=reason,
                            holding_minutes=held_minutes,
                        ))
                        position = None
                        closed_position_this_bar = True

                        if net_pnl < 0:
                            consecutive_losses += 1
                        else:
                            consecutive_losses = 0
                        if daily_pnl <= -abs(self.params.max_daily_loss):
                            paused_reason = f"daily loss limit reached: {daily_pnl:.2f}"
                        elif consecutive_losses >= self.params.max_consecutive_losses:
                            paused_reason = f"max consecutive losses reached: {consecutive_losses}"

            if position is None and not closed_position_this_bar:
                entry_signal = self._entry_signal(bar)
                if entry_signal is not None:
                    action, side, price, reason = entry_signal
                    if paused_reason:
                        skipped.append(BacktestSkippedSignal(
                            timestamp=bar.timestamp,
                            action=action,
                            price=price,
                            reason=paused_reason,
                            state="flat",
                            category="RISK",
                        ))
                    else:
                        entry_fee = self._fee(price, self.params.quantity)
                        fees_paid += entry_fee
                        realized_pnl -= entry_fee
                        position = _OpenPosition(
                            side=side,
                            quantity=self.params.quantity,
                            entry_price=price,
                            entry_at=bar.timestamp,
                            entry_fee=entry_fee,
                        )
                        trades.append(BacktestTrade(
                            timestamp=bar.timestamp,
                            action=action,
                            price=price,
                            quantity=self.params.quantity,
                            fee=entry_fee,
                            pnl=0.0,
                            state_after=side,
                            reason=reason,
                        ))

            unrealized_pnl = self._unrealized_pnl(position, bar.close)
            equity = self.params.initial_cash + realized_pnl + unrealized_pnl
            peak_equity = max(peak_equity, equity)
            drawdown_pct = 0.0 if peak_equity <= 0 else (peak_equity - equity) / peak_equity * 100
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
            equity_curve.append(BacktestEquityPoint(
                timestamp=bar.timestamp,
                close=bar.close,
                equity=equity,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                drawdown_pct=drawdown_pct,
                position=position.side if position else "flat",
            ))

        final_equity = equity_curve[-1].equity
        total_pnl = final_equity - self.params.initial_cash
        winning_trades = len([pnl for pnl in closed_trade_pnls if pnl > 0])
        losing_trades = len([pnl for pnl in closed_trade_pnls if pnl < 0])
        closed_trade_count = len(closed_trade_pnls)
        sharpe = self._calc_sharpe_ratio(equity_curve)
        sortino = self._calc_sortino_ratio(equity_curve)
        calmar = self._calc_calmar_ratio(equity_curve)
        profit_factor = self._calc_profit_factor(closed_trade_pnls)
        profit_loss_ratio = self._calc_profit_loss_ratio(closed_trade_pnls)
        metrics = BacktestMetrics(
            initial_cash=self.params.initial_cash,
            final_equity=final_equity,
            total_pnl=total_pnl,
            total_return_pct=(total_pnl / self.params.initial_cash * 100) if self.params.initial_cash else 0.0,
            max_drawdown_pct=max_drawdown_pct,
            trade_count=len(trades),
            closed_trade_count=closed_trade_count,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=(winning_trades / closed_trade_count * 100) if closed_trade_count else 0.0,
            avg_holding_minutes=sum(holding_minutes) / len(holding_minutes) if holding_minutes else 0.0,
            fees_paid=fees_paid,
            skipped_signals=len(skipped),
            final_state=position.side if position else "flat",
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            profit_factor=profit_factor,
            profit_loss_ratio=profit_loss_ratio,
        )
        return BacktestResultData(
            metrics=metrics,
            equity_curve=equity_curve,
            trades=trades,
            skipped_signals=skipped,
            fee_sensitivity=self._fee_sensitivity(ordered_bars) if include_fee_sensitivity else [],
        )

    @staticmethod
    def _calc_sharpe_ratio(equity_curve: list[BacktestEquityPoint]) -> Optional[float]:
        if len(equity_curve) < 2:
            return None
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1].equity
            if prev > 0:
                returns.append((equity_curve[i].equity - prev) / prev)
            else:
                returns.append(0.0)
        if len(returns) < 2:
            return None
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std = variance ** 0.5
        if std == 0:
            return None
        return mean_ret / std

    @staticmethod
    def _calc_sortino_ratio(equity_curve: list[BacktestEquityPoint]) -> Optional[float]:
        """Like Sharpe, but only penalises downside volatility.

        A common "is my strategy earning enough for the risk I'm taking"
        metric. Returns None when fewer than 2 returns or no downside
        deviation (i.e. every return is >= 0).
        """
        if len(equity_curve) < 2:
            return None
        returns: list[float] = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1].equity
            if prev > 0:
                returns.append((equity_curve[i].equity - prev) / prev)
            else:
                returns.append(0.0)
        if len(returns) < 2:
            return None
        mean_ret = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if not downside:
            return None
        downside_dev = (sum(r ** 2 for r in downside) / len(returns)) ** 0.5
        if downside_dev == 0:
            return None
        return mean_ret / downside_dev

    @staticmethod
    def _calc_calmar_ratio(equity_curve: list[BacktestEquityPoint]) -> Optional[float]:
        """累计收益率 / 最大回撤 (Total return / |max drawdown|). None when drawdown is 0."""
        if len(equity_curve) < 2:
            return None
        initial = equity_curve[0].equity
        final = equity_curve[-1].equity
        if initial <= 0:
            return None
        total_return = (final - initial) / initial
        # Compute peak-to-trough drawdown in pct.
        peak = initial
        max_dd = 0.0
        for point in equity_curve:
            if point.equity > peak:
                peak = point.equity
            if peak > 0:
                dd = (peak - point.equity) / peak
                if dd > max_dd:
                    max_dd = dd
        if max_dd == 0:
            return None
        return total_return / max_dd

    @staticmethod
    def _calc_profit_factor(closed_trade_pnls: list[float]) -> Optional[float]:
        gross_profit = sum(pnl for pnl in closed_trade_pnls if pnl > 0)
        gross_loss = sum(pnl for pnl in closed_trade_pnls if pnl < 0)
        if gross_loss == 0:
            return None
        return gross_profit / abs(gross_loss)

    @staticmethod
    def _calc_profit_loss_ratio(closed_trade_pnls: list[float]) -> Optional[float]:
        wins = [pnl for pnl in closed_trade_pnls if pnl > 0]
        losses = [abs(pnl) for pnl in closed_trade_pnls if pnl < 0]
        if not losses:
            return None
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0:
            return None
        return avg_win / avg_loss
    def _validate_params(self) -> None:
        if self.params.buy_low <= 0:
            raise ValueError("buy_low must be greater than 0")
        if self.params.sell_high <= self.params.buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        if self.params.quantity <= 0:
            raise ValueError("quantity must be greater than 0")
        if self.params.initial_cash <= 0:
            raise ValueError("initial_cash must be greater than 0")
        if self.params.min_profit_amount < 0:
            raise ValueError("min_profit_amount cannot be negative")
        if self.params.max_daily_loss <= 0:
            raise ValueError("max_daily_loss must be greater than 0")
        if self.params.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be at least 1")
        if self.params.fee_rate < 0 or self.params.fixed_fee < 0 or self.params.slippage_pct < 0:
            raise ValueError("fee and slippage values cannot be negative")
        if self.params.stop_loss_pct < 0:
            raise ValueError("stop_loss_pct cannot be negative")
        if self.params.stop_loss_pct > 100:
            raise ValueError("stop_loss_pct cannot exceed 100")

    def _entry_signal(self, bar: BacktestBar) -> tuple[str, str, float, str] | None:
        buy_hit = bar.low <= self.params.buy_low
        short_hit = self.params.short_selling and bar.high >= self.params.sell_high
        if buy_hit and short_hit:
            buy_distance = abs(bar.open - self.params.buy_low)
            short_distance = abs(bar.open - self.params.sell_high)
            if short_distance < buy_distance:
                price = self._apply_slippage(self.params.sell_high, "SELL_SHORT")
                return "SELL_SHORT", "short", price, "both thresholds touched; sell_high was nearer to open"
            price = self._apply_slippage(self.params.buy_low, "BUY")
            return "BUY", "long", price, "both thresholds touched; buy_low was nearer to open"
        if buy_hit:
            price = self._apply_slippage(self.params.buy_low, "BUY")
            return "BUY", "long", price, f"low {bar.low:.2f} <= buy_low {self.params.buy_low:.2f}"
        if short_hit:
            price = self._apply_slippage(self.params.sell_high, "SELL_SHORT")
            return "SELL_SHORT", "short", price, f"high {bar.high:.2f} >= sell_high {self.params.sell_high:.2f}"
        return None

    def _try_exit_position(self, bar: BacktestBar, position: _OpenPosition) -> tuple[str, float, float, float, str, bool] | None:
        stop_loss_pct = self.params.stop_loss_pct / 100
        if position.side == "long" and stop_loss_pct > 0:
            stop_price = position.entry_price * (1 - stop_loss_pct)
            if bar.low <= stop_price:
                price = self._apply_slippage(stop_price, "SELL")
                exit_fee = self._fee(price, position.quantity)
                net_pnl = self._gross_exit_pnl(position, price) - position.entry_fee - exit_fee
                return "STOP_LOSS_SELL", price, exit_fee, net_pnl, "stop loss reached", False
        if position.side == "short" and stop_loss_pct > 0:
            stop_price = position.entry_price * (1 + stop_loss_pct)
            if bar.high >= stop_price:
                price = self._apply_slippage(stop_price, "BUY_TO_COVER")
                exit_fee = self._fee(price, position.quantity)
                net_pnl = self._gross_exit_pnl(position, price) - position.entry_fee - exit_fee
                return "STOP_LOSS_COVER", price, exit_fee, net_pnl, "stop loss reached", False
        if position.side == "long" and bar.high >= self.params.sell_high:
            price = self._apply_slippage(self.params.sell_high, "SELL")
            exit_fee = self._fee(price, position.quantity)
            net_pnl = self._gross_exit_pnl(position, price) - position.entry_fee - exit_fee
            return "SELL", price, exit_fee, net_pnl, "exit threshold reached", True
        if position.side == "short" and bar.low <= self.params.buy_low:
            price = self._apply_slippage(self.params.buy_low, "BUY_TO_COVER")
            exit_fee = self._fee(price, position.quantity)
            net_pnl = self._gross_exit_pnl(position, price) - position.entry_fee - exit_fee
            return "BUY_TO_COVER", price, exit_fee, net_pnl, "exit threshold reached", True
        return None

    def _apply_slippage(self, price: float, action: str) -> float:
        if self.params.slippage_pct <= 0:
            return price
        factor = self.params.slippage_pct / 100
        if action in {"BUY", "BUY_TO_COVER"}:
            return price * (1 + factor)
        return price * (1 - factor)

    def _fee(self, price: float, quantity: float) -> float:
        return abs(price * quantity) * self.params.fee_rate + self.params.fixed_fee

    @staticmethod
    def _gross_exit_pnl(position: _OpenPosition, exit_price: float) -> float:
        if position.side == "short":
            return (position.entry_price - exit_price) * position.quantity
        return (exit_price - position.entry_price) * position.quantity

    @staticmethod
    def _unrealized_pnl(position: _OpenPosition | None, close_price: float) -> float:
        if position is None:
            return 0.0
        if position.side == "short":
            return (position.entry_price - close_price) * position.quantity
        return (close_price - position.entry_price) * position.quantity

    def _fee_sensitivity(self, bars: list[BacktestBar]) -> list[BacktestFeeSensitivityPoint]:
        if self.params.fee_rate > 0:
            rates = [0.0, self.params.fee_rate / 2, self.params.fee_rate, self.params.fee_rate * 2]
        else:
            rates = [0.0, 0.0005, 0.001, 0.002]
        points: list[BacktestFeeSensitivityPoint] = []
        seen: set[float] = set()
        for rate in rates:
            rounded_rate = round(rate, 8)
            if rounded_rate in seen:
                continue
            seen.add(rounded_rate)
            result = BacktestEngine(replace(self.params, fee_rate=rounded_rate)).run(
                bars,
                include_fee_sensitivity=False,
            )
            points.append(BacktestFeeSensitivityPoint(
                fee_rate=rounded_rate,
                total_pnl=result.metrics.total_pnl,
                total_return_pct=result.metrics.total_return_pct,
                max_drawdown_pct=result.metrics.max_drawdown_pct,
            ))
        return points


def parse_backtest_csv(csv_text: str) -> list[BacktestBar]:
    if not csv_text.strip():
        raise ValueError("csv_text is required")
    # Strip UTF-8 BOM that Excel/Numbers prepend when exporting as "CSV UTF-8";
    # otherwise the first column header becomes "﻿timestamp" and fails
    # the required-columns check below with a misleading error.
    csv_text = csv_text.lstrip("﻿").strip()
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if reader.fieldnames is None:
        raise ValueError("CSV header is required")
    normalized_headers = {name.strip().lstrip("﻿").lower() for name in reader.fieldnames}
    missing = required - normalized_headers
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    bars: list[BacktestBar] = []
    for row_number, row in enumerate(reader, start=2):
        normalized = {str(key).strip().lower(): str(value).strip() for key, value in row.items() if key is not None}
        if not any(normalized.values()):
            continue
        try:
            bar = BacktestBar(
                timestamp=_parse_timestamp(normalized["timestamp"]),
                open=_parse_float(normalized["open"], "open"),
                high=_parse_float(normalized["high"], "high"),
                low=_parse_float(normalized["low"], "low"),
                close=_parse_float(normalized["close"], "close"),
                volume=_parse_float(normalized["volume"], "volume", allow_zero=True),
            )
            _validate_bar(bar)
        except ValueError as exc:
            raise ValueError(f"row {row_number}: {exc}") from exc
        bars.append(bar)

    if not bars:
        raise ValueError("CSV must contain at least one data row")
    return bars


def _parse_timestamp(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_float(value: str, name: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if allow_zero:
        if parsed < 0:
            raise ValueError(f"{name} cannot be negative")
    elif parsed <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return parsed


def _validate_bar(bar: BacktestBar) -> None:
    if bar.high < bar.low:
        raise ValueError("high must be greater than or equal to low")
    if bar.high < max(bar.open, bar.close):
        raise ValueError("high must be greater than or equal to open and close")
    if bar.low > min(bar.open, bar.close):
        raise ValueError("low must be less than or equal to open and close")


# ---------------------------------------------------------------------------
# Parameter sweep — run BacktestEngine over a numeric grid and rank results.
#
# Pure / deterministic / offline: imports no SQLAlchemy, broker, or pydantic.
# Reuses BacktestEngine._validate_params as the per-combo validity gate and
# mirrors ExperimentGridService._expand_item's float tolerance in
# expand_numeric_range so the core never depends on the pydantic grid service.
# ---------------------------------------------------------------------------

#: Parameter axes a sweep may explore. Focused on the interval/profit/risk
#: knobs that actually change a range-trading backtest's behaviour; ``symbol``
#: and ``short_selling`` are non-numeric and intentionally excluded.
SWEEP_ALLOWED_GRID_KEYS: frozenset[str] = frozenset({
    "buy_low", "sell_high", "min_profit_amount",
    "quantity", "fee_rate", "slippage_pct", "stop_loss_pct",
})

#: Metrics the sweep may rank by. Each is a (possibly-None) attribute of
#: BacktestMetrics computed by BacktestEngine.run.
SWEEP_SORT_KEYS: tuple[str, ...] = (
    "sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "total_return_pct",
)

SWEEP_DEFAULT_MAX_COMBINATIONS = 2000
SWEEP_ABSOLUTE_MAX_COMBINATIONS = 10_000
_SWEEP_GRID_PRECISION = 10
_SWEEP_EPS = 1e-12


def expand_numeric_range(start: float, step: float, end: float) -> list[float]:
    """Expand the inclusive range ``[start, end]`` by ``step``.

    Mirrors ``ExperimentGridService._expand_item`` (``round(v, 10)`` + epsilon)
    so a pure-core caller does not pull in the pydantic-bound grid service.
    """
    if step == 0:
        raise ValueError("grid step cannot be zero")
    if step < 0:
        raise ValueError("grid step must be positive")
    values: list[float] = []
    i = 0
    while True:
        v = round(start + i * step, _SWEEP_GRID_PRECISION)
        if v > end + _SWEEP_EPS:
            break
        values.append(v)
        i += 1
    return values


@dataclass(frozen=True)
class SweepResultRow:
    params: BacktestEngineParams
    metrics: BacktestMetrics
    rank: int = 0


@dataclass(frozen=True)
class SweepHeatmapCell:
    buy_low: float
    sell_high: float
    value: float | None


@dataclass(frozen=True)
class SweepHeatmap:
    x_axis: str
    y_axis: str
    z_metric: str
    cells: list[SweepHeatmapCell]


@dataclass(frozen=True)
class SweepResult:
    rows: list[SweepResultRow]
    best: SweepResultRow | None
    heatmap: SweepHeatmap
    evaluated_count: int
    skipped_count: int
    sort_by: str


def _sweep_sort_value(metrics: BacktestMetrics, sort_by: str) -> float:
    """Ranking metric value; ``None`` -> ``-inf`` so never-trading combinations
    sort below every real value under descending order."""
    raw = getattr(metrics, sort_by)
    if raw is None:
        return float("-inf")
    return float(raw)


def _build_sweep_heatmap(rows: list[SweepResultRow], z_metric: str) -> SweepHeatmap:
    """Collapse rows to the best ``z_metric`` per ``(buy_low, sell_high)`` cell.

    A third swept axis (e.g. ``min_profit_amount``) therefore collapses to the
    best variant per cell. ``None`` never improves a cell that already holds a
    real value, and a cell whose every row is ``None`` stays ``None``.
    """
    best: dict[tuple[float, float], float | None] = {}
    for r in rows:
        key = (r.params.buy_low, r.params.sell_high)
        val = getattr(r.metrics, z_metric)
        if key not in best:
            best[key] = val
            continue
        cur = best[key]
        if val is None:
            continue
        if cur is None or val > cur:
            best[key] = val
    cells = [
        SweepHeatmapCell(buy_low=k[0], sell_high=k[1], value=v)
        for k, v in sorted(best.items())
    ]
    return SweepHeatmap(x_axis="sell_high", y_axis="buy_low", z_metric=z_metric, cells=cells)


def sweep_backtest(
    base_params: BacktestEngineParams,
    param_grid: dict[str, list[float]],
    bars: list[BacktestBar],
    *,
    sort_by: str = "sharpe_ratio",
    max_combinations: int = SWEEP_DEFAULT_MAX_COMBINATIONS,
) -> SweepResult:
    """Run ``BacktestEngine`` over the Cartesian product of ``param_grid`` and
    return the combinations ranked by ``sort_by`` plus a best-per-cell heatmap.

    Invalid combinations (e.g. ``buy_low >= sell_high``) are silently skipped
    via the existing ``BacktestEngine`` validation gate and counted in
    ``skipped_count`` rather than raising — matching ``ExperimentGridService``.
    """
    if not bars:
        raise ValueError("at least one price bar is required")
    if sort_by not in SWEEP_SORT_KEYS:
        raise ValueError(f"sort_by must be one of {SWEEP_SORT_KEYS}")
    if max_combinations < 1:
        raise ValueError("max_combinations must be at least 1")
    if max_combinations > SWEEP_ABSOLUTE_MAX_COMBINATIONS:
        raise ValueError(
            f"max_combinations cannot exceed {SWEEP_ABSOLUTE_MAX_COMBINATIONS}"
        )

    unknown = set(param_grid) - SWEEP_ALLOWED_GRID_KEYS
    if unknown:
        raise ValueError(f"unknown grid keys: {sorted(unknown)}")
    keys = list(param_grid.keys())
    for key in keys:
        if not param_grid[key]:
            raise ValueError(f"grid for {key} must not be empty")

    total = 1
    for key in keys:
        total *= len(param_grid[key])
    if total > max_combinations:
        raise ValueError(
            f"parameter grid produced {total} combinations, "
            f"max_combinations is {max_combinations}"
        )

    rows: list[SweepResultRow] = []
    skipped = 0
    for combo in itertools.product(*[param_grid[k] for k in keys]):
        overrides = {k: float(v) for k, v in zip(keys, combo)}
        try:
            params = replace(base_params, **overrides)
            result = BacktestEngine(params).run(bars, include_fee_sensitivity=False)
        except ValueError:
            skipped += 1
            continue
        rows.append(SweepResultRow(params=params, metrics=result.metrics, rank=0))

    rows.sort(
        key=lambda r: (
            _sweep_sort_value(r.metrics, sort_by),
            r.metrics.total_return_pct,
            r.metrics.total_pnl,
        ),
        reverse=True,
    )
    ranked = [replace(r, rank=idx + 1) for idx, r in enumerate(rows)]
    heatmap = _build_sweep_heatmap(ranked, sort_by)
    return SweepResult(
        rows=ranked,
        best=ranked[0] if ranked else None,
        heatmap=heatmap,
        evaluated_count=len(ranked),
        skipped_count=skipped,
        sort_by=sort_by,
    )


# ---------------------------------------------------------------------------
# Walk-forward rolling-window evaluation — optimize on each train window,
# evaluate out-of-sample on the following test window. Reuses sweep_backtest.
# Surfaces overfitting: a config that looks great on the full period but is
# inconsistent (high return std, low profitable-window %) across windows is
# fragile. With an empty grid it degrades to plain rolling-window evaluation
# of base_params (consistency only, no per-window optimization).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    start: datetime
    end: datetime
    train_size: int
    test_size: int
    best_params: BacktestEngineParams | None
    test_metrics: BacktestMetrics | None


@dataclass(frozen=True)
class WalkForwardSummary:
    window_count: int
    evaluated_window_count: int
    mean_test_return_pct: float | None
    median_test_return_pct: float | None
    mean_test_metric: float | None
    profitable_window_pct: float | None
    test_return_std_pct: float | None


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[WalkForwardWindow]
    summary: WalkForwardSummary
    sort_by: str
    train_size: int
    test_size: int
    step: int


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


def walk_forward_backtest(
    base_params: BacktestEngineParams,
    param_grid: dict[str, list[float]],
    bars: list[BacktestBar],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    sort_by: str = "sharpe_ratio",
    max_combinations: int = SWEEP_DEFAULT_MAX_COMBINATIONS,
) -> WalkForwardResult:
    """Walk-forward optimization over rolling windows.

    For each anchor the in-sample ``train`` window optimizes params via
    :func:`sweep_backtest` (or just reuses ``base_params`` when the grid is
    empty), and the following ``test`` window evaluates those params
    out-of-sample. Returns per-window results plus an aggregate summary that
    flags consistency / overfitting.
    """
    if not bars:
        raise ValueError("at least one price bar is required")
    if train_size < 2:
        raise ValueError("train_size must be at least 2")
    if test_size < 1:
        raise ValueError("test_size must be at least 1")
    if step is not None and step < 1:
        raise ValueError("step must be at least 1")
    if sort_by not in SWEEP_SORT_KEYS:
        raise ValueError(f"sort_by must be one of {SWEEP_SORT_KEYS}")
    actual_step = test_size if step is None else step

    ordered = sorted(bars, key=lambda b: b.timestamp)
    windows: list[WalkForwardWindow] = []
    anchor = 0
    index = 0
    while anchor + train_size + test_size <= len(ordered):
        train_bars = ordered[anchor:anchor + train_size]
        test_bars = ordered[anchor + train_size:anchor + train_size + test_size]

        if param_grid:
            sweep = sweep_backtest(
                base_params,
                param_grid,
                train_bars,
                sort_by=sort_by,
                max_combinations=max_combinations,
            )
            best_params = sweep.best.params if sweep.best is not None else None
        else:
            best_params = base_params

        test_metrics: BacktestMetrics | None = None
        if best_params is not None:
            try:
                test_metrics = BacktestEngine(best_params).run(
                    test_bars, include_fee_sensitivity=False
                ).metrics
            except ValueError:
                test_metrics = None

        windows.append(WalkForwardWindow(
            index=index,
            start=train_bars[0].timestamp,
            end=test_bars[-1].timestamp,
            train_size=train_size,
            test_size=test_size,
            best_params=best_params,
            test_metrics=test_metrics,
        ))
        index += 1
        anchor += actual_step

    return WalkForwardResult(
        windows=windows,
        summary=_summarize_walk_forward(windows, sort_by),
        sort_by=sort_by,
        train_size=train_size,
        test_size=test_size,
        step=actual_step,
    )


def _summarize_walk_forward(
    windows: list[WalkForwardWindow], sort_by: str
) -> WalkForwardSummary:
    evaluated = [w.test_metrics for w in windows if w.test_metrics is not None]
    returns = [m.total_return_pct for m in evaluated]
    metric_vals = [
        v for v in (getattr(m, sort_by) for m in evaluated) if v is not None
    ]
    profitable = sum(1 for m in evaluated if m.total_pnl > 0)
    return WalkForwardSummary(
        window_count=len(windows),
        evaluated_window_count=len(evaluated),
        mean_test_return_pct=(sum(returns) / len(returns)) if returns else None,
        median_test_return_pct=_median(returns) if returns else None,
        mean_test_metric=(sum(metric_vals) / len(metric_vals)) if metric_vals else None,
        profitable_window_pct=(profitable / len(evaluated) * 100) if evaluated else None,
        test_return_std_pct=_stddev(returns) if returns else None,
    )


# ---------------------------------------------------------------------------
# What-If / Stress — Monte-Carlo ensemble over jittered price paths.
#
# Distinct from sweep (param search) and walk-forward (temporal): this answers
# "how sensitive is my result to the exact price path?" by re-running the
# engine over N deterministically jittered copies of the input bars and
# reporting the return distribution. Deterministic via a seeded RNG.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StressResult:
    scenarios_run: int
    baseline_return_pct: float | None
    median_return_pct: float | None
    p5_return_pct: float | None
    p95_return_pct: float | None
    worst_return_pct: float | None
    worst_drawdown_pct: float | None
    profitable_scenario_pct: float | None
    jitter_pct: float
    seed: int
    returns: list[float]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (pct / 100)
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def _jitter_bars(
    bars: list[BacktestBar], rng: random.Random, jitter_pct: float
) -> list[BacktestBar]:
    """Return a copy of *bars* with every OHLC scaled by a per-bar uniform
    factor in ``[1 - jitter, 1 + jitter]``. Uniform positive scaling preserves
    the high>=low / high>=open,close invariants, so no bar becomes invalid."""
    if jitter_pct <= 0:
        return list(bars)
    factor = jitter_pct / 100
    out: list[BacktestBar] = []
    for b in bars:
        m = 1 + rng.uniform(-factor, factor)
        out.append(BacktestBar(
            timestamp=b.timestamp,
            open=b.open * m,
            high=b.high * m,
            low=b.low * m,
            close=b.close * m,
            volume=b.volume,
        ))
    return out


def stress_test(
    base_params: BacktestEngineParams,
    bars: list[BacktestBar],
    *,
    scenarios: int = 50,
    jitter_pct: float = 1.0,
    seed: int = 42,
) -> StressResult:
    """Run the engine over *scenarios* jittered price paths and summarise the
    return distribution. Pure / deterministic / offline."""
    if not bars:
        raise ValueError("at least one price bar is required")
    if scenarios < 1:
        raise ValueError("scenarios must be at least 1")
    if scenarios > 1000:
        raise ValueError("scenarios cannot exceed 1000")
    if jitter_pct < 0:
        raise ValueError("jitter_pct cannot be negative")

    ordered = sorted(bars, key=lambda b: b.timestamp)
    try:
        baseline = BacktestEngine(base_params).run(
            ordered, include_fee_sensitivity=False
        ).metrics.total_return_pct
    except ValueError:
        baseline = None

    rng = random.Random(seed)
    returns: list[float] = []
    drawdowns: list[float] = []
    for _ in range(scenarios):
        perturbed = _jitter_bars(ordered, rng, jitter_pct)
        try:
            metrics = BacktestEngine(base_params).run(
                perturbed, include_fee_sensitivity=False
            ).metrics
        except ValueError:
            continue
        returns.append(metrics.total_return_pct)
        drawdowns.append(metrics.max_drawdown_pct)

    return StressResult(
        scenarios_run=len(returns),
        baseline_return_pct=baseline,
        median_return_pct=_percentile(returns, 50),
        p5_return_pct=_percentile(returns, 5),
        p95_return_pct=_percentile(returns, 95),
        worst_return_pct=(min(returns) if returns else None),
        worst_drawdown_pct=(max(drawdowns) if drawdowns else None),
        profitable_scenario_pct=(
            (sum(1 for r in returns if r > 0) / len(returns) * 100) if returns else None
        ),
        jitter_pct=jitter_pct,
        seed=seed,
        returns=sorted(returns),
    )
