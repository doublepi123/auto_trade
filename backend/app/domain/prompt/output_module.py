from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class OutputModule(PromptModule):
    """Renders the expected JSON output format and constraints."""

    def render(self, context: dict[str, Any]) -> str:
        current_price = context.get("current_price", 0.0)

        return f"""## 请输出以下 JSON 格式：
{{
  "selected_indicators": ["rsi", "macd", "atr"],
  "analysis": "简短的市场分析（50字以内）",
  "suggested_buy_low": 具体价格,
  "suggested_sell_high": 具体价格,
  "confidence_score": 0.0到1.0,
  "reasoning": "简要推理过程",
  "order_action": "NONE | BUY_NOW | SELL_NOW | SELL_SHORT_NOW | BUY_TO_COVER_NOW | STOP_LOSS_SELL_NOW | STOP_LOSS_COVER_NOW | CANCEL_PENDING | CANCEL_REPLACE",
  "order_price": 具体挂单价格或 null,
  "replacement_action": "NONE | BUY_NOW | SELL_NOW | SELL_SHORT_NOW | BUY_TO_COVER_NOW | STOP_LOSS_SELL_NOW | STOP_LOSS_COVER_NOW",
  "replacement_price": 撤单重挂的新价格或 null,
  "order_reason": "如需立刻交易或撤单重挂，说明原因；否则为空字符串"
}}

注意：
1. sell_high 必须严格大于 buy_low
2. ** sell_high 必须严格大于当前价格 {current_price:.2f}，buy_low 必须严格小于当前价格 {current_price:.2f} **
3. confidence_score >= 0.7 才建议采纳
4. 避免给出与现有持仓方向矛盾的区间
5. 区间宽度应基于 ATR 尽量收窄，促进高频交易
6. FLAT 状态可参考当前价格和 ATR；已有持仓时必须结合持仓成本价、持仓数量和浮动盈亏设计区间，不要仅按当前价格 +/-1% 滚动追价
7. LONG 状态下，buy_low 是加仓触发价，应结合成本价和回撤幅度；sell_high 应优先考虑持仓成本价，不要在未说明止损的情况下长期低于成本价
8. 必须综合最近5分钟价格走势、当前价格、持仓成本和最近一次LLM分析结果；如果最新价格已明显偏离旧分析，请说明维持或调整区间的理由
9. 单笔最低盈利金额会约束 suggested_buy_low 与 suggested_sell_high 的区间宽度，也会作为普通 SELL_NOW、BUY_TO_COVER_NOW 的执行门槛，避免手续费成本吞噬收益；止损动作不受此门槛限制
10. 当价格已到达卖出价、需要普通平仓或撤单重挂时，必须在 order_reason 中说明预估收益已覆盖最低盈利门槛；止损信号明确时可以直接给出止损动作
11. 对美股/US 标的，价格波动较快；当信号、购买力和风险收益支持交易时，优先采用"先挂单"策略，不要因为担心价格变化而只给 NONE
12. 若已有当前挂单且你产生了新的即时动作或新价格，按"撤旧单再重挂"的策略输出 CANCEL_REPLACE，并给出 replacement_action 与 replacement_price
13. 必须主动评估止损：若 LONG 持仓下最近5分钟价格连续下破关键支撑、跌幅扩大、买盘无法支撑或出现开始崩盘迹象，应输出 STOP_LOSS_SELL_NOW 及时卖出；若 SHORT 持仓出现相反方向的逼空风险，应输出 STOP_LOSS_COVER_NOW
14. 止损动作允许以控制亏损为优先目标，但必须在 order_reason 中明确写出支撑失效、崩盘、量价恶化或逼空风险等依据
15. 默认 order_action 使用 NONE；只有当最近5分钟累次数据、购买力、持仓成本和风险收益都支持"立即行动"时，才输出 BUY_NOW/SELL_NOW/STOP_LOSS_SELL_NOW 等动作
16. 不允许输出 JSON 以外的解释文本
17. 最终 JSON 必须同时包含 selected_indicators、suggested_buy_low、suggested_sell_high、confidence_score；不要只输出指标选择结果"""
