from __future__ import annotations

from typing import Any

from app.domain.prompt.base import PromptModule

_PROMPT_DAILY_CANDLES = 7
_PROMPT_MINUTE_CANDLES = 30


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
        if macd and macd.get("macd") is not None:
            macd_val = float(macd.get("macd", 0.0))
            signal_val = float(macd.get("signal", 0.0))
            hist_val = float(macd.get("histogram", 0.0))
            lines.append(
                f"- MACD: {macd_val:.2f} / Signal: {signal_val:.2f} / Hist: {hist_val:.2f}"
            )
        if volume_analysis and volume_analysis.get("avg_volume") is not None:
            avg_volume = float(volume_analysis.get("avg_volume", 0.0))
            if avg_volume > 0:
                volume_ratio = float(volume_analysis.get("volume_ratio", 0.0))
                trend = volume_analysis.get("trend", "unknown")
                lines.append(
                    f"- 成交量: 均量 {avg_volume:.0f} / "
                    f"量比 {volume_ratio:.2f} / {trend}"
                )

        # Extended technical indicators
        obv = context.get("obv", {})
        adx = context.get("adx", {})
        stochastic = context.get("stochastic", {})
        cci = context.get("cci", {})
        williams_r = context.get("williams_r", {})
        vwap = context.get("vwap", {})
        aggregate_signals = context.get("aggregate_signals", {})

        has_extended = any([obv, adx, stochastic, cci, williams_r, vwap, aggregate_signals])
        if has_extended:
            lines.append("")
            lines.append("## 技术指标扩展")
            if obv and obv.get("obv_trend"):
                lines.append(
                    f"- OBV: 趋势 {obv['obv_trend']} / "
                    f"背离 {obv.get('price_obv_divergence', 'none')}"
                )
            if adx and adx.get("adx_value") is not None:
                adx_val = float(adx.get("adx_value", 0.0))
                if adx_val > 0:
                    lines.append(
                        f"- ADX: {adx_val:.2f} / "
                        f"趋势强度 {adx.get('trend_strength', 'none')}"
                    )
            if stochastic and stochastic.get("stoch_k") is not None:
                stoch_k = float(stochastic.get("stoch_k", 50.0))
                stoch_d = float(stochastic.get("stoch_d", 50.0))
                lines.append(
                    f"- Stochastic: %K {stoch_k:.2f} / %D {stoch_d:.2f} / "
                    f"{stochastic.get('signal', 'neutral')}"
                )
            if cci and cci.get("cci_value") is not None:
                cci_val = float(cci.get("cci_value", 0.0))
                lines.append(
                    f"- CCI: {cci_val:.2f} / {cci.get('signal', 'neutral')}"
                )
            if williams_r and williams_r.get("williams_r") is not None:
                wr_val = float(williams_r.get("williams_r", -50.0))
                lines.append(
                    f"- Williams %R: {wr_val:.2f} / {williams_r.get('signal', 'neutral')}"
                )
            if vwap and vwap.get("vwap_value") is not None:
                vwap_val = float(vwap.get("vwap_value", 0.0))
                if vwap_val > 0:
                    lines.append(
                        f"- VWAP: {vwap_val:.2f} / "
                        f"价格位置 {vwap.get('position', 'at')}"
                    )
            if aggregate_signals and aggregate_signals.get("overall_signal"):
                overall = aggregate_signals.get("overall_signal", "neutral")
                confidence = float(aggregate_signals.get("confidence", 0.0))
                lines.append(
                    f"- 综合信号: {overall} / 置信度 {confidence:.2f}"
                )

        # Account & buying power
        account_context_text = context.get("account_context_text")
        if account_context_text:
            lines.append("")
            lines.append("## 账户与购买力")
            lines.append(account_context_text)

        # Recent 5-min prices
        recent_price_context = context.get("recent_price_context")
        if recent_price_context:
            lines.append("")
            lines.append("## 最近5分钟价格")
            lines.append(recent_price_context)

        # Recent LLM analysis
        recent_analysis_context = context.get("recent_analysis_context")
        if recent_analysis_context:
            lines.append("")
            lines.append("## 最近一次LLM分析")
            lines.append(recent_analysis_context)

        return "\n".join(lines)
