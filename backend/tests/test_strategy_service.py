from app import database
from app.models import RuntimeState, StrategyConfig
from app.services.strategy_service import StrategyService


database.init_db()


class TestStrategyService:
    def _get_db(self):
        return database.SessionLocal()

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
        assert config.llm_interval_minutes == 2
        assert config.auto_resume_minutes == 3
        db.close()

    def test_update_config(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated, diff = svc.update_config({
            "symbol": "AAPL.US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "min_profit_amount": 6.5,
            "auto_resume_minutes": 4,
        })
        assert updated.symbol == "AAPL.US"
        assert "buy_low" in diff
        assert updated.buy_low == 100.0
        assert updated.sell_high == 200.0
        assert updated.min_profit_amount == 6.5
        assert updated.auto_resume_minutes == 4

        config = svc.get_config()
        assert config.symbol == "AAPL.US"
        assert config.min_profit_amount == 6.5
        assert config.auto_resume_minutes == 4
        db.close()

    def test_update_config_persists_margin_safety_factor(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        updated, diff = svc.update_config({
            "symbol": "AAPL.US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "margin_safety_factor": 0.75,
        })
        assert updated.margin_safety_factor == 0.75
        assert "margin_safety_factor" in diff
        assert diff["margin_safety_factor"]["new"] == 0.75

        config = svc.get_config()
        assert config.margin_safety_factor == 0.75
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
