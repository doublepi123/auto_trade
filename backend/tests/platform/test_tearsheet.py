from __future__ import annotations

from decimal import Decimal

from app.platform.backtest_service import PlatformBacktestService
from app.platform.tearsheet import TearsheetBuilder, TearsheetExporter


def _bars():
    from datetime import datetime, timezone

    return [
        {
            "timestamp": datetime(2026, 6, 23, 10, i, tzinfo=timezone.utc).isoformat(),
            "symbol": "AAPL.US",
            "open": 150,
            "high": 160,
            "low": 140,
            "close": 144 if i % 2 == 0 else 156,
            "volume": 1000,
        }
        for i in range(4)
    ]


def _run() -> dict:
    return PlatformBacktestService().run(
        strategy_name="interval",
        params={"buy_low": 145, "sell_high": 155, "quantity": 10},
        symbols=["AAPL.US"],
        bars=_bars(),
        initial_cash=Decimal("10000"),
    )


def test_build_tearsheet_has_all_sections() -> None:
    ts = TearsheetBuilder().build(_run())
    for section in ("summary", "equity_curve", "returns", "drawdown", "trades"):
        assert section in ts
    assert "sharpe" in ts["summary"]
    assert "max_drawdown" in ts["drawdown"]
    assert ts["summary"]["num_bars"] == 4


def test_export_json_roundtrips() -> None:
    import json

    ts = TearsheetBuilder().build(_run())
    blob = TearsheetExporter.to_json(ts)
    restored = json.loads(blob)
    assert "summary" in restored and "sharpe" in restored["summary"]


def test_export_csv_contains_sections() -> None:
    ts = TearsheetBuilder().build(_run())
    csv_text = TearsheetExporter.to_csv(ts)
    for marker in ("summary", "equity_curve", "returns", "drawdown", "trades"):
        assert marker in csv_text
