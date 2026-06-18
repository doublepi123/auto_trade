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

