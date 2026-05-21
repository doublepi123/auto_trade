import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, OrderRecord, RiskEvent, RuntimeState, StrategyConfig

DB_URL = "sqlite:///data/test_models.db"


class TestModels:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.engine = engine
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        db_path = "data/test_models.db"
        if os.path.exists(db_path):
            os.remove(db_path)

    def _get_db(self) -> Session:
        return self.SessionLocal()

    def _cleanup_tables(self, db: Session) -> None:
        for model in [StrategyConfig, OrderRecord, RiskEvent, RuntimeState]:
            db.query(model).delete()
        db.commit()

    def test_strategy_config_persistence(self) -> None:
        db = self._get_db()
        try:
            self._cleanup_tables(db)
            config = StrategyConfig(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=200.0)
            db.add(config)
            db.commit()
            result = db.query(StrategyConfig).first()
            assert result is not None
            assert result.symbol == "AAPL.US"
            assert result.buy_low == 100.0
            assert result.sell_high == 200.0
            assert result.updated_at is not None
        finally:
            db.close()

    def test_order_record_persistence(self) -> None:
        db = self._get_db()
        try:
            self._cleanup_tables(db)
            order = OrderRecord(symbol="AAPL.US", side="BUY", quantity=10, price=150.0)
            db.add(order)
            db.commit()
            result = db.query(OrderRecord).first()
            assert result is not None
            assert result.symbol == "AAPL.US"
            assert result.side == "BUY"
            assert result.quantity == 10
            assert result.price == 150.0
            assert result.status == "SUBMITTED"
            assert result.created_at is not None
        finally:
            db.close()

    def test_risk_event_persistence(self) -> None:
        db = self._get_db()
        try:
            self._cleanup_tables(db)
            event = RiskEvent(event_type="DAILY_LOSS", reason="exceeded max daily loss")
            db.add(event)
            db.commit()
            result = db.query(RiskEvent).first()
            assert result is not None
            assert result.event_type == "DAILY_LOSS"
            assert result.reason == "exceeded max daily loss"
        finally:
            db.close()

    def test_runtime_state_defaults(self) -> None:
        db = self._get_db()
        try:
            self._cleanup_tables(db)
            state = RuntimeState()
            db.add(state)
            db.commit()
            result = db.query(RuntimeState).first()
            assert result is not None
            assert result.engine_state == "flat"
            assert result.paused is False
            assert result.kill_switch is False
            assert result.daily_pnl == 0.0
            assert result.consecutive_losses == 0
            assert result.last_price == 0.0
            assert result.updated_at is not None
        finally:
            db.close()
