import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, RuntimeState, StrategyConfig
from app.services.strategy_service import StrategyService

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_service.db"

DB_URL = "sqlite:///data/test_service.db"


class TestStrategyService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.query(RuntimeState).delete()
        db.commit()
        db.close()

    def test_get_config_creates_default(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        config = svc.get_config()
        assert config is not None
        assert config.symbol == ""
        assert config.market == "US"
        db.close()

    def test_update_config(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated = svc.update_config({
            "symbol": "AAPL.US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert updated.symbol == "AAPL.US"
        assert updated.buy_low == 100.0
        assert updated.sell_high == 200.0

        config = svc.get_config()
        assert config.symbol == "AAPL.US"
        db.close()

    def test_get_runtime_state_defaults(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        state = svc.get_runtime_state()
        assert state.engine_state == "flat"
        assert state.paused is False
        db.close()

    def test_update_runtime_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated = svc.update_runtime_state(engine_state="long", last_price=150.0)
        assert updated.engine_state == "long"
        assert updated.last_price == 150.0
        db.close()
