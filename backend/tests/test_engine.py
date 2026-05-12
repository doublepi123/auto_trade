from app.core.engine import EngineState, StrategyEngine, StrategyParams


def make_params(buy_low: float = 100.0, sell_high: float = 200.0, short_selling: bool = False) -> StrategyParams:
    return StrategyParams(symbol="AAPL.US", market="US", buy_low=buy_low, sell_high=sell_high, short_selling=short_selling)


class TestStrategyEngine:
    def test_default_state_is_flat(self) -> None:
        engine = StrategyEngine()
        assert engine.state == EngineState.FLAT

    def test_price_below_buy_low_from_flat_triggers_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        result = engine.update_price(99.0)
        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_price_above_sell_high_from_long_triggers_sell(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(201.0)
        assert result.triggered is True
        assert result.action == "SELL"
        assert engine.state == EngineState.FLAT

    def test_price_range_no_trigger_from_flat(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        result = engine.update_price(150.0)
        assert result.triggered is False
        assert engine.state == EngineState.FLAT

    def test_price_range_no_trigger_from_long(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(150.0)
        assert result.triggered is False
        assert engine.state == EngineState.LONG

    def test_short_selling_enabled_triggers_short(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=True))
        result = engine.update_price(201.0)
        assert result.triggered is True
        assert result.action == "SELL_SHORT"
        assert engine.state == EngineState.SHORT

    def test_short_selling_disabled_does_not_trigger_short(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=False))
        result = engine.update_price(201.0)
        assert result.triggered is False
        assert engine.state == EngineState.FLAT

    def test_cover_short_when_below_buy_low(self) -> None:
        engine = StrategyEngine(make_params(100, 200, short_selling=True))
        engine.state = EngineState.SHORT
        result = engine.update_price(99.0)
        assert result.triggered is True
        assert result.action == "BUY_TO_COVER"
        assert engine.state == EngineState.FLAT

    def test_no_trigger_when_params_empty(self) -> None:
        engine = StrategyEngine(StrategyParams())
        result = engine.update_price(50.0)
        assert result.triggered is False

    def test_sync_state_from_positions(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.sync_state(has_long_position=True, has_short_position=False)
        assert engine.state == EngineState.LONG

        engine.sync_state(has_long_position=False, has_short_position=True)
        assert engine.state == EngineState.SHORT

        engine.sync_state(has_long_position=False, has_short_position=False)
        assert engine.state == EngineState.FLAT

    def test_to_dict(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        d = engine.to_dict()
        assert d["state"] == "flat"
        assert d["symbol"] == "AAPL.US"
        assert d["buy_low"] == 100.0
        assert d["sell_high"] == 200.0

    def test_cooldown_prevents_trigger(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine._last_trigger_at = None
        engine._cooldown_seconds = 60
        result = engine.update_price(99.0)
        assert result.triggered is True

        result2 = engine.update_price(201.0)
        assert result2.triggered is False

    def test_empty_params_no_trigger(self) -> None:
        engine = StrategyEngine()
        engine.update_price(50.0)
        assert engine.last_price == 50.0

    def test_sync_state_from_engine_no_position(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine.sync_state(has_long_position=False, has_short_position=False)
        assert engine.state == EngineState.FLAT
