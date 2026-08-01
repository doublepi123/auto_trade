"""Universe selection run history — DB-only service + API. Per-file sqlite.

The history path is strictly read-only and DB-only: it queries stored
``UniverseSelectionRun`` rows via ``UniverseRunHistoryService`` and never
invokes selection, quote fetches, refresh, shadow synchronization, the broker,
``get_runner``, or any write.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import universe as universe_api
from app.database import get_db
from app.models import Base, UniverseSelectionRun
from app.services.universe_run_history_service import UniverseRunHistoryService


def _make_run(
    db: Session,
    *,
    as_of_date: date,
    created_at: datetime,
    status: str = "COMPLETE",
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
    db.add(run)
    db.commit()
    db.refresh(run)
    row_id = run.id
    db.close()
    return row_id


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
            page = UniverseRunHistoryService(db).list_runs()
            assert page.items == []
            assert page.total == 0
            assert page.page == 1
            assert page.page_size == 50
        finally:
            db.close()
            engine.dispose()

    def test_newest_first_ordering(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(db, as_of_date=date(2026, 6, 14), created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc))
            _make_run(db, as_of_date=date(2026, 6, 16), created_at=datetime(2026, 6, 16, 10, tzinfo=timezone.utc))
            page = UniverseRunHistoryService(db).list_runs()
            assert page.total == 2
            assert [i.as_of_date.isoformat() for i in page.items] == ["2026-06-16", "2026-06-14"]
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
            p1 = UniverseRunHistoryService(db).list_runs(page=1, page_size=2)
            assert p1.total == 5
            assert [i.as_of_date.isoformat() for i in p1.items] == ["2026-06-05", "2026-06-04"]
            p2 = UniverseRunHistoryService(db).list_runs(page=2, page_size=2)
            assert [i.as_of_date.isoformat() for i in p2.items] == ["2026-06-03", "2026-06-02"]
            p3 = UniverseRunHistoryService(db).list_runs(page=3, page_size=2)
            assert [i.as_of_date.isoformat() for i in p3.items] == ["2026-06-01"]
            # Out-of-range page -> empty, total intact.
            p_empty = UniverseRunHistoryService(db).list_runs(page=10, page_size=2)
            assert p_empty.items == []
            assert p_empty.total == 5
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
            page = UniverseRunHistoryService(db).list_runs(
                from_date=date(2026, 6, 15),
                to_date=date(2026, 6, 16),
            )
            assert page.total == 2
            assert [i.as_of_date.isoformat() for i in page.items] == ["2026-06-16", "2026-06-15"]
            page_from = UniverseRunHistoryService(db).list_runs(from_date=date(2026, 6, 15))
            assert page_from.total == 2
        finally:
            db.close()
            engine.dispose()

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            with pytest.raises(ValueError):
                UniverseRunHistoryService(db).list_runs(
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
            # source versions sharing the same as_of_date and created_at.
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
            seen: list[int] = []
            for page in range(1, 5):
                result = UniverseRunHistoryService(db).list_runs(page=page, page_size=1)
                assert result.total == 4
                assert len(result.items) == 1
                seen.append(result.items[0].id)
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
            page = UniverseRunHistoryService(db).list_runs(page=1, page_size=10_000)
            assert page.total == 3
            assert len(page.items) == 3
            assert page.page_size == 100
        finally:
            db.close()
            engine.dispose()

    def test_summary_model_has_no_items_field(self) -> None:
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(
                db,
                as_of_date=date(2026, 6, 14),
                created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc),
            )
            page = UniverseRunHistoryService(db).list_runs()
            assert page.items[0].id >= 1
            # The honest summary model must not carry an `items` field.
            assert not hasattr(page.items[0], "items")
        finally:
            db.close()
            engine.dispose()

    def test_history_does_not_touch_session_state(self) -> None:
        """History must query stored rows only and leave the Session clean."""
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(
                db,
                as_of_date=date(2026, 6, 14),
                created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc),
            )
            # The service must not stage any new/dirty/deleted state.
            assert len(db.new) == 0
            assert len(db.dirty) == 0
            assert len(db.deleted) == 0
            page = UniverseRunHistoryService(db).list_runs()
            assert page.total == 1
            assert len(db.new) == 0
            assert len(db.dirty) == 0
            assert len(db.deleted) == 0
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

    def test_endpoint_summary_shape_no_items_field(self, monkeypatch) -> None:
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
            assert item["as_of_date"] == "2026-06-14"
            assert item["status"] == "COMPLETE"
            assert item["selected_count"] == 3
            assert item["algorithm_version"] == "test-v1"
            # The honest summary model must NOT carry candidate items.
            assert "items" not in item
            assert "parameters" not in item
        finally:
            client.close()
            db.close()
            engine.dispose()

    def test_history_succeeds_even_when_runner_broker_construction_raises(
        self, monkeypatch
    ) -> None:
        """History must not depend on the live runner/broker/selection path.

        Force ``build_universe_selection_service`` and ``get_runner`` to raise;
        the history endpoint must still succeed because it uses the DB-only
        service.
        """
        engine = _engine()
        db = sessionmaker(bind=engine)()
        try:
            _make_run(
                db,
                as_of_date=date(2026, 6, 14),
                created_at=datetime(2026, 6, 14, 10, tzinfo=timezone.utc),
            )

            def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
                raise AssertionError(
                    "history must not construct the live selection service"
                )

            monkeypatch.setattr(
                universe_api, "build_universe_selection_service", _boom
            )
            monkeypatch.setattr(universe_api, "get_runner", _boom)

            client = self._api(db, monkeypatch)
            resp = client.get("/api/universe/runs")
            assert resp.status_code == 200, resp.text
            assert resp.json()["total"] == 1
            # Session state remains untouched.
            assert len(db.new) == 0
            assert len(db.dirty) == 0
            assert len(db.deleted) == 0
        finally:
            client.close()
            db.close()
            engine.dispose()
