from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import universe as universe_api
from app.database import get_db
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardRegistration,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistScore,
)
from app.schemas import (
    StrategyV2ForwardValidationResponse,
    StrategyV2ShadowMetrics,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.universe_promotion_service import UniversePromotionService

_NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def _db() -> tuple[Engine, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _run(
    db: Session,
    *,
    as_of_date: date,
    status: str,
    created_at: datetime,
) -> UniverseSelectionRun:
    run = UniverseSelectionRun(
        as_of_date=as_of_date,
        algorithm_version="selector-v1",
        source_version="catalog-v1",
        status=status,
        candidate_count=3,
        evaluable_count=3,
        selected_count=2,
        coverage_ratio=1.0,
        parameters_json="{}",
        error="",
        started_at=created_at,
        completed_at=(
            created_at + timedelta(minutes=1)
            if status != "RUNNING"
            else None
        ),
        created_at=created_at,
    )
    db.add(run)
    db.flush()
    return run


def _candidate(
    db: Session,
    run: UniverseSelectionRun,
    *,
    symbol: str,
    selected: bool,
    rank: int | None,
    score: float,
) -> None:
    db.add(
        UniverseSelectionCandidate(
            run_id=run.id,
            symbol=symbol,
            market="US",
            alias=symbol,
            sector="Technology",
            memberships_json='["NASDAQ_100"]',
            selected=selected,
            rank=rank,
            score=score,
            metrics_json="{}",
            exclusion_reasons_json="[]",
            created_at=run.created_at,
        )
    )


def test_readiness_uses_latest_terminal_and_shrunk_quant_priority() -> None:
    engine, db = _db()
    try:
        terminal = _run(
            db,
            as_of_date=date(2026, 7, 23),
            status="COMPLETE",
            created_at=_NOW - timedelta(hours=2),
        )
        _candidate(
            db,
            terminal,
            symbol="MSFT.US",
            selected=True,
            rank=2,
            score=82.5,
        )
        _candidate(
            db,
            terminal,
            symbol="AAPL.US",
            selected=True,
            rank=1,
            score=91.25,
        )
        _candidate(
            db,
            terminal,
            symbol="META.US",
            selected=False,
            rank=None,
            score=99.0,
        )
        incomplete_terminal = _run(
            db,
            as_of_date=date(2026, 7, 25),
            status="COMPLETE",
            created_at=_NOW - timedelta(minutes=10),
        )
        incomplete_terminal.completed_at = None
        _candidate(
            db,
            incomplete_terminal,
            symbol="AMD.US",
            selected=True,
            rank=1,
            score=97.0,
        )
        running = _run(
            db,
            as_of_date=date(2026, 7, 24),
            status="RUNNING",
            created_at=_NOW - timedelta(minutes=5),
        )
        _candidate(
            db,
            running,
            symbol="NVDA.US",
            selected=True,
            rank=1,
            score=98.0,
        )
        db.add(StrategyConfig(symbol="MSFT.US", market="US"))
        db.add(
            StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
        )
        db.add_all(
            [
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=55.0,
                    confidence=0.5,
                    recommended_action="CANDIDATE",
                    source="quant_v1",
                    created_at=_NOW - timedelta(hours=2),
                    expires_at=_NOW - timedelta(hours=1),
                ),
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=23.5,
                    confidence=0.92,
                    recommended_action="AVOID",
                    source="quant_v3",
                    created_at=_NOW - timedelta(minutes=5),
                    expires_at=_NOW + timedelta(minutes=30),
                ),
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=99.0,
                    confidence=1.0,
                    recommended_action="BUY",
                    source="llm",
                    created_at=_NOW - timedelta(minutes=1),
                    expires_at=_NOW + timedelta(hours=1),
                ),
                WatchlistScore(
                    symbol="MSFT.US",
                    market="US",
                    score=88.0,
                    confidence=0.8,
                    recommended_action="BUY",
                    source="review_llm",
                    created_at=_NOW - timedelta(minutes=1),
                    expires_at=_NOW + timedelta(hours=1),
                ),
            ]
        )
        db.commit()

        response = UniversePromotionService(db, now=_NOW).get_readiness()

        assert response is not None
        assert response.universe_run_id == terminal.id
        assert response.as_of_date == date(2026, 7, 23)
        assert response.generated_at == _NOW
        assert (
            response.priority_algorithm_version
            == "selection-quant-shrinkage-v1"
        )
        assert [item.symbol for item in response.items] == [
            "AAPL.US",
            "MSFT.US",
        ]
        assert [item.rank for item in response.items] == [1, 2]
        assert [item.priority_rank for item in response.items] == [1, 2]
        by_symbol = {item.symbol: item for item in response.items}
        aapl = by_symbol["AAPL.US"]
        msft = by_symbol["MSFT.US"]
        assert aapl.selection_score == 91.25
        assert aapl.priority_score == 82.72
        assert aapl.quant_weight == 0.322
        assert aapl.shadow_enabled is True
        assert aapl.is_trading_target is False
        assert aapl.quant_score == 23.5
        assert aapl.quant_confidence == 0.92
        assert aapl.quant_recommended_action == "AVOID"
        assert aapl.quant_source == "quant_v3"
        assert aapl.quant_fresh is True
        assert aapl.quant_expires_at == (
            _NOW + timedelta(minutes=30)
        )
        assert msft.priority_score == 82.5
        assert msft.quant_weight == 0
        assert msft.shadow_enabled is False
        assert msft.is_trading_target is True
        assert msft.quant_score is None
        assert msft.quant_confidence is None
        assert msft.quant_recommended_action == ""
        assert msft.quant_source == ""
        assert msft.quant_fresh is False
        assert msft.quant_expires_at is None
        for item in response.items:
            assert item.forward_status == "NOT_REGISTERED"
            assert item.included_pairs == 0
            assert item.minimum_ready_pairs == 5
            assert item.minimum_mature_pairs == 20
            assert item.remaining_ready_pairs == 5
            assert item.remaining_mature_pairs == 20
            assert item.blockers == []
            assert item.baseline_metrics == StrategyV2ShadowMetrics()
            assert item.candidate_metrics == StrategyV2ShadowMetrics()
            assert item.review_ready is False
            assert item.mature_evidence is False
            assert item.automatic_promotion_allowed is False
    finally:
        db.close()
        engine.dispose()


def test_priority_ignores_legacy_and_stale_quant_scores() -> None:
    engine, db = _db()
    try:
        run = _run(
            db,
            as_of_date=date(2026, 7, 23),
            status="COMPLETE",
            created_at=_NOW - timedelta(hours=1),
        )
        _candidate(
            db,
            run,
            symbol="MSFT.US",
            selected=True,
            rank=1,
            score=80.0,
        )
        _candidate(
            db,
            run,
            symbol="AMD.US",
            selected=True,
            rank=2,
            score=79.0,
        )
        _candidate(
            db,
            run,
            symbol="AAPL.US",
            selected=True,
            rank=3,
            score=70.0,
        )
        db.add_all(
            [
                WatchlistScore(
                    symbol="AAPL.US",
                    market="US",
                    score=100.0,
                    confidence=1.0,
                    recommended_action="CANDIDATE",
                    source="quant_v3",
                    created_at=_NOW - timedelta(minutes=5),
                    expires_at=_NOW + timedelta(minutes=30),
                ),
                WatchlistScore(
                    symbol="MSFT.US",
                    market="US",
                    score=100.0,
                    confidence=1.0,
                    recommended_action="CANDIDATE",
                    source="quant_v2",
                    created_at=_NOW - timedelta(minutes=5),
                    expires_at=_NOW + timedelta(minutes=30),
                ),
                WatchlistScore(
                    symbol="MSFT.US",
                    market="US",
                    score=0.0,
                    confidence=0.0,
                    recommended_action="AVOID",
                    source="quant_error_v3",
                    created_at=_NOW - timedelta(minutes=10),
                    expires_at=_NOW + timedelta(minutes=20),
                ),
                WatchlistScore(
                    symbol="AMD.US",
                    market="US",
                    score=100.0,
                    confidence=1.0,
                    recommended_action="CANDIDATE",
                    source="quant_v3",
                    created_at=_NOW - timedelta(hours=2),
                    expires_at=_NOW - timedelta(minutes=1),
                ),
            ]
        )
        db.commit()

        response = UniversePromotionService(db, now=_NOW).get_readiness()

        assert response is not None
        assert [item.symbol for item in response.items] == [
            "AAPL.US",
            "MSFT.US",
            "AMD.US",
        ]
        by_symbol = {item.symbol: item for item in response.items}
        assert by_symbol["AAPL.US"].priority_score == 87.5
        assert by_symbol["AAPL.US"].quant_weight == 0.35
        assert by_symbol["MSFT.US"].priority_score == 80.0
        assert by_symbol["MSFT.US"].quant_weight == 0
        assert by_symbol["MSFT.US"].quant_score == 0
        assert by_symbol["MSFT.US"].quant_source == "quant_error_v3"
        assert by_symbol["MSFT.US"].quant_fresh is True
        assert by_symbol["AMD.US"].priority_score == 79.0
        assert by_symbol["AMD.US"].quant_weight == 0
        assert by_symbol["AMD.US"].quant_source == "quant_v3"
        assert by_symbol["AMD.US"].quant_fresh is False
    finally:
        db.close()
        engine.dispose()


def test_readiness_maps_review_flags_without_writes(
    monkeypatch,
) -> None:
    engine, db = _db()
    try:
        run = _run(
            db,
            as_of_date=date(2026, 7, 23),
            status="DEGRADED",
            created_at=_NOW - timedelta(hours=1),
        )
        _candidate(
            db,
            run,
            symbol="AAPL.US",
            selected=True,
            rank=1,
            score=90.0,
        )
        _candidate(
            db,
            run,
            symbol="MSFT.US",
            selected=True,
            rank=2,
            score=80.0,
        )
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.commit()

        def get_forward_validation(
            _service: StrategyV2ShadowService,
            symbol: str,
        ) -> StrategyV2ForwardValidationResponse:
            if symbol == "AAPL.US":
                return StrategyV2ForwardValidationResponse(
                    status="MATURE_EVIDENCE",
                    included_pairs=20,
                    remaining_ready_pairs=0,
                    remaining_mature_pairs=0,
                    baseline_metrics=StrategyV2ShadowMetrics(
                        closed_trades=10,
                        net_pnl=12.5,
                    ),
                    candidate_metrics=StrategyV2ShadowMetrics(
                        closed_trades=11,
                        net_pnl=25.0,
                    ),
                )
            return StrategyV2ForwardValidationResponse(
                status="READY_FOR_REVIEW",
                included_pairs=5,
                remaining_ready_pairs=0,
                remaining_mature_pairs=15,
            )

        monkeypatch.setattr(
            StrategyV2ShadowService,
            "get_forward_validation",
            get_forward_validation,
        )
        writes: list[str] = []

        def capture_writes(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            normalized = statement.lstrip().upper()
            if normalized.startswith(("INSERT", "UPDATE", "DELETE")):
                writes.append(normalized.split(maxsplit=1)[0])

        event.listen(engine, "before_cursor_execute", capture_writes)
        response = UniversePromotionService(db, now=_NOW).get_readiness()
        event.remove(engine, "before_cursor_execute", capture_writes)

        assert response is not None
        mature, ready = response.items
        assert mature.forward_status == "MATURE_EVIDENCE"
        assert mature.minimum_ready_pairs == 5
        assert mature.minimum_mature_pairs == 20
        assert mature.review_ready is True
        assert mature.mature_evidence is True
        assert mature.candidate_metrics.net_pnl == 25.0
        assert ready.forward_status == "READY_FOR_REVIEW"
        assert ready.minimum_ready_pairs == 5
        assert ready.minimum_mature_pairs == 20
        assert ready.review_ready is True
        assert ready.mature_evidence is False
        assert all(
            item.automatic_promotion_allowed is False
            for item in response.items
        )
        assert writes == []
        assert db.query(StrategyConfig).one().symbol == "NVDA.US"
        assert db.query(StrategyV2ForwardRegistration).count() == 0
        assert db.query(StrategyV2ForwardEvidence).count() == 0
        assert not db.new
        assert not db.dirty
        assert not db.deleted
    finally:
        db.close()
        engine.dispose()


def test_promotion_readiness_endpoint_returns_404_without_terminal_run(
    monkeypatch,
) -> None:
    engine, db = _db()
    api = FastAPI()
    api.include_router(universe_api.router)

    def override_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(
        universe_api,
        "get_runner",
        lambda: (_ for _ in ()).throw(
            AssertionError("readiness must not access the runner or broker")
        ),
    )
    client = TestClient(api)
    try:
        missing = client.get("/api/universe/promotion-readiness")
        assert missing.status_code == 404
        assert missing.json() == {
            "detail": "no universe selection run available",
        }

        _run(
            db,
            as_of_date=date(2026, 7, 24),
            status="RUNNING",
            created_at=_NOW,
        )
        db.commit()
        still_missing = client.get("/api/universe/promotion-readiness")
        assert still_missing.status_code == 404
    finally:
        client.close()
        db.close()
        engine.dispose()
