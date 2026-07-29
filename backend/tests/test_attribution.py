"""Tests for PerformanceAttributionService and the /api/attribution router.

Per-file in-memory SQLite — we build the schema fresh in each test class so
the tests are isolated and need no shared fixture state. The service is read-
only so a single shared engine per class is enough.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.attribution import router as attribution_router
from app.database import get_db
from app.models import Base, OrderRecord
from app.services.performance_attribution_service import PerformanceAttributionService


# ----------------------------------------------------------------------
# shared engine / session wiring
# ----------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Yield a session bound to a fresh in-memory SQLite database.

    ``StaticPool`` + ``check_same_thread=False`` pins a single in-memory
    connection so the schema created by ``create_all`` is visible to every
    checkout the FastAPI dependency performs (the default in-memory pool would
    create a brand-new empty database per connection). ``Base.metadata.create_all``
    builds every table so the service's ``OrderRecord`` queries compile.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    """Standalone app with the attribution router and the DB overridden."""
    app = FastAPI()
    app.include_router(attribution_router)

    def override_get_db() -> Iterator[Session]:
        try:
            yield db_session
        finally:
            pass  # session lifetime managed by the db_session fixture

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _exit_order(
    *,
    symbol: str,
    side: str,
    qty: float,
    net_pnl: float,
    exit_reason: str = "",
    exit_cause: str = "",
    filled_at: datetime | None = None,
    opened_at: datetime | None = None,
    oid: str = "",
) -> OrderRecord:
    """Build an exit-side OrderRecord with the PnL attribution fields populated."""
    ts = filled_at or _now()
    return OrderRecord(
        broker_order_id=oid or f"o-{symbol}-{ts.timestamp()}",
        symbol=symbol,
        side=side,
        quantity=qty,
        price=100.0,
        executed_quantity=qty,
        executed_price=100.0,
        status="FILLED",
        created_at=ts,
        filled_at=ts,
        net_pnl=net_pnl,
        gross_pnl=net_pnl,
        exit_reason=exit_reason,
        exit_cause=exit_cause,
        cost_basis_price=100.0,
        cost_basis_quantity=qty,
        cost_basis_opened_at=opened_at or (ts - timedelta(hours=1)),
    )


# ----------------------------------------------------------------------
# service: empty DB
# ----------------------------------------------------------------------


def test_empty_db_returns_zero_attribution(db_session: Session) -> None:
    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)

    assert result["period_days"] == 30
    assert result["total_pnl"] == 0.0
    assert result["total_trades"] == 0
    assert result["win_rate"] == 0.0
    # Every dimension is an empty (but well-formed) bucket dict / list.
    assert result["by_symbol"] == {}
    assert result["by_direction"] == {}
    assert result["by_exit_reason"] == {}
    assert result["by_session"] == {}
    assert result["by_day"] == []


def test_empty_db_top_contributors_is_empty(db_session: Session) -> None:
    rows = PerformanceAttributionService(db_session).top_contributors(days=30, limit=10)
    assert rows == []


# ----------------------------------------------------------------------
# service: populated breakdown
# ----------------------------------------------------------------------


def test_attribute_pnl_breakdown_by_symbol_direction_session(db_session: Session) -> None:
    today = _now()
    # Two US symbols (one winner, one loser) and one HK winner.
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        exit_reason="TARGET", filled_at=today, oid="a1"),
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=-50.0,
                        exit_reason="STOP", filled_at=today, oid="a2"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=-120.0,
                        exit_reason="STOP", filled_at=today, oid="m1"),
            _exit_order(symbol="0700.HK", side="SELL", qty=100, net_pnl=300.0,
                        exit_reason="TARGET", filled_at=today, oid="hk1"),
        ]
    )
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)
    # 200 - 50 - 120 + 300 = 330
    assert result["total_pnl"] == pytest.approx(330.0)
    assert result["total_trades"] == 4
    # wins = AAPL(1) + HK(1) = 2 / 4 trades
    assert result["win_rate"] == pytest.approx(0.5)

    by_symbol = result["by_symbol"]
    assert by_symbol["AAPL.US"]["total_pnl"] == pytest.approx(150.0)
    assert by_symbol["AAPL.US"]["trade_count"] == 2
    assert by_symbol["AAPL.US"]["win_count"] == 1
    assert by_symbol["AAPL.US"]["avg_pnl"] == pytest.approx(75.0)
    assert by_symbol["MSFT.US"]["total_pnl"] == pytest.approx(-120.0)
    assert by_symbol["0700.HK"]["total_pnl"] == pytest.approx(300.0)

    # All exits are SELL → LONG (the live system is long-only).
    by_direction = result["by_direction"]
    assert by_direction["LONG"]["trade_count"] == 4
    assert by_direction["LONG"]["total_pnl"] == pytest.approx(330.0)
    assert "SHORT" not in by_direction

    # Session split US vs HK.
    assert result["by_session"]["US"]["trade_count"] == 3
    assert result["by_session"]["HK"]["trade_count"] == 1

    # Exit reasons.
    assert result["by_exit_reason"]["TARGET"]["total_pnl"] == pytest.approx(500.0)
    assert result["by_exit_reason"]["STOP"]["total_pnl"] == pytest.approx(-170.0)


def test_attribute_pnl_by_day_timeline(db_session: Session) -> None:
    today = _now()
    yesterday = today - timedelta(days=1)
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=100.0,
                        filled_at=yesterday, oid="y1"),
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=50.0,
                        filled_at=today, oid="t1"),
        ]
    )
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)
    by_day = result["by_day"]
    assert len(by_day) == 2
    # Ascending by date.
    assert by_day[0]["date"] < by_day[1]["date"]
    pnl_by_date = {row["date"]: row["pnl"] for row in by_day}
    assert pnl_by_date[yesterday.date().isoformat()] == pytest.approx(100.0)
    assert pnl_by_date[today.date().isoformat()] == pytest.approx(50.0)
    for row in by_day:
        assert row["trade_count"] == 1


def test_attribute_pnl_symbol_filter(db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, oid="a1"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=-100.0,
                        filled_at=today, oid="m1"),
        ]
    )
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30, symbol="AAPL.US")
    assert result["total_trades"] == 1
    assert result["total_pnl"] == pytest.approx(200.0)
    assert set(result["by_symbol"].keys()) == {"AAPL.US"}


def test_attribute_pnl_excludes_out_of_window(db_session: Session) -> None:
    """Exits older than the lookback window must be excluded."""
    now = _now()
    old = now - timedelta(days=60)
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=old, oid="old1"),
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=50.0,
                        filled_at=now, oid="new1"),
        ]
    )
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)
    assert result["total_trades"] == 1
    assert result["total_pnl"] == pytest.approx(50.0)


def test_attribute_pnl_null_net_pnl_skipped(db_session: Session) -> None:
    """Orders without ``net_pnl`` (entries / unfilled) are never counted."""
    today = _now()
    # An entry buy (no net_pnl) and a real exit.
    db_session.add_all(
        [
            OrderRecord(
                broker_order_id="entry1", symbol="AAPL.US", side="BUY",
                quantity=10, price=100.0, executed_quantity=10, executed_price=100.0,
                status="FILLED", created_at=today, filled_at=today,
            ),
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=75.0,
                        filled_at=today, oid="exit1"),
        ]
    )
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)
    assert result["total_trades"] == 1
    assert result["total_pnl"] == pytest.approx(75.0)


def test_attribute_pnl_unspecified_exit_reason_fallback(db_session: Session) -> None:
    """Exits with neither ``exit_reason`` nor ``exit_cause`` bucket as UNSPECIFIED."""
    today = _now()
    db_session.add(_exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=10.0,
                               exit_reason="", exit_cause="", filled_at=today, oid="u1"))
    db_session.commit()

    result = PerformanceAttributionService(db_session).attribute_pnl(days=30)
    assert "UNSPECIFIED" in result["by_exit_reason"]
    assert result["by_exit_reason"]["UNSPECIFIED"]["trade_count"] == 1


# ----------------------------------------------------------------------
# service: top_contributors
# ----------------------------------------------------------------------


def test_top_contributors_orders_by_abs_pnl(db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, oid="a1"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=-300.0,
                        filled_at=today, oid="m1"),
            _exit_order(symbol="TSLA.US", side="SELL", qty=10, net_pnl=50.0,
                        filled_at=today, oid="t1"),
        ]
    )
    db_session.commit()

    rows = PerformanceAttributionService(db_session).top_contributors(days=30, limit=10)
    # Biggest absolute contributors first: |-300| > |200| > |50|.
    assert [r["symbol"] for r in rows] == ["MSFT.US", "AAPL.US", "TSLA.US"]
    assert rows[0]["total_pnl"] == pytest.approx(-300.0)


def test_top_contributors_respects_limit(db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, oid="a1"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=150.0,
                        filled_at=today, oid="m1"),
            _exit_order(symbol="TSLA.US", side="SELL", qty=10, net_pnl=50.0,
                        filled_at=today, oid="t1"),
        ]
    )
    db_session.commit()

    rows = PerformanceAttributionService(db_session).top_contributors(days=30, limit=2)
    assert len(rows) == 2
    assert [r["symbol"] for r in rows] == ["AAPL.US", "MSFT.US"]


def test_top_contributors_holding_minutes_and_win_rate(db_session: Session) -> None:
    today = _now()
    opened = today - timedelta(minutes=120)
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, opened_at=opened, oid="a1"),
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=-50.0,
                        filled_at=today, opened_at=opened, oid="a2"),
        ]
    )
    db_session.commit()

    rows = PerformanceAttributionService(db_session).top_contributors(days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "AAPL.US"
    assert row["trade_count"] == 2
    assert row["win_rate"] == pytest.approx(0.5)
    assert row["avg_holding_minutes"] == pytest.approx(120.0, abs=1e-3)


# ----------------------------------------------------------------------
# router: GET /pnl and /top-contributors
# ----------------------------------------------------------------------


def test_api_pnl_empty(client: TestClient) -> None:
    resp = client.get("/api/attribution/pnl")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_trades"] == 0
    assert data["total_pnl"] == 0.0
    assert data["by_symbol"] == {}


def test_api_pnl_with_data(client: TestClient, db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        exit_reason="TARGET", filled_at=today, oid="a1"),
        ]
    )
    db_session.commit()

    resp = client.get("/api/attribution/pnl", params={"days": 30})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 1
    assert data["total_pnl"] == pytest.approx(200.0)
    assert "AAPL.US" in data["by_symbol"]
    assert data["by_direction"]["LONG"]["trade_count"] == 1


def test_api_pnl_symbol_filter(client: TestClient, db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, oid="a1"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=-100.0,
                        filled_at=today, oid="m1"),
        ]
    )
    db_session.commit()

    resp = client.get("/api/attribution/pnl", params={"symbol": "AAPL.US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 1
    assert set(data["by_symbol"].keys()) == {"AAPL.US"}


def test_api_top_contributors(client: TestClient, db_session: Session) -> None:
    today = _now()
    db_session.add_all(
        [
            _exit_order(symbol="AAPL.US", side="SELL", qty=10, net_pnl=200.0,
                        filled_at=today, oid="a1"),
            _exit_order(symbol="MSFT.US", side="SELL", qty=10, net_pnl=-300.0,
                        filled_at=today, oid="m1"),
        ]
    )
    db_session.commit()

    resp = client.get("/api/attribution/top-contributors", params={"days": 30, "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["symbol"] == "MSFT.US"  # |−300| > |200|
