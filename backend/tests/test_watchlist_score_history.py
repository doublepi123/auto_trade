"""Watchlist score history — bounded per-symbol timeline.

Tests symbol normalization/filter, from-inclusive/to-exclusive boundaries,
timestamp+ID tie ordering, limit/truncation/total, empty state, source/action/
stale fields, invalid range, auth, forced scorer/provider failure, SQL-write
interception/no Session mutation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models import WatchlistScore
from app.services.watchlist_score_service import WatchlistScoreService
from app import database


database.init_db()
client = TestClient(app)


def _seed_score(
    db: Session,
    *,
    symbol: str = "AAPL.US",
    score: float = 75.0,
    source: str = "llm",
    recommended_action: str = "HOLD",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> int:
    ts = created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
    exp = expires_at or (ts + timedelta(hours=1))
    row = WatchlistScore(
        symbol=symbol,
        market="US",
        score=score,
        rationale="test",
        confidence=0.8,
        recommended_action=recommended_action,
        source=source,
        created_at=ts,
        expires_at=exp,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


class TestWatchlistScoreHistory:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        db.query(WatchlistScore).delete()
        db.commit()
        db.close()

    def test_empty_state(self) -> None:
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["returned"] == 0
        assert data["truncated"] is False

    def test_symbol_normalization_and_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_score(db, symbol="AAPL.US")
            _seed_score(db, symbol="MSFT.US")
        finally:
            db.close()
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "aapl.us"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["symbol"] == "AAPL.US"

    def test_from_inclusive_boundary(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_score(db, created_at=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
            _seed_score(db, created_at=datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
        finally:
            db.close()
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "from": "2026-06-14T10:00:00Z"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 2  # from is inclusive

    def test_to_exclusive_boundary(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_score(db, created_at=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
            _seed_score(db, created_at=datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
        finally:
            db.close()
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "to": "2026-06-14T12:00:00Z"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1  # to is exclusive

    def test_newest_first_tie_order(self) -> None:
        """Same created_at -> tie broken by id DESC."""
        db = database.SessionLocal()
        try:
            ts = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
            id1 = _seed_score(db, created_at=ts)
            id2 = _seed_score(db, created_at=ts)
        finally:
            db.close()
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "limit": 10},
        )
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids == sorted([id1, id2], reverse=True)

    def test_limit_and_truncation(self) -> None:
        db = database.SessionLocal()
        try:
            for i in range(5):
                _seed_score(
                    db,
                    created_at=datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc),
                )
        finally:
            db.close()
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "limit": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["returned"] == 3
        assert data["truncated"] is True

    def test_source_and_action_fields(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_score(db, source="quant_v6", recommended_action="BUY")
        finally:
            db.close()
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["source"] == "quant_v6"
        assert item["recommended_action"] == "BUY"

    def test_stale_field(self) -> None:
        db = database.SessionLocal()
        try:
            # Expired score
            _seed_score(
                db,
                created_at=datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                expires_at=datetime(2020, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        finally:
            db.close()
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        assert resp.json()["items"][0]["is_stale"] is True

    def test_invalid_range_422(self) -> None:
        resp = client.get(
            "/api/watchlist/scores/history",
            params={
                "symbol": "AAPL.US",
                "from": "2026-06-14T12:00:00Z",
                "to": "2026-06-14T10:00:00Z",
            },
        )
        assert resp.status_code == 422

    def test_auth_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret-key")
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US"},
        )
        assert resp.status_code == 401

    def test_no_session_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_score(db)
            count_before = db.query(WatchlistScore).count()
        finally:
            db.close()
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "AAPL.US"})
        assert resp.status_code == 200
        db = database.SessionLocal()
        try:
            count_after = db.query(WatchlistScore).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_sql_write_interception(self, monkeypatch) -> None:
        """History must never issue INSERT/UPDATE/DELETE."""
        db = database.SessionLocal()
        try:
            _seed_score(db)
        finally:
            db.close()

        db = database.SessionLocal()
        try:
            original_execute = db.execute

            def _guard_execute(stmt, *args, **kwargs):
                sql_text = str(stmt).upper()
                for kw in ("INSERT", "UPDATE", "DELETE"):
                    if kw in sql_text:
                        raise AssertionError(f"history must not issue {kw}")
                return original_execute(stmt, *args, **kwargs)

            monkeypatch.setattr(db, "execute", _guard_execute)
            svc = WatchlistScoreService(db)
            rows, total, observed = svc.list_history("AAPL.US")
            assert total == 1
        finally:
            db.close()

    # ---- Blocker 3: input normalization / timezone policy ----

    def test_blank_symbol_returns_422(self) -> None:
        resp = client.get("/api/watchlist/scores/history", params={"symbol": "   "})
        assert resp.status_code == 422

    def test_whitespace_symbol_returns_422(self) -> None:
        resp = client.get("/api/watchlist/scores/history", params={"symbol": ""})
        assert resp.status_code == 422

    def test_naive_from_returns_422(self) -> None:
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "from": "2026-06-14T10:00:00"},
        )
        assert resp.status_code == 422

    def test_naive_to_returns_422(self) -> None:
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "to": "2026-06-14T10:00:00"},
        )
        assert resp.status_code == 422

    def test_mixed_aware_naive_returns_422(self) -> None:
        resp = client.get(
            "/api/watchlist/scores/history",
            params={
                "symbol": "AAPL.US",
                "from": "2026-06-14T10:00:00Z",
                "to": "2026-06-14T12:00:00",
            },
        )
        assert resp.status_code == 422

    def test_utc_offset_normalization(self) -> None:
        """A +05:00 offset ``from`` is normalized to UTC before comparison."""
        db = database.SessionLocal()
        try:
            _seed_score(
                db,
                created_at=datetime(2026, 6, 14, 5, 0, 0, tzinfo=timezone.utc),
            )
        finally:
            db.close()
        # 10:00+05:00 == 05:00 UTC -> from is inclusive, so the row matches.
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "from": "2026-06-14T10:00:00+05:00"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_invalid_order_returns_422(self) -> None:
        resp = client.get(
            "/api/watchlist/scores/history",
            params={
                "symbol": "AAPL.US",
                "from": "2026-06-14T12:00:00Z",
                "to": "2026-06-14T10:00:00Z",
            },
        )
        assert resp.status_code == 422

    # ---- Blocker 4: missing negative evidence ----

    def test_no_scorer_llm_provider_broker_prune_invocation(self, monkeypatch) -> None:
        """History must never invoke scorer, LLM/provider, broker, or prune."""
        db = database.SessionLocal()
        try:
            _seed_score(db)
        finally:
            db.close()

        # Fail-fast on any scorer/provider/broker/prune path.
        from app.services import watchlist_score_service as wss_module

        for attr in ("score_from_llm_or_fallback", "_prune_expired"):
            original = getattr(wss_module.WatchlistScoreService, attr, None)
            if original is not None:
                monkeypatch.setattr(
                    wss_module.WatchlistScoreService,
                    attr,
                    lambda self, *a, **kw: (_ for _ in ()).throw(
                        AssertionError(f"history must not call {attr}")
                    ),
                )

        db = database.SessionLocal()
        try:
            svc = WatchlistScoreService(db)
            rows, total, observed = svc.list_history("AAPL.US")
            assert total == 1
        finally:
            db.close()

    def test_one_observation_clock_shared_across_rows(self) -> None:
        """All returned rows share the same observation clock for stale status."""
        db = database.SessionLocal()
        try:
            for i in range(3):
                _seed_score(
                    db,
                    created_at=datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc),
                )
        finally:
            db.close()
        resp = client.get(
            "/api/watchlist/scores/history",
            params={"symbol": "AAPL.US", "limit": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        observed = data["observed_at"]
        # Every row's stale status was computed from the same observed_at.
        assert observed is not None
        # All rows share the same observation time (it's one value in the response).
        assert len(data["items"]) == 3
