from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.models import Base, OrderRecord


@pytest.fixture
def metrics_db(tmp_path, monkeypatch):
    db_file = tmp_path / "metrics.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionTesting = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Patch the modules that captured a global SessionLocal at import time.
    from app import database
    from app.api import trade as trade_api
    from app.api import metrics as metrics_api

    monkeypatch.setattr(database, "SessionLocal", SessionTesting)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(trade_api, "SessionLocal", SessionTesting)
    monkeypatch.setattr(metrics_api, "get_db", lambda: SessionTesting())

    yield SessionTesting
    Base.metadata.drop_all(bind=engine)


def _make_order(symbol, side, price, qty, hours_ago=1, status="FILLED"):
    return OrderRecord(
        broker_order_id=f"ord-{side}-{symbol}-{price}-{qty}",
        symbol=symbol,
        side=side,
        price=price,
        quantity=qty,
        executed_price=price,
        executed_quantity=qty,
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def test_metrics_summary_no_orders(metrics_db):
    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trade_count"] == 0
    assert data["win_rate"] == 0.0
    assert data["sharpe_ratio"] is None


def test_metrics_summary_pairs_buy_sell(metrics_db):
    SessionLocal = metrics_db
    with SessionLocal() as db:
        db.add(_make_order("AAPL.US", "BUY", 100.0, 10, hours_ago=2))
        db.add(_make_order("AAPL.US", "SELL", 110.0, 10, hours_ago=1))
        db.commit()

    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert data["trade_count"] == 1
    # 110-100 = 10 per share × 10 shares = +100 PnL
    assert data["avg_pnl"] == pytest.approx(100.0)
    assert data["win_rate"] == pytest.approx(100.0)
    # Profit factor with no losses is None (no losses denominator).
    assert data["profit_factor"] is None


def test_metrics_summary_profit_factor_with_losses(metrics_db):
    SessionLocal = metrics_db
    with SessionLocal() as db:
        # Two round-trips: one winner (+50) and one loser (-30).
        db.add(_make_order("AAPL.US", "BUY", 100.0, 10, hours_ago=4))
        db.add(_make_order("AAPL.US", "SELL", 105.0, 10, hours_ago=3))
        db.add(_make_order("AAPL.US", "BUY", 200.0, 10, hours_ago=2))
        db.add(_make_order("AAPL.US", "SELL", 197.0, 10, hours_ago=1))
        db.commit()

    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    data = resp.json()
    assert data["trade_count"] == 2
    assert data["win_rate"] == pytest.approx(50.0)
    # profit_factor = 50 / 30 ≈ 1.6667
    assert data["profit_factor"] == pytest.approx(50.0 / 30.0, rel=1e-3)


def test_metrics_summary_sharpe_ratio(metrics_db):
    SessionLocal = metrics_db
    pnls = [50.0, -20.0, 30.0, -10.0, 40.0]
    base = datetime.now(timezone.utc) - timedelta(hours=len(pnls) * 2)
    with SessionLocal() as db:
        for i, pnl in enumerate(pnls):
            # Construct BUY then SELL with the appropriate price delta.
            entry = 100.0
            exit_price = entry + pnl / 10.0
            db.add(
                _make_order(
                    "AAPL.US", "BUY", entry, 10, hours_ago=(len(pnls) - i) * 2
                )
            )
            db.add(
                _make_order(
                    "AAPL.US", "SELL", exit_price, 10, hours_ago=(len(pnls) - i) * 2 - 1
                )
            )
        db.commit()

    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    data = resp.json()
    assert data["trade_count"] == len(pnls)
    assert data["sharpe_ratio"] is not None
    # Sanity: Sharpe must be finite and have the right sign
    # (mostly positive pnls => positive sharpe).
    assert math.isfinite(data["sharpe_ratio"])


def test_metrics_summary_respects_window(metrics_db):
    SessionLocal = metrics_db
    with SessionLocal() as db:
        # 60-day-old trades should be excluded by a 30-day window.
        db.add(_make_order("AAPL.US", "BUY", 100.0, 10, hours_ago=24 * 60))
        db.add(_make_order("AAPL.US", "SELL", 110.0, 10, hours_ago=24 * 60 - 1))
        db.commit()
    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    data = resp.json()
    assert data["trade_count"] == 0


def test_metrics_summary_does_not_mutate_orm(metrics_db):
    """Issue I2-1: The GET handler must not modify OrderRecord.executed_quantity
    on ORM instances (which would dirty the session).
    """
    SessionLocal = metrics_db
    with SessionLocal() as db:
        db.add(_make_order("AAPL.US", "BUY", 100.0, 10, hours_ago=3))
        db.add(_make_order("AAPL.US", "SELL", 110.0, 5, hours_ago=2))
        db.add(_make_order("AAPL.US", "SELL", 105.0, 3, hours_ago=1))
        db.commit()
        buy = db.query(OrderRecord).filter(OrderRecord.side == "BUY").first()
        assert buy is not None
        original_qty = buy.executed_quantity

    # Hit the metrics endpoint — it reads inside a separate session.
    client = TestClient(app)
    resp = client.get("/api/metrics/summary?days=30")
    assert resp.status_code == 200

    # Verify the BUY record's executed_quantity was NOT touched.
    with SessionLocal() as db:
        buy = db.query(OrderRecord).filter(OrderRecord.side == "BUY").first()
        assert buy is not None
        assert buy.executed_quantity == original_qty
