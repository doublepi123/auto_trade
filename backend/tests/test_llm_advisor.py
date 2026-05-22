from __future__ import annotations

import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_llm.db"

import pytest

from app.services.data_aggregator import DataAggregator
from app.services.llm_advisor_service import LLMAdvisorService
from app.schemas import LLMPreviewAnalyzeRequest


def test_preview_request_normalizes_symbol() -> None:
    payload = LLMPreviewAnalyzeRequest(symbol=" aapl.us ", market="US")

    assert payload.symbol == "AAPL.US"


def test_preview_request_requires_supported_market() -> None:
    with pytest.raises(ValueError):
        LLMPreviewAnalyzeRequest(symbol="AAPL.US", market="CN")


def test_preview_request_rejects_unexpected_symbol_characters() -> None:
    with pytest.raises(ValueError):
        LLMPreviewAnalyzeRequest(symbol="AAPL.US\nignore", market="US")


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

    def test_build_prompt_includes_position_cost_context(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.68,
            current_sell_high=224.12,
            short_selling=False,
            daily_candles=[],
            minute_candles=[],
            atr=0.0,
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            current_position="LONG",
            recent_trades=[],
            position_quantity=18.0,
            position_avg_price=255.942,
            unrealized_pnl_pct=-13.34,
            min_profit_amount=10.0,
        )

        assert "当前持仓方向: LONG" in prompt
        assert "当前持仓数量: 18.0" in prompt
        assert "持仓成本价: 255.94" in prompt
        assert "单笔最低盈利金额: 10.00" in prompt
        assert "浮动盈亏比例: -13.34%" in prompt
        assert "不要仅按当前价格" in prompt

    def test_build_prompt_includes_recent_price_and_analysis_context(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.68,
            current_sell_high=224.12,
            short_selling=False,
            daily_candles=[],
            minute_candles=[],
            atr=0.0,
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            current_position="FLAT",
            recent_trades=[],
            recent_prices=[
                {"observed_at": "2026-05-22T10:00:00Z", "last_price": 220.1, "bid": 220.0, "ask": 220.2},
                {"observed_at": "2026-05-22T10:02:00Z", "last_price": 221.4, "bid": 221.3, "ask": 221.5},
                {"observed_at": "2026-05-22T10:04:00Z", "last_price": 221.8, "bid": 221.7, "ask": 221.9},
            ],
            recent_analysis={
                "last_analysis_at": "2026-05-22T09:59:00Z",
                "buy_low": 219.0,
                "sell_high": 223.0,
                "confidence_score": 0.76,
                "analysis": "旧分析认为窄幅震荡",
                "applied_buy_low": 219.0,
                "applied_sell_high": 223.0,
                "reject_reason": None,
            },
        )

        assert "最近5分钟价格" in prompt
        assert "样本数: 3" in prompt
        assert "首尾变化" in prompt
        assert "最近一次LLM分析" in prompt
        assert "旧分析认为窄幅震荡" in prompt
        assert "必须综合最近5分钟价格走势" in prompt


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

    def test_is_throttled_respects_configurable_seconds(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.services.llm_advisor_service as service_module
        monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 100.0)
        monkeypatch.setattr(service_module.time, "monotonic", lambda: 120.0)

        assert advisor._is_throttled(30.0) is True
        assert advisor._is_throttled(10.0) is False

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

    def test_preview_does_not_record_or_update_analysis_throttle(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.services.llm_advisor_service as service_module

        monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 123.0)
        monkeypatch.setattr(service_module, "_LAST_PREVIEW_TIMESTAMP", 100.0)
        monkeypatch.setattr(service_module.time, "monotonic", lambda: 1000.0)
        monkeypatch.setattr(
            advisor._data_aggregator,
            "fetch_market_data",
            lambda symbol, market: {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": 205.0,
                "atr": 5.0,
                "bb_upper": 215.0,
                "bb_middle": 205.0,
                "bb_lower": 195.0,
            },
        )
        monkeypatch.setattr(
            advisor,
            "_call_deepseek",
            lambda prompt: '{"suggested_buy_low": 200.0, "suggested_sell_high": 210.0, "confidence_score": 0.82, "analysis": "preview"}',
        )
        monkeypatch.setattr(
            advisor,
            "_record_analysis",
            lambda *args, **kwargs: pytest.fail("preview must not record analysis"),
        )

        result = advisor.preview(
            symbol="AAPL.US",
            market="US",
            current_price=0.0,
            current_buy_low=0.0,
            current_sell_high=0.0,
            short_selling=False,
        )

        assert result["success"] is True
        assert result["applied"] is False
        assert result["suggested_buy_low"] == 200.0
        assert result["suggested_sell_high"] == 210.0
        assert service_module._LAST_ANALYSIS_TIMESTAMP == 123.0

    def test_preview_is_rate_limited(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.services.llm_advisor_service as service_module

        monkeypatch.setattr(service_module, "_LAST_PREVIEW_TIMESTAMP", 100.0)
        monkeypatch.setattr(service_module.time, "monotonic", lambda: 120.0)

        result = advisor.preview(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
        )

        assert result["success"] is False
        assert "Preview throttled" in result["error"]


def test_preview_endpoint_uses_payload_without_saving(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    captured = {}

    def fake_preview(self, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "applied": False,
            "reason": "Preview completed. Confirm to save and apply.",
            "suggested_buy_low": 200.0,
            "suggested_sell_high": 210.0,
            "confidence_score": 0.82,
            "analysis": "preview",
            "next_analysis_at": None,
            "applied_at": None,
        }

    monkeypatch.setattr(LLMAdvisorService, "preview", fake_preview)
    client = TestClient(app)

    response = client.post(
        "/api/strategy/llm-interval/preview",
        json={
            "symbol": " aapl.us ",
            "market": "US",
            "current_buy_low": 0,
            "current_sell_high": 0,
            "min_profit_amount": 7.5,
        },
    )

    assert response.status_code == 200
    assert response.json()["analysis"] == "preview"
    assert captured["symbol"] == "AAPL.US"
    assert captured["market"] == "US"
    assert captured["min_profit_amount"] == 7.5


def test_preview_endpoint_requires_api_key_in_production(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "api_key", "secret")
    client = TestClient(app)

    response = client.post(
        "/api/strategy/llm-interval/preview",
        json={"symbol": "AAPL.US", "market": "US", "current_buy_low": 0, "current_sell_high": 0},
    )

    assert response.status_code == 401


def test_preview_endpoint_allows_missing_api_key_header_in_dev(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    def fake_preview(self, **_kwargs):
        return {
            "success": True,
            "applied": False,
            "reason": "Preview completed. Confirm to save and apply.",
            "suggested_buy_low": 200.0,
            "suggested_sell_high": 210.0,
            "confidence_score": 0.82,
            "analysis": "preview",
            "next_analysis_at": None,
            "applied_at": None,
        }

    monkeypatch.setattr(settings, "env", "dev")
    monkeypatch.setattr(settings, "api_key", "configured-local-key")
    monkeypatch.setattr(LLMAdvisorService, "preview", fake_preview)
    client = TestClient(app)

    response = client.post(
        "/api/strategy/llm-interval/preview",
        json={"symbol": "AAPL.US", "market": "US", "current_buy_low": 0, "current_sell_high": 0},
    )

    assert response.status_code == 200
    assert response.json()["analysis"] == "preview"
