"""Strategy version diff — read-only stable diff over _VERSIONED_COLUMNS.

Tests added/removed/changed/equal, deterministic column order, null/type
transitions, ignored unknown keys, missing from/to IDs, authentication, and
SQL-write interception/no Session mutation.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models import StrategyParamVersion
from app.services.strategy_version_service import (
    StrategyVersionService,
    _VERSIONED_COLUMNS,
    build_version_diff,
)
from app import database


database.init_db()
client = TestClient(app)


def _seed_version(
    db: Session,
    params: dict,
    *,
    actor_hash: str | None = None,
) -> int:
    row = StrategyParamVersion(
        params_json=json.dumps(params, default=str),
        actor_hash=actor_hash,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _base_params(**overrides) -> dict:
    params: dict = {col: 0 for col in _VERSIONED_COLUMNS}
    params["symbol"] = "AAPL.US"
    params["market"] = "US"
    params["buy_low"] = 100.0
    params["sell_high"] = 110.0
    params["short_selling"] = False
    params.update(overrides)
    return params


class TestStrategyVersionDiff:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        db.query(StrategyParamVersion).delete()
        db.commit()
        db.close()

    def test_equal_versions_empty_diff(self) -> None:
        db = database.SessionLocal()
        try:
            params = _base_params()
            from_id = _seed_version(db, params)
            to_id = _seed_version(db, params)
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["added"] == []
        assert data["removed"] == []
        assert data["changed"] == []

    def test_changed_field(self) -> None:
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params(buy_low=100.0))
            to_id = _seed_version(db, _base_params(buy_low=105.0))
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["changed"]) == 1
        assert data["changed"][0]["field"] == "buy_low"
        assert data["changed"][0]["from_value"] == 100.0
        assert data["changed"][0]["to_value"] == 105.0

    def test_added_field(self) -> None:
        db = database.SessionLocal()
        try:
            from_params = _base_params()
            del from_params["fee_rate_us"]
            from_id = _seed_version(db, from_params)
            to_id = _seed_version(db, _base_params())
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["added"]) == 1
        assert data["added"][0]["field"] == "fee_rate_us"

    def test_removed_field(self) -> None:
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params())
            to_params = _base_params()
            del to_params["fee_rate_hk"]
            to_id = _seed_version(db, to_params)
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["removed"]) == 1
        assert data["removed"][0]["field"] == "fee_rate_hk"

    def test_deterministic_column_order(self) -> None:
        """Changed entries follow _VERSIONED_COLUMNS tuple order."""
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params(buy_low=100.0, sell_high=110.0))
            to_id = _seed_version(db, _base_params(buy_low=105.0, sell_high=115.0))
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        changed_fields = [e["field"] for e in resp.json()["changed"]]
        # buy_low comes before sell_high in _VERSIONED_COLUMNS
        assert changed_fields == ["buy_low", "sell_high"]

    def test_null_and_type_transitions(self) -> None:
        """Null-to-value and type changes are preserved truthfully."""
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params(max_drawdown_amount=None))
            to_id = _seed_version(db, _base_params(max_drawdown_amount=500.0))
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        changed = resp.json()["changed"]
        assert len(changed) == 1
        assert changed[0]["field"] == "max_drawdown_amount"
        assert changed[0]["from_value"] is None
        assert changed[0]["to_value"] == 500.0

    def test_unknown_keys_ignored(self) -> None:
        """Keys outside _VERSIONED_COLUMNS are not compared."""
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, {**_base_params(), "unknown_key": "x"})
            to_id = _seed_version(db, {**_base_params(), "unknown_key": "y"})
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["changed"] == []
        assert data["added"] == []
        assert data["removed"] == []

    def test_missing_from_id_404(self) -> None:
        db = database.SessionLocal()
        try:
            to_id = _seed_version(db, _base_params())
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": 99999, "to_version_id": to_id},
        )
        assert resp.status_code == 404
        assert "99999" in resp.json()["detail"]

    def test_missing_to_id_404(self) -> None:
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params())
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": 99999},
        )
        assert resp.status_code == 404
        assert "99999" in resp.json()["detail"]

    def test_both_missing_404(self) -> None:
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": 99998, "to_version_id": 99999},
        )
        assert resp.status_code == 404

    def test_auth_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret-key")
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": 1, "to_version_id": 2},
        )
        assert resp.status_code == 401

    def test_no_session_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params(buy_low=100.0))
            to_id = _seed_version(db, _base_params(buy_low=105.0))
            count_before = db.query(StrategyParamVersion).count()
        finally:
            db.close()
        resp = client.get(
            "/api/strategy/versions/diff",
            params={"from_version_id": from_id, "to_version_id": to_id},
        )
        assert resp.status_code == 200
        db = database.SessionLocal()
        try:
            count_after = db.query(StrategyParamVersion).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_sql_write_interception(self, monkeypatch) -> None:
        """The diff must never issue INSERT/UPDATE/DELETE."""
        db = database.SessionLocal()
        try:
            from_id = _seed_version(db, _base_params(buy_low=100.0))
            to_id = _seed_version(db, _base_params(buy_low=105.0))
        finally:
            db.close()

        db = database.SessionLocal()
        try:
            original_execute = db.execute

            def _guard_execute(stmt, *args, **kwargs):
                sql_text = str(stmt).upper()
                for kw in ("INSERT", "UPDATE", "DELETE"):
                    if kw in sql_text:
                        raise AssertionError(f"diff must not issue {kw}")
                return original_execute(stmt, *args, **kwargs)

            monkeypatch.setattr(db, "execute", _guard_execute)
            svc = StrategyVersionService(db)
            pair = svc.load_version_pair(from_id, to_id)
            assert pair is not None
            result = build_version_diff(*pair)
            assert len(result["changed"]) == 1
        finally:
            db.close()
