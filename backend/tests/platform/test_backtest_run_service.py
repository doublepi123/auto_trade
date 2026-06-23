from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database import engine
from app.models import Base
from app.platform.backtest_run_service import BacktestRunService


def _setup() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _bars() -> list[dict]:
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


def test_create_and_get_run() -> None:
    _setup()
    with Session(engine) as db:
        svc = BacktestRunService(db)
        row = svc.create(
            name="r1",
            strategy_name="interval",
            params={"buy_low": 145, "sell_high": 155, "quantity": 10},
            symbols=["AAPL.US"],
            bars=_bars(),
            initial_cash=Decimal("10000"),
        )
        assert row.id is not None
        fetched = svc.get_run(row.id)
    assert fetched is not None
    assert fetched["name"] == "r1"
    assert "result" in fetched and "equity_curve" in fetched["result"]


def test_list_runs_newest_first() -> None:
    _setup()
    with Session(engine) as db:
        svc = BacktestRunService(db)
        svc.create(
            name="a",
            strategy_name="interval",
            params={"buy_low": 145, "sell_high": 155, "quantity": 10},
            symbols=["AAPL.US"],
            bars=_bars(),
            initial_cash=Decimal("10000"),
        )
        svc.create(
            name="b",
            strategy_name="interval",
            params={"buy_low": 145, "sell_high": 155, "quantity": 10},
            symbols=["AAPL.US"],
            bars=_bars(),
            initial_cash=Decimal("10000"),
        )
        runs = svc.list_runs()
    assert len(runs) == 2
    assert runs[0]["name"] == "b"  # newest first


def test_compare_returns_metrics() -> None:
    _setup()
    with Session(engine) as db:
        svc = BacktestRunService(db)
        r1 = svc.create(
            name="a",
            strategy_name="interval",
            params={"buy_low": 145, "sell_high": 155, "quantity": 10},
            symbols=["AAPL.US"],
            bars=_bars(),
            initial_cash=Decimal("10000"),
        )
        comparison = svc.compare([r1.id])
    assert len(comparison) == 1
    assert "sharpe" in comparison[0] and "max_drawdown" in comparison[0]
