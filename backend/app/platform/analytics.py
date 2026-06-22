from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

from app.platform.events import Event, FillEvent

__all__ = ["PerformanceAnalytics"]


class PerformanceAnalytics:
    """参考 empyrical/pyfolio 的核心绩效指标。纯 Python，无外部依赖。"""

    def __init__(self, periods_per_year: int = 252) -> None:
        self.periods_per_year = periods_per_year

    @staticmethod
    def _returns(equity: list[float]) -> list[float]:
        if len(equity) < 2:
            return []
        out: list[float] = []
        for i in range(1, len(equity)):
            prev = equity[i - 1]
            if prev == 0:
                out.append(0.0)
            else:
                out.append(equity[i] / prev - 1.0)
        return out

    def _annualize(self, stddev: float) -> float:
        return stddev * (self.periods_per_year ** 0.5)

    def equity_metrics(self, equity: list[float]) -> dict[str, Any]:
        returns = self._returns(equity)
        n = len(returns)
        if n == 0:
            return {
                "total_return": 0.0, "annual_volatility": 0.0, "sharpe": 0.0,
                "sortino": 0.0, "max_drawdown": 0.0, "calmar": 0.0,
            }
        mean_ret = sum(returns) / n
        variance = sum((r - mean_ret) ** 2 for r in returns) / n
        std = variance ** 0.5
        downside = [r for r in returns if r < 0]
        downside_var = sum(r * r for r in downside) / n if downside else 0.0
        downside_std = downside_var ** 0.5

        ann_mean = mean_ret * self.periods_per_year
        ann_std = self._annualize(std)
        sharpe = (ann_mean / ann_std) if ann_std > 0 else 0.0
        ann_downside = self._annualize(downside_std)
        sortino = (ann_mean / ann_downside) if ann_downside > 0 else 0.0

        # max drawdown over the equity curve
        peak = equity[0]
        max_dd = 0.0
        for v in equity:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        total_return = (equity[-1] / equity[0] - 1.0) if equity[0] > 0 else 0.0
        calmar = (ann_mean / max_dd) if max_dd > 0 else 0.0
        return {
            "total_return": total_return,
            "annual_volatility": ann_std,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_drawdown": max_dd,
            "calmar": calmar,
        }

    def trade_metrics(self, fills: list[FillEvent]) -> dict[str, Any]:
        per_symbol: dict[str, deque[tuple[int, Decimal]]] = {}
        gross_profit = Decimal("0")
        gross_loss = Decimal("0")
        wins = 0
        trades = 0
        for fill in fills:
            sym = fill.symbol or ""
            lots = per_symbol.setdefault(sym, deque())
            if fill.side == "BUY":
                lots.append((fill.quantity, fill.price))
            else:
                remaining = fill.quantity
                while remaining > 0 and lots:
                    lot_qty, lot_price = lots[0]
                    matched = min(remaining, lot_qty)
                    pnl = (fill.price - lot_price) * Decimal(matched) - fill.commission
                    trades += 1
                    if pnl >= 0:
                        gross_profit += pnl
                        wins += 1
                    else:
                        gross_loss += abs(pnl)
                    remaining -= matched
                    if matched == lot_qty:
                        lots.popleft()
                    else:
                        lots[0] = (lot_qty - matched, lot_price)
        win_rate = (wins / trades) if trades > 0 else 0.0
        if gross_loss > 0:
            profit_factor: float | None = float(gross_profit / gross_loss)
        elif gross_profit > 0:
            # No losing trades but at least one winner: profit factor is
            # mathematically infinite. ``inf`` is not JSON-serializable, so
            # surface ``None`` to API consumers.
            profit_factor = None
        else:
            profit_factor = 0.0
        return {
            "num_trades": trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
        }

    def analyze(
        self,
        equity: list[float],
        fills: list[FillEvent] | list[Event] | None = None,
    ) -> dict[str, Any]:
        fill_events = [f for f in (fills or []) if isinstance(f, FillEvent)]
        metrics = self.equity_metrics(equity)
        metrics.update(self.trade_metrics(fill_events))
        return metrics
