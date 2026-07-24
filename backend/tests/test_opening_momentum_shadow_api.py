from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.opening_momentum_shadow import router
from app.config import settings
from app.database import get_db
from app.domain.opening_momentum import (
    ALGORITHM_VERSION,
    OpeningMomentumConfig,
)
from app.models import Base, OpeningMomentumShadowRun


_NOW = datetime(2026, 7, 23, 14, 31, tzinfo=timezone.utc)


class TestOpeningMomentumShadowApi:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )
        cls.app = FastAPI()
        cls.app.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            db = cls.session_factory()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        cls.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        settings.opening_momentum_shadow_enabled = False
        settings.opening_momentum_challenger_enabled = False
        with self.session_factory() as db:
            db.query(OpeningMomentumShadowRun).delete()
            db.commit()

    def teardown_method(self) -> None:
        settings.api_key = ""
        settings.opening_momentum_shadow_enabled = False
        settings.opening_momentum_challenger_enabled = False

    def test_status_is_explicitly_shadow_only(self) -> None:
        response = self.client.get(
            "/api/opening-momentum-shadow/status"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "DISABLED"
        assert body["config"]["mode"] == "SHADOW"
        assert body["config"]["order_submission_allowed"] is False
        assert body["config"]["signal_minutes"] == 30
        assert body["config"]["holding_minutes"] == 30
        assert body["config"]["round_trip_cost_bps"] == 14.0

    def test_runs_endpoint_serializes_evidence_and_metrics(self) -> None:
        config = OpeningMomentumConfig()
        with self.session_factory() as db:
            db.add(
                OpeningMomentumShadowRun(
                    session_date=date(2026, 7, 23),
                    algorithm_version=ALGORITHM_VERSION,
                    config_version=config.version_hash(),
                    status="CLOSED",
                    reason="FIXED_HOLD_EXIT",
                    signal_at=_NOW,
                    observed_at=_NOW,
                    universe_source="UNIVERSE_SELECTION",
                    universe_size=8,
                    universe_json='["AAPL.US","MSFT.US"]',
                    excluded_symbols_json="{}",
                    ranking_json=(
                        '[{"symbol":"AAPL.US",'
                        '"opening_return_bps":80.0}]'
                    ),
                    candidate_symbol="AAPL.US",
                    market_return_bps=10.0,
                    candidate_return_bps=80.0,
                    excess_return_bps=70.0,
                    entry_at=_NOW,
                    entry_price=100.0,
                    exit_due_at=_NOW,
                    exit_at=_NOW,
                    exit_price=101.0,
                    gross_return_bps=100.0,
                    estimated_cost_bps=14.0,
                    net_return_bps=86.0,
                )
            )
            db.commit()

        runs = self.client.get(
            "/api/opening-momentum-shadow/runs",
            params={"limit": 1},
        )
        status = self.client.get(
            "/api/opening-momentum-shadow/status"
        )

        assert runs.status_code == 200
        assert runs.json()[0]["candidate_symbol"] == "AAPL.US"
        assert runs.json()[0]["ranking"][0]["opening_return_bps"] == 80.0
        assert status.json()["metrics"]["closed_trades"] == 1
        assert status.json()["metrics"]["cumulative_net_return_bps"] == 86.0

    def test_router_enforces_api_key(self) -> None:
        settings.api_key = "opening-secret"

        assert self.client.get(
            "/api/opening-momentum-shadow/status"
        ).status_code == 401
        response = self.client.get(
            "/api/opening-momentum-shadow/status",
            headers={"X-API-Key": "opening-secret"},
        )

        assert response.status_code == 200
