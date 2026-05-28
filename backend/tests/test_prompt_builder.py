from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_prompt_builder.db",
)

import pytest
from app.domain.prompt.base import PromptModule
from app.domain.prompt.system_module import SystemModule


class TestPromptModule:
    def test_base_class_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            PromptModule()  # type: ignore[abstract]

    def test_system_module_renders_role(self) -> None:
        module = SystemModule()
        result = module.render({})
        assert "量化交易顾问" in result
        assert len(result) > 10


from app.domain.prompt.context_module import ContextModule


class TestContextModule:
    def test_renders_daily_candle_table(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_price": 200.0,
            "daily_candles": [
                {"date": "2026-05-20", "open": 195.0, "high": 202.0, "low": 194.0, "close": 200.0, "volume": 50000},
                {"date": "2026-05-21", "open": 200.0, "high": 205.0, "low": 199.0, "close": 203.0, "volume": 60000},
            ],
            "minute_candles": [],
            "atr": 3.5,
            "bb_upper": 210.0,
            "bb_middle": 200.0,
            "bb_lower": 190.0,
            "rsi": 55.0,
            "macd": {"macd": 1.2, "signal": 0.8, "histogram": 0.4},
            "volume_analysis": {"avg_volume": 55000.0, "volume_ratio": 1.1, "trend": "normal"},
        }
        result = module.render(context)
        assert "2026-05-20" in result
        assert "200.00" in result
        assert "ATR" in result

    def test_renders_placeholder_when_no_candles(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_price": 0.0,
            "daily_candles": [],
            "minute_candles": [],
            "atr": 0.0,
            "bb_upper": 0.0,
            "bb_middle": 0.0,
            "bb_lower": 0.0,
            "rsi": 0.0,
            "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "volume_analysis": {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
        }
        result = module.render(context)
        assert "暂无可用历史日 K 数据" in result

    def test_renders_rsi_macd_when_present(self) -> None:
        module = ContextModule()
        context = {
            "symbol": "TSLA.US",
            "market": "US",
            "current_price": 250.0,
            "daily_candles": [{"date": "2026-05-21", "open": 248.0, "high": 255.0, "low": 247.0, "close": 252.0, "volume": 80000}],
            "minute_candles": [],
            "atr": 5.0,
            "bb_upper": 260.0,
            "bb_middle": 250.0,
            "bb_lower": 240.0,
            "rsi": 62.5,
            "macd": {"macd": 2.1, "signal": 1.5, "histogram": 0.6},
            "volume_analysis": {"avg_volume": 75000.0, "volume_ratio": 1.07, "trend": "normal"},
        }
        result = module.render(context)
        assert "RSI" in result
        assert "62.50" in result
        assert "MACD" in result


from app.domain.prompt.strategy_module import StrategyModule


class TestStrategyModule:
    def test_renders_flat_position(self) -> None:
        module = StrategyModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_buy_low": 190.0,
            "current_sell_high": 210.0,
            "short_selling": False,
            "min_profit_amount": 5.0,
            "current_position": "FLAT",
            "position_quantity": 0.0,
            "position_avg_price": 0.0,
            "unrealized_pnl_pct": 0.0,
            "recent_trades": [],
        }
        result = module.render(context)
        assert "FLAT" in result
        assert "190.00" in result
        assert "210.00" in result

    def test_renders_long_position_with_trades(self) -> None:
        module = StrategyModule()
        context = {
            "symbol": "AAPL.US",
            "market": "US",
            "current_buy_low": 195.0,
            "current_sell_high": 210.0,
            "short_selling": False,
            "min_profit_amount": 5.0,
            "current_position": "LONG",
            "position_quantity": 100.0,
            "position_avg_price": 200.0,
            "unrealized_pnl_pct": 2.5,
            "recent_trades": [
                {"side": "BUY", "quantity": 100, "price": 200.0},
            ],
        }
        result = module.render(context)
        assert "LONG" in result
        assert "持仓数量: 100.00" in result
        assert "持仓成本价: 200.00" in result
        assert "BUY" in result
