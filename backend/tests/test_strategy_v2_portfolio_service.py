from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    StrategyV2PortfolioObservation,
    StrategyV2PortfolioRegistration,
    StrategyV2ShadowDecision,
    StrategyV2ShadowTrade,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistScore,
)
from app.services.strategy_v2_portfolio_service import (
    StrategyV2PortfolioService,
)


_REGISTERED_AT = datetime(
    2026,
    7,
    24,
    14,
    30,
    10,
    tzinfo=timezone.utc,
)
_FIRST_SIGNAL = datetime(
    2026,
    7,
    24,
    14,
    31,
    tzinfo=timezone.utc,
)


class TestStrategyV2PortfolioService:
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
                StrategyV2PortfolioObservation,
                StrategyV2PortfolioRegistration,
                StrategyV2ShadowTrade,
                StrategyV2ShadowDecision,
                WatchlistScore,
                UniverseSelectionCandidate,
                UniverseSelectionRun,
            ):
                db.query(model).delete()
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    @staticmethod
    def _register(service: StrategyV2PortfolioService) -> None:
        assert service.ensure_registrations(
            primary_symbol="NVDA.US",
            now=_REGISTERED_AT,
        ) is True

    @staticmethod
    def _universe(db: Session) -> None:
        run = UniverseSelectionRun(
            as_of_date=date(2026, 7, 23),
            algorithm_version="selection-v1",
            source_version="catalog-v1",
            status="COMPLETE",
            selected_count=2,
            completed_at=_REGISTERED_AT - timedelta(minutes=1),
        )
        db.add(run)
        db.flush()
        db.add_all([
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="MSFT.US",
                selected=True,
                rank=1,
                score=75,
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="AAPL.US",
                selected=True,
                rank=2,
                score=80,
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="NVDA.US",
                selected=False,
                score=90,
            ),
        ])
        db.commit()

    @staticmethod
    def _quant(
        db: Session,
        symbol: str,
        action: str,
        score: float,
        *,
        created_at: datetime | None = None,
    ) -> None:
        observed = created_at or (
            _REGISTERED_AT - timedelta(minutes=1)
        )
        db.add(WatchlistScore(
            symbol=symbol,
            score=score,
            confidence=0.8,
            recommended_action=action,
            source="quant_v5",
            created_at=observed,
            expires_at=observed + timedelta(hours=2),
        ))
        db.commit()

    @staticmethod
    def _signal(
        db: Session,
        symbol: str,
        signal_at: datetime,
    ) -> StrategyV2ShadowDecision:
        row = StrategyV2ShadowDecision(
            idempotency_key=f"signal-{symbol}-{signal_at.isoformat()}",
            symbol=symbol,
            market="US",
            config_version=f"version-{symbol}",
            session_date=signal_at.date(),
            bar_at=signal_at,
            observed_at=signal_at + timedelta(minutes=1, seconds=5),
            action="SUBMIT_ENTRY",
            close_price=100,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def _fill(
        db: Session,
        signal: StrategyV2ShadowDecision,
        *,
        price: float,
    ) -> StrategyV2ShadowTrade:
        entry_at = signal.bar_at + timedelta(minutes=1)
        entry = StrategyV2ShadowDecision(
            idempotency_key=f"fill-{signal.symbol}-{entry_at.isoformat()}",
            symbol=signal.symbol,
            market="US",
            config_version=signal.config_version,
            session_date=entry_at.date(),
            bar_at=entry_at,
            observed_at=entry_at + timedelta(minutes=1, seconds=5),
            action="FILL_ENTRY",
            close_price=price,
        )
        db.add(entry)
        db.flush()
        trade = StrategyV2ShadowTrade(
            symbol=signal.symbol,
            config_version=signal.config_version,
            entry_decision_id=entry.id,
            status="OPEN",
            entry_at=entry_at,
            entry_price=price,
            quantity=1,
            estimated_fee_rate=0.0005,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        return trade

    @staticmethod
    def _close(
        db: Session,
        trade: StrategyV2ShadowTrade,
        *,
        exit_at: datetime,
        exit_price: float,
    ) -> None:
        gross = exit_price - trade.entry_price
        fees = (
            trade.entry_price + exit_price
        ) * trade.quantity * 0.0005
        trade.status = "CLOSED"
        trade.exit_at = exit_at
        trade.exit_price = exit_price
        trade.exit_reason = "TARGET"
        trade.gross_pnl = gross
        trade.estimated_fees = fees
        trade.net_pnl = gross - fees
        db.add(trade)
        db.commit()

    def test_registration_is_idempotent_and_never_backfills(self) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL - timedelta(minutes=1),
            )
            self._register(service)

            assert service.ensure_registrations(
                primary_symbol="NVDA.US",
                now=_REGISTERED_AT + timedelta(minutes=5),
            ) is False
            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=2)
            )

            registrations = db.query(
                StrategyV2PortfolioRegistration
            ).all()
            assert len(registrations) == 4
            assert {
                row.eligible_after.replace(tzinfo=timezone.utc)
                for row in registrations
            } == {_FIRST_SIGNAL}
            assert all(
                len(row.evaluator_digest) == 64
                for row in registrations
            )
            assert db.query(
                StrategyV2PortfolioObservation
            ).count() == 0

    def test_policies_route_causally_and_single_slot_skips_overlap(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._quant(db, "MSFT.US", "CANDIDATE", 51)
            self._quant(db, "AAPL.US", "WATCH", 80)
            self._quant(db, "NVDA.US", "AVOID", 39)
            signals = {
                symbol: self._signal(db, symbol, _FIRST_SIGNAL)
                for symbol in ("AAPL.US", "MSFT.US", "NVDA.US")
            }
            msft_trade = self._fill(
                db,
                signals["MSFT.US"],
                price=100,
            )
            nvda_trade = self._fill(
                db,
                signals["NVDA.US"],
                price=200,
            )

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            selected = {
                row.policy: db.query(
                    StrategyV2PortfolioObservation
                ).filter(
                    StrategyV2PortfolioObservation.registration_id
                    == row.id
                ).one().selected_symbol
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            assert selected == {
                "FIXED_PRIMARY": "NVDA.US",
                "SELECTED_UNIVERSE": "MSFT.US",
                "QUANT_CANDIDATE": "MSFT.US",
                "QUANT_WATCH_PLUS": "MSFT.US",
            }

            self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL + timedelta(minutes=2),
            )
            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=5)
            )
            overlap_rows = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.signal_at
                == _FIRST_SIGNAL + timedelta(minutes=2)
            ).all()
            assert len(overlap_rows) == 4
            assert {
                row.status for row in overlap_rows
            } == {"SKIPPED_OCCUPIED"}

            exit_at = _FIRST_SIGNAL + timedelta(minutes=5)
            self._close(
                db,
                msft_trade,
                exit_at=exit_at,
                exit_price=101,
            )
            self._close(
                db,
                nvda_trade,
                exit_at=exit_at,
                exit_price=198,
            )
            service.advance(now=exit_at + timedelta(minutes=1))

            report = service.get_report("NVDA.US")
            by_policy = {
                row.policy: row
                for row in report.variants
            }
            assert report.order_submission_allowed is False
            assert report.automatic_promotion_allowed is False
            assert report.historical_backfill_allowed is False
            assert by_policy[
                "FIXED_PRIMARY"
            ].metrics.compounded_return_pct < 0
            assert by_policy[
                "QUANT_CANDIDATE"
            ].metrics.compounded_return_pct > 0
            assert by_policy[
                "QUANT_CANDIDATE"
            ].compounded_return_delta_pct > 0
            assert by_policy[
                "QUANT_CANDIDATE"
            ].metrics.skipped_occupied == 1
            assert by_policy[
                "QUANT_CANDIDATE"
            ].promotion_ready is False
            assert "MIN_CLOSED_TRADES" in by_policy[
                "QUANT_CANDIDATE"
            ].blockers

    def test_future_quant_score_is_not_visible_at_signal_time(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._signal(db, "MSFT.US", _FIRST_SIGNAL)
            self._quant(
                db,
                "MSFT.US",
                "CANDIDATE",
                90,
                created_at=(
                    _FIRST_SIGNAL
                    + timedelta(minutes=1, seconds=1)
                ),
            )

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "QUANT_CANDIDATE"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "NO_ELIGIBLE"
            assert observation.selected_symbol == ""

    def test_newer_quant_error_blocks_fallback_to_older_candidate(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._signal(db, "MSFT.US", _FIRST_SIGNAL)
            self._quant(db, "MSFT.US", "CANDIDATE", 60)
            db.add(WatchlistScore(
                symbol="MSFT.US",
                score=0,
                confidence=0,
                recommended_action="AVOID",
                source="quant_error_v5",
                created_at=_FIRST_SIGNAL + timedelta(seconds=30),
                expires_at=_FIRST_SIGNAL + timedelta(hours=1),
            ))
            db.commit()

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "QUANT_CANDIDATE"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "NO_ELIGIBLE"

    def test_future_universe_run_is_not_visible_at_signal_time(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            old_run = UniverseSelectionRun(
                as_of_date=date(2026, 7, 22),
                algorithm_version="selection-old",
                source_version="catalog-v1",
                status="COMPLETE",
                selected_count=1,
                completed_at=_REGISTERED_AT - timedelta(minutes=1),
            )
            future_run = UniverseSelectionRun(
                as_of_date=date(2026, 7, 23),
                algorithm_version="selection-future",
                source_version="catalog-v1",
                status="COMPLETE",
                selected_count=1,
                completed_at=(
                    _FIRST_SIGNAL
                    + timedelta(minutes=1, seconds=1)
                ),
            )
            db.add_all([old_run, future_run])
            db.flush()
            db.add_all([
                UniverseSelectionCandidate(
                    run_id=old_run.id,
                    symbol="AAPL.US",
                    selected=True,
                    rank=1,
                    score=60,
                ),
                UniverseSelectionCandidate(
                    run_id=old_run.id,
                    symbol="MSFT.US",
                    selected=False,
                    score=90,
                ),
                UniverseSelectionCandidate(
                    run_id=future_run.id,
                    symbol="AAPL.US",
                    selected=False,
                    score=50,
                ),
                UniverseSelectionCandidate(
                    run_id=future_run.id,
                    symbol="MSFT.US",
                    selected=True,
                    rank=1,
                    score=99,
                ),
            ])
            db.commit()
            self._signal(db, "AAPL.US", _FIRST_SIGNAL)
            self._signal(db, "MSFT.US", _FIRST_SIGNAL)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "SELECTED_UNIVERSE"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.selected_symbol == "AAPL.US"

    def test_unbound_source_entry_becomes_missed_without_blocking_forever(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._signal(db, "NVDA.US", _FIRST_SIGNAL)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=12)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "FIXED_PRIMARY"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "MISSED"
            assert observation.reason == "SOURCE_ENTRY_MISSING"
