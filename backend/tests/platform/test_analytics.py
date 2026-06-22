from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.analytics import PerformanceAnalytics
from app.platform.events import EventSource, FillEvent


def test_sharpe_positive_for_growing_curve():
    equity = [10000 * (1.001 ** i) for i in range(30)]  # steadily rising
    metrics = PerformanceAnalytics(periods_per_year=252).equity_metrics(equity)
    assert metrics["total_return"] > 0
    assert metrics["sharpe"] > 0
    assert metrics["max_drawdown"] >= 0


def test_max_drawdown_measured():
    equity = [100.0, 110.0, 90.0, 95.0]  # peak 110 -> trough 90 => dd ~0.1818
    metrics = PerformanceAnalytics(periods_per_year=252).equity_metrics(equity)
    assert abs(metrics["max_drawdown"] - (110 - 90) / 110) < 1e-9


def test_trade_metrics_win_rate_and_profit_factor():
    fills = [
        FillEvent(timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc), source=EventSource.BROKER, symbol="A", broker_order_id="1", side="BUY", quantity=10, price=Decimal("100"), commission=Decimal("0")),
        FillEvent(timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc), source=EventSource.BROKER, symbol="A", broker_order_id="2", side="SELL", quantity=10, price=Decimal("120"), commission=Decimal("0")),
        FillEvent(timestamp=datetime(2026, 6, 22, 10, 2, tzinfo=timezone.utc), source=EventSource.BROKER, symbol="A", broker_order_id="3", side="BUY", quantity=10, price=Decimal("100"), commission=Decimal("0")),
        FillEvent(timestamp=datetime(2026, 6, 22, 10, 3, tzinfo=timezone.utc), source=EventSource.BROKER, symbol="A", broker_order_id="4", side="SELL", quantity=10, price=Decimal("95"), commission=Decimal("0")),
    ]
    metrics = PerformanceAnalytics().trade_metrics(fills)
    assert metrics["num_trades"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["profit_factor"] == 200.0 / 50.0  # 4.0


def test_empty_equity_returns_zeros():
    metrics = PerformanceAnalytics().equity_metrics([])
    assert metrics["sharpe"] == 0.0
    assert metrics["max_drawdown"] == 0.0
