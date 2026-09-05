"""Real router/service proof using an isolated seeded SQLite cohort."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.strategy_shadow import router
from app.config import settings
from app.database import get_db
from app.models import Base, StrategyV2ShadowConfig, StrategyV2ShadowTrade, StrategyV2ShadowVersion
from app.schemas import SignalEdgeResponse


@pytest.mark.parametrize("floors", [(30, 20), (1, 1), (5, 3), (54, 18)])
def test_http_query_cannot_induce_futile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, floors: tuple[int, int],
) -> None:
    # Given: an isolated database; setup commits and closes before the request session.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'futility.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables[model.__tablename__] for model in (
            StrategyV2ShadowConfig, StrategyV2ShadowVersion, StrategyV2ShadowTrade,
        )
    ])
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0)
    with Session(engine) as db:
        db.add(StrategyV2ShadowConfig(
            symbol="FLOOR.US", enabled=True, stop_loss_pct=0.45, profit_target_pct=0.80,
        ))
        db.add(StrategyV2ShadowVersion(
            symbol="FLOOR.US", config_version="v1", activated_at=now,
            config_json=json.dumps({"stop_loss_pct": 0.45, "profit_target_pct": 0.80}),
        ))
        for index in range(54):
            day = index // 3
            exit_at = now - timedelta(days=day + 1)
            gross = -0.30 + (0.05 if day % 2 == 0 else -0.05)
            db.add(StrategyV2ShadowTrade(
                symbol="FLOOR.US", config_version="v1", status="CLOSED",
                entry_at=exit_at - timedelta(minutes=30), exit_at=exit_at,
                entry_price=100.0, quantity=1.0, gross_pnl=gross, net_pnl=gross - 0.10,
                exit_reason="PROFIT_TARGET" if index < 20 else "PRICE_STOP",
            ))
        db.commit()
    app = FastAPI()
    app.include_router(router)

    def isolated_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    monkeypatch.setattr(settings, "api_key", "")
    try:
        # When: real HTTP query parsing reaches the real service and domain.
        with TestClient(app) as client:
            response = client.get("/api/strategy-shadow/signal-edge", params={
                "symbol": "FLOOR.US", "min_resolved_trades": floors[0],
                "min_distinct_days": floors[1],
            })
        # Then: a powered but too-thin cohort cannot become FUTILE.
        assert response.status_code == 200
        result = SignalEdgeResponse.model_validate_json(response.content)
        assert (result.first_passage.resolved, result.gross.distinct_days) == (54, 18)
        assert result.futility.powered_for_required_effect is True
        assert result.futility.status == "INSUFFICIENT_DATA"
        assert result.futility.resolved_brackets == 54
        assert result.futility.required_resolved_brackets == 30
        assert result.futility.required_distinct_days == 20
        assert result.futility.evidence_floor_met is False
    finally:
        engine.dispose()
