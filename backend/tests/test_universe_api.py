from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import universe as universe_api
from app.api.deps import get_audit_logger
from app.core.broker import BrokerCandle, Quote
from app.database import get_db
from app.domain.universe_selection.catalog import IndexCandidate
from app.domain.universe_selection.selector import UniverseSelectionConfig
from app.models import Base, StrategyConfig
from app.schemas import UniverseObservationHealthResponse
from app.services.universe_selection_service import UniverseSelectionService
from app.services.durable_job_lease_service import (
    DurableJobLeaseService,
    LeaseBackendError,
    LeaseLostError,
)
from app.services.universe_selection_service import (
    UniverseSelectionLeaseBusyError,
)

_NOW = datetime(2026, 7, 23, 19, tzinfo=timezone.utc)
_CATALOG = (
    IndexCandidate(
        "AAPL.US",
        "Apple",
        "Hardware",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate(
        "MSFT.US",
        "Microsoft",
        "Software",
        ("NASDAQ_100", "DJIA"),
    ),
)


class _Broker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [
            Quote(
                symbol=symbol,
                last_price=100,
                bid=99.99,
                ask=100.01,
                timestamp=datetime(
                    2026,
                    7,
                    22,
                    20,
                    tzinfo=timezone.utc,
                ).isoformat(),
            )
            for symbol in symbols
        ]

    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[BrokerCandle]:
        price = 100.0
        result: list[BrokerCandle] = []
        for index in range(30):
            close = price * (1.02 if index % 2 == 0 else 0.98)
            result.append(
                BrokerCandle(
                    timestamp=(
                        datetime(
                            2026,
                            7,
                            23,
                            4,
                            tzinfo=timezone.utc,
                        )
                        - timedelta(days=29 - index)
                    ),
                    open=price,
                    high=max(price, close) * 1.005,
                    low=min(price, close) * 0.995,
                    close=close,
                    volume=10_000_000,
                )
            )
            price = close
        return result


class _Audit:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.records: list[dict[str, object]] = []

    def record(self, action: str, **kwargs: object) -> None:
        self.actions.append(action)
        self.records.append({"action": action, **kwargs})


def _service(db: Session) -> UniverseSelectionService:
    return UniverseSelectionService(
        db,
        _Broker(),
        catalog=_CATALOG,
        config=UniverseSelectionConfig(
            max_selected=2,
            max_per_sector=1,
            min_avg_dollar_volume=1_000_000,
            max_relative_spread_bps=100,
            min_realized_vol_20d=0.01,
            max_realized_vol_20d=2,
            min_atr_pct_14d=0.01,
            max_atr_pct_14d=20,
        ),
        minimum_evaluable_ratio=0.5,
        apply_to_watchlist=False,
        enable_shadow=False,
        now=_NOW,
    )


def test_production_builder_uses_active_strategy_costs(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(
            StrategyConfig(
                symbol="AAPL.US",
                market="US",
                fee_rate_us=0.0012,
            )
        )
        db.commit()
        monkeypatch.setattr(
            universe_api,
            "get_runner",
            lambda: SimpleNamespace(broker=_Broker()),
        )
        monkeypatch.setattr(
            universe_api.settings,
            "entry_round_trip_slippage_bps",
            7.5,
        )

        service = universe_api.build_universe_selection_service(db)

        assert service.config.round_trip_fee_bps == 24.0
        assert service.config.round_trip_slippage_bps == 7.5
        assert isinstance(service.lease_service, DurableJobLeaseService)
        assert service.lease_service._session_factory is universe_api.SessionLocal
        assert (
            service.lease_service.default_ttl_seconds
            == universe_api.settings.job_lease_ttl_seconds
        )
        assert not db.dirty
    finally:
        db.close()
        engine.dispose()


def test_observation_health_endpoint_preserves_research_safety_flags(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    class _ObservationHealthService:
        def __init__(self, session: Session) -> None:
            assert session is db

        def get_health(self) -> dict[str, object]:
            return {
                "generated_at": _NOW,
                "status": "WARNING",
                "order_submission_allowed": False,
                "automatic_promotion_allowed": False,
                "components": [
                    {
                        "name": "UNIVERSE_SELECTION",
                        "status": "WARNING",
                        "blockers": ["LATEST_COMPLETED_SESSION_MISSING"],
                    }
                ],
                "blockers": [
                    "UNIVERSE_SELECTION:LATEST_COMPLETED_SESSION_MISSING"
                ],
            }

    monkeypatch.setattr(
        universe_api,
        "ResearchObservationHealthService",
        _ObservationHealthService,
    )
    try:
        result = universe_api.get_universe_observation_health(db)
        payload = UniverseObservationHealthResponse.model_validate(
            result
        ).model_dump(mode="json")

        assert payload["status"] == "WARNING"
        assert payload["order_submission_allowed"] is False
        assert payload["automatic_promotion_allowed"] is False
        assert payload["components"][0]["name"] == "UNIVERSE_SELECTION"
    finally:
        db.close()
        engine.dispose()


def test_universe_endpoints_return_typed_snapshot(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    audit = _Audit()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_audit_logger] = lambda: audit
    monkeypatch.setattr(
        universe_api,
        "build_universe_selection_service",
        _service,
    )
    client = TestClient(api)
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.commit()
        missing = client.get("/api/universe/latest")
        assert missing.status_code == 404

        catalog = client.get("/api/universe/catalog")
        assert catalog.status_code == 200
        assert any(
            item["symbol"] == "NVDA.US"
            for item in catalog.json()
        )

        refreshed = client.post("/api/universe/refresh")
        assert refreshed.status_code == 200
        payload = refreshed.json()
        assert payload["run"]["status"] == "COMPLETE"
        assert payload["run"]["as_of_date"] == "2026-07-22"
        assert payload["run"]["selected_count"] == 2
        assert len(payload["run"]["items"]) == 2
        assert payload["applied"] is False
        assert payload["exploration_symbols"] == []
        items = {
            item["symbol"]: item
            for item in payload["run"]["items"]
        }
        assert items["AAPL.US"]["is_trading_target"] is True
        assert items["AAPL.US"]["exploration_selected"] is False
        assert items["AAPL.US"]["shadow_enabled"] is False
        rotation = items["AAPL.US"]["metrics"]["rotation"]
        assert rotation["algorithm_version"] == (
            "index-momentum-12-1-diversified-monthly-shadow-v3"
        )
        assert rotation["selected"] is False
        assert rotation["exclusion_reasons"] == [
            "DATA_INSUFFICIENT_DAILY_BARS",
            "ROTATION_HISTORY_INSUFFICIENT"
        ]

        latest = client.get("/api/universe/latest")
        assert latest.status_code == 200
        assert latest.json()["id"] == payload["run"]["id"]
        assert audit.actions == ["UNIVERSE_SELECTION_REFRESH"]
    finally:
        client.close()
        db.close()


def test_applied_refresh_retries_runtime_reload_after_transient_failure(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    audit = _Audit()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    def applied_service(session: Session) -> UniverseSelectionService:
        service = _service(session)
        service.apply_to_watchlist = True
        return service

    class _Runner:
        reload_calls = 0

        def reload_strategy(self) -> None:
            self.reload_calls += 1
            if self.reload_calls == 1:
                raise RuntimeError("injected transient reload failure")

    runner = _Runner()
    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_audit_logger] = lambda: audit
    monkeypatch.setattr(
        universe_api,
        "build_universe_selection_service",
        applied_service,
    )
    monkeypatch.setattr(universe_api, "get_runner", lambda: runner)
    client = TestClient(api)
    try:
        first = client.post("/api/universe/refresh")
        assert first.status_code == 503
        assert runner.reload_calls == 1

        second = client.post("/api/universe/refresh")
        assert second.status_code == 200
        payload = second.json()
        assert payload["applied"] is True
        assert payload["added_symbols"] == []
        assert set(payload["retained_symbols"]) == {"AAPL.US", "MSFT.US"}
        assert runner.reload_calls == 2
    finally:
        client.close()
        db.close()


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_detail"),
    [
        (
            UniverseSelectionLeaseBusyError("internal busy owner"),
            409,
            "universe selection refresh is already running",
        ),
        (
            LeaseBackendError("internal database path"),
            503,
            "universe selection lease is temporarily unavailable",
        ),
        (
            LeaseLostError("internal fencing token"),
            503,
            "universe selection lease is temporarily unavailable",
        ),
    ],
)
def test_refresh_maps_lease_failures_and_still_audits(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    audit = _Audit()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    class _FailingService:
        def refresh(self) -> object:
            raise failure

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_audit_logger] = lambda: audit
    monkeypatch.setattr(
        universe_api,
        "build_universe_selection_service",
        lambda _db: _FailingService(),
    )
    client = TestClient(api)
    try:
        response = client.post("/api/universe/refresh")

        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}
        assert response.headers["Retry-After"] == str(
            universe_api.settings.job_lease_heartbeat_seconds
        )
        assert str(failure) not in response.text
        assert audit.actions == ["UNIVERSE_SELECTION_REFRESH"]
        assert audit.records[-1]["result"] == "FAILED"
        assert audit.records[-1]["request_summary"] == {
            "detail": type(failure).__name__
        }
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_get_latest_builds_service_without_acquiring_durable_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    api = FastAPI()
    api.include_router(universe_api.router)
    acquire_calls: list[str] = []

    def override_db() -> Generator[Session, None, None]:
        yield db

    def _unexpected_acquire(
        _service: DurableJobLeaseService,
        lease_key: str,
        **_kwargs: object,
    ) -> object:
        acquire_calls.append(lease_key)
        raise AssertionError("GET endpoint must not acquire the refresh lease")

    api.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        universe_api,
        "get_runner",
        lambda: SimpleNamespace(broker=_Broker()),
    )
    monkeypatch.setattr(
        DurableJobLeaseService,
        "try_acquire",
        _unexpected_acquire,
    )
    client = TestClient(api)
    try:
        response = client.get("/api/universe/latest")

        assert response.status_code == 404
        assert acquire_calls == []
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_range_fitness_endpoint_reports_trend_unsuitable_symbol() -> None:
    import json

    from app.models import StrategyV2ShadowDecision

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_audit_logger] = lambda: _Audit()
    client = TestClient(api)
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        now = datetime.now(timezone.utc)
        for i in range(80):
            db.add(StrategyV2ShadowDecision(
                idempotency_key=f"api-nvda-{i}",
                symbol="NVDA.US",
                config_version="v1",
                session_date=now.date(),
                bar_at=now - timedelta(minutes=i + 1),
                action="WAIT",
                gate_passed=False,
                gate_reasons_json=json.dumps(["ADX_REGIME_BLOCKED"]),
                adx_5m=41.6,
            ))
        db.commit()

        response = client.get(
            "/api/universe/range-fitness",
            params={"lookback_days": 3, "min_samples": 60},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["lookback_days"] == 3
        item = next(i for i in body["items"] if i["symbol"] == "NVDA.US")
        assert item["verdict"] == "TREND_UNSUITABLE"
        assert item["is_primary"] is True
        assert item["trend_blocked"] == 80
        # Guards against a service field the response schema forgot to expose,
        # which surfaces as a 500 rather than a missing key.
        assert set(item) == {
            "symbol",
            "is_primary",
            "samples",
            "trend_blocked",
            "trend_blocked_pct",
            "gate_passed",
            "gate_passed_pct",
                "avg_adx_5m",
                "verdict",
                "last_close_price",
                "last_bar_at",
                "closed_trades",
                "reach_count",
                "reach_rate_pct",
            }

        # Read-only contract: the interval must be untouched.
        config = db.query(StrategyConfig).one()
        assert config.symbol == "NVDA.US"
        assert db.query(StrategyV2ShadowDecision).count() == 80
    finally:
        client.close()
        db.close()
        engine.dispose()


def test_range_fitness_endpoint_rejects_inverted_bands() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_audit_logger] = lambda: _Audit()
    client = TestClient(api)
    try:
        response = client.get(
            "/api/universe/range-fitness",
            params={"trend_unsuitable_pct": 20, "range_suitable_pct": 50},
        )
        assert response.status_code == 422
    finally:
        client.close()
        db.close()
        engine.dispose()
