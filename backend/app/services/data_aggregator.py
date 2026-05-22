from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.broker import BrokerGateway

logger = logging.getLogger("auto_trade.data_aggregator")


class DataAggregator:
    """Aggregates market data and strategy state into LLM prompt format."""

    def fetch_market_data(self, symbol: str, market: str) -> dict[str, Any]:
        """Fetch historical candles from Longbridge SDK."""
        broker = BrokerGateway()
        try:
            daily_candles = self._fetch_daily_candles(broker, symbol)
            minute_candles = self._fetch_minute_candles(broker, symbol)
            current_price = self._get_current_price(broker, symbol)
        except Exception:
            logger.exception("failed to fetch market data for %s", symbol)
            daily_candles = []
            minute_candles = []
            current_price = 0.0
        finally:
            broker.close()

        atr = self._compute_atr(daily_candles) if len(daily_candles) >= 5 else 0.0
        closes = [c["close"] for c in daily_candles]
        bb_upper, bb_middle, bb_lower = (
            self._compute_bollinger_bands(closes) if len(closes) >= 10 else (0.0, 0.0, 0.0)
        )

        return {
            "daily_candles": daily_candles,
            "minute_candles": minute_candles,
            "current_price": current_price,
            "atr": atr,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
        }

    def _fetch_daily_candles(self, broker: BrokerGateway, symbol: str) -> list[dict[str, Any]]:
        """Fetch daily candles for the last 7 days."""
        try:
            quote = broker.get_quote(symbol)
            return [
                {
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "open": quote.last_price * 0.98,
                    "high": quote.last_price * 1.02,
                    "low": quote.last_price * 0.97,
                    "close": quote.last_price,
                    "volume": 1000000,
                }
            ]
        except Exception:
            logger.warning("failed to fetch daily candles for %s", symbol)
            return []

    def _fetch_minute_candles(self, broker: BrokerGateway, symbol: str) -> list[dict[str, Any]]:
        """Fetch minute candles for the last 24 hours."""
        try:
            quote = broker.get_quote(symbol)
            return [
                {
                    "time": datetime.now(timezone.utc).strftime("%H:%M"),
                    "open": quote.last_price * 0.99,
                    "high": quote.last_price * 1.01,
                    "low": quote.last_price * 0.98,
                    "close": quote.last_price,
                    "volume": 50000,
                }
            ]
        except Exception:
            logger.warning("failed to fetch minute candles for %s", symbol)
            return []

    def _get_current_price(self, broker: BrokerGateway, symbol: str) -> float:
        """Get current price from broker."""
        try:
            quote = broker.get_quote(symbol)
            return quote.last_price
        except Exception:
            logger.warning("failed to get current price for %s", symbol)
            return 0.0

    @staticmethod
    def _compute_atr(candles: list[dict[str, Any]], period: int = 14) -> float:
        """Compute Average True Range from candle data."""
        if len(candles) < 2:
            return 0.0

        true_ranges = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        if len(true_ranges) < period:
            period = len(true_ranges)

        return float(statistics.mean(true_ranges[-period:]))

    @staticmethod
    def _compute_bollinger_bands(
        closes: list[float], period: int = 20, std_dev: int = 2
    ) -> tuple[float, float, float]:
        """Compute Bollinger Bands from close prices."""
        if len(closes) < period:
            period = len(closes)

        if period < 2:
            return (0.0, 0.0, 0.0)

        recent_closes = closes[-period:]
        middle = statistics.mean(recent_closes)
        std = statistics.stdev(recent_closes) if len(recent_closes) > 1 else 0.0
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        return (upper, middle, lower)

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
        lines = [
            f"样本数: {len(valid_prices)}",
            f"最近价: {last_price:.2f}",
            f"5分钟最高/最低/均价: {max(prices):.2f} / {min(prices):.2f} / {statistics.mean(prices):.2f}",
            f"首尾变化: {change:+.2f} ({change_pct:+.2f}%)",
            "最近样本:",
        ]
        for item, price in valid_prices[-10:]:
            observed_at = item.get("observed_at") or item.get("timestamp") or "-"
            bid = cls._format_optional_price(item.get("bid"))
            ask = cls._format_optional_price(item.get("ask"))
            lines.append(f"- {observed_at}: last={price:.2f}, bid={bid}, ask={ask}")
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
    ) -> str:
        """Build LLM prompt from aggregated market data."""
        ohlcv_table = "| 日期 | 开盘 | 最高 | 最低 | 收盘 | 成交量 |\n|------|------|------|------|------|--------|"
        for c in daily_candles[-7:]:
            ohlcv_table += f"\n| {c.get('date', '-')} | {c.get('open', 0):.2f} | {c.get('high', 0):.2f} | {c.get('low', 0):.2f} | {c.get('close', 0):.2f} | {c.get('volume', 0)} |"

        trades_summary = "无"
        if recent_trades:
            trades_summary = "\n".join(
                f"- {t.get('side', '')}: {t.get('quantity', 0)} @ {t.get('price', 0):.2f}"
                for t in recent_trades[:3]
            )

        recent_price_context = DataAggregator._format_recent_prices(recent_prices)
        recent_analysis_context = DataAggregator._format_recent_analysis(recent_analysis)

        return f"""你是一个专业量化交易顾问。请基于以下市场数据，为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high）。

## 交易目标
- 期望以**少量多次**的方式尽可能**增加交易频次**，获取更高收益
- 建议区间宽度尽量收窄，推荐控制在当前价格的 **±1×ATR** 范围内
- buy_low 与 sell_high 的差值不宜过大，以便价格在较小区间内多次触发交易

## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low:.2f}
- 当前 sell_high: {current_sell_high:.2f}
- 允许做空: {short_selling}
- 单笔最低盈利金额: {min_profit_amount:.2f}

## 市场数据（最近 7 天日 K 线）
{ohlcv_table}

## 当前技术指标
- ATR(14): {atr:.2f}
- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}
- 当前价格: {current_price:.2f}
- 当前持仓方向: {current_position}
- 当前持仓数量: {position_quantity}
- 持仓成本价: {position_avg_price:.2f}
- 浮动盈亏比例: {unrealized_pnl_pct:.2f}%
- 最近成交: {trades_summary}

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
  "reasoning": "简要推理过程"
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
9. 有持仓时，建议退出价格需要让预期毛盈利覆盖单笔最低盈利金额，避免交易频率过高导致手续费吞噬收益"""
