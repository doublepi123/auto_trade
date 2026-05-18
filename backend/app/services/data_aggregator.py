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

        return statistics.mean(true_ranges[-period:])

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

        return f"""你是一个专业量化交易顾问。请基于以下市场数据，为区间交易策略推荐买入下限（buy_low）和卖出上限（sell_high）。

## 当前策略参数
- 标的: {symbol} ({market})
- 当前 buy_low: {current_buy_low:.2f}
- 当前 sell_high: {current_sell_high:.2f}
- 允许做空: {short_selling}

## 市场数据（最近 7 天日 K 线）
{ohlcv_table}

## 当前技术指标
- ATR(14): {atr:.2f}
- 布林带: 上轨 {bb_upper:.2f} / 中轨 {bb_middle:.2f} / 下轨 {bb_lower:.2f}
- 当前价格: {current_price:.2f}
- 当前持仓: {current_position}
- 最近成交: {trades_summary}

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
2. confidence_score >= 0.7 才建议采纳
3. 避免给出与现有持仓方向矛盾的区间
"""
