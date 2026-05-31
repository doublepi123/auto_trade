from __future__ import annotations

from app.domain.prompt.context_module import ContextModule


def _base_context(**overrides: object) -> dict:
    ctx: dict = {
        "daily_candles": [],
        "minute_candles": [],
        "current_price": 200.0,
        "atr": 5.0,
        "bb_upper": 210.0,
        "bb_middle": 200.0,
        "bb_lower": 190.0,
        "rsi": 0.0,
        "macd": {},
        "volume_analysis": {},
        "current_position": "FLAT",
        "position_quantity": 0.0,
        "position_avg_price": 0.0,
        "unrealized_pnl_pct": 0.0,
    }
    ctx.update(overrides)
    return ctx


class TestContextModulePositionCost:
    def test_renders_position_cost_when_long(self) -> None:
        ctx = _base_context(
            current_position="LONG",
            position_quantity=100.0,
            position_avg_price=195.0,
            unrealized_pnl_pct=2.56,
            current_price=200.0,
        )
        rendered = ContextModule().render(ctx)
        assert "持仓方向: LONG" in rendered
        assert "持仓数量: 100" in rendered
        assert "平均成本: 195.00" in rendered
        assert "当前价格: 200.00" in rendered
        assert "+2.56%" in rendered

    def test_renders_flat_when_no_position(self) -> None:
        ctx = _base_context(current_position="FLAT", position_quantity=0.0)
        rendered = ContextModule().render(ctx)
        assert "当前无持仓" in rendered

    def test_renders_short_position(self) -> None:
        ctx = _base_context(
            current_position="SHORT",
            position_quantity=50.0,
            position_avg_price=205.0,
            unrealized_pnl_pct=2.44,
        )
        rendered = ContextModule().render(ctx)
        assert "持仓方向: SHORT" in rendered
        assert "持仓数量: 50" in rendered
        assert "平均成本: 205.00" in rendered

    def test_position_cost_section_omitted_positions_from_empty_context(self) -> None:
        ctx = _base_context()
        rendered = ContextModule().render(ctx)
        assert "当前无持仓" in rendered
