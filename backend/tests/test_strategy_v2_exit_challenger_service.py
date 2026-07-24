from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain.strategy_v2 import StrategyBar
from app.models import (
    Base,
    StrategyV2ExitChallengerRegistration,
    StrategyV2ExitChallengerTrade,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)
from app.services.strategy_v2_exit_challenger_service import (
    StrategyV2ExitChallengerService,
)


_REGISTERED_AT = datetime(2026, 7, 24, 14, 30, 10, tzinfo=timezone.utc)
_ELIGIBLE_ENTRY = datetime(2026, 7, 24, 14, 31, tzinfo=timezone.utc)
_VERSION = "a" * 64


def _bar(
    minute: int,
    *,
    open_price: float,
    high: float,
    low: float,
) -> StrategyBar:
    return StrategyBar(
        timestamp=_ELIGIBLE_ENTRY + timedelta(minutes=minute),
        open=open_price,
        high=high,
        low=low,
        close=open_price,
        volume=1000,
        symbol="AAPL.US",
    )


class TestStrategyV2ExitChallengerService:
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
                StrategyV2ExitChallengerTrade,
                StrategyV2ExitChallengerRegistration,
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
            ):
                db.query(model).delete()
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    @staticmethod
    def _register(service: StrategyV2ExitChallengerService) -> None:
        created = service.ensure_registrations(
            symbol="AAPL.US",
            market="US",
            source_config_version=_VERSION,
            slippage_bps=2.0,
            now=_REGISTERED_AT,
        )
        assert created is True

    @staticmethod
    def _baseline_entry(
        db: Session,
        *,
        entry_at: datetime = _ELIGIBLE_ENTRY,
    ) -> StrategyV2ShadowTrade:
        decision = StrategyV2ShadowDecision(
            idempotency_key=f"entry-{entry_at.isoformat()}",
            symbol="AAPL.US",
            market="US",
            config_version=_VERSION,
            session_date=entry_at.date(),
            bar_at=entry_at,
            observed_at=entry_at + timedelta(minutes=1, seconds=5),
            action="FILL_ENTRY",
            close_price=100.0,
            gate_reasons_json="[]",
            features_json="{}",
        )
        db.add(decision)
        db.flush()
        trade = StrategyV2ShadowTrade(
            symbol="AAPL.US",
            config_version=_VERSION,
            entry_decision_id=decision.id,
            status="OPEN",
            entry_at=entry_at,
            entry_price=100.0,
            quantity=1.0,
            entry_reason="NEXT_BAR_OPEN_FILL",
            estimated_fees=0.05,
            estimated_fee_rate=0.0005,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        return trade

    @staticmethod
    def _close_baseline(
        db: Session,
        trade: StrategyV2ShadowTrade,
        *,
        exit_at: datetime,
        exit_price: float,
        reason: str,
    ) -> None:
        gross = exit_price - trade.entry_price
        fees = (trade.entry_price + exit_price) * 0.0005
        trade.status = "CLOSED"
        trade.exit_at = exit_at
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.gross_pnl = gross
        trade.estimated_fees = fees
        trade.net_pnl = gross - fees
        db.add(trade)
        db.commit()

    def test_registrations_are_frozen_idempotent_and_start_next_full_minute(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2ExitChallengerService(db)
            self._register(service)

            assert service.ensure_registrations(
                symbol="AAPL.US",
                market="US",
                source_config_version=_VERSION,
                slippage_bps=2.0,
                now=_REGISTERED_AT + timedelta(minutes=5),
            ) is False
            rows = db.query(StrategyV2ExitChallengerRegistration).order_by(
                StrategyV2ExitChallengerRegistration.locked_profit_pct
            ).all()

            assert [row.locked_profit_pct for row in rows] == [0.10, 0.20, 0.30]
            assert {row.activation_pct for row in rows} == {0.40}
            assert {
                row.eligible_after.replace(tzinfo=timezone.utc)
                for row in rows
            } == {_ELIGIBLE_ENTRY}
            assert all(len(row.evaluator_digest) == 64 for row in rows)
            with pytest.raises(
                ValueError,
                match="differs from frozen evaluator",
            ):
                service.ensure_registrations(
                    symbol="AAPL.US",
                    market="US",
                    source_config_version=_VERSION,
                    slippage_bps=3.0,
                    now=_REGISTERED_AT + timedelta(minutes=10),
                )

    def test_entry_before_registration_is_never_backfilled(self) -> None:
        with self._db() as db:
            service = StrategyV2ExitChallengerService(db)
            self._register(service)
            self._baseline_entry(
                db,
                entry_at=_ELIGIBLE_ENTRY - timedelta(minutes=1),
            )

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(1, open_price=100.1, high=100.5, low=100.0),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2),
            )

            assert db.query(StrategyV2ExitChallengerTrade).count() == 0

    def test_activation_is_next_bar_causal_and_pairs_later_baseline_outcome(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2ExitChallengerService(db)
            self._register(service)
            baseline = self._baseline_entry(db)

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(0, open_price=100.0, high=100.6, low=99.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1, seconds=5),
            )
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(1, open_price=100.1, high=100.5, low=100.0),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2, seconds=5),
            )

            floor_twenty = (
                db.query(StrategyV2ExitChallengerTrade)
                .join(
                    StrategyV2ExitChallengerRegistration,
                    StrategyV2ExitChallengerRegistration.id
                    == StrategyV2ExitChallengerTrade.registration_id,
                )
                .filter(
                    StrategyV2ExitChallengerRegistration.locked_profit_pct
                    == 0.20
                )
                .one()
            )
            assert floor_twenty.status == "OPEN"
            assert floor_twenty.activation_at is not None
            assert floor_twenty.activation_at.replace(
                tzinfo=timezone.utc
            ) == _ELIGIBLE_ENTRY + timedelta(minutes=1)

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(2, open_price=100.3, high=100.35, low=100.05),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=3, seconds=5),
            )
            db.refresh(floor_twenty)
            assert floor_twenty.status == "CLOSED"
            assert floor_twenty.challenger_exit_reason == "PROFIT_LOCK"
            assert floor_twenty.challenger_exit_price == pytest.approx(
                100.2 * 0.9998
            )
            assert floor_twenty.baseline_net_pnl is None

            baseline_exit_at = _ELIGIBLE_ENTRY + timedelta(minutes=9)
            self._close_baseline(
                db,
                baseline,
                exit_at=baseline_exit_at,
                exit_price=99.5,
                reason="MAX_HOLD",
            )
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(9, open_price=99.5, high=99.6, low=99.4),
                observed_at=baseline_exit_at + timedelta(minutes=1, seconds=5),
            )
            db.refresh(floor_twenty)

            assert floor_twenty.baseline_net_pnl == pytest.approx(
                baseline.net_pnl
            )
            assert floor_twenty.net_pnl_delta == pytest.approx(
                float(floor_twenty.challenger_net_pnl or 0.0)
                - float(baseline.net_pnl or 0.0)
            )
            report = service.get_report("AAPL.US")
            variant = next(
                item for item in report.variants
                if item.locked_profit_pct == 0.20
            )
            assert report.order_submission_allowed is False
            assert report.automatic_promotion_allowed is False
            assert variant.paired_trades == 1
            assert variant.profit_lock_exits == 1
            assert variant.net_pnl_delta > 0
            assert variant.promotion_ready is False
            assert "MIN_PAIRED_TRADES" in variant.blockers

    def test_max_hold_at_bar_open_precedes_intrabar_profit_lock(self) -> None:
        with self._db() as db:
            service = StrategyV2ExitChallengerService(db)
            self._register(service)
            baseline = self._baseline_entry(db)
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(0, open_price=100.0, high=100.1, low=99.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1, seconds=5),
            )
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(1, open_price=100.3, high=100.5, low=100.25),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2, seconds=5),
            )

            exit_at = _ELIGIBLE_ENTRY + timedelta(minutes=2)
            self._close_baseline(
                db,
                baseline,
                exit_at=exit_at,
                exit_price=100.35,
                reason="MAX_HOLD",
            )
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(2, open_price=100.35, high=100.4, low=100.0),
                observed_at=exit_at + timedelta(minutes=1, seconds=5),
            )

            rows = db.query(StrategyV2ExitChallengerTrade).all()
            assert len(rows) == 3
            assert {
                row.challenger_exit_reason for row in rows
            } == {"BASELINE_MAX_HOLD"}
            assert all(
                row.challenger_net_pnl == pytest.approx(baseline.net_pnl)
                for row in rows
            )
            assert all(row.net_pnl_delta == pytest.approx(0.0) for row in rows)
