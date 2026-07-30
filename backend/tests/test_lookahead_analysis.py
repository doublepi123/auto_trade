"""Tests for LookaheadAnalysisService and /api/lookahead-analysis router."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.lookahead_analysis import router as lookahead_router
from app.database import get_db
from app.models import Base, OrderRecord
from app.services.lookahead_analysis_service import LookaheadAnalysisService


@pytest.fixture()
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    app = FastAPI()
    app.include_router(lookahead_router)

    def override_get_db() -> Iterator[Session]:
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _exit(
    symbol: str = "AAPL.US",
    pnl: float = 100.0,
    minutes_ago: int = 0,
) -> OrderRecord:
    return OrderRecord(
        symbol=symbol,
        side="SELL",
        quantity=10,
        price=150.0,
        net_pnl=pnl,
        filled_at=_now() - timedelta(minutes=minutes_ago),
        broker_order_id=f"ord-{symbol}-{minutes_ago}",
        status="FILLED",
    )


# ------------------------------------------------------------------
# service tests
# ------------------------------------------------------------------


def test_empty_db(db_session: Session) -> None:
    result = LookaheadAnalysisService(db_session).analyze()
    assert result["total_exits"] == 0
    assert result["baseline"]["trade_count"] == 0
    assert result["has_bias"] is False
    assert result["bias_score"] == 0.0


def test_uniform_pnl_no_bias(db_session: Session) -> None:
    for i in range(20):
        db_session.add(_exit(pnl=100.0, minutes_ago=i * 60))
    db_session.commit()
    result = LookaheadAnalysisService(db_session).analyze(lookback_days=30)
    assert result["total_exits"] == 20
    assert result["has_bias"] is False
    for s in result["slices"]:
        assert s["win_rate"] == pytest.approx(1.0)


def test_recent_losses_flag_bias(db_session: Session) -> None:
    # First 15 trades are wins, last 5 are big losses
    for i in range(15):
        db_session.add(_exit(pnl=200.0, minutes_ago=(20 - i) * 60))
    for i in range(5):
        db_session.add(_exit(pnl=-500.0, minutes_ago=i * 60))
    db_session.commit()
    result = LookaheadAnalysisService(db_session).analyze(lookback_days=30)
    assert result["total_exits"] == 20
    # 50% slice should have very different win_rate than baseline
    half_slice = [s for s in result["slices"] if s["pct"] == 50.0][0]
    assert half_slice["win_rate_delta"] > 0.15
    assert result["has_bias"] is True


def test_symbol_filter(db_session: Session) -> None:
    db_session.add(_exit(symbol="AAPL.US", pnl=100.0, minutes_ago=10))
    db_session.add(_exit(symbol="MSFT.US", pnl=-50.0, minutes_ago=20))
    db_session.commit()
    result = LookaheadAnalysisService(db_session).analyze(symbol="AAPL.US")
    assert result["total_exits"] == 1
    assert result["symbol"] == "AAPL.US"


# ------------------------------------------------------------------
# router tests
# ------------------------------------------------------------------


def test_api_analyze_empty(client: TestClient) -> None:
    resp = client.get("/api/lookahead-analysis/analyze")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_exits"] == 0
    assert data["has_bias"] is False


def test_api_analyze_with_data(client: TestClient, db_session: Session) -> None:
    for i in range(10):
        db_session.add(_exit(pnl=50.0, minutes_ago=i * 30))
    db_session.commit()
    resp = client.get("/api/lookahead-analysis/analyze?lookback_days=7")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_exits"] == 10
    assert len(data["slices"]) > 0
