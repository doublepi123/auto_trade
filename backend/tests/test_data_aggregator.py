from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_data_aggregator.db",
)

from datetime import datetime, timezone

import pytest

from app.core.broker import BrokerCandle, Quote
from app.services.data_aggregator import DataAggregator


class _FakeBroker:
    def __init__(
        self,
        *,
        daily: list[BrokerCandle] | None = None,
        minute: list[BrokerCandle] | None = None,
        quote_price: float = 0.0,
        raise_on: set[str] | None = None,
        quote_error: bool = False,
    ) -> None:
        self.daily = daily or []
        self.minute = minute or []
        self.quote_price = quote_price
        self.raise_on = raise_on or set()
        self.quote_error = quote_error
        self.calls: list[tuple[str, str, int]] = []
        self.quote_calls = 0
        self.closed = False

    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        self.calls.append((symbol, period, count))
        if period in self.raise_on:
            raise RuntimeError("simulated broker failure")
        return self.daily if period == "Day" else self.minute

    def get_quote(self, symbol: str) -> Quote:
        self.quote_calls += 1
        if self.quote_error:
            raise RuntimeError("simulated quote failure")
        return Quote(symbol=symbol, last_price=self.quote_price, bid=0.0, ask=0.0, timestamp="")

    def close(self) -> None:
        self.closed = True


def _build_candles(base: float, count: int) -> list[BrokerCandle]:
    bars: list[BrokerCandle] = []
    price = base
    for i in range(count):
        high = price + 2
        low = price - 2
        close = price + (1 if i % 2 == 0 else -1)
        bars.append(BrokerCandle(
            timestamp=datetime(2026, 4, 1 + i, tzinfo=timezone.utc),
            open=price,
            high=high,
            low=low,
            close=close,
            volume=1000.0 + i,
        ))
        price = close
    return bars


class TestFetchMarketData:
    def test_returns_indicators_from_broker_candles(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 20),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)

        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert result["current_price"] == pytest.approx(101.5)
        assert len(result["daily_candles"]) == 20
        assert len(result["minute_candles"]) == 10
        assert result["atr"] > 0
        assert result["bb_upper"] > result["bb_middle"] > result["bb_lower"]
        assert broker.calls[0] == ("AAPL.US", "Day", 30)
        assert broker.calls[1] == ("AAPL.US", "Min_1", 120)
        assert broker.closed is False, "injected broker must not be closed by aggregator"

    def test_uses_last_minute_close_when_quote_fails(self) -> None:
        minute = _build_candles(50.0, 5)
        broker = _FakeBroker(daily=[], minute=minute, quote_error=True)
        aggregator = DataAggregator(broker=broker)

        result = aggregator.fetch_market_data("0700.HK", "HK")

        assert result["current_price"] == pytest.approx(minute[-1].close)
        assert result["atr"] == 0.0
        assert result["bb_upper"] == 0.0
        assert result["bb_middle"] == 0.0
        assert result["bb_lower"] == 0.0

    def test_returns_zero_indicators_when_broker_throws(self) -> None:
        broker = _FakeBroker(raise_on={"Day", "Min_1"}, quote_error=True)
        aggregator = DataAggregator(broker=broker)

        result = aggregator.fetch_market_data("TSLA.US", "US")

        assert result["daily_candles"] == []
        assert result["minute_candles"] == []
        assert result["current_price"] == 0.0
        assert result["atr"] == 0.0
        assert result["bb_upper"] == 0.0

    def test_prompt_renders_real_daily_table(self) -> None:
        daily = _build_candles(200.0, 7)
        minute = _build_candles(200.0, 5)
        broker = _FakeBroker(daily=daily, minute=minute, quote_price=201.0)
        aggregator = DataAggregator(broker=broker)
        market_data = aggregator.fetch_market_data("AAPL.US", "US")

        prompt = aggregator.build_prompt(
            symbol="AAPL.US",
            market="US",
            current_price=201.0,
            current_buy_low=195.0,
            current_sell_high=205.0,
            short_selling=False,
            daily_candles=market_data["daily_candles"],
            minute_candles=market_data["minute_candles"],
            atr=market_data["atr"],
            bb_upper=market_data["bb_upper"],
            bb_middle=market_data["bb_middle"],
            bb_lower=market_data["bb_lower"],
            current_position="FLAT",
            recent_trades=[],
            rsi=market_data["rsi"],
            macd=market_data["macd"],
            volume_analysis=market_data["volume_analysis"],
        )

        assert "暂无可用历史日 K 数据" not in prompt
        assert "暂无可用 1 分钟 K 数据" not in prompt
        assert daily[-1].timestamp.date().isoformat() in prompt
        # 7 daily rows + 1 header + 1 separator row = 9 lines starting with `|`
        daily_section = prompt.split("市场数据（最近日 K 线）")[1].split("市场数据（最近 1 分钟 K 线）")[0]
        assert daily_section.count("\n|") >= 7

    def test_prompt_shows_placeholder_when_no_candles(self) -> None:
        aggregator = DataAggregator(broker=_FakeBroker(raise_on={"Day", "Min_1"}, quote_error=True))
        data = aggregator.fetch_market_data("AAPL.US", "US")
        prompt = aggregator.build_prompt(
            symbol="AAPL.US",
            market="US",
            current_price=0.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
            daily_candles=data["daily_candles"],
            minute_candles=data["minute_candles"],
            atr=data["atr"],
            bb_upper=data["bb_upper"],
            bb_middle=data["bb_middle"],
            bb_lower=data["bb_lower"],
            current_position="FLAT",
            recent_trades=[],
            rsi=data["rsi"],
            macd=data["macd"],
            volume_analysis=data["volume_analysis"],
        )
        assert "暂无可用历史日 K 数据" in prompt
        assert "暂无可用 1 分钟 K 数据" in prompt


class TestNewIndicators:
    def test_fetch_market_data_includes_rsi_macd_volume(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 30),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "rsi" in result
        assert "macd" in result
        assert "volume_analysis" in result
        assert result["rsi"] > 0
        assert "macd" in result["macd"]
        assert "signal" in result["macd"]
        assert result["volume_analysis"]["avg_volume"] > 0

    def test_fetch_market_data_includes_sentiment(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 30),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "sentiment" in result
        assert "sentiment" in result["sentiment"]
        assert "score" in result["sentiment"]
        assert "description" in result["sentiment"]
        assert result["sentiment"]["sentiment"] in ("bullish", "bearish", "neutral")

    def test_fetch_market_data_sentiment_neutral_when_few_candles(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 2),
            minute=_build_candles(100.0, 5),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "sentiment" in result
        # Only 1 price change from 2 candles, so sentiment should still have valid structure
        assert result["sentiment"]["sentiment"] in ("bullish", "bearish", "neutral")

    def test_fetch_market_data_includes_new_indicator_fields(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 30),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "obv" in result
        assert "obv_trend" in result["obv"]
        assert result["obv"]["obv_trend"] in ("rising", "falling", "flat")

        assert "adx" in result
        assert "adx_value" in result["adx"]
        assert "trend_strength" in result["adx"]
        assert result["adx"]["trend_strength"] in ("none", "weak", "moderate", "strong", "extreme")

        assert "stochastic" in result
        assert "stoch_k" in result["stochastic"]
        assert "stoch_d" in result["stochastic"]
        assert result["stochastic"]["signal"] in ("overbought", "oversold", "neutral")

        assert "cci" in result
        assert "cci_value" in result["cci"]
        assert result["cci"]["signal"] in ("overbought", "oversold", "neutral")

        assert "williams_r" in result
        assert "williams_r" in result["williams_r"]
        assert result["williams_r"]["signal"] in ("overbought", "oversold", "neutral")

        assert "vwap" in result
        assert "vwap_value" in result["vwap"]
        assert "position" in result["vwap"]
        assert result["vwap"]["position"] in ("above", "below", "at")

        assert "aggregate_signals" in result
        assert "overall_signal" in result["aggregate_signals"]
        assert result["aggregate_signals"]["overall_signal"] in ("bullish", "bearish", "neutral")
        assert "confidence" in result["aggregate_signals"]
        assert "summary" in result["aggregate_signals"]

    def test_fetch_market_data_new_indicators_empty_when_few_candles(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 3),
            minute=_build_candles(100.0, 5),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert result["adx"] == {}
        assert result["stochastic"] == {}
        assert result["cci"] == {}
        assert result["williams_r"] == {}
        assert "obv" in result
        assert "vwap" in result
        assert "aggregate_signals" in result


class TestMarketState:
    def test_fetch_market_data_includes_market_state(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 30),
            minute=_build_candles(100.0, 10),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert "market_state" in result
        assert "state" in result["market_state"]
        assert "confidence" in result["market_state"]
        assert "description" in result["market_state"]
        assert "suggested_indicators" in result["market_state"]
        assert result["market_state"]["state"] in ("trending", "ranging", "volatile", "neutral")
        assert 0.0 <= result["market_state"]["confidence"] <= 1.0

    def test_fetch_market_data_market_state_neutral_when_insufficient_data(self) -> None:
        broker = _FakeBroker(
            daily=_build_candles(100.0, 2),
            minute=_build_candles(100.0, 5),
            quote_price=101.5,
        )
        aggregator = DataAggregator(broker=broker)
        result = aggregator.fetch_market_data("AAPL.US", "US")

        assert result["market_state"]["state"] == "neutral"
        assert result["market_state"]["suggested_indicators"] == ["rsi", "macd", "atr", "vwap"]
