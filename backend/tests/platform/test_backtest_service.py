from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.platform.backtest_service import PlatformBacktestService


def _bar(symbol: str, close: str, ts_minute: int) -> dict:
    return {
        "timestamp": datetime(2026, 6, 22, 10, ts_minute, tzinfo=timezone.utc),
        "symbol": symbol,
        "open": Decimal("150"),
        "high": Decimal("160"),
        "low": Decimal("140"),
        "close": Decimal(close),
        "volume": 1000,
    }


def test_backtest_runs_interval_strategy_and_reports_equity():
    service = PlatformBacktestService()
    result = service.run(
        strategy_name="interval",
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10},
        symbols=["AAPL.US"],
        bars=[
            _bar("AAPL.US", "144", 0),  # buy trigger (close 144 <= buy_low 145)
            _bar("AAPL.US", "156", 1),  # sell trigger (close 156 >= sell_high 155)
        ],
        initial_cash=Decimal("10000"),
    )
    assert len(result["fills"]) == 2
    assert result["fills"][0]["side"] == "BUY"
    assert result["fills"][1]["side"] == "SELL"
    assert result["final_positions"]["AAPL.US"] == 0
    assert len(result["equity_curve"]) == 2
    assert "nav" in result["equity_curve"][0]
    assert result["stats"]["num_bars"] == 2
    assert result["stats"]["num_fills"] == 2
    # Round-trip is profitable (buy 144 -> sell 156) but commissions + slippage
    # reduce NAV below the theoretical no-friction profit of 120.
    assert result["stats"]["final_nav"] > 10000
    assert result["stats"]["final_nav"] < 10000 + 120
    # Friction (commission + slippage) is non-zero.
    buy_fill = result["fills"][0]
    sell_fill = result["fills"][1]
    assert Decimal(str(buy_fill["commission"])) > 0
    assert Decimal(str(sell_fill["commission"])) > 0


def test_backtest_unknown_strategy_raises():
    service = PlatformBacktestService()
    with pytest.raises(KeyError):
        service.run(
            strategy_name="nope",
            params={},
            symbols=["AAPL.US"],
            bars=[_bar("AAPL.US", "150", 0)],
        )


def test_backtest_records_equity_snapshot_per_bar():
    service = PlatformBacktestService()
    result = service.run(
        strategy_name="interval",
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10},
        symbols=["AAPL.US"],
        bars=[
            _bar("AAPL.US", "144", 0),
            _bar("AAPL.US", "143", 1),
            _bar("AAPL.US", "142", 2),
        ],
        initial_cash=Decimal("10000"),
    )
    # Three bars -> three equity snapshots; NAV after bar 0 reflects the buy
    # (cash spent, position gained); mark-to-market uses last_close per bar.
    assert len(result["equity_curve"]) == 3
    # Only one fill expected (single BUY at bar 0; bars 1/2 don't trigger SELL
    # since position > 0 but close < sell_high).
    assert result["stats"]["num_fills"] == 1
    # Equity-curve nav values are floats.
    for snap in result["equity_curve"]:
        assert isinstance(snap["nav"], float)


def test_backtest_reports_realized_pnl():
    service = PlatformBacktestService()
    result = service.run(
        strategy_name="interval",
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10},
        symbols=["AAPL.US"],
        bars=[
            {"timestamp": datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc), "symbol": "AAPL.US", "open": Decimal("150"), "high": Decimal("160"), "low": Decimal("140"), "close": Decimal("144"), "volume": 1000},
            {"timestamp": datetime(2026, 6, 23, 10, 1, tzinfo=timezone.utc), "symbol": "AAPL.US", "open": Decimal("150"), "high": Decimal("160"), "low": Decimal("140"), "close": Decimal("156"), "volume": 1000},
        ],
        initial_cash=Decimal("10000"),
    )
    assert "realized_pnl" in result["stats"]
    # round-trip realized should be positive (buy ~144, sell ~156)
    assert result["stats"]["realized_pnl"] > 0
    assert result["final_positions"]["AAPL.US"] == 0
