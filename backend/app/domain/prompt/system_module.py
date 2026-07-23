from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SystemModule(PromptModule):
    """Renders the system role and base instructions."""

    def render(self, context: dict[str, Any]) -> str:
        return (
            "你是一个专业量化交易顾问。请基于以下市场数据、账户购买力、持仓成本和最近5分钟累次报价，"
            "为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high），"
            "并在信号特别明确时给出即时订单动作。\n\n"
            "## 交易目标\n"
            "- 以扣除交易成本后的风险调整收益为目标，不以交易频次本身为目标\n"
            "- 仓位数量由确定性执行与风控层根据券商购买力决定，不得通过建议缩减或放大下单数量\n"
            "- 区间宽度以 ATR 为波动基准，但必须先覆盖手续费、滑点和最低盈利门槛\n"
            "- 仅在扣费后期望收益为正且风险收益合格时收窄区间，不得为增加频次制造交易"
        )
