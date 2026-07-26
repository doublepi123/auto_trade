from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain.strategy_v2 import StrategyBar
from app.models import (
    Base,
    StrategyV2BracketChallengerRegistration,
    StrategyV2BracketChallengerTrade,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
)
from app.services.strategy_v2_bracket_challenger_service import (
    StrategyV2BracketChallengerService,
)


_REGISTERED_AT = datetime(2026, 7, 24, 14, 30, 10, tzinfo=timezone.utc)
_ELIGIBLE_ENTRY = datetime(2026, 7, 24, 14, 31, tzinfo=timezone.utc)
_VERSION = "b" * 64


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


class TestStrategyV2BracketChallengerService:
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
                StrategyV2BracketChallengerTrade,
                StrategyV2BracketChallengerRegistration,
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
            ):
                db.query(model).delete()
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    @staticmethod
    def _register(
        service: StrategyV2BracketChallengerService,
        *,
        fee_rate: float = 0.0005,
    ) -> None:
        assert service.ensure_registrations(
            symbol="AAPL.US",
            market="US",
            source_config_version=_VERSION,
            slippage_bps=2.0,
            estimated_fee_rate=fee_rate,
            max_holding_minutes=60,
            flatten_minutes_before_close=15,
            now=_REGISTERED_AT,
        ) is True

    @staticmethod
    def _baseline_entry(
        db: Session,
        *,
        entry_at: datetime = _ELIGIBLE_ENTRY,
        fee_rate: float = 0.0005,
        holding_minutes: int = 60,
        signal_vwap: float = 100.0,
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
            quantity=2.0,
            stop_price=99.55,
            target_price=max(100.8, signal_vwap),
            signal_vwap=signal_vwap,
            holding_deadline=entry_at + timedelta(minutes=holding_minutes),
            entry_reason="FIRST_CAUSAL_BAR_OPEN_FILL",
            estimated_fees=100.0 * 2.0 * fee_rate,
            estimated_fee_rate=fee_rate,
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
        fee_rate = float(trade.estimated_fee_rate or 0.0)
        gross = (exit_price - trade.entry_price) * trade.quantity
        fees = (
            (trade.entry_price + exit_price)
            * trade.quantity
            * fee_rate
        )
        trade.status = "CLOSED"
        trade.exit_at = exit_at
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.gross_pnl = gross
        trade.estimated_fees = fees
        trade.net_pnl = gross - fees
        db.add(trade)
        db.commit()

    def test_registrations_are_frozen_idempotent_and_forward_only(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service)

            assert service.ensure_registrations(
                symbol="AAPL.US",
                market="US",
                source_config_version=_VERSION,
                slippage_bps=2.0,
                estimated_fee_rate=0.0005,
                max_holding_minutes=60,
                flatten_minutes_before_close=15,
                now=_REGISTERED_AT + timedelta(minutes=5),
            ) is False
            rows = db.query(
                StrategyV2BracketChallengerRegistration
            ).order_by(
                StrategyV2BracketChallengerRegistration.stop_loss_pct
            ).all()

            assert {
                (
                    row.stop_loss_pct,
                    row.profit_target_pct,
                    row.vwap_target_cap_bps,
                )
                for row in rows
            } == {
                (0.40, 0.70, None),
                (0.40, 0.70, 75.0),
                (0.50, 1.00, None),
            }
            assert {
                row.eligible_after.replace(tzinfo=timezone.utc)
                for row in rows
            } == {_ELIGIBLE_ENTRY}
            assert all(
                row.estimated_net_reward_risk_ratio >= 1.0
                for row in rows
            )
            assert all(len(row.evaluator_digest) == 64 for row in rows)

            with pytest.raises(
                ValueError,
                match="differs from frozen evaluator",
            ):
                service.ensure_registrations(
                    symbol="AAPL.US",
                    market="US",
                    source_config_version=_VERSION,
                    slippage_bps=2.0,
                    estimated_fee_rate=0.0005,
                    max_holding_minutes=59,
                    flatten_minutes_before_close=15,
                    now=_REGISTERED_AT + timedelta(minutes=10),
                )

    def test_entry_before_registration_is_never_backfilled(self) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
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

            assert db.query(StrategyV2BracketChallengerTrade).count() == 0

    def test_candidate_can_finish_after_baseline_exits_and_pair(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service)
            baseline = self._baseline_entry(db)

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(0, open_price=100.0, high=100.1, low=99.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1),
            )
            baseline_exit_at = _ELIGIBLE_ENTRY + timedelta(minutes=1)
            self._close_baseline(
                db,
                baseline,
                exit_at=baseline_exit_at,
                exit_price=100.8,
                reason="PROFIT_TARGET",
            )
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(1, open_price=100.8, high=100.85, low=100.7),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2),
            )

            wide = (
                db.query(StrategyV2BracketChallengerTrade)
                .join(
                    StrategyV2BracketChallengerRegistration,
                    StrategyV2BracketChallengerRegistration.id
                    == StrategyV2BracketChallengerTrade.registration_id,
                )
                .filter(
                    StrategyV2BracketChallengerRegistration.stop_loss_pct
                    == 0.50
                )
                .one()
            )
            assert wide.status == "OPEN"
            assert service.has_open_trades("AAPL.US") is True

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(2, open_price=101.0, high=101.1, low=100.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=3),
            )
            db.refresh(wide)

            assert wide.status == "CLOSED"
            assert wide.challenger_exit_reason == "PROFIT_TARGET"
            assert wide.challenger_exit_price == pytest.approx(
                101.0 * 0.9998
            )
            assert wide.baseline_net_pnl == pytest.approx(baseline.net_pnl)
            assert wide.net_pnl_delta is not None
            assert service.has_open_trades("AAPL.US") is False

            report = service.get_report("AAPL.US")
            variant = next(
                item
                for item in report.variants
                if item.stop_loss_pct == 0.50
            )
            assert report.order_submission_allowed is False
            assert report.automatic_promotion_allowed is False
            assert report.historical_backfill_allowed is False
            assert variant.paired_trades == 1
            assert variant.changed_exits == 1
            assert variant.exit_reasons == {"PROFIT_TARGET": 1}
            assert "MIN_PAIRED_TRADES" in variant.blockers

    def test_ambiguous_bar_uses_conservative_stop(self) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service)
            self._baseline_entry(db)
            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(0, open_price=100.0, high=100.1, low=99.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1),
            )

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(1, open_price=100.0, high=101.2, low=99.0),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2),
            )

            rows = db.query(StrategyV2BracketChallengerTrade).all()
            assert len(rows) == 3
            assert {
                row.challenger_exit_reason for row in rows
            } == {"PRICE_STOP"}
            expected = {
                0.40: 99.6 * 0.9998,
                0.50: 99.5 * 0.9998,
            }
            for row in rows:
                registration = db.get(
                    StrategyV2BracketChallengerRegistration,
                    row.registration_id,
                )
                assert registration is not None
                assert row.challenger_exit_price == pytest.approx(
                    expected[registration.stop_loss_pct]
                )

    @pytest.mark.parametrize(
        ("fee_rate", "holding_minutes", "message"),
        (
            (0.0006, 60, "fee rate differs"),
            (0.0005, 59, "holding deadline differs"),
        ),
    )
    def test_frozen_entry_assumption_mismatch_fails_closed(
        self,
        fee_rate: float,
        holding_minutes: int,
        message: str,
    ) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service)
            self._baseline_entry(
                db,
                fee_rate=fee_rate,
                holding_minutes=holding_minutes,
            )

            with pytest.raises(ValueError, match=message):
                service.advance_bar(
                    symbol="AAPL.US",
                    bar=_bar(
                        1,
                        open_price=100.0,
                        high=100.1,
                        low=99.9,
                    ),
                    observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=2),
                )

            assert db.query(StrategyV2BracketChallengerTrade).count() == 0

    def test_zero_fee_registration_and_entry_are_supported(self) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service, fee_rate=0.0)
            self._baseline_entry(db, fee_rate=0.0)

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(0, open_price=100.0, high=100.1, low=99.9),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1),
            )

            assert db.query(StrategyV2BracketChallengerTrade).count() == 3

    def test_capped_vwap_target_is_evaluated_on_entry_bar(self) -> None:
        with self._db() as db:
            service = StrategyV2BracketChallengerService(db)
            self._register(service)
            self._baseline_entry(db, signal_vwap=101.0)

            service.advance_bar(
                symbol="AAPL.US",
                bar=_bar(
                    0,
                    open_price=100.0,
                    high=100.80,
                    low=99.90,
                ),
                observed_at=_ELIGIBLE_ENTRY + timedelta(minutes=1),
            )

            capped = (
                db.query(StrategyV2BracketChallengerTrade)
                .join(
                    StrategyV2BracketChallengerRegistration,
                    StrategyV2BracketChallengerRegistration.id
                    == StrategyV2BracketChallengerTrade.registration_id,
                )
                .filter(
                    StrategyV2BracketChallengerRegistration.vwap_target_cap_bps
                    == 75.0
                )
                .one()
            )
            uncapped = (
                db.query(StrategyV2BracketChallengerTrade)
                .join(
                    StrategyV2BracketChallengerRegistration,
                    StrategyV2BracketChallengerRegistration.id
                    == StrategyV2BracketChallengerTrade.registration_id,
                )
                .filter(
                    StrategyV2BracketChallengerRegistration.stop_loss_pct
                    == 0.40,
                    StrategyV2BracketChallengerRegistration.vwap_target_cap_bps.is_(
                        None
                    ),
                )
                .one()
            )

            assert capped.status == "CLOSED"
            assert capped.challenger_exit_reason == "PROFIT_TARGET"
            assert capped.target_price == pytest.approx(100.75)
            assert uncapped.status == "OPEN"
            assert uncapped.target_price == pytest.approx(101.0)
