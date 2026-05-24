from __future__ import annotations

import logging
import statistics
from typing import Any

from app.core.broker import BrokerCandle, BrokerGateway

logger = logging.getLogger("auto_trade.data_aggregator")


_DAILY_CANDLE_COUNT = 30
_MINUTE_CANDLE_COUNT = 120
_PROMPT_DAILY_CANDLES = 7
_PROMPT_MINUTE_CANDLES = 30


class DataAggregator:
    """Aggregates market data and strategy state into LLM prompt format."""

    def __init__(self, broker: BrokerGateway | None = None) -> None:
        self._broker = broker

    def fetch_market_data(self, symbol: str, market: str) -> dict[str, Any]:
        """Fetch historical candles from Longbridge SDK."""
        del market
        broker, owns_broker = self._acquire_broker()
        try:
            daily_candles = self._safe_fetch(
                broker.get_candlesticks, symbol, "Day", _DAILY_CANDLE_COUNT,
                label="daily candles",
            )
            minute_candles = self._safe_fetch(
                broker.get_candlesticks, symbol, "Min_1", _MINUTE_CANDLE_COUNT,
                label="minute candles",
            )
            current_price = self._safe_get_current_price(broker, symbol, daily_candles, minute_candles)
        finally:
            if owns_broker:
                broker.close()

        daily_payload = [_candle_to_dict_daily(c) for c in daily_candles]
        minute_payload = [_candle_to_dict_minute(c) for c in minute_candles]

        atr = _compute_atr(daily_candles) if len(daily_candles) >= 5 else 0.0
        closes = [c.close for c in daily_candles]
        bb_upper, bb_middle, bb_lower = (
            _compute_bollinger_bands(closes) if len(closes) >= 10 else (0.0, 0.0, 0.0)
        )

        return {
            "daily_candles": daily_payload,
            "minute_candles": minute_payload,
            "current_price": current_price,
            "atr": atr,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
        }

    def _acquire_broker(self) -> tuple[BrokerGateway, bool]:
        if self._broker is not None:
            return self._broker, False
        return BrokerGateway(), True

    @staticmethod
    def _safe_fetch(
        fn: Any,
        symbol: str,
        period: str,
        count: int,
        *,
        label: str,
    ) -> list[BrokerCandle]:
        try:
            return fn(symbol, period, count)
        except Exception:
            logger.exception("failed to fetch %s for %s", label, symbol)
            return []

    @staticmethod
    def _safe_get_current_price(
        broker: BrokerGateway,
        symbol: str,
        daily_candles: list[BrokerCandle],
        minute_candles: list[BrokerCandle],
    ) -> float:
        try:
            quote = broker.get_quote(symbol)
            if quote.last_price > 0:
                return float(quote.last_price)
        except Exception:
            logger.warning("failed to fetch current quote for %s", symbol)
        if minute_candles:
            return float(minute_candles[-1].close)
        if daily_candles:
            return float(daily_candles[-1].close)
        return 0.0

    @staticmethod
    def _format_optional_price(value: Any) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "-"

    @classmethod
    def _format_recent_prices(cls, recent_prices: list[dict[str, Any]] | None) -> str:
        valid_prices: list[tuple[dict[str, Any], float]] = []
        for item in recent_prices or []:
            try:
                price = float(item.get("last_price", 0))
            except (TypeError, ValueError):
                continue
            if price > 0:
                valid_prices.append((item, price))

        if not valid_prices:
            return "无"

        prices = [price for _item, price in valid_prices]
        first_price = prices[0]
        last_price = prices[-1]
        change = last_price - first_price
        change_pct = change / first_price * 100 if first_price else 0.0
        cumulative_abs_change = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
        lines = [
            f"样本数: {len(valid_prices)}",
            f"最近价: {last_price:.2f}",
            f"5分钟最高/最低/均价: {max(prices):.2f} / {min(prices):.2f} / {statistics.mean(prices):.2f}",
            f"首尾变化: {change:+.2f} ({change_pct:+.2f}%)",
            f"累计绝对波动: {cumulative_abs_change:.2f}",
            "最近样本:",
        ]
        for item, price in valid_prices[-10:]:
            observed_at = item.get("observed_at") or item.get("timestamp") or "-"
            bid = cls._format_optional_price(item.get("bid"))
            ask = cls._format_optional_price(item.get("ask"))
            lines.append(f"- {observed_at}: last={price:.2f}, bid={bid}, ask={ask}")
        return "\n".join(lines)

    @classmethod
    def _format_account_context(cls, account_context: dict[str, Any] | None) -> str:
        if not account_context:
            return "无"

        currency = account_context.get("cash_currency") or "-"
        available_cash = cls._format_optional_price(account_context.get("available_cash"))
        buying_power = cls._format_optional_price(account_context.get("buying_power"))
        max_buy_quantity = account_context.get("max_buy_quantity")
        max_short_quantity = account_context.get("max_short_quantity")
        lines = [
            f"可用现金: {available_cash} {currency}",
            f"购买力估算: {buying_power}",
        ]
        if max_buy_quantity is not None:
            lines.append(f"最大可买数量: {max_buy_quantity}")
        if max_short_quantity is not None:
            lines.append(f"最大可做空数量: {max_short_quantity}")

        pending = account_context.get("pending_order")
        if pending:
            order_id = pending.get("broker_order_id") or pending.get("order_id") or "-"
            side = pending.get("side") or pending.get("action") or "-"
            price = pending.get("price")
            lines.append(f"当前挂单: {order_id} {side} @ {price}")
        else:
            lines.append("当前挂单: 无")

        errors = account_context.get("errors") or []
        if errors:
            lines.append("账户上下文警告: " + "; ".join(str(item) for item in errors))
        return "\n".join(lines)

    @classmethod
    def _format_recent_analysis(cls, recent_analysis: dict[str, Any] | None) -> str:
        if not recent_analysis:
            return "无"

        lines = []
        last_analysis_at = recent_analysis.get("last_analysis_at") or "-"
        lines.append(f"时间: {last_analysis_at}")
        buy_low = cls._format_optional_price(recent_analysis.get("buy_low"))
        sell_high = cls._format_optional_price(recent_analysis.get("sell_high"))
        confidence = recent_analysis.get("confidence_score")
        lines.append(f"建议区间: {buy_low} ~ {sell_high}")
        if confidence is not None:
            lines.append(f"置信度: {confidence}")
        applied_buy_low = recent_analysis.get("applied_buy_low")
        applied_sell_high = recent_analysis.get("applied_sell_high")
        if applied_buy_low is not None and applied_sell_high is not None:
            lines.append(
                "已应用区间: "
                f"{cls._format_optional_price(applied_buy_low)} ~ {cls._format_optional_price(applied_sell_high)}"
            )
        reject_reason = recent_analysis.get("reject_reason")
        if reject_reason:
            lines.append(f"上次被拒原因: {reject_reason}")
        analysis = recent_analysis.get("analysis")
        if analysis:
            lines.append(f"分析摘要: {analysis}")
        return "\n".join(lines)

    @staticmethod
    def build_prompt(
        symbol: str,
        market: str,
        current_price: float,
        current_buy_low: float,
        current_sell_high: float,
        short_selling: bool,
        daily_candles: list[dict[str, Any]],
        minute_candles: list[dict[str, Any]],
        atr: float,
        bb_upper: float,
        bb_middle: float,
        bb_lower: float,
        current_position: str,
        recent_trades: list[dict[str, Any]],
        position_quantity: float = 0.0,
        position_avg_price: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        min_profit_amount: float = 0.0,
        recent_prices: list[dict[str, Any]] | None = None,
        recent_analysis: dict[str, Any] | None = None,
        account_context: dict[str, Any] | None = None,
    ) -> str:
        """Build LLM prompt from aggregated market data."""
        ohlcv_table = _render_daily_table(daily_candles[-_PROMPT_DAILY_CANDLES:])
        minute_table = _render_minute_table(minute_candles[-_PROMPT_MINUTE_CANDLES:])

        trades_summary = "无"
        if recent_trades:
            trades_summary = "\n".join(
                f"- {t.get('side', '')}: {t.get('quantity', 0)} @ {t.get('price', 0):.2f}"
                for t in recent_trades[:3]
            )

        recent_price_context = DataAggregator._format_recent_prices(recent_prices)
        recent_analysis_context = DataAggregator._format_recent_analysis(recent_analysis)
        account_context_text = DataAggregator._format_account_context(account_context)

        return f"""你是一个专业量化交易顾问。请基于以下市场数据、账户购买力、持仓成本和最近5分钟累次报价，为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high），并在信号特别明确时给出即时订单动作。

## 交易目标
- 期望以**少量多次**的方式尽可能**增加交易频次**，获取更高收益
- 建议区间宽度尽量收窄，推荐控制在当前价格的 **±1×ATR** 范围内
- buy_low 与 sell_high 的差值不宜过大，以便价格在较小区间内多次触发交易

## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low:.2f}
- 当前 sell_high: {current_sell_high:.2f}
- 允许做空: {short_selling}
- 单笔最低盈利金额: {min_profit_amount:.2f}（约束普通即时卖出/平仓和建议区间宽度；止损动作不受此限制）

## 市场数据（最近日 K 线）
{ohlcv_table}

## 市场数据（最近 1 分钟 K 线）
{minute_table}

## 当前技术指标
- ATR(14): {atr:.2f}
- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}
- 当前价格: {current_price:.2f}
- 当前持仓方向: {current_position}
- 当前持仓数量: {position_quantity}
- 持仓成本价: {position_avg_price:.2f}
- 浮动盈亏比例: {unrealized_pnl_pct:.2f}%
- 最近成交: {trades_summary}

## 账户与购买力
{account_context_text}

## 最近5分钟价格
{recent_price_context}

## 最近一次LLM分析
{recent_analysis_context}

## 请输出以下 JSON 格式：
{{
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
6. FLAT 状态可参考当前价格和 ATR；已有持仓时必须结合持仓成本价、持仓数量和浮动盈亏设计区间，不要仅按当前价格 ±1% 滚动追价
7. LONG 状态下，buy_low 是加仓触发价，应结合成本价和回撤幅度；sell_high 应优先考虑持仓成本价，不要在未说明止损的情况下长期低于成本价
8. 必须综合最近5分钟价格走势、当前价格、持仓成本和最近一次LLM分析结果；如果最新价格已明显偏离旧分析，请说明维持或调整区间的理由
9. 单笔最低盈利金额会约束 suggested_buy_low 与 suggested_sell_high 的区间宽度，也会作为普通 SELL_NOW、BUY_TO_COVER_NOW 的执行门槛，避免手续费成本吞噬收益；止损动作不受此门槛限制
10. 当价格已到达卖出价、需要普通平仓或撤单重挂时，必须在 order_reason 中说明预估收益已覆盖最低盈利门槛；止损信号明确时可以直接给出止损动作
11. 对美股/US 标的，价格波动较快；当信号、购买力和风险收益支持交易时，优先采用“先挂单”策略，不要因为担心价格变化而只给 NONE
12. 若已有当前挂单且你产生了新的即时动作或新价格，按“撤旧单再重挂”的策略输出 CANCEL_REPLACE，并给出 replacement_action 与 replacement_price
13. 必须主动评估止损：若 LONG 持仓下最近5分钟价格连续下破关键支撑、跌幅扩大、买盘无法支撑或出现开始崩盘迹象，应输出 STOP_LOSS_SELL_NOW 及时卖出；若 SHORT 持仓出现相反方向的逼空风险，应输出 STOP_LOSS_COVER_NOW
14. 止损动作允许以控制亏损为优先目标，但必须在 order_reason 中明确写出支撑失效、崩盘、量价恶化或逼空风险等依据
15. 默认 order_action 使用 NONE；只有当最近5分钟累次数据、购买力、持仓成本和风险收益都支持“立即行动”时，才输出 BUY_NOW/SELL_NOW/STOP_LOSS_SELL_NOW 等动作
16. 不允许输出 JSON 以外的解释文本"""


def _candle_to_dict_daily(candle: BrokerCandle) -> dict[str, Any]:
    return {
        "date": candle.timestamp.date().isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def _candle_to_dict_minute(candle: BrokerCandle) -> dict[str, Any]:
    return {
        "time": candle.timestamp.strftime("%Y-%m-%d %H:%M"),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


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


def _compute_atr(candles: list[BrokerCandle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if not true_ranges:
        return 0.0
    span = min(period, len(true_ranges))
    return float(statistics.mean(true_ranges[-span:]))


def _compute_bollinger_bands(
    closes: list[float], period: int = 20, std_dev: int = 2
) -> tuple[float, float, float]:
    if len(closes) < 2:
        return (0.0, 0.0, 0.0)

    span = min(period, len(closes))
    recent_closes = closes[-span:]
    middle = statistics.mean(recent_closes)
    std = statistics.stdev(recent_closes) if len(recent_closes) > 1 else 0.0
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return (upper, middle, lower)
