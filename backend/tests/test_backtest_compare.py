"""Backtest run metric comparison — enhanced compare with baseline + metric table.

Tests baseline default/explicit selection, requested order/dedup/cap, stable
metric ordering, positive/negative/zero deltas, missing/non-numeric/bool/
non-finite/corrupt values, missing run IDs, invalid baseline, legacy runs
compatibility, auth, forced backtest execution failure, SQL-write interception.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models import BacktestRun
from app.services.backtest_run_service import BacktestRunService
from app import database


database.init_db()
client = TestClient(app)


def _seed_run(
    db: Session,
    *,
    name: str = "test",
    symbol: str = "AAPL.US",
    metrics: dict,
    run_id: int | None = None,
) -> int:
    run = BacktestRun(
        name=name,
        symbol=symbol,
        params_json='{"buy_low":1.0,"sell_high":2.0}',
        metrics_json=json.dumps(metrics),
    )
    if run_id is not None:
        run.id = run_id
    db.add(run)
    db.commit()
    db.refresh(run)
    return run.id


class TestBacktestCompare:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        db.query(BacktestRun).delete()
        db.commit()
        db.close()

    def test_baseline_default_first_existing(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
            id2 = _seed_run(db, metrics={"total_pnl": 150.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["baseline_id"] == id1

    def test_baseline_explicit(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
            id2 = _seed_run(db, metrics={"total_pnl": 150.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id1, id2], "baseline_id": id2},
        )
        assert resp.status_code == 200
        assert resp.json()["baseline_id"] == id2

    def test_invalid_baseline_not_in_ids(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
            id2 = _seed_run(db, metrics={"total_pnl": 150.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id1, id2], "baseline_id": 99999},
        )
        assert resp.status_code == 422

    def test_missing_run_ids_reported(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id1, 99998, 99999]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["missing_run_ids"] == [99998, 99999]

    def test_requested_order_preserved(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, name="first", metrics={"total_pnl": 100.0})
            id2 = _seed_run(db, name="second", metrics={"total_pnl": 200.0})
            id3 = _seed_run(db, name="third", metrics={"total_pnl": 300.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id3, id1, id2]},
        )
        assert resp.status_code == 200
        run_ids = [r["id"] for r in resp.json()["runs"]]
        assert run_ids == [id3, id1, id2]

    def test_dedup(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id1]})
        assert resp.status_code == 200
        assert len(resp.json()["runs"]) == 1

    def test_stable_metric_ordering(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"zebra": 1.0, "alpha": 2.0, "middle": 3.0})
            id2 = _seed_run(db, metrics={"zebra": 4.0, "alpha": 5.0, "middle": 6.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        metric_names = [r["metric"] for r in resp.json()["metric_comparison"]]
        assert metric_names == ["alpha", "middle", "zebra"]

    def test_positive_negative_zero_deltas(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 100.0, "b": 200.0, "c": 50.0})
            id2 = _seed_run(db, metrics={"a": 150.0, "b": 100.0, "c": 50.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id1, id2], "baseline_id": id1},
        )
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        # id2 relative to baseline id1
        id2_a = next(e for e in mc["a"]["runs"] if e["run_id"] == id2)
        assert id2_a["delta"] == 50.0  # positive
        id2_b = next(e for e in mc["b"]["runs"] if e["run_id"] == id2)
        assert id2_b["delta"] == -100.0  # negative
        id2_c = next(e for e in mc["c"]["runs"] if e["run_id"] == id2)
        assert id2_c["delta"] == 0.0  # zero

    def test_missing_metric_classification(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 100.0, "b": 200.0})
            id2 = _seed_run(db, metrics={"a": 150.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        id2_b = next(e for e in mc["b"]["runs"] if e["run_id"] == id2)
        assert id2_b["classification"] == "MISSING"
        assert id2_b["delta"] is None

    def test_non_numeric_classification(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 100.0, "text_metric": "hello"})
            id2 = _seed_run(db, metrics={"a": 150.0, "text_metric": "world"})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        text_row = mc["text_metric"]
        for entry in text_row["runs"]:
            assert entry["classification"] == "NON_NUMERIC"
            assert entry["delta"] is None

    def test_bool_is_non_numeric(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"flag": True})
            id2 = _seed_run(db, metrics={"flag": False})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        for entry in mc["flag"]["runs"]:
            assert entry["classification"] == "NON_NUMERIC"

    def test_non_finite_is_non_numeric(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"inf": float("inf"), "nan": float("nan")})
            id2 = _seed_run(db, metrics={"inf": 1.0, "nan": 2.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        id1_inf = next(e for e in mc["inf"]["runs"] if e["run_id"] == id1)
        assert id1_inf["classification"] == "NON_NUMERIC"

    def test_corrupt_metrics_json(self) -> None:
        db = database.SessionLocal()
        try:
            run = BacktestRun(
                name="corrupt",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="not-valid-json",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            corrupt_id = run.id
            id2 = _seed_run(db, metrics={"a": 1.0})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [corrupt_id, id2]},
        )
        assert resp.status_code == 200
        # The corrupt run should still appear in runs (via _to_out fallback)
        assert len(resp.json()["runs"]) == 2

    def test_legacy_runs_compatibility(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"total_pnl": 100.0})
            id2 = _seed_run(db, metrics={"total_pnl": 200.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data
        assert len(data["runs"]) == 2
        assert all("id" in r and "metrics" in r for r in data["runs"])

    def test_auth_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret-key")
        resp = client.get("/api/backtest/runs/compare", params={"ids": [1]})
        assert resp.status_code == 401

    def test_no_session_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 1.0})
            id2 = _seed_run(db, metrics={"a": 2.0})
            count_before = db.query(BacktestRun).count()
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1, id2]})
        assert resp.status_code == 200
        db = database.SessionLocal()
        try:
            count_after = db.query(BacktestRun).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_sql_write_interception(self, monkeypatch) -> None:
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 1.0})
            id2 = _seed_run(db, metrics={"a": 2.0})
        finally:
            db.close()

        db = database.SessionLocal()
        try:
            original_execute = db.execute

            def _guard_execute(stmt, *args, **kwargs):
                sql_text = str(stmt).upper()
                for kw in ("INSERT", "UPDATE", "DELETE"):
                    if kw in sql_text:
                        raise AssertionError(f"compare must not issue {kw}")
                return original_execute(stmt, *args, **kwargs)

            monkeypatch.setattr(db, "execute", _guard_execute)
            result = BacktestRunService(db).compare_with_metrics([id1, id2])
            assert len(result["runs"]) == 2
        finally:
            db.close()

    # ---- Blocker 1: raw metrics corruption semantics ----

    def test_invalid_json_document_metadata(self) -> None:
        """Corrupt metrics_json is classified INVALID_JSON, not hidden."""
        db = database.SessionLocal()
        try:
            run = BacktestRun(
                name="corrupt",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="not-valid-json",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            corrupt_id = run.id
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare", params={"ids": [corrupt_id]}
        )
        assert resp.status_code == 200
        data = resp.json()
        meta = next(m for m in data["document_metadata"] if m["run_id"] == corrupt_id)
        assert meta["document_status"] == "INVALID_JSON"

    def test_non_object_json_document_metadata(self) -> None:
        """Valid JSON that is not an object is classified NON_OBJECT."""
        db = database.SessionLocal()
        try:
            run = BacktestRun(
                name="nonobj",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="[1, 2, 3]",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            nonobj_id = run.id
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare", params={"ids": [nonobj_id]}
        )
        assert resp.status_code == 200
        meta = next(
            m for m in resp.json()["document_metadata"] if m["run_id"] == nonobj_id
        )
        assert meta["document_status"] == "NON_OBJECT"

    def test_legitimate_underscore_keys_preserved(self) -> None:
        """Metric names beginning with ``__`` are preserved in the union."""
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"__meta": 42, "normal": 1.0})
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": [id1]})
        assert resp.status_code == 200
        names = [r["metric"] for r in resp.json()["metric_comparison"]]
        assert "__meta" in names
        assert "normal" in names

    def test_invalid_run_classified_non_numeric_in_comparison(self) -> None:
        """Invalid document cells are NON_NUMERIC, not MISSING."""
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 1.0})
            run = BacktestRun(
                name="corrupt",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="bad",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            corrupt_id = run.id
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare", params={"ids": [id1, corrupt_id]}
        )
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        # metric "a" exists from the valid run; the corrupt run's cell is NON_NUMERIC
        a_row = mc["a"]
        corrupt_cell = next(e for e in a_row["runs"] if e["run_id"] == corrupt_id)
        assert corrupt_cell["classification"] == "NON_NUMERIC"
        assert corrupt_cell["delta"] is None

    def test_empty_metric_comparison_when_all_invalid(self) -> None:
        """If no valid document provides metric names, comparison is empty."""
        db = database.SessionLocal()
        try:
            run = BacktestRun(
                name="corrupt",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="bad",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            corrupt_id = run.id
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare", params={"ids": [corrupt_id]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["metric_comparison"] == []
        assert data["document_metadata"][0]["document_status"] == "INVALID_JSON"

    def test_runs_compatibility_with_corrupt(self) -> None:
        """Legacy runs field still returns rows for corrupt metrics."""
        db = database.SessionLocal()
        try:
            run = BacktestRun(
                name="corrupt",
                symbol="X.US",
                params_json='{"buy_low":1.0,"sell_high":2.0}',
                metrics_json="bad",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            corrupt_id = run.id
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare", params={"ids": [corrupt_id]}
        )
        assert resp.status_code == 200
        assert len(resp.json()["runs"]) == 1
        assert resp.json()["runs"][0]["id"] == corrupt_id

    # ---- Blocker 2: finite-only delta ----

    def test_delta_overflow_produces_none(self) -> None:
        """1e308 - (-1e308) overflows to inf; delta must be None."""
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"x": -1e308})
            id2 = _seed_run(db, metrics={"x": 1e308})
        finally:
            db.close()
        resp = client.get(
            "/api/backtest/runs/compare",
            params={"ids": [id1, id2], "baseline_id": id1},
        )
        assert resp.status_code == 200
        mc = {r["metric"]: r for r in resp.json()["metric_comparison"]}
        id2_x = next(e for e in mc["x"]["runs"] if e["run_id"] == id2)
        assert id2_x["delta"] is None  # overflow suppressed
        assert id2_x["classification"] == "NUMERIC"

    # ---- Blocker 4: missing negative evidence ----

    def test_eight_unique_ids_accepted_and_ordered(self) -> None:
        """Exactly 8 unique IDs are accepted and preserve requested order."""
        db = database.SessionLocal()
        try:
            ids = []
            for i in range(8):
                ids.append(_seed_run(db, name=f"run-{i}", metrics={"v": float(i)}))
        finally:
            db.close()
        resp = client.get("/api/backtest/runs/compare", params={"ids": ids})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 8
        assert [r["id"] for r in data["runs"]] == ids

    def test_no_backtest_engine_invocation(self, monkeypatch) -> None:
        """Compare must never invoke BacktestEngine or execute a backtest."""
        db = database.SessionLocal()
        try:
            id1 = _seed_run(db, metrics={"a": 1.0})
            id2 = _seed_run(db, metrics={"a": 2.0})
        finally:
            db.close()

        # Fail-fast: if any backtest execution path is invoked, raise.
        from app.core import backtest as bt_module

        original_run = getattr(bt_module, "run_backtest", None)

        def _boom(*a, **kw):
            raise AssertionError("compare must not invoke run_backtest")

        if original_run is not None:
            monkeypatch.setattr(bt_module, "run_backtest", _boom)

        db = database.SessionLocal()
        try:
            result = BacktestRunService(db).compare_with_metrics([id1, id2])
            assert len(result["runs"]) == 2
        finally:
            db.close()
