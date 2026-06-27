"""P286: vectorbt-style signal-array backtest."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import validate_series


@dataclass(frozen=True)
class SignalBacktestResult:
    equity_curve: list[float]
    trades: list[dict[str, Any]]
    stats: dict[str, float | int | None]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def signal_backtest_report(prices: list[float], *, entries: list[bool] | None = None, exits: list[bool] | None = None, target_positions: list[float] | None = None, size: float = 1.0, initial_cash: float = 10000.0, fee_bps: float = 0.0, slippage_bps: float = 0.0) -> SignalBacktestResult:
    px = validate_series(prices, name="prices", min_len=2)
    if any(price <= 0 for price in px):
        raise ValueError("prices must be positive")
    initial = _finite_number(initial_cash, "initial_cash")
    order_size = _finite_number(size, "size")
    fees = _finite_number(fee_bps, "fee_bps")
    slippage = _finite_number(slippage_bps, "slippage_bps")
    if initial <= 0 or order_size <= 0 or fees < 0 or slippage < 0:
        raise ValueError("initial_cash/size must be positive and costs non-negative")
    if target_positions is not None:
        targets = validate_series(target_positions, name="target_positions", min_len=len(px))
        if len(targets) != len(px):
            raise ValueError("target_positions length mismatch")
        return _target_mode(px, targets, initial)
    if entries is None or exits is None or len(entries) != len(px) or len(exits) != len(px):
        raise ValueError("entries/exits must match prices length")
    if any(not isinstance(x, bool) for x in entries) or any(not isinstance(x, bool) for x in exits):
        raise ValueError("entries/exits must be boolean lists")
    cash = initial
    position = 0.0
    entry_price = 0.0
    entry_bar = 0
    trades: list[dict[str, Any]] = []
    equity: list[float] = []
    slip = slippage / 10000.0
    fee_rate = fees / 10000.0
    for i, price in enumerate(px):
        if position == 0 and entries[i]:
            fill = price * (1 + slip)
            position = order_size
            entry_price = fill
            entry_bar = i
            cash -= fill * position * fee_rate
        elif position != 0 and exits[i]:
            fill = price * (1 - slip)
            pnl = (fill - entry_price) * position - fill * position * fee_rate
            cash += pnl
            trades.append({"entry_bar": entry_bar, "exit_bar": i, "side": "long", "size": position, "entry_price": entry_price, "exit_price": fill, "pnl": pnl, "return_pct": (fill - entry_price) / entry_price})
            position = 0.0
        equity.append(cash + (price - entry_price) * position if position else cash)
    return SignalBacktestResult(equity, trades, {"num_trades": len(trades), "total_return": equity[-1] / initial - 1.0, "fee_total": None, "turnover": len(trades) / len(px), "avg_trade_pnl": sum(t["pnl"] for t in trades) / len(trades) if trades else 0.0})


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _target_mode(px: list[float], targets: list[float], initial_cash: float) -> SignalBacktestResult:
    equity = [float(initial_cash)]
    trades: list[dict[str, Any]] = []
    for i in range(1, len(px)):
        equity.append(equity[-1] * (1.0 + targets[i - 1] * (px[i] / px[i - 1] - 1.0)))
    return SignalBacktestResult(equity, trades, {"num_trades": 0, "total_return": equity[-1] / initial_cash - 1.0, "turnover": sum(abs(b - a) for a, b in zip(targets, targets[1:]))})


__all__ = ["SignalBacktestResult", "signal_backtest_report"]
