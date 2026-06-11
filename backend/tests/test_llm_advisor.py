from __future__ import annotations
from typing import Any, cast

from datetime import datetime, timezone

import pytest

from app.core.broker import BrokerCandle
from app.services.data_aggregator import (
    DataAggregator,
    _compute_atr,
    _compute_bollinger_bands,
)
from app.services.llm_advisor_service import LLMAdvisorService
from app.schemas import LLMPreviewAnalyzeRequest


def _candle(high: float, low: float, close: float, *, day: int = 1) -> BrokerCandle:
    return BrokerCandle(
        timestamp=datetime(2026, 5, day, tzinfo=timezone.utc),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def test_deepseek_chat_payload_defaults_to_v4_flash_thinking_max() -> None:
    payload = LLMAdvisorService._deepseek_chat_payload("analyze NVDA")

    assert payload["model"] == "deepseek-v4-pro"
    assert payload["reasoning_effort"] == "max"
    payload = cast(dict[str, Any], payload)
    assert payload["messages"][1]["content"] == "analyze NVDA"


def test_deepseek_chat_payload_uses_256k_completion_budget_for_thinking() -> None:
    payload = LLMAdvisorService._deepseek_chat_payload("analyze NVDA")

    assert payload["max_tokens"] == 262144


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
        del aggregator
        candles = [
            _candle(110, 100, 105, day=1),
            _candle(112, 103, 108, day=2),
            _candle(115, 106, 110, day=3),
            _candle(113, 107, 109, day=4),
            _candle(116, 108, 112, day=5),
        ]
        atr = _compute_atr(candles)
        assert atr > 0
        assert isinstance(atr, float)

    def test_compute_atr_insufficient_data(self, aggregator: DataAggregator) -> None:
        del aggregator
        assert _compute_atr([]) == 0.0
        assert _compute_atr([_candle(100, 90, 95)]) == 0.0

    def test_compute_bollinger_bands_basic(self, aggregator: DataAggregator) -> None:
        del aggregator
        closes = [100.0, 102.0, 101.0, 103.0, 104.0, 102.0, 105.0, 106.0, 104.0, 107.0]
        upper, middle, lower = _compute_bollinger_bands(closes)
        assert upper > middle > lower
        assert isinstance(upper, float)
        assert isinstance(middle, float)
        assert isinstance(lower, float)

    def test_compute_bollinger_bands_insufficient_data(self, aggregator: DataAggregator) -> None:
        del aggregator
        assert _compute_bollinger_bands([]) == (0.0, 0.0, 0.0)
        assert _compute_bollinger_bands([100]) == (0.0, 0.0, 0.0)

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
        assert "约束普通即时卖出/平仓和建议区间宽度" in prompt
        assert "止损动作不受此限制" in prompt
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

    def test_build_prompt_includes_account_context_and_order_action_schema(self, aggregator: DataAggregator) -> None:
        prompt = aggregator.build_prompt(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.68,
            current_sell_high=224.12,
            short_selling=True,
            daily_candles=[],
            minute_candles=[],
            atr=0.0,
            bb_upper=0.0,
            bb_middle=0.0,
            bb_lower=0.0,
            current_position="FLAT",
            recent_trades=[],
            min_profit_amount=12.5,
            account_context={
                "cash_currency": "USD",
                "available_cash": 12500.75,
                "buying_power": 6180.25,
                "max_buy_quantity": 27,
                "max_short_quantity": 9,
                "pending_order": {"broker_order_id": "order-1", "side": "BUY", "price": 221.5},
            },
            recent_prices=[
                {"observed_at": "2026-05-22T10:00:00Z", "last_price": 220.1, "bid": 220.0, "ask": 220.2},
                {"observed_at": "2026-05-22T10:01:00Z", "last_price": 221.2, "bid": 221.1, "ask": 221.3},
                {"observed_at": "2026-05-22T10:02:00Z", "last_price": 220.8, "bid": 220.7, "ask": 220.9},
            ],
        )

        assert "账户与购买力" in prompt
        assert "可用现金: 12500.75 USD" in prompt
        assert "购买力估算: 6180.25" in prompt
        assert "最大可买数量: 27" in prompt
        assert "最大可做空数量: 9" in prompt
        assert "当前挂单: order-1 BUY @ 221.5" in prompt
        assert "累计绝对波动" in prompt
        assert '"order_action"' in prompt
        assert "BUY_NOW" in prompt
        assert "STOP_LOSS_SELL_NOW" in prompt
        assert "STOP_LOSS_COVER_NOW" in prompt
        assert "CANCEL_REPLACE" in prompt
        assert "先挂单" in prompt
        assert "撤旧单再重挂" in prompt
        assert "止损" in prompt
        assert "支撑失效" in prompt
        assert "崩盘" in prompt


class TestLLMAdvisorService:
    @pytest.fixture
    def advisor(self) -> LLMAdvisorService:
        return LLMAdvisorService()

    def test_parse_response_plain_json(self, advisor: LLMAdvisorService) -> None:
        raw = '{"suggested_buy_low": 180.0, "suggested_sell_high": 220.0, "confidence_score": 0.85, "analysis": "test"}'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 180.0
        assert result["confidence_score"] == 0.85
        assert result["order_action"] == "NONE"

    def test_parse_response_markdown_json(self, advisor: LLMAdvisorService) -> None:
        raw = '```json\n{"suggested_buy_low": 190.0, "suggested_sell_high": 230.0, "confidence_score": 0.9, "analysis": "md"}\n```'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 190.0
        assert result["confidence_score"] == 0.9

    def test_parse_response_markdown_no_json(self, advisor: LLMAdvisorService) -> None:
        raw = '```\n{"suggested_buy_low": 200.0, "suggested_sell_high": 240.0, "confidence_score": 0.75, "analysis": " plain"}\n```'
        result = advisor._parse_response(raw)
        assert result["suggested_buy_low"] == 200.0

    def test_parse_response_preserves_immediate_order_action(self, advisor: LLMAdvisorService) -> None:
        raw = """
        {
          "suggested_buy_low": 218.0,
          "suggested_sell_high": 224.0,
          "confidence_score": 0.86,
          "analysis": "突破后回踩",
          "order_action": "BUY_NOW",
          "order_price": 221.75,
          "order_reason": "价格重新站上均线"
        }
        """
        result = advisor._parse_response(raw)

        assert result["order_action"] == "BUY_NOW"
        assert result["order_price"] == 221.75
        assert result["order_reason"] == "价格重新站上均线"

    def test_parse_response_preserves_stop_loss_action(self, advisor: LLMAdvisorService) -> None:
        raw = """
        {
          "suggested_buy_low": 214.0,
          "suggested_sell_high": 224.0,
          "confidence_score": 0.9,
          "analysis": "支撑失效",
          "order_action": "STOP_LOSS_SELL_NOW",
          "order_price": 215.0,
          "order_reason": "跌破支撑且量价恶化"
        }
        """
        result = advisor._parse_response(raw)

        assert result["order_action"] == "STOP_LOSS_SELL_NOW"
        assert result["order_price"] == 215.0

    def test_call_deepseek_reports_empty_content_after_token_exhaustion(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.config
        import app.services.llm_advisor_service as service_module

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning_content": "long hidden reasoning",
                            },
                        }
                    ],
                    "usage": {
                        "completion_tokens_details": {"reasoning_tokens": 2048},
                    },
                }

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(service_module.httpx, "post", lambda *args, **kwargs: FakeResponse())

        with pytest.raises(RuntimeError, match="empty content.*finish_reason=length"):
            advisor._call_deepseek("analyze NVDA")

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

    def test_analyze_records_llm_interaction_history(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        from datetime import datetime, timezone

        from app.database import SessionLocal
        from app.models import LLMInteraction
        import app.services.llm_advisor_service as service_module

        with SessionLocal() as db:
            db.query(LLMInteraction).delete()
            db.commit()
        monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 0.0)
        monkeypatch.setattr(
            advisor._data_aggregator,
            "fetch_market_data",
            lambda symbol, market: {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": 221.8,
                "atr": 1.2,
                "bb_upper": 224.0,
                "bb_middle": 221.0,
                "bb_lower": 218.0,
            },
        )
        monkeypatch.setattr(
            advisor,
            "_call_deepseek",
            lambda prompt: '{"suggested_buy_low": 220.0, "suggested_sell_high": 224.0, "confidence_score": 0.91, "analysis": "test", "order_action": "BUY_NOW", "order_price": 221.8}',
        )
        monkeypatch.setattr(
            advisor,
            "_record_analysis",
            lambda db, result, interval_minutes: datetime(2026, 5, 22, 10, 5, tzinfo=timezone.utc),
        )

        result = advisor.analyze(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.0,
            current_sell_high=224.0,
            short_selling=False,
            current_position="FLAT",
            recent_trades=[],
            min_profit_amount=10.0,
            account_context={"cash_currency": "USD", "available_cash": 10000, "buying_power": 5000},
            force=True,
        )

        assert result["success"] is True
        assert result["interaction_id"] is not None
        assert result["order_action"] == "BUY_NOW"

        db = SessionLocal()
        try:
            row = db.get(LLMInteraction, result["interaction_id"])
            assert row is not None
            assert row.symbol == "NVDA.US"
            assert row.interaction_type == "analyze"
            assert row.success is True
            assert row.order_action == "BUY_NOW"
            assert "账户与购买力" in row.prompt
            assert "BUY_NOW" in row.raw_response
        finally:
            db.close()

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


def test_analyze_passes_new_indicators_to_prompt(monkeypatch) -> None:
    """Verify RSI, MACD, volume_analysis are included in prompt context."""
    captured_prompt: dict[str, object] = {}

    def mock_fetch(self, symbol, market):
        return {
            "daily_candles": [],
            "minute_candles": [],
            "current_price": 100.0,
            "atr": 3.0,
            "bb_upper": 110.0,
            "bb_middle": 100.0,
            "bb_lower": 90.0,
            "rsi": 65.0,
            "macd": {"macd": 1.5, "signal": 1.0, "histogram": 0.5},
            "volume_analysis": {"avg_volume": 50000.0, "volume_ratio": 1.2, "trend": "normal"},
        }

    from app.services.data_aggregator import DataAggregator

    original_build_prompt = LLMAdvisorService._build_prompt

    def capturing_build_prompt(self, **kwargs):
        market_data = kwargs.get("market_data", {})
        captured_prompt.update(market_data)
        return original_build_prompt(self, **kwargs)

    monkeypatch.setattr(DataAggregator, "fetch_market_data", mock_fetch)
    monkeypatch.setattr(LLMAdvisorService, "_build_prompt", capturing_build_prompt)

    import app.config
    import app.services.llm_advisor_service as service_module

    monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 0.0)
    monkeypatch.setattr(
        service_module.httpx,
        "post",
        lambda *args, **kwargs: _FakeSuccessResponse(),
    )
    monkeypatch.setattr(
        service_module.LLMAdvisorService,
        "_record_analysis",
        lambda self, db, result, interval: datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        service_module.LLMAdvisorService,
        "_record_interaction",
        staticmethod(lambda **kwargs: 1),
    )

    advisor = LLMAdvisorService()
    result = advisor.analyze(
        symbol="AAPL.US",
        market="US",
        current_price=100.0,
        current_buy_low=90.0,
        current_sell_high=110.0,
        short_selling=False,
        current_position="FLAT",
        recent_trades=[],
        force=True,
    )

    assert result["success"] is True
    assert captured_prompt["rsi"] == 65.0
    assert captured_prompt["macd"] == {"macd": 1.5, "signal": 1.0, "histogram": 0.5}
    assert captured_prompt["volume_analysis"] == {
        "avg_volume": 50000.0,
        "volume_ratio": 1.2,
        "trend": "normal",
    }


class _FakeSuccessResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"suggested_buy_low": 95.0, "suggested_sell_high": 105.0, "confidence_score": 0.8, "analysis": "test"}',
                    },
                }
            ],
            "usage": {},
        }


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

    assert response.status_code == 401

    authed = client.post(
        "/api/strategy/llm-interval/preview",
        json={"symbol": "AAPL.US", "market": "US", "current_buy_low": 0, "current_sell_high": 0},
        headers={"X-API-Key": "configured-local-key"},
    )
    assert authed.status_code == 200
    assert authed.json()["analysis"] == "preview"


class TestABVariantSelection:
    def test_select_variant_returns_none_when_no_experiment_configured(self, monkeypatch) -> None:
        import app.config
        monkeypatch.setattr(app.config.settings, "llm_experiment_name", "")
        advisor = LLMAdvisorService()
        template, variant = advisor._select_variant("AAPL.US")
        assert template is None
        assert variant is None
    def test_select_variant_returns_none_when_experiment_has_no_versions(self, monkeypatch) -> None:
        import app.config
        monkeypatch.setattr(app.config.settings, "llm_experiment_name", "test-exp")
        advisor = LLMAdvisorService()
        template, variant = advisor._select_variant("AAPL.US")
        assert template is None
        assert variant is None
    def test_build_prompt_uses_custom_template_when_provided(self) -> None:
        advisor = LLMAdvisorService()
        custom = "custom prompt template"
        result = advisor._build_prompt(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
            current_position="FLAT",
            recent_trades=[],
            position_quantity=0.0,
            position_avg_price=0.0,
            unrealized_pnl_pct=0.0,
            min_profit_amount=0.0,
            recent_prices=None,
            recent_analysis=None,
            account_context=None,
            market_data={},
            prompt_template=custom,
        )
        # System instructions are always prepended to custom templates to
        # prevent full override of safety directives.
        assert result.startswith("你是一个专业量化交易顾问")
        assert custom in result
    def test_build_prompt_uses_default_modules_when_no_template(self) -> None:
        advisor = LLMAdvisorService()
        result = advisor._build_prompt(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
            current_position="FLAT",
            recent_trades=[],
            position_quantity=0.0,
            position_avg_price=0.0,
            unrealized_pnl_pct=0.0,
            min_profit_amount=0.0,
            recent_prices=None,
            recent_analysis=None,
            account_context=None,
            market_data={"daily_candles": [], "atr": 0.0, "bb_upper": 0.0, "bb_middle": 0.0, "bb_lower": 0.0},
        )
        assert "AAPL.US" in result
        assert "买入下限" in result or "buy_low" in result
    def test_select_variant_returns_template_from_existing_version(self, monkeypatch) -> None:
        import app.config
        from app.database import SessionLocal
        from app.domain.experiment.ab_test_manager import ABTestManager
        from app.models import PromptVersion

        with SessionLocal() as db:
            db.query(PromptVersion).delete()
            db.commit()
        monkeypatch.setattr(app.config.settings, "llm_experiment_name", "ab-test")
        db = SessionLocal()
        try:
            manager = ABTestManager(db)
            manager.create_version("ab-test", "1.0", "first", "template v1")
            db.commit()
            manager.activate_version(1)

            advisor = LLMAdvisorService()
            template, variant = advisor._select_variant("AAPL.US")
            assert template == "template v1"
            assert variant == "ab-test:1.0"
        finally:
            db.close()


class TestLLMAdvisorDegradation:
    @pytest.fixture
    def advisor(self) -> LLMAdvisorService:
        return LLMAdvisorService()

    def test_call_deepseek_timeout_raises_runtime_error(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        import app.config
        import app.services.llm_advisor_service as service_module
        import httpx

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(
            service_module.httpx,
            "post",
            lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("request timed out")),
        )

        with pytest.raises(RuntimeError) as excinfo:
            advisor._call_deepseek("analyze NVDA")

        assert "timeout" in str(excinfo.value).lower()

    def test_call_deepseek_request_error_raises_runtime_error(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        """httpx.RequestError (e.g. ConnectError) should be caught and re-raised as RuntimeError."""
        import app.config
        import app.services.llm_advisor_service as service_module
        import httpx

        fake_request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")

        def raise_request_error(*args, **kwargs):
            raise httpx.ConnectError("connection failed", request=fake_request)

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(service_module.httpx, "post", raise_request_error)

        with pytest.raises(RuntimeError) as excinfo:
            advisor._call_deepseek("analyze NVDA")

        # must contain "request" or be a network error message
        msg = str(excinfo.value).lower()
        assert "request" in msg

    def test_call_deepseek_http_status_error_raises_runtime_error(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        """httpx.HTTPStatusError (5xx / 429) should be caught and re-raised as RuntimeError."""
        import app.config
        import app.services.llm_advisor_service as service_module
        import httpx

        fake_request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        fake_response = httpx.Response(500, text="internal server error")

        class FakeErrorResponse:
            def raise_for_status(self) -> None:
                raise httpx.HTTPStatusError(
                    "500 Internal Server Error", request=fake_request, response=fake_response
                )

            @property
            def text(self) -> str:
                return "internal server error"

            @property
            def status_code(self) -> int:
                return 500

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(
            service_module.httpx, "post", lambda *args, **kwargs: FakeErrorResponse()
        )

        with pytest.raises(RuntimeError) as excinfo:
            advisor._call_deepseek("analyze NVDA")

        msg = str(excinfo.value).lower()
        assert "http" in msg
        assert "500" in str(excinfo.value)

    def test_call_deepseek_non_json_response_raises_runtime_error(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        """response.json() raising ValueError should be caught and re-raised as RuntimeError."""
        import app.config
        import app.services.llm_advisor_service as service_module

        class FakeNonJsonResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                raise ValueError("response body is not valid JSON")

            @property
            def text(self) -> str:
                return "<html>not json</html>"

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(
            service_module.httpx,
            "post",
            lambda *args, **kwargs: FakeNonJsonResponse(),
        )

        with pytest.raises(RuntimeError) as excinfo:
            advisor._call_deepseek("analyze NVDA")

        msg = str(excinfo.value).lower()
        assert "json" in msg or "response" in msg

    def test_call_deepseek_missing_choices_raises_runtime_error(self, advisor: LLMAdvisorService, monkeypatch) -> None:
        """response.json() returning {'choices': []} should be caught and re-raised as RuntimeError."""
        import app.config
        import app.services.llm_advisor_service as service_module

        class FakeEmptyChoicesResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"choices": []}

        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(
            service_module.httpx,
            "post",
            lambda *args, **kwargs: FakeEmptyChoicesResponse(),
        )

        with pytest.raises(RuntimeError) as excinfo:
            advisor._call_deepseek("analyze NVDA")

        msg = str(excinfo.value).lower()
        assert any(keyword in msg for keyword in ("empty", "content", "choice"))

    def test_analyze_records_failed_interaction_on_runtime_error(
        self, advisor: LLMAdvisorService, monkeypatch
    ) -> None:
        """When _call_deepseek raises RuntimeError, analyze() must record a failed
        llm_interactions row (success=False, error contains underlying info, raw_response empty)
        and return success=False.
        """
        import app.config
        from app.database import SessionLocal
        from app.models import LLMInteraction
        import app.services.llm_advisor_service as service_module

        with SessionLocal() as db:
            db.query(LLMInteraction).delete()
            db.commit()
        monkeypatch.setattr(service_module, "_LAST_ANALYSIS_TIMESTAMP", 0.0)
        monkeypatch.setattr(app.config.settings, "deepseek_api_key", "test-key")
        monkeypatch.setattr(
            advisor._data_aggregator,
            "fetch_market_data",
            lambda symbol, market: {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": 221.8,
                "atr": 1.2,
                "bb_upper": 224.0,
                "bb_middle": 221.0,
                "bb_lower": 218.0,
            },
        )

        def raise_runtime_error(prompt: str) -> str:
            raise RuntimeError("Simulated API outage")

        monkeypatch.setattr(advisor, "_call_deepseek", raise_runtime_error)

        result = advisor.analyze(
            symbol="NVDA.US",
            market="US",
            current_price=221.8,
            current_buy_low=219.0,
            current_sell_high=224.0,
            short_selling=False,
            current_position="FLAT",
            recent_trades=[],
            min_profit_amount=10.0,
            force=True,
        )

        assert result["success"] is False
        assert result["error"] == "LLM analysis failed"
        assert result["interaction_id"] is not None

        db = SessionLocal()
        try:
            row = db.get(LLMInteraction, result["interaction_id"])
            assert row is not None
            assert row.symbol == "NVDA.US"
            assert row.interaction_type == "analyze"
            assert row.success is False
            assert row.error is not None
            assert "Simulated API outage" in row.error
            assert row.raw_response == ""
        finally:
            db.close()

    def test_preview_records_failed_interaction_on_runtime_error(
        self, advisor: LLMAdvisorService, monkeypatch
    ) -> None:
        """When _call_deepseek raises RuntimeError, preview() must record a failed
        llm_interactions row (success=False, error contains underlying info, raw_response empty)
        and return success=False.
        """
        from app.database import SessionLocal
        from app.models import LLMInteraction
        import app.services.llm_advisor_service as service_module

        with SessionLocal() as db:
            db.query(LLMInteraction).delete()
            db.commit()
        monkeypatch.setattr(service_module, "_LAST_PREVIEW_TIMESTAMP", 0.0)
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

        def raise_runtime_error(prompt: str) -> str:
            raise RuntimeError("Simulated API outage")

        monkeypatch.setattr(advisor, "_call_deepseek", raise_runtime_error)

        result = advisor.preview(
            symbol="AAPL.US",
            market="US",
            current_price=200.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
        )

        assert result["success"] is False
        assert "interaction_id" not in result or result.get("interaction_id") is None

        db = SessionLocal()
        try:
            rows = (
                db.query(LLMInteraction)
                .filter(LLMInteraction.symbol == "AAPL.US", LLMInteraction.interaction_type == "preview")
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.success is False
            assert row.error is not None
            assert "Simulated API outage" in row.error
            assert row.raw_response == ""
        finally:
            db.close()

    def test_parse_response_non_dict_json_raises(self, advisor: LLMAdvisorService) -> None:
        """Top-level non-dict JSON (list / str / number / list-of-pairs) must raise
        TypeError/ValueError instead of silently producing a dict.
        """
        # top-level list
        with pytest.raises((TypeError, ValueError)):
            advisor._parse_response("[1, 2, 3]")

        # top-level string
        with pytest.raises((TypeError, ValueError)):
            advisor._parse_response('"hello"')

        # top-level number
        with pytest.raises((TypeError, ValueError)):
            advisor._parse_response("42")

        # top-level list of pairs (would silently become a dict via dict() coercion)
        with pytest.raises((TypeError, ValueError)):
            advisor._parse_response('[[1, 2], [3, 4]]')

    def test_preview_returns_error_when_current_price_unavailable(
        self, advisor: LLMAdvisorService, monkeypatch
    ) -> None:
        """When market_data.current_price <= 0, preview() must return success=False
        without calling _call_deepseek.
        """
        import app.services.llm_advisor_service as service_module

        monkeypatch.setattr(service_module, "_LAST_PREVIEW_TIMESTAMP", 0.0)

        def fail_call(prompt: str) -> str:
            raise AssertionError("must not call _call_deepseek when current_price unavailable")

        monkeypatch.setattr(advisor, "_call_deepseek", fail_call)
        monkeypatch.setattr(
            advisor._data_aggregator,
            "fetch_market_data",
            lambda symbol, market: {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": 0.0,
                "atr": 0.0,
                "bb_upper": 0.0,
                "bb_middle": 0.0,
                "bb_lower": 0.0,
            },
        )

        result = advisor.preview(
            symbol="AAPL.US",
            market="US",
            current_price=0.0,
            current_buy_low=180.0,
            current_sell_high=220.0,
            short_selling=False,
        )

        assert result["success"] is False
        assert "unavailable" in result.get("error", "").lower()
