"""Universe selection explainer — service + API. Per-module sqlite."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.universe_explainer import router
from app.config import settings
from app.database import get_db
from app.models import (
    Base,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.universe_explainer_service import UniverseExplainerService


_NOW = datetime(2026, 7, 30, 14, 31, tzinfo=timezone.utc)
_TODAY = _NOW.date()


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=cls.engine,
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
        with self.session_factory() as db:
            db.query(UniverseSelectionCandidate).delete()
            db.query(UniverseSelectionRun).delete()
            db.commit()

    def _db(self) -> Session:
        return self.session_factory()

    def _add_run(
        self,
        *,
        status: str = "COMPLETE",
        candidate_count: int = 0,
        selected_count: int = 0,
        coverage_ratio: float = 0.0,
        as_of_date: date = _TODAY,
    ) -> UniverseSelectionRun:
        run = UniverseSelectionRun(
            as_of_date=as_of_date,
            algorithm_version="v1",
            source_version="v1",
            status=status,
            candidate_count=candidate_count,
            evaluable_count=candidate_count,
            selected_count=selected_count,
            coverage_ratio=coverage_ratio,
            started_at=_NOW,
            completed_at=_NOW,
            created_at=_NOW,
        )
        db = self._db()
        db.add(run)
        db.commit()
        db.refresh(run)
        db.close()
        return run

    def _add_candidate(
        self,
        run_id: int,
        *,
        symbol: str,
        selected: bool,
        rank: int | None,
        score: float,
        metrics: dict | None = None,
        exclusion_reasons: list[str] | None = None,
    ) -> UniverseSelectionCandidate:
        candidate = UniverseSelectionCandidate(
            run_id=run_id,
            symbol=symbol,
            selected=selected,
            rank=rank,
            score=score,
            metrics_json=json.dumps(metrics or {}),
            exclusion_reasons_json=json.dumps(exclusion_reasons or []),
            created_at=_NOW,
        )
        db = self._db()
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        db.close()
        return candidate


class TestExplainSelectionEmpty(_Base):
    def test_no_runs_returns_empty_response(self) -> None:
        result = UniverseExplainerService(self._db()).explain_selection("AAPL.US")
        assert result["symbol"] == "AAPL.US"
        assert result["selected"] is False
        assert result["run_id"] is None
        assert result["as_of_date"] is None
        assert result["hard_filters_failed"] == []
        assert result["peer_comparison"] == []

    def test_symbol_not_in_latest_run(self) -> None:
        run = self._add_run(candidate_count=1, selected_count=1)
        self._add_candidate(
            run.id, symbol="MSFT.US", selected=True, rank=1, score=90.0,
        )
        result = UniverseExplainerService(self._db()).explain_selection("AAPL.US")
        # The symbol wasn't considered → empty-state body with run context.
        assert result["selected"] is False
        assert result["run_id"] == run.id
        assert result["hard_filters_failed"] == []

    def test_api_symbol_endpoint_no_data(self) -> None:
        resp = self.client.get("/api/universe-explainer/symbol/AAPL.US")
        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "AAPL.US"
        assert body["selected"] is False
        assert body["peer_comparison"] == []


class TestExplainSelectionWithData(_Base):
    def test_selected_candidate_shows_score_breakdown_and_passed_filters(self) -> None:
        run = self._add_run(candidate_count=2, selected_count=1, coverage_ratio=1.0)
        self._add_candidate(
            run.id,
            symbol="AAPL.US",
            selected=True,
            rank=1,
            score=88.5,
            metrics={
                "momentum": 0.92,
                "liquidity": 0.81,
                "volatility": 0.35,
                "sector": "TECH",
            },
        )
        result = UniverseExplainerService(self._db()).explain_selection("aapl.us")
        assert result["symbol"] == "AAPL.US"  # normalized
        assert result["selected"] is True
        assert result["rank"] == 1
        assert result["score"] == 88.5
        # score_breakdown lifts numeric factors; drops the non-numeric sector.
        assert result["score_breakdown"]["headline_score"] == 88.5
        assert result["score_breakdown"]["momentum"] == 0.92
        assert result["score_breakdown"]["liquidity"] == 0.81
        assert "sector" not in result["score_breakdown"]
        # No exclusion reasons → no failed hard filters.
        assert result["hard_filters_failed"] == []
        # Known filters that did not fire appear as passed.
        assert "INSUFFICIENT_HISTORY" in result["hard_filters_passed"]

    def test_rejected_candidate_reports_failed_hard_filters(self) -> None:
        run = self._add_run(candidate_count=1, selected_count=0)
        self._add_candidate(
            run.id,
            symbol="PENN.US",
            selected=False,
            rank=None,
            score=12.0,
            exclusion_reasons=["ILLIQUID", "WIDE_SPREAD"],
        )
        result = UniverseExplainerService(self._db()).explain_selection("PENN.US")
        assert result["selected"] is False
        assert set(result["hard_filters_failed"]) == {"ILLIQUID", "WIDE_SPREAD"}
        # The two failed filters must NOT also appear in passed.
        passed = set(result["hard_filters_passed"])
        assert "ILLIQUID" not in passed
        assert "WIDE_SPREAD" not in passed

    def test_peer_comparison_returns_top_five(self) -> None:
        run = self._add_run(candidate_count=6, selected_count=2, coverage_ratio=0.5)
        self._add_candidate(run.id, symbol="AAPL.US", selected=True, rank=1, score=90.0)
        self._add_candidate(run.id, symbol="MSFT.US", selected=True, rank=2, score=85.0)
        self._add_candidate(run.id, symbol="NVDA.US", selected=False, rank=None, score=80.0)
        self._add_candidate(run.id, symbol="AMD.US", selected=False, rank=None, score=70.0)
        self._add_candidate(run.id, symbol="INTC.US", selected=False, rank=None, score=60.0)
        self._add_candidate(run.id, symbol="IBM.US", selected=False, rank=None, score=50.0)

        result = UniverseExplainerService(self._db()).explain_selection("AAPL.US")
        peers = result["peer_comparison"]
        assert len(peers) == 5  # capped at 5
        # The focus symbol is flagged.
        focus = [p for p in peers if p["is_focus"]]
        assert len(focus) == 1
        assert focus[0]["symbol"] == "AAPL.US"
        # Selected candidates rank ahead of rejected ones.
        assert peers[0]["selected"] is True


class TestExplainRun(_Base):
    def test_no_runs_returns_not_found_state(self) -> None:
        result = UniverseExplainerService(self._db()).explain_run()
        assert result["status"] == "NOT_FOUND"
        assert result["total_candidates"] == 0
        assert result["top_selected"] == []

    def test_latest_run_summary(self) -> None:
        run = self._add_run(
            candidate_count=3, selected_count=1, coverage_ratio=0.33,
        )
        self._add_candidate(run.id, symbol="AAPL.US", selected=True, rank=1, score=90.0)
        self._add_candidate(
            run.id, symbol="PENN.US", selected=False, rank=None, score=40.0,
            exclusion_reasons=["ILLIQUID"],
        )
        self._add_candidate(
            run.id, symbol="GME.US", selected=False, rank=None, score=20.0,
            exclusion_reasons=["VOLATILITY_OUT_OF_RANGE"],
        )

        result = UniverseExplainerService(self._db()).explain_run()
        assert result["run_id"] == run.id
        assert result["status"] == "COMPLETE"
        assert result["total_candidates"] == 3
        assert result["selected_count"] == 1
        assert result["coverage_ratio"] == 0.33
        assert len(result["top_selected"]) == 1
        assert result["top_selected"][0]["symbol"] == "AAPL.US"
        # Rejected sorted by score desc → PENN before GME.
        assert result["top_rejected"][0]["symbol"] == "PENN.US"
        assert result["top_rejected"][1]["symbol"] == "GME.US"
        assert result["top_rejected"][0]["exclusion_reasons"] == ["ILLIQUID"]

    def test_explicit_run_id(self) -> None:
        old = self._add_run(
            candidate_count=1, selected_count=1,
            as_of_date=date(2026, 7, 1),
        )
        latest = self._add_run(
            candidate_count=2, selected_count=1,
            as_of_date=date(2026, 7, 30),
        )
        # No run_id → latest wins.
        assert UniverseExplainerService(self._db()).explain_run()["run_id"] == latest.id
        # Explicit old run_id → that run.
        result = UniverseExplainerService(self._db()).explain_run(old.id)
        assert result["run_id"] == old.id
        assert result["as_of_date"] == "2026-07-01"

    def test_top_lists_capped_at_ten(self) -> None:
        run = self._add_run(candidate_count=15, selected_count=12)
        for i in range(12):
            self._add_candidate(
                run.id, symbol=f"S{i:02d}.US", selected=True, rank=i + 1, score=90.0 - i,
            )
        for i in range(3):
            self._add_candidate(
                run.id, symbol=f"R{i:02d}.US", selected=False, rank=None, score=10.0 - i,
            )
        result = UniverseExplainerService(self._db()).explain_run()
        assert len(result["top_selected"]) == 10
        assert len(result["top_rejected"]) == 3


class TestExplainRunApi(_Base):
    def test_run_endpoint_returns_summary(self) -> None:
        run = self._add_run(candidate_count=1, selected_count=1, coverage_ratio=1.0)
        self._add_candidate(run.id, symbol="AAPL.US", selected=True, rank=1, score=90.0)

        resp = self.client.get("/api/universe-explainer/run")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run.id
        assert body["selected_count"] == 1
        assert body["top_selected"][0]["symbol"] == "AAPL.US"

    def test_run_endpoint_missing_explicit_run_is_404(self) -> None:
        resp = self.client.get(
            "/api/universe-explainer/run", params={"run_id": 999999}
        )
        assert resp.status_code == 404

    def test_run_endpoint_no_runs_returns_not_found_state_200(self) -> None:
        resp = self.client.get("/api/universe-explainer/run")
        assert resp.status_code == 200
        assert resp.json()["status"] == "NOT_FOUND"
