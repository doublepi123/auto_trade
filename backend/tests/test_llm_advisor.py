from __future__ import annotations

import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_llm.db"

import pytest

from app.services.data_aggregator import DataAggregator
from app.services.llm_advisor_service import LLMAdvisorService


class TestDataAggregator:
    @pytest.fixture
    def aggregator(self) -> DataAggregator:
        return DataAggregator()

    def test_compute_atr_basic(self, aggregator: DataAggregator) -> None:
        candles = [
            {"high": 110, "low": 100, "close": 105},
            {"high": 112, "low": 103, "close": 108},
            {"high": 115, "low": 106, "close": 110},
            {"high": 113, "low": 107, "close": 109},
            {"high": 116, "low": 108, "close": 112},
        ]
        atr = aggregator._compute_atr(candles)
        assert atr > 0
        assert isinstance(atr, float)

    def test_compute_atr_insufficient_data(self, aggregator: DataAggregator) -> None:
        assert aggregator._compute_atr([]) == 0.0
        assert aggregator._compute_atr([{"high": 100, "low": 90, "close": 95}]) == 0.0

    def test_compute_bollinger_bands_basic(self, aggregator: DataAggregator) -> None:
        closes = [100, 102, 101, 103, 104, 102, 105, 106, 104, 107]
        upper, middle, lower = aggregator._compute_bollinger_bands(closes)
        assert upper > middle > lower
        assert isinstance(upper, float)
        assert isinstance(middle, float)
        assert isinstance(lower, float)

    def test_compute_bollinger_bands_insufficient_data(self, aggregator: DataAggregator) -> None:
        assert aggregator._compute_bollinger_bands([]) == (0.0, 0.0, 0.0)
        assert aggregator._compute_bollinger_bands([100]) == (0.0, 0.0, 0.0)

    def test_build_prompt_structure(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
            daily_candles=[
                {"date": "2026-06-01", "open": 195, "high": 205, "low": 193, "close": 200, "volume": 1000000},
            ],
            minute_candles=[
                {"time": "10:00", "open": 199, "high": 201, "low": 198, "close": 200, "volume": 50000},
            ],
            atr=5.0,
            bb_upper=210.0,
            bb_middle=200.0,
            bb_lower=190.0,
            current_position="FLAT",
            recent_trades=[],
        )
        assert "AAPL.US" in prompt
        assert "200.0" in prompt
        assert "JSON" in prompt
        assert "sell_high 必须严格大于 buy_low" in prompt

    def test_build_prompt_with_trades(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="TSLA.US",
            market="US",
            current_price=250.0,
            current_buy_low=230.0,
            current_sell_high=270.0,
            short_selling=True,
            daily_candles=[],
            minute_candles=[],
            atr=8.0,
            bb_upper=260.0,
            bb_middle=250.0,
            bb_lower=240.0,
            current_position="LONG",
            recent_trades=[
                {"side": "BUY", "quantity": 10, "price": 240.0},
                {"side": "SELL", "quantity": 10, "price": 260.0},
            ],
        )
        assert "TSLA.US" in prompt
        assert "BUY" in prompt
        assert "SELL" in prompt


class TestLLMAdvisorService:
    @pytest.fixture
    def advisor(self) -> LLMAdvisorService:
        return LLMAdvisorService()

    def test_parse_response_plain_json(self, advisor: LLMAdvisorService) -> None:
        raw = '{"suggested_buy_low": 180.0, "suggested_sell_high": 220.0, "confidence_score": 0.85, "analysis": "test"}'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 180.0
        assert result["confidence_score"] == 0.85

    def test_parse_response_markdown_json(self, advisor: LLMAdvisorService) -> None:
        raw = '```json\n{"suggested_buy_low": 190.0, "suggested_sell_high": 230.0, "confidence_score": 0.9, "analysis": "md"}\n```'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 190.0
        assert result["confidence_score"] == 0.9

    def test_parse_response_markdown_no_json(self, advisor: LLMAdvisorService) -> None:
        raw = '```\n{"suggested_buy_low": 200.0, "suggested_sell_high": 240.0, "confidence_score": 0.75, "analysis": " plain"}\n```'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 200.0

    def test_is_throttled_initially_false(self, advisor: LLMAdvisorService) -> None:
        assert advisor._is_throttled() is False

    def test_analyze_no_api_key(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.config
        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "")
        result = advisor.analyze(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
            current_position="FLAT",
            recent_trades=[],
        )
        assert result["success"] is False
        assert "DEEPSEEK_API_KEY" in result["error"] or "failed" in result["error"]
