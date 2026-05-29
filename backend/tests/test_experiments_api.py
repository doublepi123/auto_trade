import os
import tempfile
os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_experiments_api_{os.getpid()}.db"
)

from app.database import engine as db_engine, SessionLocal
from app.models import Base, ExperimentResult, PromptVersion
from app.main import app
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)
client = TestClient(app)


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.query(PromptVersion).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ExperimentResult).delete()
    db.query(PromptVersion).delete()
    db.commit()
    db.close()


class TestExperimentsApi:
    def test_list_experiment_names_empty(self, clean_db):
        resp = client.get("/api/experiments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_experiment_names_distinct(self, clean_db):
        db = SessionLocal()
        for name in ["exp_a", "exp_a", "exp_b"]:
            db.add(ExperimentResult(
                experiment_name=name, variant_name="A", interaction_id=None,
                order_action="SUBMIT", predicted_direction="up",
                actual_pnl=1.0, was_profitable=True,
            ))
        db.commit(); db.close()
        resp = client.get("/api/experiments")
        assert resp.status_code == 200
        assert sorted(resp.json()) == ["exp_a", "exp_b"]

    def test_version_crud_and_activate(self, clean_db):
        created = client.post("/api/experiments/versions", json={
            "name": "baseline", "version": "v1", "description": "d", "template": "TPL",
        })
        assert created.status_code == 200
        vid = created.json()["id"]
        listed = client.get("/api/experiments/versions")
        assert listed.status_code == 200
        assert any(v["id"] == vid for v in listed.json())
        act = client.post(f"/api/experiments/versions/{vid}/activate")
        assert act.status_code == 200
        active = client.get("/api/experiments/versions/active")
        assert active.status_code == 200
        assert active.json()["id"] == vid
