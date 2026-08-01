from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services.live_exit_challenger_service as live_exit_challenger_module
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
        filled_at: datetime | None = None,
    ) -> OrderRecord:
        order_filled_at = entry_at if filled_at is None else filled_at
        entry = OrderRecord(
            broker_order_id=(
                f"entry-{entry_at.isoformat()}-{order_filled_at.isoformat()}"
            ),
            symbol=_SYMBOL,
            side="BUY",
            quantity=10,
            price=100,
            executed_quantity=10,
            executed_price=100,
            status="FILLED",
            filled_at=order_filled_at,
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

            profit_rows = [row for row in rows if row.policy_type == "PROFIT_LOCK"]
            time_rows = [row for row in rows if row.policy_type == "TIME_STOP"]
            assert [row.locked_profit_pct for row in profit_rows] == [
                0.1,
                0.2,
                0.2,
                0.3,
                0.4,
                0.5,
                0.6,
            ]
            assert {row.activation_pct for row in profit_rows} == {
                0.3,
                0.4,
                0.6,
                0.7,
            }
            assert {row.algorithm_version for row in rows} == {
                "live-profit-lock-a30-f20-v1",
                "live-profit-lock-a40-f10-v1",
                "live-profit-lock-a40-f20-v1",
                "live-profit-lock-a40-f30-v1",
                "live-profit-lock-a60-f40-v1",
                "live-profit-lock-a60-f50-v1",
                "live-profit-lock-a70-f60-v1",
                "live-time-stop-m10-v1",
                "live-time-stop-m15-v1",
                "live-time-stop-m30-v1",
                "live-time-stop-m45-v1",
            }
            assert sorted(
                row.max_holding_minutes
                for row in time_rows
                if row.max_holding_minutes is not None
            ) == [10, 15, 30, 45]
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

    def test_pre_registration_tracked_entry_is_not_backfilled_when_order_fills_later(
        self,
    ) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(
                db,
                entry_at=_ENTRY_AT - timedelta(seconds=30),
                filled_at=_ENTRY_AT,
            )

            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=30),
            ) is False
            assert db.query(LiveExitChallengerTrade).count() == 0

    def test_order_filled_after_tracked_entry_is_not_matched(self) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(
                db,
                entry_at=_ENTRY_AT,
                filled_at=_ENTRY_AT + timedelta(seconds=30),
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
            assert len(rows) == 11
            profit_rows: list[LiveExitChallengerTrade] = []
            for row in rows:
                registration = db.get(
                    LiveExitChallengerRegistration,
                    row.registration_id,
                )
                assert registration is not None
                if registration.policy_type == "PROFIT_LOCK":
                    profit_rows.append(row)
            assert {row.status for row in profit_rows} == {"CLOSED"}
            assert {
                row.challenger_exit_reason for row in profit_rows
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

            for row in profit_rows:
                db.refresh(row)
            assert all(
                row.baseline_exit_order_id == baseline.id
                for row in profit_rows
            )
            assert all(
                row.baseline_net_pnl == pytest.approx(baseline.net_pnl)
                for row in profit_rows
            )
            assert all(
                float(row.net_pnl_delta or 0) > 0
                for row in profit_rows
            )

            report = service.get_report(_SYMBOL)
            assert report.order_submission_allowed is False
            assert report.automatic_promotion_allowed is False
            assert report.historical_backfill_allowed is False
            assert len(report.variants) == 11
            profit_variants = [
                item for item in report.variants
                if item.policy_type == "PROFIT_LOCK"
            ]
            assert all(item.paired_trades == 1 for item in profit_variants)
            time_variants = [
                item for item in report.variants
                if item.policy_type == "TIME_STOP"
            ]
            assert all(item.profit_lock_exits == 1 for item in profit_variants)
            assert all(item.time_stop_exits == 0 for item in time_variants)
            assert all(item.paired_trades == 0 for item in time_variants)
            assert all(
                "MIN_PAIRED_TRADES" in item.blockers
                and "MIN_PROFIT_LOCK_EXITS" in item.blockers
                for item in profit_variants
            )
            assert all(
                "MIN_TIME_STOP_EXITS" in item.blockers
                for item in time_variants
            )

    def test_time_stop_exits_at_first_bar_open_after_fifteen_minutes(
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

            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(14, open_price=100.2, high=100.4, low=100.1),
                observed_at=_ENTRY_AT + timedelta(minutes=15, seconds=5),
            )
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(15, open_price=100.3, high=100.5, low=100.2),
                observed_at=_ENTRY_AT + timedelta(minutes=16, seconds=5),
            )

            registration = db.query(LiveExitChallengerRegistration).filter(
                LiveExitChallengerRegistration.algorithm_version
                == "live-time-stop-m15-v1"
            ).one()
            row = db.query(LiveExitChallengerTrade).filter(
                LiveExitChallengerTrade.registration_id == registration.id
            ).one()
            assert row.status == "CLOSED"
            assert row.challenger_exit_reason == "TIME_STOP"
            assert row.challenger_exit_price == pytest.approx(100.3 * 0.9996)

            self._close_real_position(
                db,
                exit_at=_ENTRY_AT + timedelta(minutes=60),
            )
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(60, open_price=100.5, high=100.6, low=100.4),
                observed_at=_ENTRY_AT + timedelta(minutes=61, seconds=5),
            )
            variant = next(
                item
                for item in service.get_report(_SYMBOL).variants
                if item.algorithm_version == "live-time-stop-m15-v1"
            )
            assert variant.baseline_mean_holding_minutes == pytest.approx(60)
            assert variant.challenger_mean_holding_minutes == pytest.approx(15)
            assert variant.mean_holding_minutes_saved == pytest.approx(45)

    def test_report_blocks_invalid_paired_pnl_and_holding_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(live_exit_challenger_module, "_MIN_READY_PAIRS", 1)
        monkeypatch.setattr(live_exit_challenger_module, "_MIN_MATURE_PAIRS", 1)
        monkeypatch.setattr(
            live_exit_challenger_module,
            "_MIN_TIME_STOP_EXITS",
            1,
        )
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(db)
            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=30),
            ) is True
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(15, open_price=100.3, high=100.5, low=100.2),
                observed_at=_ENTRY_AT + timedelta(minutes=16, seconds=5),
            )
            self._close_real_position(
                db,
                exit_at=_ENTRY_AT + timedelta(minutes=60),
            )
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(60, open_price=99.5, high=99.6, low=99.4),
                observed_at=_ENTRY_AT + timedelta(minutes=61, seconds=5),
            )

            registration = db.query(LiveExitChallengerRegistration).filter(
                LiveExitChallengerRegistration.algorithm_version
                == "live-time-stop-m15-v1"
            ).one()
            row = db.query(LiveExitChallengerTrade).filter(
                LiveExitChallengerTrade.registration_id == registration.id
            ).one()
            valid = service._variant_report(registration)
            assert valid.promotion_ready is True
            assert valid.blockers == []

            original_baseline_net_pnl = row.baseline_net_pnl
            row.baseline_net_pnl = math.inf
            with db.no_autoflush:
                invalid_pnl = service._variant_report(registration)
            assert "EVIDENCE_DATA_INVALID" in invalid_pnl.blockers
            assert invalid_pnl.promotion_ready is False
            assert invalid_pnl.paired_trades == 0
            assert invalid_pnl.status == "COLLECTING"
            assert invalid_pnl.time_stop_exits == 0
            assert invalid_pnl.baseline_net_pnl == 0.0
            assert invalid_pnl.challenger_net_pnl == 0.0
            assert invalid_pnl.net_pnl_delta == 0.0
            assert invalid_pnl.baseline_mean_holding_minutes == 0.0
            assert invalid_pnl.challenger_mean_holding_minutes == 0.0
            row.baseline_net_pnl = original_baseline_net_pnl

            original_challenger_exit_at = row.challenger_exit_at
            row.challenger_exit_at = row.entry_at - timedelta(minutes=1)
            with db.no_autoflush:
                reversed_holding = service._variant_report(registration)
            assert "EVIDENCE_DATA_INVALID" in reversed_holding.blockers
            assert reversed_holding.promotion_ready is False
            assert reversed_holding.paired_trades == 0
            assert reversed_holding.status == "COLLECTING"
            assert reversed_holding.time_stop_exits == 0
            assert reversed_holding.baseline_net_pnl == 0.0
            assert reversed_holding.challenger_net_pnl == 0.0
            assert reversed_holding.net_pnl_delta == 0.0
            assert reversed_holding.baseline_mean_holding_minutes == 0.0
            assert reversed_holding.challenger_mean_holding_minutes == 0.0
            row.challenger_exit_at = original_challenger_exit_at

            original_baseline_exit_at = row.baseline_exit_at
            row.baseline_exit_at = None
            with db.no_autoflush:
                incomplete_holding = service._variant_report(registration)
            assert "EVIDENCE_DATA_INVALID" in incomplete_holding.blockers
            assert incomplete_holding.promotion_ready is False
            assert incomplete_holding.paired_trades == 0
            assert incomplete_holding.status == "COLLECTING"
            assert incomplete_holding.time_stop_exits == 0
            assert incomplete_holding.baseline_net_pnl == 0.0
            assert incomplete_holding.challenger_net_pnl == 0.0
            assert incomplete_holding.net_pnl_delta == 0.0
            assert incomplete_holding.baseline_mean_holding_minutes == 0.0
            assert incomplete_holding.challenger_mean_holding_minutes == 0.0
            row.baseline_exit_at = original_baseline_exit_at

    def test_unknown_policy_type_fails_closed_without_advancing_trade(
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

            trade = db.query(LiveExitChallengerTrade).order_by(
                LiveExitChallengerTrade.id
            ).first()
            assert trade is not None
            db.query(LiveExitChallengerTrade).filter(
                LiveExitChallengerTrade.id != trade.id
            ).delete(synchronize_session=False)
            registration = db.get(
                LiveExitChallengerRegistration,
                trade.registration_id,
            )
            assert registration is not None
            registration.policy_type = "UNKNOWN"
            original_last_bar_at = trade.last_bar_at
            db.commit()

            with pytest.raises(
                ValueError,
                match="unsupported live exit challenger policy type",
            ):
                service.advance_bar(
                    symbol=_SYMBOL,
                    bar=_bar(
                        1,
                        open_price=100.2,
                        high=100.4,
                        low=100.1,
                    ),
                    observed_at=_ENTRY_AT + timedelta(minutes=2, seconds=5),
                )

            db.refresh(trade)
            assert trade.status == "OPEN"
            assert trade.last_bar_at == original_last_bar_at

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

    @pytest.mark.parametrize(
        ("field", "invalid_value"),
        (
            ("filled_at", None),
            ("executed_price", 0.0),
            ("gross_pnl", None),
            ("gross_pnl", math.nan),
            ("pnl_fee", None),
            ("pnl_fee", math.nan),
            ("pnl_fee", -1.0),
            ("net_pnl", None),
            ("net_pnl", math.nan),
        ),
    )
    def test_incomplete_baseline_does_not_close_open_variant(
        self,
        field: str,
        invalid_value: object,
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
                exit_at=_ENTRY_AT + timedelta(seconds=30),
            )
            setattr(baseline, field, invalid_value)
            db.commit()

            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=1),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert len(rows) == 11
            assert {row.status for row in rows} == {"OPEN"}
            assert all(row.challenger_exit_at is None for row in rows)
            assert all(row.challenger_net_pnl is None for row in rows)
            assert all(row.baseline_exit_order_id is None for row in rows)
            assert all(row.baseline_net_pnl is None for row in rows)

    def test_baseline_reason_fields_keep_cause_and_detail_semantics(
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
                exit_at=_ENTRY_AT + timedelta(seconds=30),
            )
            baseline.exit_cause = ""
            baseline.exit_reason = "manual broker close"
            db.commit()

            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=1),
            )

            rows = db.query(LiveExitChallengerTrade).all()
            assert len(rows) == 11
            assert {row.status for row in rows} == {"CLOSED"}
            assert {
                row.challenger_exit_reason for row in rows
            } == {"BASELINE_EXIT"}
            assert {
                row.baseline_exit_reason for row in rows
            } == {"manual broker close"}

    @pytest.mark.parametrize(
        ("field", "invalid_value"),
        (
            ("filled_at", None),
            ("filled_at", _ENTRY_AT - timedelta(seconds=1)),
            ("executed_price", 0.0),
            ("executed_price", math.inf),
            ("net_pnl", None),
            ("net_pnl", math.inf),
        ),
    )
    def test_invalid_baseline_pair_waits_for_complete_revision(
        self,
        field: str,
        invalid_value: object,
    ) -> None:
        with self._db() as db:
            service = LiveExitChallengerService(db)
            self._register(service)
            self._open_real_position(db)
            assert service.prepare_open_position(
                symbol=_SYMBOL,
                now=_ENTRY_AT + timedelta(seconds=30),
            ) is True
            service.advance_bar(
                symbol=_SYMBOL,
                bar=_bar(
                    10,
                    open_price=100.2,
                    high=100.3,
                    low=100.1,
                ),
                observed_at=_ENTRY_AT + timedelta(minutes=11),
            )
            registration = db.query(LiveExitChallengerRegistration).filter(
                LiveExitChallengerRegistration.algorithm_version
                == "live-time-stop-m10-v1"
            ).one()
            row = db.query(LiveExitChallengerTrade).filter(
                LiveExitChallengerTrade.registration_id == registration.id
            ).one()
            assert row.status == "CLOSED"
            assert row.challenger_exit_reason == "TIME_STOP"

            baseline = self._close_real_position(
                db,
                exit_at=_ENTRY_AT + timedelta(minutes=20),
            )
            original_value = getattr(baseline, field)
            setattr(baseline, field, invalid_value)
            db.commit()

            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=21),
            )
            db.refresh(row)

            assert row.baseline_exit_order_id is None
            assert row.baseline_exit_at is None
            assert row.baseline_exit_price is None
            assert row.baseline_exit_reason == ""
            assert row.baseline_net_pnl is None
            assert row.net_pnl_delta is None
            assert row.paired_at is None

            setattr(baseline, field, original_value)
            db.commit()
            service.sync_baseline_outcomes(
                symbol=_SYMBOL,
                paired_at=_ENTRY_AT + timedelta(minutes=22),
            )
            db.refresh(row)

            assert row.baseline_exit_order_id == baseline.id
            assert row.baseline_exit_at == baseline.filled_at
            assert row.baseline_exit_price == baseline.executed_price
            assert row.baseline_exit_reason == "TIME_STOP"
            assert row.baseline_net_pnl == baseline.net_pnl
            assert row.net_pnl_delta == pytest.approx(
                float(row.challenger_net_pnl or 0) - float(baseline.net_pnl or 0)
            )
            assert row.paired_at is not None
            assert row.paired_at.replace(tzinfo=timezone.utc) == (
                _ENTRY_AT + timedelta(minutes=22)
            )

    def test_report_reflects_runtime_enable_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
        with self._db() as db:
            report = LiveExitChallengerService(db).get_report(_SYMBOL)
        assert report.enabled is True
