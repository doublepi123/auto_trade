from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.domain.strategy_v2 import StrategyBar
from app.models import (
    Base,
    LiveExitChallengerRegistration,
    LiveExitChallengerTrade,
    OrderRecord,
    TrackedEntry,
)
from app.services.live_exit_challenger_service import (
    LiveExitChallengerService,
)


_REGISTERED_AT = datetime(2026, 7, 24, 14, 30, 10, tzinfo=timezone.utc)
_ENTRY_AT = datetime(2026, 7, 24, 14, 31, tzinfo=timezone.utc)
_SYMBOL = "AAPL.US"


def _bar(
    minute: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float | None = None,
) -> StrategyBar:
    return StrategyBar(
        timestamp=_ENTRY_AT + timedelta(minutes=minute),
        open=open_price,
        high=high,
        low=low,
        close=open_price if close is None else close,
        volume=1_000,
        symbol=_SYMBOL,
    )


class TestLiveExitChallengerService:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            for model in (
                LiveExitChallengerTrade,
                LiveExitChallengerRegistration,
                TrackedEntry,
                OrderRecord,
            ):
                db.query(model).delete()
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    @staticmethod
    def _register(service: LiveExitChallengerService) -> None:
        assert service.ensure_registrations(
            symbol=_SYMBOL,
            market="US",
            now=_REGISTERED_AT,
        ) is True

    @staticmethod
    def _open_real_position(
        db: Session,
        *,
        entry_at: datetime = _ENTRY_AT,
    ) -> OrderRecord:
        entry = OrderRecord(
            broker_order_id=f"entry-{entry_at.isoformat()}",
            symbol=_SYMBOL,
            side="BUY",
            quantity=10,
            price=100,
            executed_quantity=10,
            executed_price=100,
            status="FILLED",
            filled_at=entry_at,
            config_version="live-config-v1",
            estimated_fee=0.5,
        )
        db.add(entry)
        db.add(
            TrackedEntry(
                symbol=_SYMBOL,
                side="LONG",
                quantity=10,
                cost=1_000,
                opened_at=entry_at,
            )
        )
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def _close_real_position(
        db: Session,
        *,
        entry_at: datetime = _ENTRY_AT,
        exit_at: datetime,
        exit_price: float = 99.5,
    ) -> OrderRecord:
        gross_pnl = (exit_price - 100) * 10
        pnl_fee = 1.0
        exit_order = OrderRecord(
            broker_order_id=f"exit-{exit_at.isoformat()}",
            symbol=_SYMBOL,
            side="SELL",
            quantity=10,
            price=exit_price,
            executed_quantity=10,
            executed_price=exit_price,
            status="FILLED",
            filled_at=exit_at,
            exit_cause="TIME_STOP",
            exit_reason="maximum holding time reached",
            gross_pnl=gross_pnl,
            pnl_fee=pnl_fee,
            net_pnl=gross_pnl - pnl_fee,
            cost_basis_price=100,
            cost_basis_quantity=10,
            cost_basis_opened_at=entry_at,
        )
        db.add(exit_order)
        db.query(TrackedEntry).filter(
            TrackedEntry.symbol == _SYMBOL
        ).delete()
        db.commit()
        db.refresh(exit_order)
        return exit_order

    def test_registrations_are_frozen_idempotent_and_start_next_minute(
        self,
    ) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)

            assert service.ensure_registrations(
                symbol=_SYMBOL.lower(),
                market="us",
                now=_REGISTERED_AT + timedelta(minutes=5),
            ) is False
            rows = db.query(LiveExitChallengerRegistration).order_by(
                LiveExitChallengerRegistration.locked_profit_pct
            ).all()

            assert [row.locked_profit_pct for row in rows] == [
                0.1,
                0.2,
                0.3,
                0.4,
            ]
            assert {row.activation_pct for row in rows} == {0.4, 0.6}
            assert {
                row.eligible_after.replace(tzinfo=timezone.utc)
                for row in rows
            } == {_ENTRY_AT}
            assert all(len(row.evaluator_digest) == 64 for row in rows)

    def test_pre_registration_entry_is_not_backfilled(self) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(
                db,
                entry_at=_ENTRY_AT - timedelta(minutes=1),
            )

            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(minutes=1),
            ) is False
            assert db.query(LiveExitChallengerTrade).count() == 0

    def test_profit_locks_pair_with_later_real_exit_and_report_delta(
        self,
    ) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            entry = self._open_real_position(db)

            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=30),
            ) is True
            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=45),
            ) is False
            assert {
                row.entry_order_id
                for row in db.query(LiveExitChallengerTrade).all()
            } == {entry.id}

            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(
                    1,
                    open_price=100.2,
                    high=100.7,
                    low=100.1,
                ),
                observed_at=_ENTRY_AT + timedelta(minutes=2, seconds=5),
            )
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(
                    2,
                    open_price=100.35,
                    high=100.4,
                    low=100.05,
                ),
                observed_at=_ENTRY_AT + timedelta(minutes=3, seconds=5),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert len(rows) == 4
            assert {row.status for row in rows} == {"CLOSED"}
            assert {
                row.challenger_exit_reason for row in rows
            } == {"PROFIT_LOCK"}
            assert all(
                item.profit_lock_exits == 0
                for item in service.get_report(_SYMBOL).variants
            )

            baseline = self._close_real_position(
                db,
                exit_at=_ENTRY_AT + timedelta(minutes=5),
            )
            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=6),
            )
            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=7),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert all(
                row.baseline_exit_order_id == baseline.id for row in rows
            )
            assert all(
                row.baseline_net_pnl == pytest.approx(baseline.net_pnl)
                for row in rows
            )
            assert all(float(row.net_pnl_delta or 0) > 0 for row in rows)

            report = service.get_report(_SYMBOL)
            assert report.order_submission_allowed is False
            assert report.automatic_promotion_allowed is False
            assert report.historical_backfill_allowed is False
            assert len(report.variants) == 4
            assert all(item.paired_trades == 1 for item in report.variants)
            assert all(item.profit_lock_exits == 1 for item in report.variants)
            assert all(
                "MIN_PAIRED_TRADES" in item.blockers
                and "MIN_PROFIT_LOCK_EXITS" in item.blockers
                for item in report.variants
            )

    def test_open_variant_waits_for_complete_pre_exit_bar_frontier(
        self,
    ) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(db)
            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=30),
            ) is True
            baseline = self._close_real_position(
                db,
                exit_at=_ENTRY_AT + timedelta(minutes=3),
                exit_price=100.1,
            )

            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=4),
            )
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(
                    3,
                    open_price=100.1,
                    high=100.2,
                    low=100.0,
                ),
                observed_at=_ENTRY_AT + timedelta(minutes=4, seconds=5),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert {row.status for row in rows} == {"OPEN"}
            assert all(row.baseline_net_pnl is None for row in rows)
            assert {
                row.last_bar_at.replace(tzinfo=timezone.utc) for row in rows
            } == {_ENTRY_AT}

            for minute in (1, 2):
                service.advance_bar(
                    symbol=_SYMBOL,
                    bar=_bar(
                        minute,
                        open_price=100.1,
                        high=100.2,
                        low=100.0,
                    ),
                    observed_at=_ENTRY_AT
                    + timedelta(minutes=minute + 1, seconds=5),
                )
            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=5),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert {row.status for row in rows} == {"CLOSED"}
            assert {
                row.challenger_exit_reason for row in rows
            } == {"BASELINE_TIME_STOP"}
            assert all(
                row.baseline_exit_order_id == baseline.id for row in rows
            )
            assert all(row.net_pnl_delta == pytest.approx(0) for row in rows)

    def test_report_reflects_runtime_enable_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
        with self._db() as db:
            report = LiveExitChallengerService(db).get_report(_SYMBOL)
        assert report.enabled is True
