# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
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
        assert engine.long_entry_rearm_required is True

    def test_forced_long_exit_requires_reclaim_before_reentry(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine._cooldown_seconds = 0
        assert engine.update_price(99.0).action == "BUY"
        assert engine.transition_for_action("SELL") == "OK"

        still_below = engine.update_price(98.0)
        reclaim = engine.update_price(101.0)
        next_breach = engine.update_price(100.0)

        assert still_below.triggered is False
        assert reclaim.triggered is False
        assert next_breach.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_band_profit_exit_is_already_rearmed(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine._cooldown_seconds = 0
        assert engine.update_price(99.0).action == "BUY"

        exit_result = engine.update_price(200.0)
        next_breach = engine.update_price(100.0)

        assert exit_result.action == "SELL"
        assert next_breach.action == "BUY"

    def test_syncing_long_position_flat_preserves_reentry_latch(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.sync_state(has_long_position=True, has_short_position=False)
        engine.sync_state(has_long_position=False, has_short_position=False)

        assert engine.state == EngineState.FLAT
        assert engine.long_entry_rearm_required is True
        assert engine.update_price(99.0).triggered is False

    def test_snapshot_restore_includes_reentry_latch(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        before_entry = engine.snapshot()
        engine.update_price(99.0)
        assert engine.long_entry_rearm_required is True

        engine.restore(before_entry)

        assert engine.state == EngineState.FLAT
        assert engine.long_entry_rearm_required is False

    def test_restore_preserving_trigger_restores_reentry_latch(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.sync_state(has_long_position=True, has_short_position=False)
        before_exit = engine.snapshot()
        engine.restore_long_entry_rearm(False)
        engine.state = EngineState.FLAT

        engine.restore_preserving_trigger(before_exit)

        assert engine.state == EngineState.LONG
        assert engine.long_entry_rearm_required is True

    def test_reclaim_rearms_even_during_trigger_cooldown(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        assert engine.update_price(99.0).action == "BUY"
        assert engine.transition_for_action("SELL") == "OK"
        engine._cooldown_seconds = 60

        result = engine.update_price(101.0)

        assert result.triggered is False
        assert engine.long_entry_rearm_required is False

    def test_explicit_buy_cannot_bypass_reentry_latch(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.sync_state(has_long_position=True, has_short_position=False)
        engine.sync_state(has_long_position=False, has_short_position=False)

        assert engine.transition_for_action("BUY") == "ENTRY_REARM_REQUIRED"
        assert engine.state == EngineState.FLAT

    def test_price_at_buy_low_boundary_triggers_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        result = engine.update_price(100.0)
        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_position_addon_policy_does_not_block_initial_buy(self) -> None:
        params = make_params(100, 200)
        params.allow_position_addons = False
        engine = StrategyEngine(params)

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

    def test_price_at_sell_high_boundary_from_long_triggers_sell(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(200.0)
        assert result.triggered is True
        assert result.action == "SELL"
        assert engine.state == EngineState.FLAT

    def test_price_below_buy_low_from_long_triggers_add_on_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG

        result = engine.update_price(99.0)

        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_add_on_buy_at_buy_low_boundary(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(100.0)
        assert result.triggered is True
        assert result.action == "BUY"
        assert engine.state == EngineState.LONG

    def test_position_addon_policy_blocks_buy_while_long(self) -> None:
        params = make_params(100, 200)
        params.allow_position_addons = False
        engine = StrategyEngine(params)
        engine.state = EngineState.LONG

        result = engine.update_price(99.0)

        assert result.triggered is False
        assert engine.state == EngineState.LONG

    def test_sell_priority_over_add_on_buy_in_long(self) -> None:
        """SELL is evaluated first; with valid config buy_low < sell_high, both can't be true."""
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        result = engine.update_price(201.0)
        assert result.triggered is True
        assert result.action == "SELL"
        assert engine.state == EngineState.FLAT

    def test_cooldown_blocks_add_on_buy(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine._cooldown_seconds = 60
        first = engine.update_price(99.0)
        assert first.triggered is True
        assert first.action == "BUY"
        assert engine.state == EngineState.LONG
        second = engine.update_price(98.0)
        assert second.triggered is False
        assert engine.state == EngineState.LONG

    def test_cooldown_after_add_on_buy_blocks_sell(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine._cooldown_seconds = 60
        engine.update_price(99.0)
        result = engine.update_price(201.0)
        assert result.triggered is False
        assert engine.state == EngineState.LONG

    def test_long_stays_long_after_multiple_add_on_buys(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine._cooldown_seconds = 0
        engine.update_price(99.0)
        assert engine.state == EngineState.LONG
        engine.update_price(95.0)
        assert engine.state == EngineState.LONG
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

    def test_sync_state_preserves_trigger_cooldown(self) -> None:
        from datetime import datetime, timezone

        engine = StrategyEngine(make_params(100, 200))
        engine.last_trigger_at = datetime.now(timezone.utc)
        engine.sync_state(has_long_position=True, has_short_position=False)
        assert engine.last_trigger_at is not None

    def test_to_dict(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        d = engine.to_dict()
        assert d["state"] == "flat"
        assert d["symbol"] == "AAPL.US"
        assert d["buy_low"] == 100.0
        assert d["sell_high"] == 200.0
        assert d["long_entry_rearm_required"] is False

    def test_cooldown_prevents_trigger(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.last_trigger_at = None
        engine._cooldown_seconds = 60
        result = engine.update_price(99.0)
        assert result.triggered is True

        result2 = engine.update_price(201.0)
        assert result2.triggered is False

    def test_empty_params_no_trigger(self) -> None:
        engine = StrategyEngine()
        result = engine.update_price(50.0)
        assert result.triggered is False
        assert engine.last_price == 50.0

    def test_short_state_no_add_on_when_price_above_sell_high(self) -> None:
        """SHORT 状态不追加做空 — 有意限制空头敞口,与 LONG 的 add-on 不对称"""
        engine = StrategyEngine(make_params(100, 200, short_selling=True))
        engine.state = EngineState.SHORT
        result = engine.update_price(201.0)
        assert result.triggered is False
        assert engine.state == EngineState.SHORT

    def test_sync_state_from_engine_no_position(self) -> None:
        engine = StrategyEngine(make_params(100, 200))
        engine.state = EngineState.LONG
        engine.sync_state(has_long_position=False, has_short_position=False)
        assert engine.state == EngineState.FLAT
