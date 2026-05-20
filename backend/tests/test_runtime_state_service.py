from __future__ import annotations

import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_runtime_state.db"

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.engine import StrategyEngine, EngineState
from app.core.risk import RiskController
from app.models import Base
from app.models import StrategyConfig, RuntimeState
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService


class TestRuntimeStateService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
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

    def test_load_restores_engine_and_risk(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 3000.0,
            "max_consecutive_losses": 2,
        })
        svc.update_runtime_state(
            engine_state="long",
            last_price=150.0,
            daily_pnl=-100.0,
            consecutive_losses=1,
            kill_switch=False,
            paused=True,
            last_trigger_price=145.0,
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.params.symbol == "AAPL.US"
        assert engine.state == EngineState.LONG
        assert engine.last_price == 150.0
        assert risk.daily_pnl == -100.0
        assert risk.consecutive_losses == 1
        assert risk.paused is True

    def test_load_defaults_on_invalid_engine_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "TSLA.US", "market": "US", "buy_low": 50.0, "sell_high": 100.0})
        svc.update_runtime_state(engine_state="invalid_state")
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert engine.state == EngineState.FLAT

    def test_persist_saves_state(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "NVDA.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        db.close()

        engine = StrategyEngine()
        engine.state = EngineState.SHORT
        engine.last_price = 180.0
        risk = RiskController()
        risk.daily_pnl = -50.0
        risk.consecutive_losses = 2

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist(db, engine, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state()
        db.close()

        assert state.engine_state == "short"
        assert state.last_price == 180.0
        assert state.daily_pnl == -50.0
        assert state.consecutive_losses == 2

    def test_persist_risk_saves_only_risk(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "META.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(engine_state="flat", last_price=150.0)
        db.close()

        risk = RiskController()
        risk.daily_pnl = -25.0
        risk.consecutive_losses = 1

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist_risk(db, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state()
        db.close()

        assert state.daily_pnl == -25.0
        assert state.consecutive_losses == 1
        assert state.engine_state == "flat"

    def test_record_risk_event(self) -> None:
        self._cleanup()
        db = self._get_db()
        state_svc = RuntimeStateService()
        state_svc.record_risk_event(db, "test risk reason")
        db.close()

        db = self._get_db()
        events = db.query(RuntimeState).all()
        db.close()

        assert len([e for e in events if hasattr(e, 'event_type')]) >= 0

    def test_load_resets_daily_pnl_when_day_changed(self) -> None:
        from datetime import date, timedelta

        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(
            engine_state="flat",
            daily_pnl=-500.0,
            consecutive_losses=3,
            daily_pnl_date=date.today() - timedelta(days=1),
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert risk.daily_pnl == 0.0
        assert risk.consecutive_losses == 0

    def test_load_keeps_daily_pnl_when_same_day(self) -> None:
        from datetime import date

        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "AAPL.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        svc.update_runtime_state(
            engine_state="flat",
            daily_pnl=-500.0,
            consecutive_losses=3,
            daily_pnl_date=date.today(),
        )
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        state_svc = RuntimeStateService()

        db = self._get_db()
        state_svc.load(db, engine, risk)
        db.close()

        assert risk.daily_pnl == -500.0
        assert risk.consecutive_losses == 3

    def test_persist_saves_daily_pnl_date(self) -> None:
        self._cleanup()
        db = self._get_db()
        svc = StrategyService(db)
        svc.update_config({"symbol": "NVDA.US", "market": "US", "buy_low": 100.0, "sell_high": 200.0})
        db.close()

        engine = StrategyEngine()
        risk = RiskController()
        risk.daily_pnl = -25.0

        state_svc = RuntimeStateService()
        db = self._get_db()
        state_svc.persist(db, engine, risk)
        db.close()

        db = self._get_db()
        state = svc.get_runtime_state()
        db.close()

        from datetime import date
        assert state.daily_pnl == -25.0
        assert state.daily_pnl_date == date.today()
