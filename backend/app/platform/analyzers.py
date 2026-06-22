from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any, Sequence

from app.platform.events import FillEvent

__all__ = ["TradeAnalyzer", "DrawDownAnalyzer", "ReturnsAnalyzer", "analyze_backtest"]


def _coerce_fills(fills: list[Any]) -> list[FillEvent]:
    out: list[FillEvent] = []
    for f in fills:
        if isinstance(f, FillEvent):
            out.append(f)
        elif isinstance(f, dict):
            out.append(FillEvent.from_dict(f))
    return out


class TradeAnalyzer:
    """参考 Backtrader TradeAnalyzer / pyfolio：FIFO 配对，输出每笔交易明细与汇总。"""

    def analyze(self, fills: list[Any]) -> dict[str, Any]:
        events = _coerce_fills(fills)
        lots: dict[str, deque[tuple[int, Decimal]]] = {}
        trades: list[dict[str, Any]] = []
        for fill in events:
            sym = fill.symbol or ""
            q = lots.setdefault(sym, deque())
            if fill.side == "BUY":
                q.append((fill.quantity, fill.price))
            else:
                remaining = fill.quantity
                while remaining > 0 and q:
                    lot_qty, lot_price = q[0]
                    matched = min(remaining, lot_qty)
                    pnl = (fill.price - lot_price) * Decimal(matched) - fill.commission
                    trades.append({"symbol": sym, "quantity": matched, "pnl": float(pnl)})
                    remaining -= matched
                    if matched == lot_qty:
                        q.popleft()
                    else:
                        q[0] = (lot_qty - matched, lot_price)
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = sum(abs(p) for p in losses)
        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0
        return {
            "num_trades": len(trades),
            "win_rate": (len(wins) / len(pnls)) if pnls else 0.0,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "largest_win": max(wins) if wins else 0.0,
            "largest_loss": min(losses) if losses else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else None,
            "expectancy": expectancy,
            "trades": trades,
        }


class DrawDownAnalyzer:
    """参考 pyfolio underwater 曲线：从权益序列计算每点回撤与最大回撤/持续时间。"""

    def analyze(self, equity: Sequence[float]) -> dict[str, Any]:
        if len(equity) < 2:
            return {"max_drawdown": 0.0, "max_drawdown_duration": 0, "underwater": []}
        peak = equity[0]
        underwater: list[float] = []
        max_dd = 0.0
        dd_start = 0
        cur_start = 0
        max_duration = 0
        for i, v in enumerate(equity):
            if v > peak:
                peak = v
                cur_start = i
            dd = (peak - v) / peak if peak > 0 else 0.0
            underwater.append(dd)
            if dd > max_dd:
                max_dd = dd
                dd_start = cur_start
            max_duration = max(max_duration, i - cur_start)
        return {
            "max_drawdown": max_dd,
            "max_drawdown_duration": max_duration,
            "underwater": underwater,
        }


class ReturnsAnalyzer:
    """参考 pyfolio returns：周期收益序列与分布特征。"""

    def analyze(self, equity: Sequence[float]) -> dict[str, Any]:
        returns: list[float] = []
        for i in range(1, len(equity)):
            prev = equity[i - 1]
            returns.append((equity[i] / prev - 1.0) if prev != 0 else 0.0)
        if not returns:
            return {
                "num_periods": 0,
                "cumulative_return": 0.0,
                "best_period": 0.0,
                "worst_period": 0.0,
                "positive_pct": 0.0,
                "returns": [],
            }
        positive = sum(1 for r in returns if r > 0)
        cum = (equity[-1] / equity[0] - 1.0) if equity[0] > 0 else 0.0
        return {
            "num_periods": len(returns),
            "cumulative_return": cum,
            "best_period": max(returns),
            "worst_period": min(returns),
            "positive_pct": positive / len(returns),
            "returns": returns,
        }


def analyze_backtest(result: dict[str, Any]) -> dict[str, Any]:
    equity = [float(pt["nav"]) for pt in result.get("equity_curve", [])]
    fills = result.get("fills", [])
    return {
        "trades": TradeAnalyzer().analyze(fills),
        "drawdown": DrawDownAnalyzer().analyze(equity),
        "returns": ReturnsAnalyzer().analyze(equity),
    }
