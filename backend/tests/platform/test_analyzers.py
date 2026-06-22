from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.analyzers import (
    DrawDownAnalyzer,
    ReturnsAnalyzer,
    TradeAnalyzer,
    analyze_backtest,
)
from app.platform.events import EventSource, FillEvent


def _fill(side, qty, price, commission="0"):
    return FillEvent(
        timestamp=datetime(2026, 6, 23, 10, 0, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol="A",
        broker_order_id="o",
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal(commission),
    )


def test_trade_analyzer_pairs_fifo_and_summarizes():
    fills = [
        _fill("BUY", 10, "100"),
        _fill("SELL", 10, "130"),
        _fill("BUY", 10, "100"),
        _fill("SELL", 10, "90"),
    ]
    result = TradeAnalyzer().analyze(fills)
    assert result["num_trades"] == 2
    assert result["win_rate"] == 0.5
    assert result["largest_win"] == 300.0
    assert result["largest_loss"] == -100.0
    assert result["profit_factor"] == 300.0 / 100.0
    assert result["expectancy"] == 100.0  # (300 - 100)/2


def test_drawdown_analyzer_underwater_curve():
    equity = [100, 110, 90, 95, 105]
    result = DrawDownAnalyzer().analyze(equity)
    # peak 110 at idx1, trough 90 at idx2 -> dd = 20/110
    assert abs(result["max_drawdown"] - 20 / 110) < 1e-9
    assert result["underwater"][2] > 0
    assert result["underwater"][0] == 0.0


def test_returns_analyzer_distribution():
    equity = [100, 110, 99]  # +10%, -10%
    result = ReturnsAnalyzer().analyze(equity)
    assert result["num_periods"] == 2
    assert abs(result["best_period"] - 0.10) < 1e-9
    assert abs(result["worst_period"] + 0.10) < 1e-9
    assert result["positive_pct"] == 0.5
    assert abs(result["cumulative_return"] - (99 / 100 - 1)) < 1e-9


def test_analyze_backtest_combines_all():
    result = {
        "equity_curve": [{"nav": 10000}, {"nav": 10500}, {"nav": 10200}],
        "fills": [_fill("BUY", 10, "100"), _fill("SELL", 10, "120")],
    }
    out = analyze_backtest(result)
    assert "trades" in out and "drawdown" in out and "returns" in out
    assert out["trades"]["num_trades"] == 1
    assert len(out["drawdown"]["underwater"]) == 3
    assert out["returns"]["num_periods"] == 2


def test_trade_analyzer_accepts_fill_dicts():
    fill = _fill("BUY", 10, "100")
    d = fill.to_dict()
    # round-trip via from_dict to ensure dict path works
    result = TradeAnalyzer().analyze([d])
    assert result["num_trades"] == 0  # only a BUY, no SELL -> no completed trade
