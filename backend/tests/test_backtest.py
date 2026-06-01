import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_backtest.db"

from datetime import datetime, timezone
from typing import TypedDict, cast

from fastapi.testclient import TestClient

from app.core.backtest import BacktestBar, BacktestEngine, BacktestEngineParams, parse_backtest_csv
from app.main import app

client = TestClient(app)


class _BacktestMetricsJson(TypedDict):
    total_pnl: float
    win_rate: float


class _BacktestTradeJson(TypedDict):
    action: str


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


class TestBacktestEngine:
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

        assert [trade.action for trade in result.trades] == ["BUY", "STOP_LOSS_SELL"]
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
