from __future__ import annotations

import logging
import statistics
from typing import Any

from app.core.broker import BrokerCandle, BrokerGateway
from app.domain.analysis.technical_indicators import TechnicalIndicators
from app.domain.prompt.context_module import ContextModule
from app.domain.prompt.output_module import OutputModule
from app.domain.prompt.prompt_builder import PromptBuilder
from app.domain.prompt.sentiment_module import SentimentModule
from app.domain.prompt.strategy_module import StrategyModule
from app.domain.prompt.system_module import SystemModule
from app.domain.sentiment.market_sentiment import MarketSentimentAnalyzer

logger = logging.getLogger("auto_trade.data_aggregator")


_DAILY_CANDLE_COUNT = 30
_MINUTE_CANDLE_COUNT = 120


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
        volumes = [c.volume for c in daily_candles]
        bb_upper, bb_middle, bb_lower = (
            _compute_bollinger_bands(closes) if len(closes) >= 10 else (0.0, 0.0, 0.0)
        )
        rsi = TechnicalIndicators.calculate_rsi(closes) if len(closes) >= 15 else 0.0
        macd = TechnicalIndicators.calculate_macd(closes)
        volume_analysis = TechnicalIndicators.analyze_volume(volumes)

        price_changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        sentiment_analyzer = MarketSentimentAnalyzer()
        sentiment = sentiment_analyzer.analyze_from_price_changes(price_changes[-10:])

        return {
            "daily_candles": daily_payload,
            "minute_candles": minute_payload,
            "current_price": current_price,
            "atr": atr,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "rsi": rsi,
            "macd": macd,
            "volume_analysis": volume_analysis,
            "sentiment": sentiment,
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
        rsi: float = 0.0,
        macd: dict[str, float] | None = None,
        volume_analysis: dict[str, Any] | None = None,
        sentiment: dict[str, Any] | None = None,
    ) -> str:
        """Build LLM prompt using modular PromptBuilder."""
        context: dict[str, Any] = {
            "symbol": symbol,
            "market": market,
            "current_price": current_price,
            "current_buy_low": current_buy_low,
            "current_sell_high": current_sell_high,
            "short_selling": short_selling,
            "daily_candles": daily_candles,
            "minute_candles": minute_candles,
            "atr": atr,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "current_position": current_position,
            "recent_trades": recent_trades,
            "position_quantity": position_quantity,
            "position_avg_price": position_avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "min_profit_amount": min_profit_amount,
            "rsi": rsi,
            "macd": macd or {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "volume_analysis": volume_analysis or {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
            "sentiment": sentiment or {"sentiment": "neutral", "score": 0.0, "description": "无"},
            "account_context_text": DataAggregator._format_account_context(account_context),
            "recent_price_context": DataAggregator._format_recent_prices(recent_prices),
            "recent_analysis_context": DataAggregator._format_recent_analysis(recent_analysis),
        }

        builder = PromptBuilder()
        builder.add_module(SystemModule())
        builder.add_module(ContextModule())
        builder.add_module(SentimentModule())
        builder.add_module(StrategyModule())
        builder.add_module(OutputModule())
        return builder.build(context)


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
