from __future__ import annotations

import json
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
    StrategyV2ShadowVersion,
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
                StrategyV2ShadowVersion,
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
        *,
        close_price: float = 100,
        vwap_1m: float | None = None,
        vwap_5m: float | None = None,
    ) -> StrategyV2ShadowDecision:
        features: dict[str, float] = {}
        if vwap_1m is not None:
            features["residual_1m"] = close_price / vwap_1m - 1
        if vwap_5m is not None:
            features["residual_5m"] = close_price / vwap_5m - 1
        row = StrategyV2ShadowDecision(
            idempotency_key=f"signal-{symbol}-{signal_at.isoformat()}",
            symbol=symbol,
            market="US",
            config_version=f"version-{symbol}",
            session_date=signal_at.date(),
            bar_at=signal_at,
            observed_at=signal_at + timedelta(minutes=1, seconds=5),
            action="SUBMIT_ENTRY",
            close_price=close_price,
            vwap_1m=vwap_1m,
            vwap_5m=vwap_5m,
            features_json=json.dumps(features),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def _version(
        db: Session,
        symbol: str,
        *,
        activated_at: datetime | None = None,
    ) -> None:
        db.add(StrategyV2ShadowVersion(
            symbol=symbol,
            config_version=f"version-{symbol}",
            config_json=json.dumps({
                "estimated_fee_rate_us": 0.0005,
                "estimated_fee_rate_hk": 0.003,
                "slippage_bps": 2.0,
                "stop_loss_pct": 0.75,
            }),
            activated_at=(
                activated_at
                or _REGISTERED_AT - timedelta(minutes=1)
            ),
        ))
        db.commit()

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
            assert len(registrations) == 6
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
                "SELECTED_VWAP_EDGE": "",
                "VWAP_EDGE_POOL": "",
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
            assert len(overlap_rows) == 6
            registrations_by_id = {
                row.id: row.policy
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            status_by_policy = {
                registrations_by_id[row.registration_id]: row.status
                for row in overlap_rows
            }
            assert status_by_policy == {
                "FIXED_PRIMARY": "SKIPPED_OCCUPIED",
                "SELECTED_UNIVERSE": "SKIPPED_OCCUPIED",
                "QUANT_CANDIDATE": "SKIPPED_OCCUPIED",
                "QUANT_WATCH_PLUS": "SKIPPED_OCCUPIED",
                "SELECTED_VWAP_EDGE": "NO_ELIGIBLE",
                "VWAP_EDGE_POOL": "NO_ELIGIBLE",
            }

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

    def test_vwap_edge_policies_use_frozen_cost_and_stop_band(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            for symbol in ("AAPL.US", "MSFT.US", "TER.US"):
                self._version(db, symbol)
            signals = {
                "AAPL.US": self._signal(
                    db,
                    "AAPL.US",
                    _FIRST_SIGNAL,
                    close_price=99.7,
                    vwap_1m=100,
                    vwap_5m=100.2,
                ),
                "MSFT.US": self._signal(
                    db,
                    "MSFT.US",
                    _FIRST_SIGNAL,
                    close_price=100.1,
                    vwap_1m=100,
                    vwap_5m=100.2,
                ),
                "TER.US": self._signal(
                    db,
                    "TER.US",
                    _FIRST_SIGNAL,
                    close_price=99.6,
                    vwap_1m=100,
                    vwap_5m=100.1,
                ),
            }
            self._fill(db, signals["AAPL.US"], price=99.72)
            self._fill(db, signals["TER.US"], price=99.62)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registrations = {
                row.policy: row
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            selected_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["SELECTED_VWAP_EDGE"].id
            ).one()
            pool_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["VWAP_EDGE_POOL"].id
            ).one()
            assert selected_observation.selected_symbol == "AAPL.US"
            assert pool_observation.selected_symbol == "TER.US"
            assert selected_observation.status == "OPEN"
            assert pool_observation.status == "OPEN"
            selected_candidates = json.loads(
                selected_observation.candidates_json
            )
            assert selected_candidates[0]["round_trip_cost_bps"] == 14
            assert selected_candidates[0]["stop_distance_bps"] == 75
            assert selected_candidates[0]["residual_1m_bps"] == pytest.approx(
                -30
            )
            report = service.get_report("NVDA.US")
            report_by_policy = {
                row.policy: row for row in report.variants
            }
            assert (
                report_by_policy["SELECTED_VWAP_EDGE"].edge_filter
                == "COST_TO_STOP_VWAP_DISCOUNT"
            )
            assert (
                report_by_policy["FIXED_PRIMARY"].edge_filter
                == "NONE"
            )

    def test_future_shadow_version_is_not_visible_to_vwap_edge(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._version(
                db,
                "AAPL.US",
                activated_at=(
                    _FIRST_SIGNAL
                    + timedelta(minutes=1, seconds=1)
                ),
            )
            self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.7,
                vwap_1m=100,
                vwap_5m=100.2,
            )

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registrations = {
                row.policy: row
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            for policy in (
                "SELECTED_VWAP_EDGE",
                "VWAP_EDGE_POOL",
            ):
                observation = db.query(
                    StrategyV2PortfolioObservation
                ).filter(
                    StrategyV2PortfolioObservation.registration_id
                    == registrations[policy].id
                ).one()
                assert observation.status == "NO_ELIGIBLE"

    def test_cash_baseline_uses_observed_sessions_not_trade_count(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            registrations = {
                row.policy: row
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            baseline = registrations["FIXED_PRIMARY"]
            for offset in range(10):
                db.add(StrategyV2PortfolioObservation(
                    registration_id=baseline.id,
                    signal_at=(
                        _FIRST_SIGNAL + timedelta(days=offset)
                    ),
                    observed_at=(
                        _FIRST_SIGNAL
                        + timedelta(days=offset, minutes=1)
                    ),
                    status="NO_ELIGIBLE",
                    reason="NO_PRIMARY_SIGNAL",
                ))
            db.commit()

            report = service.get_report("NVDA.US")
            pool = next(
                row
                for row in report.variants
                if row.policy == "VWAP_EDGE_POOL"
            )
            assert "BASELINE_EVIDENCE_INSUFFICIENT" not in pool.blockers

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
