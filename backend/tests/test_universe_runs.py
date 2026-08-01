"""Universe selection run history — service + API. Per-file sqlite.

The history path is strictly read-only: it must query stored rows only and
never invoke selection, quote fetches, refresh, or shadow synchronization.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Generator

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_universe_runs_{os.getpid()}.db"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import universe as universe_api
from app.database import get_db
from app.models import Base, UniverseSelectionRun
from app.services.universe_selection_service import UniverseSelectionService


def _make_run(
    db: Session,
    *,
    as_of_date: date,
    created_at: datetime,
    status: str = "COMPLETE",
    run_id: int | None = None,
    selected_count: int = 1,
    algorithm_version: str = "test-v1",
    source_version: str = "test-src-v1",
) -> int:
    run = UniverseSelectionRun(
        as_of_date=as_of_date,
        algorithm_version=algorithm_version,
        source_version=source_version,
        status=status,
        candidate_count=10,
        evaluable_count=10,
        selected_count=selected_count,
        coverage_ratio=1.0,
        parameters_json="{}",
        error="",
        started_at=created_at,
        completed_at=created_at,
        created_at=created_at,
    )
    if run_id is not None:
        # Force a specific id by inserting then updating is not portable; rely
        # on autoincrement ordering instead for tie tests.
        pass
    db.add(run)
    db.commit()
    db.refresh(run)
    row_id = run.id
    db.close()
    return row_id


class _NoCallBroker:
    """Broker stub that fails if any market-data method is invoked.

    The history path must never fetch quotes or candlesticks, so any call
    here proves a write/selection path leaked into the read-only endpoint.
    """

    def get_quotes(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("history path must not fetch quotes")

    def get_candlesticks(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("history path must not fetch candlesticks")


def _service(db: Session) -> UniverseSelectionService:
    return UniverseSelectionService(
        db,
        _NoCallBroker(),
        catalog=(
            __import__(
                "app.domain.universe_selection.catalog",
                fromlist=["INDEX_CANDIDATE_CATALOG"],
            ).INDEX_CANDIDATE_CATALOG
        ),
        minimum_evaluable_ratio=0.5,
        apply_to_watchlist=False,
        enable_shadow=False,
        now=datetime(2026, 7, 23, 19, tzinfo=timezone.utc),
    )


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


class TestUniverseRunHistoryService:
    def test_empty_state(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            rows, total = _service(db).list_runs()
            assert rows == []
            assert total == 0
        finally:
            db.close()
            engine.dispose()

    def test_newest_first_ordering(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(db, as_of_date=date(2026, 6, 14), created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc))
            _make_run(db, as_of_date=date(2026, 6, 16), created_at=datetime(2026, 6, 16, 10, tzinfo=timezone.utc))
            rows, total = _service(db).list_runs()
            assert total == 2
            assert [r.as_of_date.isoformat() for r in rows] == ["2026-06-16", "2026-06-14"]
        finally:
            db.close()
            engine.dispose()

    def test_pagination_and_total(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            for day in range(1, 6):
                _make_run(
                    db,
                    as_of_date=date(2026, 6, day),
                    created_at=datetime(2026, 6, day, 10, tzinfo=timezone.utc),
                )
            rows_p1, total = _service(db).list_runs(page=1, page_size=2)
            assert total == 5
            assert [r.as_of_date.isoformat() for r in rows_p1] == ["2026-06-05", "2026-06-04"]
            rows_p2, _ = _service(db).list_runs(page=2, page_size=2)
            assert [r.as_of_date.isoformat() for r in rows_p2] == ["2026-06-03", "2026-06-02"]
            rows_p3, _ = _service(db).list_runs(page=3, page_size=2)
            assert [r.as_of_date.isoformat() for r in rows_p3] == ["2026-06-01"]
            # Out-of-range page -> empty, total intact.
            rows_empty, total_empty = _service(db).list_runs(page=10, page_size=2)
            assert rows_empty == []
            assert total_empty == 5
        finally:
            db.close()
            engine.dispose()

    def test_date_filters_inclusive_boundaries(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(db, as_of_date=date(2026, 6, 14), created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc))
            _make_run(db, as_of_date=date(2026, 6, 15), created_at=datetime(2026, 6, 15, 10, tzinfo=timezone.utc))
            _make_run(db, as_of_date=date(2026, 6, 16), created_at=datetime(2026, 6, 16, 10, tzinfo=timezone.utc))
            rows, total = _service(db).list_runs(
                from_date=date(2026, 6, 15),
                to_date=date(2026, 6, 16),
            )
            assert total == 2
            assert [r.as_of_date.isoformat() for r in rows] == ["2026-06-16", "2026-06-15"]
            # Inclusive boundary on from_date.
            rows_from, total_from = _service(db).list_runs(from_date=date(2026, 6, 15))
            assert total_from == 2
        finally:
            db.close()
            engine.dispose()

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            with pytest.raises(ValueError):
                _service(db).list_runs(
                    from_date=date(2026, 6, 17),
                    to_date=date(2026, 6, 16),
                )
        finally:
            db.close()
            engine.dispose()

    def test_timestamp_ties_paginate_deterministically(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            # The model enforces uniqueness on (as_of_date, algorithm_version,
            # source_version), so genuine ties arise from distinct algorithm or
            # source versions sharing the same as_of_date and created_at. The
            # id tie-breaker must still paginate deterministically.
            ids = []
            for index in range(4):
                ids.append(
                    _make_run(
                        db,
                        as_of_date=date(2026, 6, 14),
                        created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc),
                        algorithm_version=f"test-v{index + 1}",
                    )
                )
            # Page through one at a time; expect strict id-desc order with no
            # duplicates or omissions across pages.
            seen: list[int] = []
            for page in range(1, 5):
                rows, total = _service(db).list_runs(page=page, page_size=1)
                assert total == 4
                assert len(rows) == 1
                seen.append(rows[0].id)
            assert seen == sorted(ids, reverse=True)
            assert len(set(seen)) == 4
        finally:
            db.close()
            engine.dispose()

    def test_page_size_capped(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            for day in range(1, 4):
                _make_run(
                    db,
                    as_of_date=date(2026, 6, day),
                    created_at=datetime(2026, 6, day, 10, tzinfo=timezone.utc),
                )
            # Request an oversized page_size; it is capped to 100, not an error.
            rows, total = _service(db).list_runs(page=1, page_size=10_000)
            assert total == 3
            assert len(rows) == 3
        finally:
            db.close()
            engine.dispose()


class TestUniverseRunHistoryGuard:
    def test_history_does_not_invoke_selection_or_refresh(self, monkeypatch) -> None:
        """The history path must never call selector/refresh/quote code.

        We assert this structurally: ``list_runs`` only issues a SELECT over
        ``UniverseSelectionRun`` and never touches the broker, the selector,
        or the refresh machinery. A call to any of them would raise here.
        """
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            service = _service(db)
            # Instrument the methods that must NOT be called.
            for forbidden in ("refresh", "_refresh_locked", "_evaluate_catalog", "items_for_run"):
                monkeypatch.setattr(
                    service,
                    forbidden,
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError(f"history must not call {forbidden}")
                    ),
                )
            rows, total = service.list_runs()
            assert rows == []
            assert total == 0
        finally:
            db.close()
            engine.dispose()


class TestUniverseRunHistoryAPI:
    def _api(self, db: Session, monkeypatch) -> TestClient:
        api = FastAPI()
        api.include_router(universe_api.router)

        def override_db() -> Generator[Session, None, None]:
            yield db

        api.dependency_overrides[get_db] = override_db
        monkeypatch.setattr(universe_api, "build_universe_selection_service", _service)
        client = TestClient(api)
        return client

    def test_endpoint_empty(self, monkeypatch) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        client = self._api(db, monkeypatch)
        try:
            resp = client.get("/api/universe/runs")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["items"] == []
            assert data["total"] == 0
            assert data["page"] == 1
            assert data["page_size"] == 50
        finally:
            client.close()
            db.close()
            engine.dispose()

    def test_endpoint_pagination_and_filters(self, monkeypatch) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            for day in range(1, 6):
                _make_run(
                    db,
                    as_of_date=date(2026, 6, day),
                    created_at=datetime(2026, 6, day, 10, tzinfo=timezone.utc),
                )
            client = self._api(db, monkeypatch)
            resp = client.get("/api/universe/runs", params={"page": 1, "page_size": 2})
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 5
            assert [i["as_of_date"] for i in data["items"]] == ["2026-06-05", "2026-06-04"]
            # Date filter.
            resp = client.get(
                "/api/universe/runs",
                params={"from_date": "2026-06-04", "to_date": "2026-06-04"},
            )
            assert resp.status_code == 200
            assert resp.json()["total"] == 1
            assert resp.json()["items"][0]["as_of_date"] == "2026-06-04"
        finally:
            client.close()
            db.close()
            engine.dispose()

    def test_endpoint_invalid_date_range_422(self, monkeypatch) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        client = self._api(db, monkeypatch)
        try:
            resp = client.get(
                "/api/universe/runs",
                params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
            )
            assert resp.status_code == 422
        finally:
            client.close()
            db.close()
            engine.dispose()

    def test_endpoint_invalid_date_format_422(self, monkeypatch) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        client = self._api(db, monkeypatch)
        try:
            resp = client.get("/api/universe/runs", params={"from_date": "not-a-date"})
            assert resp.status_code == 422
        finally:
            client.close()
            db.close()
            engine.dispose()

    def test_endpoint_run_response_shape(self, monkeypatch) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(
                db,
                as_of_date=date(2026, 6, 14),
                created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc),
                status="COMPLETE",
                selected_count=3,
            )
            client = self._api(db, monkeypatch)
            resp = client.get("/api/universe/runs")
            assert resp.status_code == 200
            item = resp.json()["items"][0]
            # Reuses UniverseSelectionRunResponse semantics.
            assert item["as_of_date"] == "2026-06-14"
            assert item["status"] == "COMPLETE"
            assert item["selected_count"] == 3
            assert item["algorithm_version"] == "test-v1"
            assert "parameters" in item
            # History list view does not load candidate items.
            assert item["items"] == []
        finally:
            client.close()
            db.close()
            engine.dispose()
