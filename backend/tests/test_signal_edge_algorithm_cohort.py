"""Algorithm provenance is mandatory even when price barriers are identical."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.strategy_shadow import router
from app.config import settings
from app.database import get_db
from app.models import Base, StrategyV2ShadowConfig, StrategyV2ShadowTrade, StrategyV2ShadowVersion
from app.schemas import SignalEdgeResponse
from app.services.signal_edge_service import SignalEdgeService
from app.services.strategy_v2_shadow_service import _ALGORITHM_VERSION


@pytest.fixture
def cohort(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cohort.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables[model.__tablename__] for model in (
            StrategyV2ShadowConfig, StrategyV2ShadowVersion, StrategyV2ShadowTrade,
        )
    ])
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0)
    with Session(engine) as db:
        for symbol in ("MIX.US", "CLEAN.US"):
            db.add(StrategyV2ShadowConfig(
                symbol=symbol, enabled=True, stop_loss_pct=0.45, profit_target_pct=0.80,
            ))
            versions = [("current", _ALGORITHM_VERSION, 2)]
            if symbol == "MIX.US":
                versions.append(("stale", "strategy-v2-rth-mr-v4-frozen-config", 3))
            for version, algorithm, count in versions:
                db.add(StrategyV2ShadowVersion(
                    symbol=symbol, config_version=version, activated_at=now,
                    config_json=json.dumps({
                        "algorithm_version": algorithm,
                        "stop_loss_pct": 0.45, "profit_target_pct": 0.80,
                    }),
                ))
                for index in range(count):
                    exit_at = now - timedelta(days=index + 1)
                    db.add(StrategyV2ShadowTrade(
                        symbol=symbol, config_version=version, status="CLOSED",
                        entry_at=exit_at - timedelta(minutes=30), exit_at=exit_at,
                        entry_price=100.0, quantity=1.0, gross_pnl=0.2, net_pnl=0.1,
                        exit_reason="PROFIT_TARGET" if index == 0 else "MAX_HOLD",
                    ))
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()


def test_per_symbol_excludes_stale_algorithm_when_barriers_are_identical(cohort: Engine) -> None:
    # Given: v4 and v5 share 0.45/0.80 barriers for one symbol.
    with Session(cohort) as db:
        # When: a per-symbol assessment assembles evidence.
        result, _, _, _ = SignalEdgeService(db).assess(symbol="MIX.US")
    # Then: only the current algorithm contributes versions, days and trades.
    assert result.first_passage.matched_versions == 1
    assert result.first_passage.matched_trades == 2
    assert result.first_passage.provenance_excluded_trades == 3
    assert (result.net.distinct_days, result.net.observations) == (2, 2)
    assert result.first_passage.resolved == 1


def test_default_cohort_remains_current_algorithm_only(cohort: Engine) -> None:
    # Given: the same mixed cohort and a second, clean symbol.
    with Session(cohort) as db:
        # When: the default cross-symbol path assesses it.
        result, _, _, _ = SignalEdgeService(db).assess()
    # Then: these pre-fix counts remain unchanged.
    assert (result.first_passage.matched_versions, result.first_passage.matched_trades) == (2, 4)
    assert result.first_passage.provenance_excluded_trades == 3
    assert (result.net.distinct_days, result.net.observations) == (2, 4)
    assert result.first_passage.resolved == 2


def test_current_algorithm_symbol_loses_no_evidence(cohort: Engine) -> None:
    # Given: a symbol with only legitimate current-algorithm evidence.
    with Session(cohort) as db:
        # When: its cohort is assessed.
        result, _, _, _ = SignalEdgeService(db).assess(symbol="CLEAN.US")
    # Then: all its evidence survives.
    assert (result.first_passage.matched_versions, result.first_passage.matched_trades) == (1, 2)
    assert result.first_passage.provenance_excluded_trades == 0
    assert (result.net.distinct_days, result.net.observations) == (2, 2)
    assert result.first_passage.resolved == 1


@pytest.mark.parametrize("symbol, expected", [("MIX.US", 3), ("CLEAN.US", 0), (None, 3)])
def test_algorithm_exclusion_is_disclosed(
    cohort: Engine, symbol: str | None, expected: int,
) -> None:
    # Given: matching barriers with known current/stale provenance.
    with Session(cohort) as db:
        # When: the existing disclosure object is serialized.
        result, _, _, _ = SignalEdgeService(db).assess(symbol=symbol)
    # Then: stale closed trades (including time exits) are explicitly counted.
    from dataclasses import asdict

    assert asdict(result.first_passage).get("algorithm_mismatch_excluded_trades") == expected


def test_http_per_symbol_cohort_excludes_stale_algorithm(
    cohort: Engine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a real router with its own seeded database, no nested sessions.
    app = FastAPI()
    app.include_router(router)

    def isolated_db() -> Iterator[Session]:
        with Session(cohort) as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    monkeypatch.setattr(settings, "api_key", "")
    # When: the operator requests the per-symbol reading.
    with TestClient(app) as client:
        response = client.get("/api/strategy-shadow/signal-edge?symbol=MIX.US")
    # Then: the HTTP result contains only v5 evidence.
    assert response.status_code == 200
    result = SignalEdgeResponse.model_validate_json(response.content)
    assert result.first_passage.matched_versions == 1
    assert result.first_passage.matched_trades == 2
    assert result.first_passage.provenance_excluded_trades == 3
    assert response.json()["first_passage"].get("algorithm_mismatch_excluded_trades") == 3
