import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_performance_api_{os.getpid()}.db"
)

from app.database import engine as db_engine, SessionLocal
from app.models import Base, ExperimentResult
from app.main import app
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.commit()
    db.close()


def _seed(db, experiment="exp1", variant="A", pnl=10.0, profitable=True):
    db.add(ExperimentResult(
        experiment_name=experiment,
        variant_name=variant,
        interaction_id=None,
        order_action="SUBMIT",
        predicted_direction="up",
        actual_pnl=pnl,
        was_profitable=profitable,
    ))


class TestPerformanceApi:
    def test_stats_empty_returns_zero_structure(self, clean_db):
        resp = client.get("/api/performance/stats", params={"experiment": "none"})
        assert resp.status_code == 200
        assert resp.json() == {
            "total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        }

    def test_stats_shape(self, clean_db):
        db = SessionLocal()
        _seed(db, pnl=10.0, profitable=True)
        _seed(db, pnl=-4.0, profitable=False)
        db.commit(); db.close()
        resp = client.get("/api/performance/stats", params={"experiment": "exp1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_trades"] == 2
        assert body["win_rate"] == 0.5
        assert body["total_pnl"] == 6.0

    def test_compare_shape(self, clean_db):
        db = SessionLocal()
        _seed(db, variant="A", pnl=10.0, profitable=True)
        _seed(db, variant="B", pnl=-2.0, profitable=False)
        db.commit(); db.close()
        resp = client.get("/api/performance/compare", params={"experiment": "exp1"})
        assert resp.status_code == 200
        rows = resp.json()
        assert {r["variant"] for r in rows} == {"A", "B"}
        assert all({"variant", "total_trades", "win_rate", "total_pnl", "avg_pnl"} <= r.keys() for r in rows)

    def test_recommendations_returns_list(self, clean_db):
        resp = client.get("/api/performance/recommendations", params={"experiment": "exp1"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_stats_missing_experiment_param_422(self, clean_db):
        resp = client.get("/api/performance/stats")
        assert resp.status_code == 422
