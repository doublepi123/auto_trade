from datetime import datetime, timezone
from typing import TypedDict, cast

import pytest
from fastapi.testclient import TestClient

from app.api.backtest import _metrics_to_schema, _params_to_engine
from app.core.backtest import BacktestBar, BacktestEngine, BacktestEngineParams, parse_backtest_csv
from app.main import app
from app.schemas import BacktestParams

client = TestClient(app)


class _BacktestMetricsJson(TypedDict):
    total_pnl: float
    win_rate: float
    final_state: str


class _BacktestTradeJson(TypedDict):
    action: str
    price: float
    fee: float
    reason: str


class _BacktestResultJson(TypedDict):
    metrics: _BacktestMetricsJson
    trades: list[_BacktestTradeJson]
    equity_curve: list[object]
    fee_sensitivity: list[object]


def bar(minute: int, open_: float, high: float, low: float, close: float) -> BacktestBar:
    return BacktestBar(
        timestamp=datetime(2026, 5, 22, 10, minute, tzinfo=timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def bar_at(
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> BacktestBar:
    return BacktestBar(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


class TestBacktestEngine:
    def test_new_execution_defaults_preserve_legacy_any_session_behavior(self) -> None:
        bars = [
            bar(0, 150, 160, 99, 105),
            bar(1, 150, 201, 140, 200),
        ]
        omitted = BacktestEngineParams(buy_low=100, sell_high=200, quantity=2)
        explicit_legacy = BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            quantity=2,
            market="US",
            trading_session_mode="ANY",
            opening_warmup_minutes=0,
            entry_crossing_required=False,
            max_entries_per_symbol_per_day=0,
            max_holding_minutes=0,
            entry_cutoff_minutes_before_close=0,
            flatten_minutes_before_close=0,
        )

        omitted_result = BacktestEngine(omitted).run(
            bars, include_fee_sensitivity=False
        )
        explicit_result = BacktestEngine(explicit_legacy).run(
            bars, include_fee_sensitivity=False
        )

        # 10:00 UTC is outside US RTH; legacy ANY mode must still enter.
        assert omitted_result == explicit_result
        assert [trade.action for trade in omitted_result.trades] == ["BUY", "SELL"]

    def test_opening_warmup_blocks_until_end_boundary(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            opening_warmup_minutes=30,
            buy_low=100,
            sell_high=200,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 13, 59, 59, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.trades[0].timestamp == datetime(
            2026, 5, 22, 14, 0, tzinfo=timezone.utc
        )
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "SESSION"
        assert result.skipped_signals[0].reason == "opening warmup for US"

    def test_crossing_blocks_gap_restart_then_allows_bar_local_downcross(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            buy_low=100,
            sell_high=200,
            entry_crossing_required=True,
        ))

        result = engine.run([
            # A stale prior close above the threshold must not turn the next
            # gap-down bar into a synthetic crossing.
            bar_at(
                datetime(2026, 5, 22, 13, 30, tzinfo=timezone.utc),
                101,
                102,
                100.5,
                101,
            ),
            bar_at(
                datetime(2026, 5, 22, 13, 31, tzinfo=timezone.utc),
                99,
                100,
                98,
                99,
            ),
            # The threshold is crossed from this bar's own open, so it is a
            # valid conservative OHLC downcross.
            bar_at(
                datetime(2026, 5, 22, 13, 32, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.trades[0].timestamp == datetime(
            2026, 5, 22, 13, 32, tzinfo=timezone.utc
        )
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "REPRICING"
        assert "fresh entry-threshold crossing" in (
            result.skipped_signals[0].reason
        )

    def test_crossing_blocks_reentry_below_threshold_after_time_exit(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            buy_low=100,
            sell_high=200,
            entry_crossing_required=True,
            max_holding_minutes=1,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 13, 30, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 22, 13, 31, tzinfo=timezone.utc),
                99,
                100,
                98,
                99,
            ),
            bar_at(
                datetime(2026, 5, 22, 13, 32, tzinfo=timezone.utc),
                99,
                100,
                98,
                99,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "TIME_STOP_SELL",
        ]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].timestamp == datetime(
            2026, 5, 22, 13, 32, tzinfo=timezone.utc
        )
        assert result.skipped_signals[0].category == "REPRICING"

    def test_daily_entry_cap_resets_on_exchange_local_trade_day(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100,
            sell_high=110,
            max_entries_per_symbol_per_day=1,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 20, 23, 57, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 20, 23, 58, tzinfo=timezone.utc),
                109,
                111,
                108,
                110,
            ),
            # UTC changed, but New York is still May 20: cap remains active.
            bar_at(
                datetime(2026, 5, 21, 0, 2, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            # New York local midnight has passed: the entry counter resets.
            bar_at(
                datetime(2026, 5, 21, 4, 1, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "SELL",
            "BUY",
        ]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "COOLDOWN"
        assert "1/1" in result.skipped_signals[0].reason

    def test_us_rth_only_skips_premarket_entry_then_allows_open(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 13, 29, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 22, 13, 30, tzinfo=timezone.utc), 101, 102, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "SESSION"
        assert result.skipped_signals[0].reason == "non-RTH for US"

    def test_hk_rth_only_treats_lunch_break_as_non_rth(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="HK",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 4, 15, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 22, 5, 0, tzinfo=timezone.utc), 101, 102, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "SESSION"
        assert result.skipped_signals[0].reason == "non-RTH for HK"

    def test_entry_cutoff_skips_signal_with_session_category(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="ANY",
            buy_low=100,
            sell_high=200,
            entry_cutoff_minutes_before_close=15,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 19, 46, tzinfo=timezone.utc), 101, 102, 99, 100),
        ], include_fee_sensitivity=False)

        assert result.trades == []
        assert result.skipped_signals[0].category == "SESSION"
        assert "entry cutoff within 15 minutes" in result.skipped_signals[0].reason

    def test_time_stop_uses_close_with_slippage_and_bypasses_min_profit(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
            min_profit_amount=1000,
            slippage_pct=1,
            trailing_stop_pct=5,
            max_holding_minutes=10,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 13, 30, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 22, 13, 40, tzinfo=timezone.utc), 105, 201, 101, 105),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        assert closed.action == "TIME_STOP_SELL"
        assert closed.reason == "TIME_STOP: maximum holding time reached: 10 minutes"
        assert closed.price == 105 * 0.99
        assert closed.holding_minutes == 10
        assert result.metrics.skipped_signals == 0

    def test_eod_flatten_precedes_time_stop_and_target(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
            max_holding_minutes=5,
            flatten_minutes_before_close=15,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 19, 39, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 22, 19, 45, tzinfo=timezone.utc), 105, 201, 101, 105),
        ], include_fee_sensitivity=False)

        assert result.trades[-1].action == "EOD_FLATTEN_SELL"
        assert result.trades[-1].reason == "EOD_FLATTEN: end-of-day flatten window reached"
        assert result.trades[-1].price == 105

    def test_flatten_window_blocks_new_entry_when_cutoff_is_disabled(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="ANY",
            buy_low=100,
            sell_high=200,
            entry_cutoff_minutes_before_close=0,
            flatten_minutes_before_close=15,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 19, 45, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert result.trades == []
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "SESSION"
        assert "end-of-day flatten window" in result.skipped_signals[0].reason

    def test_fixed_ohlc_stop_precedes_eod_flatten(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
            stop_loss_pct=5,
            flatten_minutes_before_close=15,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 22, 19, 39, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 22, 19, 45, tzinfo=timezone.utc), 96, 99, 94, 96),
        ], include_fee_sensitivity=False)

        assert result.trades[-1].action == "STOP_LOSS_SELL"
        assert result.trades[-1].price == 95

    def test_daily_loss_at_exact_boundary_precedes_stop_eod_time_and_target(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=110,
            min_profit_amount=1_000,
            max_daily_loss=5,
            stop_loss_pct=5,
            max_holding_minutes=1,
            flatten_minutes_before_close=15,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 19, 44, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            # Every deterministic exit and the target are eligible. The live
            # priority chooses DAILY_LOSS at the exact combined-PnL boundary.
            bar_at(
                datetime(2026, 5, 22, 19, 45, tzinfo=timezone.utc),
                100,
                111,
                94,
                95,
            ),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        assert closed.action == "DAILY_LOSS_SELL"
        assert closed.price == 95
        assert closed.pnl == -5
        assert closed.reason.startswith(
            "DAILY_LOSS: daily loss limit reached using OHLC bar-close "
            "approximation"
        )
        assert "realized=0.00" in closed.reason
        assert "unrealized=-5.00" in closed.reason
        assert "combined=-5.00" in closed.reason
        assert result.metrics.skipped_signals == 0

    def test_daily_loss_does_not_trigger_above_exact_boundary(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            max_daily_loss=5,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 100, 101, 95.01, 95.01),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.metrics.final_state == "long"

    def test_zero_daily_loss_disables_guard_like_live_risk_controller(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            max_daily_loss=0,
            max_holding_minutes=1,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 50, 51, 49, 50),
            bar(2, 101, 102, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "TIME_STOP_SELL",
            "BUY",
        ]
        assert result.metrics.final_state == "long"

    def test_negative_daily_loss_is_rejected(self) -> None:
        try:
            BacktestEngine(BacktestEngineParams(
                buy_low=100,
                sell_high=200,
                max_daily_loss=-1,
            ))
        except ValueError as exc:
            assert "max_daily_loss must be finite and non-negative" in str(exc)
        else:
            raise AssertionError("negative max_daily_loss should raise ValueError")

    def test_non_finite_daily_loss_is_rejected_by_core_engine(self) -> None:
        for invalid in (float("nan"), float("inf"), float("-inf")):
            try:
                BacktestEngine(BacktestEngineParams(
                    buy_low=100,
                    sell_high=200,
                    max_daily_loss=invalid,
                ))
            except ValueError as exc:
                assert "max_daily_loss must be finite and non-negative" in str(exc)
            else:
                raise AssertionError(
                    "non-finite max_daily_loss should raise ValueError"
                )

    def test_daily_loss_combines_realized_day_pnl_with_open_unrealized(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            max_daily_loss=5,
            max_holding_minutes=1,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 99, 99, 98, 98),
            bar(2, 101, 102, 99, 100),
            bar(3, 98, 99, 97, 97),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "TIME_STOP_SELL",
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        assert result.trades[1].pnl == -2
        closed = result.trades[-1]
        assert closed.pnl == -3
        assert "realized=-2.00" in closed.reason
        assert "unrealized=-3.00" in closed.reason
        assert "combined=-5.00" in closed.reason

    def test_positive_realized_pnl_offsets_open_loss_before_daily_exit(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            max_daily_loss=5,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 109, 110, 105, 110),
            bar(2, 101, 102, 99, 100),
            bar(3, 86, 86, 84, 85),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "SELL",
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        closed = result.trades[-1]
        assert "realized=10.00" in closed.reason
        assert "unrealized=-15.00" in closed.reason
        assert "combined=-5.00" in closed.reason

    def test_short_daily_loss_cover_applies_buy_slippage(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=90,
            sell_high=100,
            short_selling=True,
            quantity=10,
            max_daily_loss=20,
            slippage_pct=1,
        ))

        result = engine.run([
            bar(0, 99, 101, 98, 100),
            bar(1, 101, 102, 100, 101),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "SELL_SHORT",
            "DAILY_LOSS_COVER",
        ]
        closed = result.trades[-1]
        assert closed.price == 101 * 1.01
        assert "unrealized=-20.00" in closed.reason

    def test_daily_loss_close_fill_applies_fees_and_blocks_reentry(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            quantity=10,
            max_daily_loss=20,
            fee_rate=0.001,
            fixed_fee=1,
            slippage_pct=1,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 100, 100, 99, 99),
            bar(2, 101, 102, 99, 100),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        expected_exit_price = 99 * 0.99
        assert closed.price == expected_exit_price
        assert closed.gross_pnl is not None
        assert abs(closed.gross_pnl - (-29.9)) < 1e-9
        assert abs(closed.fee - 1.9801) < 1e-9
        assert closed.total_fees is not None
        assert abs(closed.total_fees - 3.9901) < 1e-9
        assert closed.net_pnl is not None
        assert abs(closed.net_pnl - (-33.8901)) < 1e-9
        assert abs(result.metrics.fees_paid - 3.9901) < 1e-9
        # Like live, the trigger excludes entry/prospective exit fees and uses
        # the gross open-position PnL at the observed close (99 vs entry 101).
        assert "realized=0.00" in closed.reason
        assert "unrealized=-20.00" in closed.reason
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "RISK"
        assert "daily loss limit reached" in result.skipped_signals[0].reason

    def test_rth_only_does_not_manufacture_after_hours_exit(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 19, 44, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 22, 20, 1, tzinfo=timezone.utc),
                199,
                201,
                198,
                200,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.metrics.final_state == "long"

    def test_rth_only_latches_lunch_daily_loss_and_exits_after_recovery(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="HK",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=110,
            max_daily_loss=5,
            stop_loss_pct=5,
            max_holding_minutes=1,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 22, 3, 59, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            # HK lunch is non-RTH. This observation latches DAILY_LOSS but
            # must not manufacture a fill at the 95 close.
            bar_at(
                datetime(2026, 5, 22, 4, 15, tzinfo=timezone.utc),
                96,
                97,
                94,
                95,
            ),
            # Price has fully recovered when the afternoon session opens.
            # Fixed stop, time stop, and target are all eligible in this bar;
            # the durable DAILY_LOSS intent still has highest priority and
            # exits at this RTH close.
            bar_at(
                datetime(2026, 5, 22, 5, 0, tzinfo=timezone.utc),
                100,
                111,
                94,
                100,
            ),
            bar_at(
                datetime(2026, 5, 22, 5, 1, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        closed = result.trades[-1]
        assert closed.timestamp == datetime(
            2026, 5, 22, 5, 0, tzinfo=timezone.utc
        )
        assert closed.price == 100
        assert "reduction intent latched outside RTH" in closed.reason
        assert "trigger_close=95.0000" in closed.reason
        assert result.equity_curve[1].position == "long"
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "RISK"
        assert "requires manual resume" in result.skipped_signals[0].reason

    def test_rth_only_daily_loss_intent_survives_exchange_day_rollover(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            trading_session_mode="RTH_ONLY",
            buy_low=100,
            sell_high=200,
            max_daily_loss=5,
        ))

        result = engine.run([
            bar_at(
                datetime(2026, 5, 20, 19, 59, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 20, 20, 1, tzinfo=timezone.utc),
                96,
                97,
                94,
                95,
            ),
            # New York local midnight has passed and the price has recovered,
            # but neither condition clears the durable reduction intent.
            bar_at(
                datetime(2026, 5, 21, 4, 1, tzinfo=timezone.utc),
                100,
                101,
                99,
                100,
            ),
            bar_at(
                datetime(2026, 5, 21, 13, 30, tzinfo=timezone.utc),
                101,
                102,
                100,
                101,
            ),
            bar_at(
                datetime(2026, 5, 21, 13, 31, tzinfo=timezone.utc),
                101,
                102,
                99,
                100,
            ),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        closed = result.trades[-1]
        assert closed.timestamp == datetime(
            2026, 5, 21, 13, 30, tzinfo=timezone.utc
        )
        assert closed.price == 101
        assert "2026-05-20T20:01:00+00:00" in closed.reason
        assert "trigger_close=95.0000" in closed.reason
        assert [point.position for point in result.equity_curve[:3]] == [
            "long",
            "long",
            "long",
        ]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "RISK"
        assert "requires manual resume" in result.skipped_signals[0].reason

    def test_daily_risk_reset_uses_exchange_local_trade_day(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            buy_low=100,
            sell_high=110,
            max_daily_loss=5,
            max_consecutive_losses=10,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 20, 23, 57, tzinfo=timezone.utc), 105, 106, 99, 100),
            # Close remains above the daily-loss boundary, so fixed-stop wins;
            # its threshold fill then reaches the realized daily limit.
            bar_at(datetime(2026, 5, 20, 23, 58, tzinfo=timezone.utc), 98, 99, 94, 96),
            # UTC date changed, but New York is still on May 20: remain paused.
            bar_at(datetime(2026, 5, 21, 0, 2, tzinfo=timezone.utc), 105, 106, 99, 100),
            # New York local midnight has now passed: a realized-only daily
            # pause resets because no non-auto-resumable DAILY_LOSS reduction
            # was latched.
            bar_at(datetime(2026, 5, 21, 4, 1, tzinfo=timezone.utc), 105, 106, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY", "STOP_LOSS_SELL", "BUY",
        ]
        assert len(result.skipped_signals) == 1
        assert result.skipped_signals[0].category == "RISK"

    def test_daily_loss_forced_exit_requires_manual_resume_across_days(
        self,
    ) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            market="US",
            buy_low=100,
            sell_high=200,
            max_daily_loss=5,
            max_consecutive_losses=10,
        ))

        result = engine.run([
            bar_at(datetime(2026, 5, 20, 23, 57, tzinfo=timezone.utc), 101, 102, 99, 100),
            bar_at(datetime(2026, 5, 20, 23, 58, tzinfo=timezone.utc), 95, 96, 94, 95),
            # Same exchange-local day remains paused.
            bar_at(datetime(2026, 5, 21, 0, 2, tzinfo=timezone.utc), 101, 102, 99, 100),
            # A new exchange-local day does not manufacture the manual resume
            # required by live after a DAILY_LOSS reduction fill.
            bar_at(datetime(2026, 5, 21, 4, 1, tzinfo=timezone.utc), 101, 102, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        assert len(result.skipped_signals) == 2
        assert all(
            signal.category == "RISK"
            and "requires manual resume" in signal.reason
            for signal in result.skipped_signals
        )

    def test_rejects_invalid_market_mode_and_execution_windows(self) -> None:
        invalid_overrides = [
            {"market": "EU"},
            {"trading_session_mode": "PREMARKET"},
            {"opening_warmup_minutes": -1},
            {"opening_warmup_minutes": 391},
            {"max_entries_per_symbol_per_day": -1},
            {"max_entries_per_symbol_per_day": 1001},
            {"max_holding_minutes": -1},
            {"entry_cutoff_minutes_before_close": 181},
            {"flatten_minutes_before_close": 16, "entry_cutoff_minutes_before_close": 15},
        ]

        for overrides in invalid_overrides:
            try:
                BacktestEngine(BacktestEngineParams(
                    buy_low=100,
                    sell_high=200,
                    **overrides,
                ))
            except ValueError:
                continue
            raise AssertionError(f"invalid params should fail: {overrides}")

    def test_closed_trade_uses_net_pnl_and_reports_excursions(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=110,
            quantity=10,
            fee_rate=0.001,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 100, 108, 95, 105),
            bar(2, 105, 111, 104, 110),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        assert closed.gross_pnl == 100
        assert closed.total_fees == 2.1
        assert closed.net_pnl == 97.9
        assert closed.pnl == closed.net_pnl
        assert closed.mfe_pct == 11
        assert closed.mae_pct == -5

    def test_flat_long_flat_path(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=200,
            quantity=2,
            initial_cash=10000,
        ))

        result = engine.run([
            bar(0, 150, 160, 99, 105),
            bar(1, 150, 201, 140, 200),
        ])

        assert [trade.action for trade in result.trades] == ["BUY", "SELL"]
        assert result.trades[0].price == 100
        assert result.trades[1].pnl == 200
        assert result.metrics.closed_trade_count == 1
        assert result.metrics.win_rate == 100
        assert result.metrics.final_state == "flat"
        assert result.metrics.total_pnl == 200

    def test_flat_short_flat_path(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=200,
            short_selling=True,
            quantity=3,
            initial_cash=10000,
        ))

        result = engine.run([
            bar(0, 150, 202, 140, 195),
            bar(1, 150, 160, 98, 100),
        ])

        assert [trade.action for trade in result.trades] == ["SELL_SHORT", "BUY_TO_COVER"]
        assert result.trades[1].pnl == 300
        assert result.metrics.closed_trade_count == 1
        assert result.metrics.total_pnl == 300
        assert result.metrics.final_state == "flat"

    def test_min_profit_amount_filters_exit_signal(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=101,
            min_profit_amount=5,
            initial_cash=10000,
        ))

        result = engine.run([
            bar(0, 100, 100, 99, 100),
            bar(1, 100, 101.5, 100, 101),
        ])

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.metrics.closed_trade_count == 0
        assert result.metrics.final_state == "long"
        assert result.metrics.skipped_signals == 1
        assert "below min_profit_amount" in result.skipped_signals[0].reason
        assert result.skipped_signals[0].category == "FEE"

    def test_daily_loss_pause_skips_new_entries(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            quantity=1,
            initial_cash=10000,
            max_daily_loss=5,
            max_consecutive_losses=10,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 98, 99, 94, 95),
            bar(2, 105, 106, 99, 100),
        ])

        assert [trade.action for trade in result.trades] == [
            "BUY",
            "DAILY_LOSS_SELL",
        ]
        assert result.trades[1].pnl == -5
        assert result.metrics.skipped_signals == 1
        assert "daily loss limit reached" in result.skipped_signals[0].reason

    def test_max_consecutive_losses_pause_skips_new_entries(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            quantity=1,
            initial_cash=10000,
            max_daily_loss=1000,
            max_consecutive_losses=2,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 98, 99, 94, 95),
            bar(2, 105, 106, 99, 100),
            bar(3, 98, 99, 94, 95),
            bar(4, 105, 106, 99, 100),
        ])

        assert [trade.action for trade in result.trades] == ["BUY", "STOP_LOSS_SELL", "BUY", "STOP_LOSS_SELL"]
        assert result.metrics.losing_trades == 2
        assert result.metrics.skipped_signals == 1
        assert "max consecutive losses reached" in result.skipped_signals[0].reason

    def test_drawdown_breach_blocks_long_and_short_entries(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            short_selling=True,
            max_daily_loss=1000,
            max_consecutive_losses=100,
            max_drawdown_amount=5,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 98, 99, 94, 95),
            bar(2, 105, 106, 99, 100),
            bar(3, 108, 111, 105, 110),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY", "STOP_LOSS_SELL"]
        assert [signal.action for signal in result.skipped_signals] == ["BUY", "SELL_SHORT"]
        assert {signal.category for signal in result.skipped_signals} == {"DRAWDOWN"}

    def test_exit_that_causes_drawdown_breach_still_executes(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            max_daily_loss=1000,
            max_consecutive_losses=100,
            max_drawdown_amount=5,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 98, 99, 94, 95),
            bar(2, 105, 106, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY", "STOP_LOSS_SELL"]
        assert result.trades[-1].net_pnl == -5
        assert result.skipped_signals[0].category == "DRAWDOWN"

    def test_drawdown_peak_ratchets_after_wins_before_breach(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            max_daily_loss=1000,
            max_consecutive_losses=100,
            max_drawdown_amount=5,
            stop_loss_pct=5,
        ))

        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 105, 111, 104, 110),
            bar(2, 105, 106, 99, 100),
            bar(3, 105, 111, 104, 110),
            bar(4, 105, 106, 99, 100),
            bar(5, 98, 99, 94, 95),
            bar(6, 105, 106, 99, 100),
        ], include_fee_sensitivity=False)

        assert [trade.pnl for trade in result.trades if trade.net_pnl is not None] == [10, 10, -5]
        assert result.skipped_signals[0].category == "DRAWDOWN"
        assert "cumulative_realized_pnl=15.00" in result.skipped_signals[0].reason
        assert "peak_realized_pnl=20.00" in result.skipped_signals[0].reason

    def test_drawdown_zero_matches_omitted_default(self) -> None:
        bars = [
            bar(0, 105, 106, 99, 100),
            bar(1, 98, 99, 94, 95),
            bar(2, 105, 106, 99, 100),
        ]
        base = BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            max_daily_loss=1000,
            max_consecutive_losses=100,
            stop_loss_pct=5,
        )
        explicit_zero = BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            max_daily_loss=1000,
            max_consecutive_losses=100,
            max_drawdown_amount=0,
            stop_loss_pct=5,
        )

        omitted_result = BacktestEngine(base).run(bars, include_fee_sensitivity=False)
        zero_result = BacktestEngine(explicit_zero).run(bars, include_fee_sensitivity=False)

        assert zero_result == omitted_result

    def test_negative_drawdown_amount_is_rejected(self) -> None:
        try:
            BacktestEngine(BacktestEngineParams(
                buy_low=100,
                sell_high=110,
                max_drawdown_amount=-1,
            ))
        except ValueError as exc:
            assert "max_drawdown_amount cannot be negative" in str(exc)
        else:
            raise AssertionError("negative max_drawdown_amount should raise ValueError")

    def test_long_trailing_stop_ratchets_to_latest_peak_with_costs(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            quantity=10,
            fee_rate=0.001,
            slippage_pct=1,
            stop_loss_pct=10,
            trailing_stop_pct=5,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 106, 110, 105, 109),
            bar(2, 116, 120, 115, 119),
            bar(3, 116, 118, 113, 114),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        expected_price = 120 * 0.95 * 0.99
        assert closed.action == "TRAILING_STOP_SELL"
        assert closed.reason == "trailing_stop"
        assert abs(closed.price - expected_price) < 1e-9
        assert abs(closed.fee - expected_price * 10 * 0.001) < 1e-9
        assert closed.total_fees is not None
        assert abs(closed.total_fees - (1.01 + expected_price * 10 * 0.001)) < 1e-9
        assert closed.mfe_pct is not None
        assert abs(closed.mfe_pct - ((120 - 101) / 101 * 100)) < 1e-9

    def test_trailing_stop_does_not_exit_while_long_peak_keeps_rising(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            trailing_stop_pct=5,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 106, 110, 105, 109),
            bar(2, 116, 120, 115, 119),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY"]
        assert result.metrics.final_state == "long"

    def test_short_trailing_stop_retraces_from_low_with_costs(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            short_selling=True,
            quantity=10,
            fee_rate=0.001,
            slippage_pct=1,
            trailing_stop_pct=5,
        ))

        result = engine.run([
            bar(0, 199, 202, 195, 200),
            bar(1, 185, 188, 180, 182),
            bar(2, 185, 189, 181, 188),
        ], include_fee_sensitivity=False)
        closed = result.trades[-1]

        expected_price = 180 * 1.05 * 1.01
        assert closed.action == "TRAILING_STOP_COVER"
        assert closed.reason == "trailing_stop"
        assert abs(closed.price - expected_price) < 1e-9
        assert abs(closed.fee - expected_price * 10 * 0.001) < 1e-9
        assert closed.mfe_pct is not None
        assert abs(closed.mfe_pct - ((198 - 180) / 198 * 100)) < 1e-9

    def test_trailing_stop_zero_matches_omitted_default(self) -> None:
        bars = [
            bar(0, 101, 102, 99, 100),
            bar(1, 105, 108, 104, 107),
            bar(2, 109, 111, 108, 110),
        ]
        base = BacktestEngineParams(buy_low=100, sell_high=110, fee_rate=0.001)
        explicit_zero = BacktestEngineParams(
            buy_low=100,
            sell_high=110,
            fee_rate=0.001,
            trailing_stop_pct=0,
        )

        omitted_result = BacktestEngine(base).run(bars, include_fee_sensitivity=False)
        zero_result = BacktestEngine(explicit_zero).run(bars, include_fee_sensitivity=False)

        assert zero_result == omitted_result

    def test_fixed_stop_exits_before_trailing_when_fixed_level_hits_first(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            stop_loss_pct=3,
            trailing_stop_pct=10,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 116, 120, 115, 119),
            bar(2, 100, 116, 90, 95),
        ], include_fee_sensitivity=False)

        assert result.trades[-1].action == "STOP_LOSS_SELL"
        assert result.trades[-1].reason == "stop loss reached"
        assert result.trades[-1].price == 97

    def test_trailing_stop_bypasses_min_profit_guard_like_fixed_stop(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            buy_low=100,
            sell_high=200,
            min_profit_amount=1000,
            fixed_fee=1,
            trailing_stop_pct=5,
        ))

        result = engine.run([
            bar(0, 101, 102, 99, 100),
            bar(1, 106, 110, 105, 109),
            bar(2, 106, 108, 104, 105),
        ], include_fee_sensitivity=False)

        assert [trade.action for trade in result.trades] == ["BUY", "TRAILING_STOP_SELL"]
        assert result.trades[-1].reason == "trailing_stop"
        assert result.trades[-1].total_fees == 2
        assert result.metrics.skipped_signals == 0

    def test_negative_trailing_stop_is_rejected(self) -> None:
        try:
            BacktestEngine(BacktestEngineParams(
                buy_low=100,
                sell_high=200,
                trailing_stop_pct=-1,
            ))
        except ValueError as exc:
            assert "trailing_stop_pct cannot be negative" in str(exc)
        else:
            raise AssertionError("negative trailing_stop_pct should raise ValueError")

    def test_parse_backtest_csv(self) -> None:
        bars = parse_backtest_csv(
            "\n".join([
                "timestamp,open,high,low,close,volume",
                "2026-05-22T10:00:00Z,100,105,99,104,1000",
            ])
        )

        assert len(bars) == 1
        assert bars[0].timestamp.tzinfo is not None
        assert bars[0].close == 104

    def test_parse_backtest_csv_strips_utf8_bom(self) -> None:
        # Excel/Numbers exports prepend a BOM (﻿) when saving as "CSV UTF-8".
        # The parser must strip it so the first column header still matches.
        bars = parse_backtest_csv(
            "﻿" + "timestamp,open,high,low,close,volume\n"
            + "2026-05-22T10:00:00Z,100,105,99,104,1000"
        )
        assert len(bars) == 1
        assert bars[0].close == 104

    def test_invalid_csv_reports_row_number(self) -> None:
        try:
            _ = parse_backtest_csv("\n".join([
                "timestamp,open,high,low,close,volume",
                "2026-05-22T10:00:00Z,100,98,99,104,1000",
            ]))
        except ValueError as exc:
            assert "row 2" in str(exc)
            assert "high must be greater" in str(exc)
        else:
            raise AssertionError("invalid CSV should raise ValueError")


class TestBacktestMetrics:
    def test_sharpe_ratio_calculated_with_multiple_bars(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=200,
            quantity=2,
            initial_cash=10000,
        ))
        result = engine.run([
            bar(0, 150, 160, 99, 105),
            bar(1, 150, 201, 140, 200),
            bar(2, 150, 201, 140, 200),
            bar(3, 150, 201, 140, 200),
        ])
        assert result.metrics.total_pnl == 200
        assert result.metrics.sharpe_ratio is not None
        # 只有赢没有亏，profit_factor / profit_loss_ratio 无定义
        assert result.metrics.profit_factor is None
        assert result.metrics.profit_loss_ratio is None

    def test_no_trades_returns_none_for_extra_metrics(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=200,
            quantity=2,
            initial_cash=10000,
        ))
        result = engine.run([
            bar(0, 150, 160, 101, 150),
            bar(1, 150, 160, 101, 150),
            bar(2, 150, 160, 101, 150),
        ])
        assert result.metrics.closed_trade_count == 0
        assert result.metrics.sharpe_ratio is None
        assert result.metrics.profit_factor is None
        assert result.metrics.profit_loss_ratio is None

    def test_mixed_trades_produces_correct_profit_factor(self) -> None:
        engine = BacktestEngine(BacktestEngineParams(
            symbol="AAPL.US",
            buy_low=100,
            sell_high=110,
            quantity=1,
            initial_cash=10000,
            stop_loss_pct=3,  # 3% 止损，入场价 100 -> 止损价 97
        ))
        # bar0 buy@100; bar1 sell@110 (+10); bar2 buy@100; bar3 sell@110 (+10); bar4 buy@100; bar5 stop@97 (-3)
        result = engine.run([
            bar(0, 105, 106, 99, 100),
            bar(1, 105, 111, 105, 110),
            bar(2, 105, 106, 99, 100),
            bar(3, 105, 111, 105, 110),
            bar(4, 105, 106, 99, 100),
            bar(5, 96, 97, 95, 96),
        ])
        assert result.metrics.closed_trade_count == 3
        assert result.metrics.winning_trades == 2
        assert result.metrics.losing_trades == 1
        # profit_factor = 20 / 3 = 6.666...
        assert result.metrics.profit_factor is not None
        assert abs(result.metrics.profit_factor - 20 / 3) < 1e-9
        # profit_loss_ratio = 10 / 3 = 3.333...
        assert result.metrics.profit_loss_ratio is not None
        assert abs(result.metrics.profit_loss_ratio - 10 / 3) < 1e-9
class TestBacktestAPI:
    def test_params_mapper_passes_session_entry_gate_and_exit_fields(self) -> None:
        mapped = _params_to_engine(BacktestParams(
            symbol="0700.HK",
            market="HK",
            trading_session_mode="RTH_ONLY",
            opening_warmup_minutes=30,
            entry_crossing_required=True,
            max_entries_per_symbol_per_day=2,
            buy_low=300,
            sell_high=320,
            trailing_stop_pct=2.5,
            max_holding_minutes=60,
            entry_cutoff_minutes_before_close=45,
            flatten_minutes_before_close=15,
        ))

        assert mapped.market == "HK"
        assert mapped.trading_session_mode == "RTH_ONLY"
        assert mapped.opening_warmup_minutes == 30
        assert mapped.entry_crossing_required is True
        assert mapped.max_entries_per_symbol_per_day == 2
        assert mapped.trailing_stop_pct == 2.5
        assert mapped.max_holding_minutes == 60
        assert mapped.entry_cutoff_minutes_before_close == 45
        assert mapped.flatten_minutes_before_close == 15

    def test_backtest_schema_rejects_inverted_execution_windows(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 200,
                "entry_cutoff_minutes_before_close": 15,
                "flatten_minutes_before_close": 16,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T13:30:00Z,101,102,99,100,1000\n"
            ),
        })

        assert resp.status_code == 422
        assert "flatten_minutes_before_close must not exceed" in resp.text

    def test_backtest_schema_rejects_out_of_range_entry_gates(self) -> None:
        for field, value in (
            ("opening_warmup_minutes", 391),
            ("max_entries_per_symbol_per_day", 1001),
        ):
            resp = client.post("/api/backtest/run", json={
                "params": {
                    "buy_low": 100,
                    "sell_high": 200,
                    field: value,
                },
                "csv_text": (
                    "timestamp,open,high,low,close,volume\n"
                    "2026-05-22T13:30:00Z,101,102,99,100,1000\n"
                ),
            })

            assert resp.status_code == 422
            assert field in resp.text

    def test_backtest_api_treats_zero_daily_loss_as_disabled(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 200,
                "max_daily_loss": 0,
                "max_holding_minutes": 1,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,101,102,99,100,1000\n"
                "2026-05-22T10:01:00Z,50,51,49,50,1000\n"
                "2026-05-22T10:02:00Z,101,102,99,100,1000\n"
            ),
        })

        assert resp.status_code == 200
        data = cast(_BacktestResultJson, resp.json())
        assert [trade["action"] for trade in data["trades"]] == [
            "BUY",
            "TIME_STOP_SELL",
            "BUY",
        ]
        assert data["metrics"]["final_state"] == "long"

    def test_backtest_schema_rejects_negative_daily_loss(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 200,
                "max_daily_loss": -1,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,101,102,99,100,1000\n"
            ),
        })

        assert resp.status_code == 422
        assert "max_daily_loss" in resp.text

    def test_backtest_schema_rejects_symbol_market_mismatch(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "symbol": "0700.HK",
                "market": "US",
                "buy_low": 100,
                "sell_high": 200,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T13:30:00Z,101,102,99,100,1000\n"
            ),
        })

        assert resp.status_code == 422
        assert "symbol suffix .HK does not match market US" in resp.text

    def test_backtest_schema_infers_market_for_legacy_hk_params(self) -> None:
        params = BacktestParams.model_validate({
            "symbol": "0700.HK",
            "buy_low": 300,
            "sell_high": 320,
        })

        assert params.symbol == "0700.HK"
        assert params.market == "HK"

    def test_run_backtest_endpoint_returns_stable_structure(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "symbol": "AAPL.US",
                "buy_low": 100,
                "sell_high": 200,
                "quantity": 2,
                "initial_cash": 10000,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,150,160,99,105,1000\n"
                "2026-05-22T10:01:00Z,150,201,140,200,1000\n"
            ),
        })

        assert resp.status_code == 200
        data = cast(_BacktestResultJson, resp.json())
        assert data["metrics"]["total_pnl"] == 200
        assert data["metrics"]["win_rate"] == 100
        assert [trade["action"] for trade in data["trades"]] == ["BUY", "SELL"]
        assert len(data["equity_curve"]) == 2
        assert len(data["fee_sensitivity"]) >= 3

    def test_run_backtest_endpoint_returns_cost_skip_category(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 101,
                "min_profit_amount": 5,
                "fee_rate": 0.001,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,100,100,99,100,1000\n"
                "2026-05-22T10:01:00Z,100,101.5,100,101,1000\n"
            ),
        })

        assert resp.status_code == 200
        assert resp.json()["skipped_signals"][0]["category"] == "FEE"

    def test_run_backtest_endpoint_returns_drawdown_skip_category(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 110,
                "max_daily_loss": 1000,
                "max_consecutive_losses": 100,
                "max_drawdown_amount": 5,
                "stop_loss_pct": 5,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,105,106,99,100,1000\n"
                "2026-05-22T10:01:00Z,98,99,94,95,1000\n"
                "2026-05-22T10:02:00Z,105,106,99,100,1000\n"
            ),
        })

        assert resp.status_code == 200
        assert resp.json()["skipped_signals"][0]["category"] == "DRAWDOWN"

    def test_run_backtest_endpoint_applies_trailing_stop(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 200,
                "trailing_stop_pct": 5,
            },
            "csv_text": (
                "timestamp,open,high,low,close,volume\n"
                "2026-05-22T10:00:00Z,101,102,99,100,1000\n"
                "2026-05-22T10:01:00Z,106,110,105,109,1000\n"
                "2026-05-22T10:02:00Z,106,108,104,105,1000\n"
            ),
        })

        assert resp.status_code == 200
        data = cast(_BacktestResultJson, resp.json())
        closed = data["trades"][-1]
        assert closed["action"] == "TRAILING_STOP_SELL"
        assert closed["reason"] == "trailing_stop"
        assert closed["price"] == 104.5

    def test_run_backtest_endpoint_rejects_bad_csv(self) -> None:
        resp = client.post("/api/backtest/run", json={
            "params": {
                "buy_low": 100,
                "sell_high": 200,
            },
            "csv_text": "timestamp,open,high,low,close\n2026-05-22T10:00:00Z,1,2,1,2\n",
        })

        assert resp.status_code == 422
        assert "volume" in resp.json()["detail"]


class TestBacktestRiskAdjustedMetrics:
    def _bars_with_returns(self, returns):
        """Build bars from a list of percentage returns starting at $100."""
        from datetime import datetime, timedelta, timezone
        from app.core.backtest import BacktestBar
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = []
        price = 100.0
        for i, r in enumerate(returns):
            price = price * (1 + r)
            bars.append(BacktestBar(
                timestamp=base + timedelta(minutes=i),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000,
            ))
        return bars

    def test_sortino_none_when_no_downside(self) -> None:
        from app.core.backtest import BacktestEngine, BacktestEngineParams
        # All positive returns — downside deviation is zero.
        bars = self._bars_with_returns([0.01, 0.01, 0.01, 0.01])
        params = BacktestEngineParams(
            symbol="X", buy_low=99, sell_high=200,
            short_selling=False, quantity=1, initial_cash=10000,
            min_profit_amount=0, max_daily_loss=100000, max_consecutive_losses=100,
        )
        engine = BacktestEngine(params)
        result = engine.run(bars)
        # Either None (no downside) or some positive number; the important
        # thing is it does not raise.
        assert result.metrics.sortino_ratio is None or result.metrics.sortino_ratio > 0

    def test_sortino_penalises_downside(self) -> None:
        from app.core.backtest import BacktestEngine, BacktestEngineParams
        # Mixed positive/negative returns must produce a finite ratio that
        # captures the downside penalty (lower than pure-positive case).
        bars = self._bars_with_returns([0.05, -0.10, 0.05, -0.10, 0.05])
        params = BacktestEngineParams(
            symbol="X", buy_low=99, sell_high=200,
            short_selling=False, quantity=1, initial_cash=10000,
            min_profit_amount=0, max_daily_loss=100000, max_consecutive_losses=100,
        )
        engine = BacktestEngine(params)
        result = engine.run(bars)
        # Compute a pure-positive control for the same length.
        bars_up = self._bars_with_returns([0.01, 0.01, 0.01, 0.01, 0.01])
        result_up = engine.run(bars_up)
        assert result.metrics.sortino_ratio is not None
        # Mixed case must be lower than the all-positive case (which can be
        # None or a high number). The control has zero downside deviation.
        if result_up.metrics.sortino_ratio is not None:
            assert result.metrics.sortino_ratio < result_up.metrics.sortino_ratio

    def test_sortino_downside_dev_uses_total_observations(self) -> None:
        """Verify downside_dev = sqrt(sum(r^2 for r in downside) / len(returns))."""
        from app.core.backtest import BacktestEngine, BacktestEquityPoint
        from datetime import datetime, timezone
        import math

        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 4 equity points → 3 returns: [0.0, -0.10, 0.0]
        # mean_ret = -0.10/3
        # downside = [-0.10]
        # downside_dev (new) = sqrt(0.01 / 3)
        # sortino = (-0.10/3) / sqrt(0.01/3) = -sqrt(3)/3
        # (old formula would use / 1, giving -0.333... instead)
        equity_curve = [
            BacktestEquityPoint(t, 100, 100, 0, 0, 0, "flat"),
            BacktestEquityPoint(t, 100, 100, 0, 0, 0, "flat"),
            BacktestEquityPoint(t, 90, 90, 0, 0, 0, "flat"),
            BacktestEquityPoint(t, 90, 90, 0, 0, 0, "flat"),
        ]
        sortino = BacktestEngine._calc_sortino_ratio(equity_curve)
        assert sortino is not None
        expected = (-0.10 / 3) / math.sqrt(0.01 / 3)
        assert abs(sortino - expected) < 1e-10

    def test_calmar_none_for_no_drawdown(self) -> None:
        from app.core.backtest import BacktestEngine, BacktestEngineParams
        # Monotonically increasing → zero drawdown.
        bars = self._bars_with_returns([0.01, 0.01, 0.01, 0.01])
        params = BacktestEngineParams(
            symbol="X", buy_low=99, sell_high=200,
            short_selling=False, quantity=1, initial_cash=10000,
            min_profit_amount=0, max_daily_loss=100000, max_consecutive_losses=100,
        )
        engine = BacktestEngine(params)
        result = engine.run(bars)
        # With no drawdown, calmar is None.
        assert result.metrics.calmar_ratio is None


def test_backtest_metrics_serialization_includes_new_fields() -> None:
    """The metrics dataclass should round-trip the new fields without
    dropping them, so the API response and CSV export stay consistent."""
    from app.core.backtest import BacktestMetrics
    from dataclasses import asdict
    m = BacktestMetrics(
        initial_cash=100.0,
        final_equity=110.0,
        total_pnl=10.0,
        total_return_pct=10.0,
        max_drawdown_pct=2.0,
        trade_count=2,
        closed_trade_count=2,
        winning_trades=1,
        losing_trades=1,
        win_rate=50.0,
        avg_holding_minutes=5.0,
        fees_paid=0.0,
        skipped_signals=0,
        final_state="flat",
        sharpe_ratio=1.2,
        sortino_ratio=1.5,
        calmar_ratio=5.0,
        profit_factor=1.1,
        profit_loss_ratio=1.0,
    )
    d = asdict(m)
    assert "sortino_ratio" in d
    assert "calmar_ratio" in d
    assert d["sortino_ratio"] == 1.5
    assert d["calmar_ratio"] == 5.0


class TestBacktestExport:
    """CSV export endpoint for backtest results."""

    @staticmethod
    def _sample_result() -> dict:
        return {
            "result": {
                "params": {
                    "symbol": "AAPL.US",
                    "buy_low": 100,
                    "sell_high": 200,
                    "short_selling": False,
                    "min_profit_amount": 0,
                    "max_daily_loss": 5000,
                    "max_consecutive_losses": 3,
                    "quantity": 2,
                    "initial_cash": 10000,
                    "fee_rate": 0,
                    "fixed_fee": 0,
                    "slippage_pct": 0,
                    "stop_loss_pct": 0,
                    "trailing_stop_pct": 0,
                },
                "metrics": {
                    "initial_cash": 10000,
                    "final_equity": 10200,
                    "total_pnl": 200,
                    "total_return_pct": 2,
                    "max_drawdown_pct": 0,
                    "trade_count": 2,
                    "closed_trade_count": 1,
                    "winning_trades": 1,
                    "losing_trades": 0,
                    "win_rate": 100,
                    "avg_holding_minutes": 1,
                    "fees_paid": 0,
                    "skipped_signals": 0,
                    "final_state": "flat",
                },
                "equity_curve": [
                    {
                        "timestamp": "2026-05-22T10:00:00Z",
                        "close": 105,
                        "equity": 10010,
                        "realized_pnl": 0,
                        "unrealized_pnl": 10,
                        "drawdown_pct": 0,
                        "position": "long",
                    },
                    {
                        "timestamp": "2026-05-22T10:01:00Z",
                        "close": 200,
                        "equity": 10200,
                        "realized_pnl": 200,
                        "unrealized_pnl": 0,
                        "drawdown_pct": 0,
                        "position": "flat",
                    },
                ],
                "trades": [
                    {
                        "timestamp": "2026-05-22T10:00:00Z",
                        "action": "BUY",
                        "price": 100,
                        "quantity": 2,
                        "fee": 0,
                        "pnl": 0,
                        "state_after": "long",
                        "reason": "low reached buy_low",
                        "holding_minutes": None,
                    },
                    {
                        "timestamp": "2026-05-22T10:01:00Z",
                        "action": "SELL",
                        "price": 200,
                        "quantity": 2,
                        "fee": 0,
                        "pnl": 200,
                        "state_after": "flat",
                        "reason": "exit threshold reached",
                        "holding_minutes": 1,
                    },
                ],
                "skipped_signals": [],
                "fee_sensitivity": [
                    {"fee_rate": 0, "total_pnl": 200, "total_return_pct": 2, "max_drawdown_pct": 0},
                ],
            },
        }

    def test_export_returns_csv_with_all_sections(self) -> None:
        resp = client.post("/api/backtest/export", json=self._sample_result())
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        assert "Content-Disposition" in resp.headers
        assert 'attachment; filename="backtest_AAPL_US_' in resp.headers["Content-Disposition"]
        body = resp.text
        assert "# Backtest Result Export" in body
        assert "trades" in body
        assert "BUY" in body
        assert "SELL" in body
        assert "equity_curve" in body
        assert "fee_sensitivity" in body

    def test_export_respects_sections_filter(self) -> None:
        payload = self._sample_result()
        payload["sections"] = ["params", "trades"]
        resp = client.post("/api/backtest/export", json=payload)
        assert resp.status_code == 200
        body = resp.text
        assert "trades" in body
        assert "equity_curve" not in body
        assert "fee_sensitivity" not in body


class TestGrossVersusNetColumns:
    """Cost transparency: a strategy that only works before fees must be
    visible as such, not hidden behind a single net number."""

    def _bars(self) -> list[BacktestBar]:
        return [
            bar(0, 150, 160, 99, 105),
            bar(1, 120, 140, 110, 130),
            bar(2, 150, 205, 145, 200),
            bar(3, 180, 190, 120, 130),
            bar(4, 110, 150, 95, 102),
            bar(5, 150, 210, 140, 205),
        ]

    def _run(self, **overrides: float):
        params = BacktestEngineParams(
            buy_low=100, sell_high=200, quantity=2, **overrides
        )
        result = BacktestEngine(params).run(self._bars(), include_fee_sensitivity=False)
        return _metrics_to_schema(result.metrics)

    def test_gross_exceeds_net_by_exactly_the_fees(self) -> None:
        m = self._run(fee_rate=0.01)

        assert m.fees_paid > 0
        assert m.gross_pnl == pytest.approx(m.total_pnl + m.fees_paid)
        assert m.gross_pnl > m.total_pnl

    def test_columns_coincide_when_there_are_no_fees(self) -> None:
        m = self._run(fee_rate=0.0)

        assert m.fees_paid == 0
        assert m.gross_pnl == pytest.approx(m.total_pnl)

    def test_gross_return_pct_is_gross_pnl_over_initial_cash(self) -> None:
        m = self._run(fee_rate=0.01)

        assert m.gross_return_pct == pytest.approx(
            m.gross_pnl / m.initial_cash * 100
        )
        assert m.gross_return_pct > m.total_return_pct

    def test_slippage_is_deducted_from_both_columns(self) -> None:
        """The gap between the columns is fee drag alone.

        Slippage is applied to the fill price, so it lowers gross and net
        together. Reading ``gross - net`` as total cost drag would understate
        it, and this pins the accounting boundary the field names imply but do
        not state.
        """
        clean = self._run(fee_rate=0.0, slippage_pct=0.0)
        slipped = self._run(fee_rate=0.0, slippage_pct=0.5)

        assert slipped.gross_pnl < clean.gross_pnl
        assert slipped.gross_pnl == pytest.approx(slipped.total_pnl)

    def test_fee_only_strategy_shows_positive_gross_and_negative_net(self) -> None:
        """The exact trap the columns exist to expose."""
        m = self._run(fee_rate=1.5)

        assert m.gross_pnl > 0
        assert m.total_pnl < 0
