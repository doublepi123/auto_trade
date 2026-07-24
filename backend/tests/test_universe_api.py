from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Generator

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
from app.services.universe_selection_service import UniverseSelectionService

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

    def record(self, action: str, **_kwargs: object) -> None:
        self.actions.append(action)


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
        assert not db.dirty
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
        items = {
            item["symbol"]: item
            for item in payload["run"]["items"]
        }
        assert items["AAPL.US"]["is_trading_target"] is True
        assert items["AAPL.US"]["shadow_enabled"] is False

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
