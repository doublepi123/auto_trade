from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule

_PROMPT_DAILY_CANDLES = 7
_PROMPT_MINUTE_CANDLES = 30


def _format_optional_price(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _render_daily_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无可用历史日 K 数据）"
    table = "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|------|------|------|------|------|--------|"
    for c in rows:
        table += (
            f"\n| {c.get('date', '-')} "
            f"| {float(c.get('open', 0)):.2f} "
            f"| {float(c.get('high', 0)):.2f} "
            f"| {float(c.get('low', 0)):.2f} "
            f"| {float(c.get('close', 0)):.2f} "
            f"| {int(c.get('volume', 0))} |"
        )
    return table


def _render_minute_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "（暂无可用 1 分钟 K 数据）"
    table = "| 时间 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|------|------|------|------|------|--------|"
    for c in rows:
        table += (
            f"\n| {c.get('time', '-')} "
            f"| {float(c.get('open', 0)):.2f} "
            f"| {float(c.get('high', 0)):.2f} "
            f"| {float(c.get('low', 0)):.2f} "
            f"| {float(c.get('close', 0)):.2f} "
            f"| {int(c.get('volume', 0))} |"
        )
    return table


class ContextModule(PromptModule):
    """Renders market data: candle tables, technical indicators, current price."""

    def render(self, context: dict[str, Any]) -> str:
        daily_candles = context.get("daily_candles", [])
        minute_candles = context.get("minute_candles", [])
        ohlcv_table = _render_daily_table(daily_candles[-_PROMPT_DAILY_CANDLES:])
        minute_table = _render_minute_table(minute_candles[-_PROMPT_MINUTE_CANDLES:])

        atr = context.get("atr", 0.0)
        bb_upper = context.get("bb_upper", 0.0)
        bb_middle = context.get("bb_middle", 0.0)
        bb_lower = context.get("bb_lower", 0.0)
        current_price = context.get("current_price", 0.0)

        rsi = context.get("rsi", 0.0)
        macd = context.get("macd", {})
        volume_analysis = context.get("volume_analysis", {})

        lines = [
            "## 市场数据（最近日 K 线）",
            ohlcv_table,
            "",
            "## 市场数据（最近 1 分钟 K 线）",
            minute_table,
            "",
            "## 当前技术指标",
            f"- ATR(14): {atr:.2f}",
            f"- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}",
            f"- 当前价格: {current_price:.2f}",
        ]

        if rsi > 0:
            lines.append(f"- RSI(14): {rsi:.2f}")
        if macd and macd.get("macd", 0) != 0:
            lines.append(f"- MACD: {macd['macd']:.2f} / Signal: {macd['signal']:.2f} / Hist: {macd['histogram']:.2f}")
        if volume_analysis and volume_analysis.get("avg_volume", 0) > 0:
            lines.append(
                f"- 成交量: 均量 {volume_analysis['avg_volume']:.0f} / "
                f"量比 {volume_analysis['volume_ratio']:.2f} / {volume_analysis['trend']}"
            )

        return "\n".join(lines)
