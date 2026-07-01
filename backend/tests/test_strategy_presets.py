"""Strategy presets — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_strategy_presets_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, StrategyConfig, StrategyPreset
from app.services.strategy_preset_service import StrategyPresetService


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False}
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(StrategyPreset).delete()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)


class TestStrategyPresetService(_Base):
    def test_create_list_get_delete(self) -> None:
        svc = StrategyPresetService(self._db())
        out = svc.create("aggressive", {"buy_low": 90, "sell_high": 190})
        assert out.id > 0
        assert out.params == {"buy_low": 90, "sell_high": 190}
        assert len(svc.list_presets()) == 1
        got = svc.get(out.id)
        assert got is not None
        assert got.name == "aggressive"
        assert svc.delete(out.id) is True
        assert svc.delete(out.id) is False

    def test_get_params_missing(self) -> None:
        assert StrategyPresetService(self._db()).get_params(999) is None


class TestStrategyPresetAPI(_Base):
    def test_create_apply_flow(self) -> None:
        create = self.client.post("/api/strategy-presets", json={
            "name": "conservative",
            "params": {"buy_low": 80, "sell_high": 180, "min_profit_amount": 5},
        })
        assert create.status_code == 200, create.text
        pid = create.json()["id"]

        lst = self.client.get("/api/strategy-presets")
        assert lst.json()["total"] == 1

        apply = self.client.post(f"/api/strategy-presets/{pid}/apply")
        assert apply.status_code == 200, apply.text
        changed = apply.json()["changed"]
        assert "buy_low" in changed and "sell_high" in changed

        # Active config now reflects the preset.
        db = self._db()
        cfg = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        assert cfg is not None
        assert cfg.buy_low == 80
        assert cfg.sell_high == 180
        db.close()

        missing = self.client.post("/api/strategy-presets/999/apply")
        assert missing.status_code == 404

        dele = self.client.delete(f"/api/strategy-presets/{pid}")
        assert dele.status_code == 204

    def test_create_validates_name(self) -> None:
        resp = self.client.post("/api/strategy-presets", json={"name": "", "params": {}})
        assert resp.status_code == 422
