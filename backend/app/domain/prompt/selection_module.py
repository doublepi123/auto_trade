from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule


class SelectionModule(PromptModule):
    """Renders market state and available indicators for LLM selection."""

    def render(self, context: dict[str, Any]) -> str:
        market_state = context.get("market_state")
        if not market_state:
            return ""

        state = market_state.get("state", "neutral")
        description = market_state.get("description", "")
        suggested = market_state.get("suggested_indicators", [])

        lines = [
            "## 市场状态分析",
            f"当前市场状态：{description}",
            "",
            "## 可用技术指标",
            "请选择 3-5 个最相关的指标用于本次分析：",
            "",
            "趋势指标：",
            "- ADX（趋势强度）",
            "- MACD（趋势动量）",
            "- OBV（量价关系）",
            "",
            "震荡指标：",
            "- RSI（超买超卖）",
            "- Stochastic（随机指标）",
            "- CCI（商品通道）",
            "- Williams %R（威廉指标）",
            "",
            "成本指标：",
            "- VWAP（成交量加权均价）",
        ]

        if suggested:
            lines.append("")
            lines.append(f"基于当前市场状态，建议优先考虑：{', '.join(suggested)}")

        lines.append("")
        lines.append('请以 JSON 格式返回：{"selected_indicators": ["adx", "macd", "obv"], "reasoning": "选择理由"}')

        return "\n".join(lines)
