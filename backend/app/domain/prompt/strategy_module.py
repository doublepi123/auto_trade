from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class StrategyModule(PromptModule):
    """Renders strategy params, position state, and recent trades."""

    def render(self, context: dict[str, Any]) -> str:
        symbol = context.get("symbol", "")
        market = context.get("market", "")
        current_buy_low = context.get("current_buy_low", 0.0)
        current_sell_high = context.get("current_sell_high", 0.0)
        short_selling = context.get("short_selling", False)
        min_profit_amount = context.get("min_profit_amount", 0.0)
        current_position = context.get("current_position", "FLAT")
        position_quantity = context.get("position_quantity", 0.0)
        position_avg_price = context.get("position_avg_price", 0.0)
        unrealized_pnl_pct = context.get("unrealized_pnl_pct", 0.0)
        recent_trades = context.get("recent_trades", [])

        trades_summary = "无"
        if recent_trades:
            trades_summary = "\n".join(
                f"- {t.get('side', '')}: {t.get('quantity', 0)} @ {t.get('price', 0):.2f}"
                for t in recent_trades[:3]
            )

        return f"""## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low:.2f}
- 当前 sell_high: {current_sell_high:.2f}
- 允许做空: {short_selling}
- 单笔最低盈利金额: {min_profit_amount:.2f}（约束普通即时卖出/平仓和建议区间宽度；止损动作不受此限制）

## 持仓状态
- 当前持仓方向: {current_position}
- 当前持仓数量: {position_quantity:.2f}
- 持仓成本价: {position_avg_price:.2f}
- 浮动盈亏比例: {unrealized_pnl_pct:.2f}%
- 最近成交: {trades_summary}"""
