import os
from typing import Any
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_strategy_experiments_api_{os.getpid()}.db"
)

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine as db_engine
from app.models import Base, StrategyExperiment, StrategyExperimentRun
from app.main import app

Base.metadata.create_all(bind=db_engine)

client = TestClient(app)

_CSV = (
    "timestamp,open,high,low,close,volume\n"
    "2026-05-22T10:00:00Z,150,160,99,105,1000\n"
    "2026-05-22T10:01:00Z,150,201,140,200,1000\n"
)

_VALID_CREATE_PAYLOAD: dict[str, Any] = {
    "name": "test-exp",
    "symbol": "AAPL.US",
    "base_params": {
        "symbol": "AAPL.US",
        "buy_low": 120.0,
        "sell_high": 180.0,
        "short_selling": False,
        "min_profit_amount": 0.0,
        "max_daily_loss": 5000.0,
        "max_consecutive_losses": 3,
        "quantity": 1.0,
        "initial_cash": 10000.0,
        "fee_rate": 0.0,
        "fixed_fee": 0.0,
        "slippage_pct": 0.0,
        "stop_loss_pct": 0.0,
    },
    "parameter_grid": {
        "buy_low": {"value": 100.0},
    },
}


def _clean_db() -> None:
    db = SessionLocal()
    try:
        db.query(StrategyExperimentRun).delete()
        db.query(StrategyExperiment).delete()
        db.commit()
    finally:
        db.close()


from typing import Generator

@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    _clean_db()
    yield
    _clean_db()


class TestCreateStrategyExperiment:
    def test_returns_pending_and_estimated_count(self, clean_db: None) -> None:
        resp = client.post("/api/strategy-experiments", json=_VALID_CREATE_PAYLOAD)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "test-exp"
        assert data["symbol"] == "AAPL.US"
        assert data["status"] == "PENDING"
        assert data["estimated_runs"] == 1
        assert data["completed_runs"] == 0
        assert data["failed_runs"] == 0
        assert data["id"] > 0


class TestListStrategyExperiments:
    def test_lists_created_experiments(self, clean_db: None) -> None:
        # Empty list.
        resp = client.get("/api/strategy-experiments")
        assert resp.status_code == 200
        assert resp.json() == []

        # Create one.
        created = client.post("/api/strategy-experiments", json=_VALID_CREATE_PAYLOAD)
        assert created.status_code == 200
        created_id = created.json()["id"]

        resp = client.get("/api/strategy-experiments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == created_id
        assert items[0]["name"] == "test-exp"


class TestGetStrategyExperiment:
    def test_returns_experiment_by_id(self, clean_db: None) -> None:
        created = client.post("/api/strategy-experiments", json=_VALID_CREATE_PAYLOAD)
        exp_id = created.json()["id"]

        resp = client.get(f"/api/strategy-experiments/{exp_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == exp_id
        assert data["name"] == "test-exp"
        assert data["status"] == "PENDING"


class TestRunStrategyExperiment:
    def test_run_returns_completed(self, clean_db: None) -> None:
        created = client.post("/api/strategy-experiments", json=_VALID_CREATE_PAYLOAD)
        exp_id = created.json()["id"]

        run_payload = {"csv_text": _CSV}
        resp = client.post(
            f"/api/strategy-experiments/{exp_id}/run", json=run_payload
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert data["completed_runs"] == 1
        assert data["failed_runs"] == 0
        assert data["completed_at"] is not None


class TestListRuns:
    def test_returns_sorted_page(self, clean_db: None) -> None:
        # Create experiment with 3 buy_low values → 3 runs.
        payload = dict(_VALID_CREATE_PAYLOAD)
        payload["parameter_grid"] = {"buy_low": {"values": [100.0, 120.0, 140.0]}}
        created = client.post("/api/strategy-experiments", json=payload)
        exp_id = created.json()["id"]

        run_payload = {"csv_text": _CSV}
        run_resp = client.post(
            f"/api/strategy-experiments/{exp_id}/run", json=run_payload
        )
        assert run_resp.status_code == 200

        resp = client.get(
            f"/api/strategy-experiments/{exp_id}/runs",
            params={"sort": "total_return_pct", "order": "desc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert data["page"] == 1

        # Verify descending order.
        returns = [r["total_return_pct"] for r in data["items"]]
        assert returns == sorted(returns, reverse=True)

        # Verify parameters field is a dict.
        for item in data["items"]:
            assert isinstance(item["parameters"], dict)

    def test_paginates_runs(self, clean_db: None) -> None:
        # Same 3-run setup.
        payload = dict(_VALID_CREATE_PAYLOAD)
        payload["parameter_grid"] = {"buy_low": {"values": [100.0, 120.0, 140.0]}}
        created = client.post("/api/strategy-experiments", json=payload)
        exp_id = created.json()["id"]

        run_payload = {"csv_text": _CSV}
        run_resp = client.post(
            f"/api/strategy-experiments/{exp_id}/run", json=run_payload
        )
        assert run_resp.status_code == 200

        # First page (page_size=2).
        page1 = client.get(
            f"/api/strategy-experiments/{exp_id}/runs",
            params={"sort": "total_return_pct", "order": "desc", "page": 1, "page_size": 2},
        ).json()
        assert page1["total"] == 3
        assert len(page1["items"]) == 2
        assert page1["page"] == 1
        assert page1["page_size"] == 2

        # Second page: exactly 1 item, not one from page 1.
        page2 = client.get(
            f"/api/strategy-experiments/{exp_id}/runs",
            params={"sort": "total_return_pct", "order": "desc", "page": 2, "page_size": 2},
        ).json()
        assert page2["total"] == 3
        assert len(page2["items"]) == 1
        assert page2["page"] == 2
        assert page2["page_size"] == 2
        page1_ids = {r["id"] for r in page1["items"]}
        assert page2["items"][0]["id"] not in page1_ids


class TestErrors:
    def test_invalid_grid_returns_422(self, clean_db: None) -> None:
        payload = dict(_VALID_CREATE_PAYLOAD)
        # Use an invalid grid key.
        payload["parameter_grid"] = {"invalid_key": {"value": 1.0}}
        resp = client.post("/api/strategy-experiments", json=payload)
        assert resp.status_code == 422

    def test_unknown_experiment_returns_404(self, clean_db: None) -> None:
        resp = client.get("/api/strategy-experiments/99999")
        assert resp.status_code == 404

    def test_run_unknown_experiment_returns_404(self, clean_db: None) -> None:
        resp = client.post(
            "/api/strategy-experiments/99999/run", json={"csv_text": _CSV}
        )
        assert resp.status_code == 404

    def test_unsupported_sort_returns_400(self, clean_db: None) -> None:
        created = client.post("/api/strategy-experiments", json=_VALID_CREATE_PAYLOAD)
        exp_id = created.json()["id"]

        resp = client.get(
            f"/api/strategy-experiments/{exp_id}/runs",
            params={"sort": "nonexistent", "order": "asc"},
        )
        assert resp.status_code == 400
