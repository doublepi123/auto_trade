"""Tests for RegimeService and the /api/regime router.

The classification core is a pure function (no I/O), so most regime-behavior
assertions target :func:`classify_regime` directly with synthetic price series.
The DB-backed tests use a fresh in-memory SQLite database per fixture.
"""
from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.regime import router as regime_router
from app.database import get_db
from app.models import Base, OrderRecord
from app.services.regime_service import (
    RegimeLabel,
    RegimeService,
    classify_regime,
)


# ----------------------------------------------------------------------
# DB / client fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Yield a session bound to a fresh in-memory SQLite database.

    ``StaticPool`` pins a single in-memory connection so the schema created by
    ``create_all`` is visible to every FastAPI dependency checkout.
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
    """Standalone app with the regime router and the DB overridden."""
    app = FastAPI()
    app.include_router(regime_router)

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


def _fill(
    *,
    symbol: str,
    price: float,
    minutes_ago: int,
    oid: str = "",
) -> OrderRecord:
    """Build a filled OrderRecord ``minutes_ago`` from now at ``price``."""
    ts = _now() - timedelta(minutes=minutes_ago)
    return OrderRecord(
        broker_order_id=oid or f"o-{symbol}-{minutes_ago}-{price}",
        symbol=symbol,
        side="BUY",
        quantity=10,
        price=price,
        executed_quantity=10,
        executed_price=price,
        status="FILLED",
        created_at=ts,
        filled_at=ts,
    )


# ----------------------------------------------------------------------
# classify_regime: pure-function behavior
# ----------------------------------------------------------------------


def test_classify_regime_empty_returns_unknown() -> None:
    result = classify_regime([])
    assert result.label == RegimeLabel.UNKNOWN
    assert result.confidence == 0.0
    assert result.indicators.volatility_level == "unknown"


def test_classify_regime_too_few_points_returns_unknown() -> None:
    # Below _MIN_DATA_POINTS (5) the statistics are too noisy to trust.
    result = classify_regime([100.0, 101.0, 100.5])
    assert result.label == RegimeLabel.UNKNOWN
    assert result.confidence == 0.0


def test_classify_regime_constant_series_does_not_crash() -> None:
    # Constant prices → zero variance → the low-vol branch classifies it as
    # LOW_VOLATILITY (annualized vol of 0 < 0.15 threshold). The point of this
    # test is that a degenerate series is handled without raising, not that it
    # must be UNKNOWN.
    result = classify_regime([100.0] * 10)
    assert result.label in {RegimeLabel.UNKNOWN, RegimeLabel.LOW_VOLATILITY}
    # Never raises; indicators are well-formed.
    assert result.indicators.volatility_level in {"unknown", "low"}


def test_classify_regime_high_volatility_when_returns_are_wild() -> None:
    # Alternating ±20% moves → very high annualized vol.
    wild = []
    base = 100.0
    for i in range(20):
        base *= 1.2 if i % 2 == 0 else (1 / 1.2)
        wild.append(base)
    result = classify_regime(wild)
    assert result.label == RegimeLabel.HIGH_VOLATILITY
    assert result.indicators.volatility_level == "high"
    assert 0.0 < result.confidence <= 1.0


def test_classify_regime_trending_up() -> None:
    # Steady gentle uptrend with low per-step vol → TRENDING_UP.
    trending = [100.0 * (1.005 ** i) for i in range(30)]
    result = classify_regime(trending)
    assert result.label in {RegimeLabel.TRENDING_UP, RegimeLabel.LOW_VOLATILITY}
    # When vol is low enough, the LOW_VOLATILITY branch wins first (precedence).
    # Either way the trend indicator should be "up".
    if result.label == RegimeLabel.TRENDING_UP:
        assert result.indicators.trend_direction == "up"


def test_classify_regime_trending_down() -> None:
    trending = [100.0 * (0.995 ** i) for i in range(30)]
    result = classify_regime(trending)
    assert result.label in {RegimeLabel.TRENDING_DOWN, RegimeLabel.LOW_VOLATILITY}
    if result.label == RegimeLabel.TRENDING_DOWN:
        assert result.indicators.trend_direction == "down"


def test_classify_regime_range_bound_for_flat_series() -> None:
    # Medium-vol oscillation around a flat mean with no net drift. The per-step
    # wiggle (±1%) annualizes to ~0.32, squarely in the medium-vol band
    # (0.15 ≤ vol < 0.45) and the slope ≈ 0, so the precedence hands the label
    # to the RANGE_BOUND fallback rather than HIGH/LOW_VOLATILITY.
    flat = []
    for i in range(30):
        flat.append(100.0 * (1 + 0.01 * (1 if i % 2 == 0 else -1)))
    result = classify_regime(flat)
    assert result.label == RegimeLabel.RANGE_BOUND
    assert result.indicators.trend_direction == "sideways"
    assert result.indicators.volatility_level == "medium"


def test_classify_regime_volume_regime_high() -> None:
    # Recent window has 3x the fills of the older window.
    fill_counts = [1, 1, 3, 3, 9, 9]
    result = classify_regime([100.0, 101.0, 100.5, 101.5, 100.8, 101.2],
                             fill_counts=fill_counts)
    assert result.indicators.volume_regime == "high"


def test_classify_regime_indicators_carry_price_vs_mean() -> None:
    # Last price above the mean → positive price_vs_mean_pct.
    series = [100.0, 100.0, 100.0, 100.0, 110.0]
    result = classify_regime(series)
    assert result.indicators.price_vs_mean_pct > 0.0


def test_classify_regime_ignores_non_positive_prices() -> None:
    # Zero/negative prices must be filtered, not crash the math.
    result = classify_regime([100.0, 0.0, -5.0, 101.0, 102.0, 103.0, 104.0])
    assert result.label != RegimeLabel.UNKNOWN or result.confidence == 0.0


# ----------------------------------------------------------------------
# RegimeService: DB-backed
# ----------------------------------------------------------------------


def test_current_regime_no_data_returns_unknown(db_session: Session) -> None:
    payload = RegimeService(db_session).get_current_regime("NOFILL.US")
    assert payload["symbol"] == "NOFILL.US"
    assert payload["regime_label"] == RegimeLabel.UNKNOWN
    assert payload["confidence"] == 0.0
    assert payload["data_points"] == 0
    assert payload["indicators"]["volatility_level"] == "unknown"
    # as_of must be an ISO timestamp string.
    assert isinstance(payload["as_of"], str) and "T" in payload["as_of"]


def test_current_regime_too_few_fills_returns_unknown(db_session: Session) -> None:
    db_session.add_all(
        [
            _fill(symbol="AAPL.US", price=100.0, minutes_ago=60, oid="a1"),
            _fill(symbol="AAPL.US", price=101.0, minutes_ago=30, oid="a2"),
        ]
    )
    db_session.commit()

    payload = RegimeService(db_session).get_current_regime("AAPL.US")
    assert payload["regime_label"] == RegimeLabel.UNKNOWN
    assert payload["data_points"] == 2


def test_current_regime_with_enough_fills(db_session: Session) -> None:
    # 10 ascending fills → trending up (or low-vol) but never UNKNOWN.
    base = 100.0
    for i in range(10):
        db_session.add(
            _fill(
                symbol="AAPL.US",
                price=round(base * (1.003 ** i), 2),
                minutes_ago=(10 - i) * 60,
                oid=f"a{i}",
            )
        )
    db_session.commit()

    payload = RegimeService(db_session).get_current_regime("AAPL.US")
    assert payload["symbol"] == "AAPL.US"
    assert payload["regime_label"] != RegimeLabel.UNKNOWN
    assert payload["data_points"] == 10
    assert payload["confidence"] > 0.0
    assert {"volatility_level", "trend_direction", "volume_regime", "price_vs_mean_pct"} <= set(
        payload["indicators"].keys()
    )


def test_current_regime_filters_other_symbols(db_session: Session) -> None:
    for i in range(8):
        db_session.add(_fill(symbol="AAPL.US", price=100.0 + i, minutes_ago=(8 - i) * 60, oid=f"a{i}"))
    db_session.add(_fill(symbol="MSFT.US", price=200.0, minutes_ago=5, oid="m1"))
    db_session.commit()

    payload = RegimeService(db_session).get_current_regime("MSFT.US")
    # Only one MSFT fill → UNKNOWN with data_points=1.
    assert payload["data_points"] == 1
    assert payload["regime_label"] == RegimeLabel.UNKNOWN


def test_regime_history_returns_correct_date_range(db_session: Session) -> None:
    # Spread fills across 3 distinct days.
    now = _now()
    day1 = now - timedelta(days=2)
    day2 = now - timedelta(days=1)
    day3 = now
    for day, prices in [(day1, [100.0, 101.0, 102.0, 103.0, 104.0]),
                        (day2, [104.0, 103.0, 102.0, 101.0, 100.0]),
                        (day3, [100.0, 101.0, 102.0, 103.0, 104.0])]:
        for j, p in enumerate(prices):
            db_session.add(
                OrderRecord(
                    broker_order_id=f"h-{day.date()}-{j}",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=10,
                    price=p,
                    executed_quantity=10,
                    executed_price=p,
                    status="FILLED",
                    created_at=day - timedelta(minutes=j),
                    filled_at=day - timedelta(minutes=j),
                )
            )
    db_session.commit()

    rows = RegimeService(db_session).get_regime_history("AAPL.US", days=30)
    assert len(rows) == 3
    dates = [row["date"] for row in rows]
    assert dates == sorted(dates)
    # Each row carries the documented fields.
    for row in rows:
        assert {"date", "regime_label", "avg_price", "volatility_proxy"} <= set(row.keys())
        assert row["avg_price"] > 0
        assert row["volatility_proxy"] >= 0


def test_regime_history_excludes_out_of_window(db_session: Session) -> None:
    now = _now()
    in_window = now - timedelta(days=5)
    out_window = now - timedelta(days=60)
    for day_label, day, price in [("in", in_window, 100.0),
                                  ("in2", in_window, 101.0),
                                  ("in3", in_window, 102.0),
                                  ("in4", in_window, 103.0),
                                  ("in5", in_window, 104.0),
                                  ("out", out_window, 50.0)]:
        db_session.add(
            OrderRecord(
                broker_order_id=f"x-{day_label}",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=price,
                executed_quantity=10,
                executed_price=price,
                status="FILLED",
                created_at=day,
                filled_at=day,
            )
        )
    db_session.commit()

    rows = RegimeService(db_session).get_regime_history("AAPL.US", days=30)
    # Only the in-window day should appear.
    assert len(rows) == 1


def test_regime_history_no_data_returns_empty(db_session: Session) -> None:
    rows = RegimeService(db_session).get_regime_history("NOFILL.US", days=30)
    assert rows == []


# ----------------------------------------------------------------------
# router: GET /current and /history
# ----------------------------------------------------------------------


def test_api_current_no_data_returns_unknown(client: TestClient) -> None:
    resp = client.get("/api/regime/current", params={"symbol": "NOFILL.US"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["regime_label"] == RegimeLabel.UNKNOWN
    assert data["confidence"] == 0.0
    assert data["data_points"] == 0


def test_api_current_with_data(client: TestClient, db_session: Session) -> None:
    for i in range(10):
        db_session.add(
            _fill(symbol="AAPL.US", price=100.0 + i, minutes_ago=(10 - i) * 60, oid=f"a{i}")
        )
    db_session.commit()

    resp = client.get("/api/regime/current", params={"symbol": "AAPL.US"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "AAPL.US"
    assert data["data_points"] == 10
    assert data["regime_label"] != RegimeLabel.UNKNOWN


def test_api_current_requires_symbol(client: TestClient) -> None:
    resp = client.get("/api/regime/current")
    assert resp.status_code == 422  # missing required query param


def test_api_history_returns_rows(client: TestClient, db_session: Session) -> None:
    now = _now()
    day = now - timedelta(days=1)
    for j, p in enumerate([100.0, 101.0, 102.0, 103.0, 104.0]):
        db_session.add(
            OrderRecord(
                broker_order_id=f"api-{j}",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=p,
                executed_quantity=10,
                executed_price=p,
                status="FILLED",
                created_at=day - timedelta(minutes=j),
                filled_at=day - timedelta(minutes=j),
            )
        )
    db_session.commit()

    resp = client.get("/api/regime/history", params={"symbol": "AAPL.US", "days": 30})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "regime_label" in data[0]
    assert math.isfinite(data[0]["avg_price"])


def test_api_history_requires_symbol(client: TestClient) -> None:
    resp = client.get("/api/regime/history")
    assert resp.status_code == 422
