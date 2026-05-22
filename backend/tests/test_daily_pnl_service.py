from __future__ import annotations

import os
from datetime import date, datetime, time, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_daily_pnl.db"

from pytest import approx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, OrderRecord
from app.services.daily_pnl_service import DailyPnlService


class TestDailyPnlService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _dt(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def test_calculates_today_pnl_using_carryover_cost_basis(self) -> None:
        self._cleanup()
        prior_day = date(2026, 5, 21)
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-prior",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(prior_day, 14),
                filled_at=self._dt(prior_day, 14, 1),
            ),
            OrderRecord(
                broker_order_id="sell-today",
                symbol="NVDA.US",
                side="SELL",
                quantity=4,
                price=110,
                executed_quantity=4,
                executed_price=110,
                status="FILLED",
                created_at=self._dt(trade_day, 14),
                filled_at=self._dt(trade_day, 14, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(40.0)
        assert result.consecutive_losses == 0
        assert [(trade.broker_order_id, trade.pnl) for trade in result.trades] == [("sell-today", approx(40.0))]

    def test_calculates_average_cost_for_same_day_round_trip(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy-1",
                symbol="NVDA.US",
                side="BUY",
                quantity=105,
                price=220.15,
                executed_quantity=105,
                executed_price=220.15,
                status="FILLED",
                created_at=self._dt(trade_day, 12, 32),
                filled_at=self._dt(trade_day, 12, 33),
            ),
            OrderRecord(
                broker_order_id="buy-2",
                symbol="NVDA.US",
                side="BUY",
                quantity=16,
                price=219.51,
                executed_quantity=16,
                executed_price=219.51,
                status="FILLED",
                created_at=self._dt(trade_day, 13, 30),
                filled_at=self._dt(trade_day, 13, 31),
            ),
            OrderRecord(
                broker_order_id="sell-1",
                symbol="NVDA.US",
                side="SELL",
                quantity=121,
                price=217.53,
                executed_quantity=121,
                executed_price=217.530909,
                status="FILLED",
                created_at=self._dt(trade_day, 15),
                filled_at=self._dt(trade_day, 15, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        avg_cost = ((105 * 220.15) + (16 * 219.51)) / 121
        assert result.realized_pnl == approx((217.530909 - avg_cost) * 121)
        assert result.consecutive_losses == 1

    def test_counts_executed_quantity_on_partially_filled_terminal_order(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="buy",
                symbol="AAPL.US",
                side="BUY",
                quantity=5,
                price=100,
                executed_quantity=5,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="sell-cancelled-partial",
                symbol="AAPL.US",
                side="SELL",
                quantity=5,
                price=101,
                executed_quantity=3,
                executed_price=101,
                status="CANCELLED",
                created_at=self._dt(trade_day, 11),
                filled_at=self._dt(trade_day, 11, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(3.0)
        assert result.consecutive_losses == 0

    def test_calculates_short_cover_pnl(self) -> None:
        self._cleanup()
        trade_day = date(2026, 5, 22)
        db = self._get_db()
        db.add_all([
            OrderRecord(
                broker_order_id="short",
                symbol="TSLA.US",
                side="SELL_SHORT",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                created_at=self._dt(trade_day, 10),
                filled_at=self._dt(trade_day, 10, 1),
            ),
            OrderRecord(
                broker_order_id="cover",
                symbol="TSLA.US",
                side="BUY_TO_COVER",
                quantity=10,
                price=95,
                executed_quantity=10,
                executed_price=95,
                status="FILLED",
                created_at=self._dt(trade_day, 11),
                filled_at=self._dt(trade_day, 11, 1),
            ),
        ])
        db.commit()

        result = DailyPnlService(db).calculate(trade_day=trade_day)
        db.close()

        assert result.realized_pnl == approx(50.0)
        assert result.consecutive_losses == 0
