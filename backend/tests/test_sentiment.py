from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_sentiment_{os.getpid()}.db"
)

import pytest
from app.domain.sentiment.market_sentiment import MarketSentimentAnalyzer
from app.domain.prompt.sentiment_module import SentimentModule


class TestMarketSentiment:
    def test_analyze_from_price_data_uptrend(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        # Simulate uptrend: prices rising over 5 days
        price_changes = [1.0, 2.0, 1.5, 3.0, 2.5]
        result = analyzer.analyze_from_price_changes(price_changes)
        assert result["sentiment"] == "bullish"
        assert result["score"] > 0.2

    def test_analyze_from_price_data_downtrend(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        price_changes = [-2.0, -1.5, -3.0, -1.0, -2.5]
        result = analyzer.analyze_from_price_changes(price_changes)
        assert result["sentiment"] == "bearish"
        assert result["score"] < -0.2

    def test_analyze_empty_returns_neutral(self) -> None:
        analyzer = MarketSentimentAnalyzer()
        result = analyzer.analyze_from_price_changes([])
        assert result["sentiment"] == "neutral"
        assert result["score"] == 0.0


class TestSentimentModule:
    def test_renders_bullish_sentiment(self) -> None:
        module = SentimentModule()
        context = {
            "sentiment": {"sentiment": "bullish", "score": 0.6, "description": "市场情绪偏多"},
        }
        result = module.render(context)
        assert "偏多" in result or "bullish" in result

    def test_renders_no_data_when_missing(self) -> None:
        module = SentimentModule()
        context = {}
        result = module.render(context)
        assert "无" in result
