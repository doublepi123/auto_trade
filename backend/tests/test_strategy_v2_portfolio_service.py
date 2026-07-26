from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain.universe_selection import (
    DIVERSIFIED_INVERSE_VOLATILITY_VARIANT,
    DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT,
    ROTATION_ALGORITHM_VERSION,
    ROTATION_WALK_FORWARD_VERSION,
)
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
                sector="Software",
                selected=True,
                rank=1,
                score=75,
                metrics_json=json.dumps({
                    "rotation": {
                        "algorithm_version": ROTATION_ALGORITHM_VERSION,
                        "selected": True,
                        "rank": 2,
                        "score": 90,
                    }
                }),
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="AAPL.US",
                sector="Technology Hardware",
                selected=True,
                rank=2,
                score=80,
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="NVDA.US",
                sector="Semiconductors",
                selected=False,
                score=90,
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="IBM.US",
                sector="Technology Hardware",
                selected=False,
                score=74,
                metrics_json=json.dumps({
                    "rotation": {
                        "algorithm_version": ROTATION_ALGORITHM_VERSION,
                        "selected": True,
                        "rank": 1,
                        "score": 100,
                    }
                }),
            ),
            UniverseSelectionCandidate(
                run_id=run.id,
                symbol="CSCO.US",
                sector="Technology Hardware",
                selected=False,
                score=63,
            ),
        ])
        db.commit()

    @staticmethod
    def _validated_inverse_volatility_registration(db: Session) -> None:
        run = db.query(UniverseSelectionRun).one()
        variant_name = DIVERSIFIED_INVERSE_VOLATILITY_VARIANT.name

        def signal(
            symbol: str,
            rank: int,
            score: float,
            target_weight_pct: float,
        ) -> dict[str, object]:
            return {
                "symbol": symbol,
                "rank": rank,
                "risk_group": "Test",
                "momentum_pct": score,
                "sma_price": 100.0,
                "above_sma": True,
                "score": score,
                "signal_spread_bps": 1.0,
                "ranking_method": "raw_momentum",
                "formation_realized_volatility": None,
                "ranking_metric": score,
                "target_weight_pct": target_weight_pct,
            }

        remaining_weight = 65.0 / 3
        run.parameters_json = json.dumps({
            "rotation_evaluation": {
                "algorithm_version": ROTATION_WALK_FORWARD_VERSION,
                "status": "COMPLETE",
                "validated_challenger_variant": variant_name,
                "variants": [{
                    "variant": {"name": variant_name},
                    "validation_passed": True,
                    "expanding_validation_passed": True,
                }],
            },
            "rotation_weighting_challenger_registration": {
                "cohort_month": "2026-07-01",
                "rotation_algorithm_version": ROTATION_ALGORITHM_VERSION,
                "variant_name": variant_name,
                "signal_date": "2026-06-30",
                "registered_as_of_date": "2026-07-23",
                "forward_eligible": False,
                "target_signals": [
                    signal("IBM.US", 1, 100, 25),
                    signal("MSFT.US", 2, 90, 10),
                    signal("CAT.US", 3, 80, remaining_weight),
                    signal("GS.US", 4, 70, remaining_weight),
                    signal("AEP.US", 5, 60, remaining_weight),
                ],
            },
        })
        db.commit()

    @staticmethod
    def _validated_point_in_time_shrinkage_registration(
        db: Session,
    ) -> None:
        run = db.query(UniverseSelectionRun).one()
        variant_name = DIVERSIFIED_SHRINKAGE_ROTATION_VARIANT.name

        def signal(
            symbol: str,
            rank: int,
            score: float,
            target_weight_pct: float,
        ) -> dict[str, object]:
            return {
                "symbol": symbol,
                "rank": rank,
                "risk_group": "Test",
                "momentum_pct": score,
                "sma_price": 100.0,
                "above_sma": True,
                "score": score,
                "signal_spread_bps": 1.0,
                "ranking_method": "raw_momentum",
                "formation_realized_volatility": None,
                "ranking_metric": score,
                "target_weight_pct": target_weight_pct,
            }

        run.parameters_json = json.dumps({
            "rotation_point_in_time_sensitivity": {
                "status": "COMPLETE",
                "membership_history": {
                    "authoritative_ratio": 0.98,
                    "source_version": "pit-membership-v1",
                },
                "evaluation": {
                    "algorithm_version": ROTATION_WALK_FORWARD_VERSION,
                    "status": "COMPLETE",
                    "data_scope": "POINT_IN_TIME_RESEARCH_CATALOG",
                    "validated_challenger_variant": variant_name,
                    "variants": [{
                        "variant": {"name": variant_name},
                        "validation_passed": True,
                        "expanding_validation_passed": True,
                    }],
                },
            },
            "rotation_shrinkage_challenger_registration": {
                "cohort_month": "2026-07-01",
                "rotation_algorithm_version": ROTATION_ALGORITHM_VERSION,
                "variant_name": variant_name,
                "signal_date": "2026-06-30",
                "registered_as_of_date": "2026-07-23",
                "forward_eligible": False,
                "target_signals": [
                    signal("IBM.US", 1, 100, 15),
                    signal("MSFT.US", 2, 90, 10),
                    signal("CAT.US", 3, 80, 12.5),
                    signal("GS.US", 4, 70, 12.5),
                    signal("AEP.US", 5, 60, 12.5),
                    signal("ROST.US", 6, 50, 12.5),
                    signal("MRK.US", 7, 40, 12.5),
                    signal("GOOGL.US", 8, 30, 12.5),
                ],
            },
        })
        db.commit()

    @staticmethod
    def _quant(
        db: Session,
        symbol: str,
        action: str,
        score: float,
        *,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        estimated_cost_bps: float | None = None,
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
            estimated_round_trip_cost_bps=estimated_cost_bps,
            created_at=observed,
            expires_at=expires_at or observed + timedelta(hours=2),
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
        zscore_1m: float | None = None,
        zscore_5m: float | None = None,
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
            zscore_1m=zscore_1m,
            vwap_5m=vwap_5m,
            zscore_5m=zscore_5m,
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
        stop_loss_pct: float = 0.75,
    ) -> None:
        db.add(StrategyV2ShadowVersion(
            symbol=symbol,
            config_version=f"version-{symbol}",
            config_json=json.dumps({
                "estimated_fee_rate_us": 0.0005,
                "estimated_fee_rate_hk": 0.003,
                "slippage_bps": 2.0,
                "stop_loss_pct": stop_loss_pct,
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
        entry_at = signal.bar_at + timedelta(minutes=2)
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
            assert len(registrations) == 19
            assert {
                row.eligible_after.replace(tzinfo=timezone.utc)
                for row in registrations
            } == {_FIRST_SIGNAL}
            assert all(
                len(row.evaluator_digest) == 64
                for row in registrations
            )
            risk_group_v1 = next(
                row
                for row in registrations
                if row.policy == "RISK_GROUP_REL_OBS_75BPS_POOL"
            )
            risk_group_loo = next(
                row
                for row in registrations
                if row.policy == "RISK_GROUP_LOO_OBS_75BPS_POOL"
            )
            sector_loo = next(
                row
                for row in registrations
                if row.policy == "SECTOR_LOO_OBS_75BPS_POOL"
            )
            selected_sector_loo = next(
                row
                for row in registrations
                if row.policy
                == "SELECTED_SECTOR_LOO_OBS_75BPS_POOL"
            )
            selected_zscore = next(
                row
                for row in registrations
                if row.policy == "SELECTED_ZSCORE_OBS_75BPS_POOL"
            )
            rotation_zscore = next(
                row
                for row in registrations
                if row.policy == "ROTATION_ZSCORE_OBS_75BPS_POOL"
            )
            weighted_rotation_zscore = next(
                row
                for row in registrations
                if row.policy == "ROTATION_IV_WEIGHTED_ZSCORE_POOL"
            )
            net_edge_rotation_zscore = next(
                row
                for row in registrations
                if row.policy == "ROTATION_IV_NET_EDGE_ZSCORE_POOL"
            )
            pit_shrinkage_weighted = next(
                row
                for row in registrations
                if row.policy
                == "PIT_SHRINK_WEIGHTED_ZSCORE_POOL"
            )
            pit_shrinkage_net_edge = next(
                row
                for row in registrations
                if row.policy
                == "PIT_SHRINK_NET_EDGE_ZSCORE_POOL"
            )
            assert risk_group_v1.evaluator_digest == (
                "9fa13dbedfbeb508e4b3ecca23d1a63c7"
                "fe6c559dc71356922dda4c11528806e"
            )
            assert (
                risk_group_loo.evaluator_digest
                != risk_group_v1.evaluator_digest
            )
            assert sector_loo.evaluator_digest not in {
                risk_group_v1.evaluator_digest,
                risk_group_loo.evaluator_digest,
            }
            assert (
                selected_sector_loo.evaluator_digest
                != sector_loo.evaluator_digest
            )
            assert selected_zscore.evaluator_digest == (
                "4935c83dc7d73e9a1335fd9f8b6205c3"
                "565790ba517d1fd0db53c4a8fbe32157"
            )
            assert len(rotation_zscore.evaluator_digest) == 64
            assert (
                rotation_zscore.evaluator_digest
                != selected_zscore.evaluator_digest
            )
            assert weighted_rotation_zscore.evaluator_digest not in {
                selected_zscore.evaluator_digest,
                rotation_zscore.evaluator_digest,
            }
            assert net_edge_rotation_zscore.evaluator_digest not in {
                selected_zscore.evaluator_digest,
                rotation_zscore.evaluator_digest,
                weighted_rotation_zscore.evaluator_digest,
            }
            assert pit_shrinkage_weighted.evaluator_digest not in {
                weighted_rotation_zscore.evaluator_digest,
                net_edge_rotation_zscore.evaluator_digest,
            }
            assert pit_shrinkage_net_edge.evaluator_digest not in {
                pit_shrinkage_weighted.evaluator_digest,
                net_edge_rotation_zscore.evaluator_digest,
            }
            assert db.query(
                StrategyV2PortfolioObservation
            ).count() == 0

    def test_legacy_router_registration_is_hidden_and_not_advanced(
        self,
    ) -> None:
        with self._db() as db:
            legacy = StrategyV2PortfolioRegistration(
                baseline_symbol="NVDA.US",
                policy="FIXED_PRIMARY",
                algorithm_version=(
                    "strategy-v2-portfolio-fixed-primary-v1"
                ),
                evaluator_digest="0" * 64,
                registered_at=_REGISTERED_AT - timedelta(days=1),
                eligible_after=_REGISTERED_AT - timedelta(days=1),
            )
            legacy_rotation = StrategyV2PortfolioRegistration(
                baseline_symbol="NVDA.US",
                policy="PIT_SHRINK_WEIGHTED_ZSCORE_POOL",
                algorithm_version=(
                    "strategy-v2-portfolio-rotation-pit-shrinkage-"
                    "weighted-zscore-observed-cost-75bps-v1"
                ),
                evaluator_digest="1" * 64,
                registered_at=_REGISTERED_AT - timedelta(days=1),
                eligible_after=_REGISTERED_AT - timedelta(days=1),
            )
            db.add_all((legacy, legacy_rotation))
            db.commit()
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._signal(db, "NVDA.US", _FIRST_SIGNAL)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )
            report = service.get_report("NVDA.US")

            assert len(report.variants) == 19
            assert sum(
                row.algorithm_version.endswith("-v2")
                for row in report.variants
            ) == 13
            assert any(
                row.algorithm_version.endswith("-v1")
                and row.policy
                == "RISK_GROUP_REL_OBS_75BPS_POOL"
                for row in report.variants
            )
            assert any(
                row.algorithm_version.endswith("-v2")
                and row.policy == "PIT_SHRINK_WEIGHTED_ZSCORE_POOL"
                for row in report.variants
            )
            assert (
                db.query(StrategyV2PortfolioObservation)
                .filter(
                    StrategyV2PortfolioObservation.registration_id
                    == legacy.id
                )
                .count()
                == 0
            )
            assert (
                db.query(StrategyV2PortfolioObservation)
                .filter(
                    StrategyV2PortfolioObservation.registration_id
                    == legacy_rotation.id
                )
                .count()
                == 0
            )

    def test_signal_observed_at_fill_open_is_ineligible(self) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            signal = self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL,
            )
            signal.observed_at = _FIRST_SIGNAL + timedelta(minutes=2)
            db.add(signal)
            db.commit()

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
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
            assert observation.status == "NO_ELIGIBLE"
            assert observation.candidate_count == 0
            assert observation.selected_symbol == ""
            assert observation.reason == "NO_CAUSAL_SIGNALS"
            assert observation.candidates_json == "[]"

    def test_stale_peer_does_not_move_causal_routing_timestamp(self) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            timely = self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL,
            )
            stale = self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
            )
            stale.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=2,
                seconds=5,
            )
            db.add(stale)
            db.commit()

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
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
            assert observation.status == "PENDING_ENTRY"
            assert observation.selected_symbol == "NVDA.US"
            assert observation.candidate_count == 1
            assert observation.observed_at == timely.observed_at

    def test_source_fill_is_bound_only_after_decision_is_observed(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            signal = self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL,
            )
            self._fill(db, signal, price=100)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
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
            assert observation.status == "PENDING_ENTRY"

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=4)
            )
            db.refresh(observation)
            assert observation.status == "OPEN"

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
                now=_FIRST_SIGNAL + timedelta(minutes=4)
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
                "VWAP_EDGE_75BPS_POOL": "",
                "VWAP_EDGE_OBSERVED_COST_POOL": "",
                "VWAP_EDGE_OBS_COST_75BPS_POOL": "",
                "RISK_GROUP_REL_OBS_75BPS_POOL": "",
                "RISK_GROUP_LOO_OBS_75BPS_POOL": "",
                "SECTOR_LOO_OBS_75BPS_POOL": "",
                "SELECTED_SECTOR_LOO_OBS_75BPS_POOL": "",
                "SELECTED_ZSCORE_OBS_75BPS_POOL": "",
                "ROTATION_ZSCORE_OBS_75BPS_POOL": "",
                "ROTATION_IV_WEIGHTED_ZSCORE_POOL": "",
                "ROTATION_IV_NET_EDGE_ZSCORE_POOL": "",
                "PIT_SHRINK_WEIGHTED_ZSCORE_POOL": "",
                "PIT_SHRINK_NET_EDGE_ZSCORE_POOL": "",
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
            assert len(overlap_rows) == 19
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
                "VWAP_EDGE_75BPS_POOL": "NO_ELIGIBLE",
                "VWAP_EDGE_OBSERVED_COST_POOL": "NO_ELIGIBLE",
                "VWAP_EDGE_OBS_COST_75BPS_POOL": "NO_ELIGIBLE",
                "RISK_GROUP_REL_OBS_75BPS_POOL": "NO_ELIGIBLE",
                "RISK_GROUP_LOO_OBS_75BPS_POOL": "NO_ELIGIBLE",
                "SECTOR_LOO_OBS_75BPS_POOL": "NO_ELIGIBLE",
                "SELECTED_SECTOR_LOO_OBS_75BPS_POOL": (
                    "NO_ELIGIBLE"
                ),
                "SELECTED_ZSCORE_OBS_75BPS_POOL": "NO_ELIGIBLE",
                "ROTATION_ZSCORE_OBS_75BPS_POOL": "NO_ELIGIBLE",
                "ROTATION_IV_WEIGHTED_ZSCORE_POOL": "NO_ELIGIBLE",
                "ROTATION_IV_NET_EDGE_ZSCORE_POOL": "NO_ELIGIBLE",
                "PIT_SHRINK_WEIGHTED_ZSCORE_POOL": (
                    "NO_ELIGIBLE"
                ),
                "PIT_SHRINK_NET_EDGE_ZSCORE_POOL": (
                    "NO_ELIGIBLE"
                ),
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
                now=_FIRST_SIGNAL + timedelta(minutes=4)
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
            fixed_75bps_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["VWAP_EDGE_75BPS_POOL"].id
            ).one()
            assert selected_observation.selected_symbol == "AAPL.US"
            assert pool_observation.selected_symbol == "TER.US"
            assert fixed_75bps_observation.selected_symbol == "TER.US"
            assert selected_observation.status == "OPEN"
            assert pool_observation.status == "OPEN"
            assert fixed_75bps_observation.status == "OPEN"
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
            assert (
                report_by_policy[
                    "VWAP_EDGE_75BPS_POOL"
                ].edge_filter
                == "COST_TO_75BPS_VWAP_DISCOUNT"
            )

    def test_fixed_75bps_vwap_pool_is_independent_of_exit_stop(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            for symbol in ("AAPL.US", "MSFT.US"):
                self._version(
                    db,
                    symbol,
                    stop_loss_pct=0.45,
                )
            self._quant(
                db,
                "AAPL.US",
                "AVOID",
                39,
                estimated_cost_bps=24,
            )
            aapl_signal = self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100.1,
            )
            self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.2,
                vwap_1m=100,
                vwap_5m=100.1,
            )
            self._fill(db, aapl_signal, price=99.42)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=4)
            )

            registrations = {
                row.policy: row
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            stop_band = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["VWAP_EDGE_POOL"].id
            ).one()
            fixed_band = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["VWAP_EDGE_75BPS_POOL"].id
            ).one()
            observed_fixed_band = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "VWAP_EDGE_OBS_COST_75BPS_POOL"
                ].id
            ).one()

            assert stop_band.status == "NO_ELIGIBLE"
            assert fixed_band.status == "OPEN"
            assert fixed_band.selected_symbol == "AAPL.US"
            assert observed_fixed_band.status == "OPEN"
            assert observed_fixed_band.selected_symbol == "AAPL.US"
            candidates = json.loads(fixed_band.candidates_json)
            assert len(candidates) == 1
            assert candidates[0]["stop_distance_bps"] == 45
            assert candidates[0]["residual_1m_bps"] == pytest.approx(
                -60
            )
            report = service.get_report("NVDA.US")
            challenger = next(
                row
                for row in report.variants
                if row.policy == "VWAP_EDGE_75BPS_POOL"
            )
            assert (
                challenger.edge_filter
                == "COST_TO_75BPS_VWAP_DISCOUNT"
            )
            observed_challenger = next(
                row
                for row in report.variants
                if row.policy
                == "VWAP_EDGE_OBS_COST_75BPS_POOL"
            )
            assert (
                observed_challenger.edge_filter
                == "OBSERVED_COST_TO_75BPS_VWAP_DISCOUNT"
            )
            observed_candidates = json.loads(
                observed_fixed_band.candidates_json
            )
            assert (
                observed_candidates[0][
                    "observed_round_trip_cost_bps"
                ]
                == 24
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
                    + timedelta(minutes=1, seconds=6)
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
                "VWAP_EDGE_75BPS_POOL",
                "VWAP_EDGE_OBSERVED_COST_POOL",
                "VWAP_EDGE_OBS_COST_75BPS_POOL",
                "RISK_GROUP_REL_OBS_75BPS_POOL",
                "RISK_GROUP_LOO_OBS_75BPS_POOL",
                "SECTOR_LOO_OBS_75BPS_POOL",
                "SELECTED_SECTOR_LOO_OBS_75BPS_POOL",
                "SELECTED_ZSCORE_OBS_75BPS_POOL",
                "ROTATION_ZSCORE_OBS_75BPS_POOL",
                "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
                "ROTATION_IV_NET_EDGE_ZSCORE_POOL",
                "PIT_SHRINK_WEIGHTED_ZSCORE_POOL",
                "PIT_SHRINK_NET_EDGE_ZSCORE_POOL",
            ):
                observation = db.query(
                    StrategyV2PortfolioObservation
                ).filter(
                    StrategyV2PortfolioObservation.registration_id
                    == registrations[policy].id
                ).one()
                assert observation.status == "NO_ELIGIBLE"

    def test_observed_cost_vwap_pool_uses_only_causal_fresh_costs(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            for symbol in (
                "AAPL.US",
                "MSFT.US",
                "TER.US",
                "NVDA.US",
                "AMZN.US",
            ):
                self._version(db, symbol)
            self._quant(
                db,
                "AAPL.US",
                "AVOID",
                39,
                estimated_cost_bps=24,
            )
            self._quant(
                db,
                "MSFT.US",
                "AVOID",
                39,
                created_at=(
                    _FIRST_SIGNAL
                    + timedelta(minutes=1, seconds=6)
                ),
                estimated_cost_bps=15,
            )
            self._quant(
                db,
                "TER.US",
                "AVOID",
                39,
                created_at=_FIRST_SIGNAL - timedelta(hours=2),
                expires_at=_FIRST_SIGNAL - timedelta(seconds=1),
                estimated_cost_bps=15,
            )
            self._quant(
                db,
                "NVDA.US",
                "AVOID",
                39,
            )
            self._quant(
                db,
                "AMZN.US",
                "AVOID",
                39,
                created_at=_FIRST_SIGNAL - timedelta(hours=2),
                expires_at=_FIRST_SIGNAL + timedelta(hours=2),
                estimated_cost_bps=15,
            )
            signal_inputs = {
                "AAPL.US": (99.7, 100.0, 100.1),
                "MSFT.US": (99.6, 100.0, 100.1),
                "TER.US": (99.65, 100.0, 100.1),
                "NVDA.US": (99.68, 100.0, 100.1),
                "AMZN.US": (99.62, 100.0, 100.1),
            }
            for symbol, values in signal_inputs.items():
                self._signal(
                    db,
                    symbol,
                    _FIRST_SIGNAL,
                    close_price=values[0],
                    vwap_1m=values[1],
                    vwap_5m=values[2],
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
            fixed_pool = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["VWAP_EDGE_POOL"].id
            ).one()
            observed_pool = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "VWAP_EDGE_OBSERVED_COST_POOL"
                ].id
            ).one()
            observed_fixed_pool = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "VWAP_EDGE_OBS_COST_75BPS_POOL"
                ].id
            ).one()

            assert fixed_pool.selected_symbol == "MSFT.US"
            assert observed_pool.selected_symbol == "AAPL.US"
            assert observed_fixed_pool.selected_symbol == "AAPL.US"
            fixed_candidates = json.loads(
                fixed_pool.candidates_json
            )
            assert "observed_round_trip_cost_bps" not in (
                fixed_candidates[0]
            )
            candidates = json.loads(observed_pool.candidates_json)
            assert len(candidates) == 1
            assert (
                candidates[0]["observed_round_trip_cost_bps"]
                == 24
            )
            report = service.get_report("NVDA.US")
            observed_variant = next(
                row
                for row in report.variants
                if row.policy
                == "VWAP_EDGE_OBSERVED_COST_POOL"
            )
            assert (
                observed_variant.edge_filter
                == "OBSERVED_COST_TO_STOP_VWAP_DISCOUNT"
            )
            fixed_band_variant = next(
                row
                for row in report.variants
                if row.policy
                == "VWAP_EDGE_OBS_COST_75BPS_POOL"
            )
            assert (
                fixed_band_variant.edge_filter
                == "OBSERVED_COST_TO_75BPS_VWAP_DISCOUNT"
            )

    def test_selected_zscore_route_uses_frozen_standardized_edge(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            for symbol in ("AAPL.US", "MSFT.US"):
                self._version(db, symbol)
                self._quant(
                    db,
                    symbol,
                    "WATCH",
                    49,
                    estimated_cost_bps=20,
                )
            self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.55,
                vwap_1m=100,
                vwap_5m=100.05,
                zscore_1m=-2.4,
                zscore_5m=-1.6,
            )
            self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100.05,
                zscore_1m=-1.8,
                zscore_5m=-1.2,
            )

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "SELECTED_ZSCORE_OBS_75BPS_POOL"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "PENDING_ENTRY"
            assert observation.selected_symbol == "AAPL.US"
            candidates = json.loads(observation.candidates_json)
            assert [row["symbol"] for row in candidates] == [
                "AAPL.US",
                "MSFT.US",
            ]
            assert candidates[0]["zscore_1m"] == -2.4
            assert candidates[0]["zscore_5m"] == -1.6
            assert candidates[0]["observed_round_trip_cost_bps"] == 20
            assert "rotation_selected" not in candidates[0]

            rotation_registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "ROTATION_ZSCORE_OBS_75BPS_POOL"
            ).one()
            rotation_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == rotation_registration.id
            ).one()
            assert rotation_observation.status == "PENDING_ENTRY"
            assert rotation_observation.selected_symbol == "MSFT.US"
            rotation_candidates = json.loads(
                rotation_observation.candidates_json
            )
            assert [row["symbol"] for row in rotation_candidates] == [
                "MSFT.US"
            ]
            assert rotation_candidates[0]["rotation_selected"] is True
            assert rotation_candidates[0]["rotation_rank"] == 2
            assert rotation_candidates[0]["rotation_score"] == 90

            variant = next(
                row
                for row in service.get_report("NVDA.US").variants
                if row.policy == "SELECTED_ZSCORE_OBS_75BPS_POOL"
            )
            assert variant.edge_filter == "ZSCORE_OBS_COST_TO_75BPS"
            rotation_variant = next(
                row
                for row in service.get_report("NVDA.US").variants
                if row.policy == "ROTATION_ZSCORE_OBS_75BPS_POOL"
            )
            assert (
                rotation_variant.edge_filter
                == "ZSCORE_OBS_COST_TO_75BPS"
            )
            for policy in (
                "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
                "ROTATION_IV_NET_EDGE_ZSCORE_POOL",
            ):
                registration = db.query(
                    StrategyV2PortfolioRegistration
                ).filter(
                    StrategyV2PortfolioRegistration.policy == policy
                ).one()
                observation = db.query(
                    StrategyV2PortfolioObservation
                ).filter(
                    StrategyV2PortfolioObservation.registration_id
                    == registration.id
                ).one()
                assert observation.status == "NO_ELIGIBLE"
                diagnostic_candidates = json.loads(
                    observation.candidates_json
                )
                assert [
                    row["symbol"] for row in diagnostic_candidates
                ] == ["AAPL.US", "MSFT.US"]
                assert all(
                    "MISSING_ROTATION_TARGET_WEIGHT"
                    in row["rejection_reasons"]
                    for row in diagnostic_candidates
                )
                metrics = next(
                    row.metrics
                    for row in service.get_report("NVDA.US").variants
                    if row.policy == policy
                )
                assert metrics.diagnosed_no_eligible == 1
                assert metrics.no_causal_signal_groups == 0
                assert (
                    metrics.rejection_counts[
                        "MISSING_ROTATION_TARGET_WEIGHT"
                    ]
                    == 2
                )

    def test_validated_inverse_volatility_routes_compare_priority(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._validated_inverse_volatility_registration(db)
            for symbol in ("IBM.US", "MSFT.US"):
                self._version(db, symbol)
                self._quant(
                    db,
                    symbol,
                    "WATCH",
                    49,
                    estimated_cost_bps=20,
                )
            self._signal(
                db,
                "IBM.US",
                _FIRST_SIGNAL,
                close_price=99.7,
                vwap_1m=100,
                vwap_5m=100,
                zscore_1m=-2.5,
                zscore_5m=-2.0,
            )
            self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.5,
                vwap_1m=100,
                vwap_5m=100,
                zscore_1m=-1.5,
                zscore_5m=-1.2,
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
            weighted = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "ROTATION_IV_WEIGHTED_ZSCORE_POOL"
                ].id
            ).one()
            unweighted = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "ROTATION_ZSCORE_OBS_75BPS_POOL"
                ].id
            ).one()
            net_edge = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "ROTATION_IV_NET_EDGE_ZSCORE_POOL"
                ].id
            ).one()

            assert weighted.status == "PENDING_ENTRY"
            assert weighted.selected_symbol == "IBM.US"
            assert unweighted.selected_symbol == "IBM.US"
            assert net_edge.selected_symbol == "MSFT.US"
            candidates = json.loads(weighted.candidates_json)
            assert [row["symbol"] for row in candidates] == [
                "IBM.US",
                "MSFT.US",
            ]
            assert candidates[0]["rotation_target_weight_pct"] == 25
            assert candidates[1]["rotation_target_weight_pct"] == 10
            net_edge_candidates = json.loads(net_edge.candidates_json)
            assert [row["symbol"] for row in net_edge_candidates] == [
                "MSFT.US",
                "IBM.US",
            ]
            assert all(
                row.edge_filter == "ZSCORE_OBS_COST_TO_75BPS"
                for row in service.get_report("NVDA.US").variants
                if row.policy in {
                    "ROTATION_IV_WEIGHTED_ZSCORE_POOL",
                    "ROTATION_IV_NET_EDGE_ZSCORE_POOL",
                }
            )

    def test_inverse_volatility_targets_fail_closed_on_invalid_context(
        self,
    ) -> None:
        with self._db() as db:
            self._universe(db)
            self._validated_inverse_volatility_registration(db)
            run = db.query(UniverseSelectionRun).one()
            valid = (
                StrategyV2PortfolioService
                ._validated_inverse_volatility_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
            )
            assert set(valid) == {
                "IBM.US",
                "MSFT.US",
                "CAT.US",
                "GS.US",
                "AEP.US",
            }
            assert (
                StrategyV2PortfolioService
                ._validated_inverse_volatility_targets(
                    run,
                    session_date=date(2026, 8, 3),
                )
                == {}
            )

            parameters = json.loads(run.parameters_json)
            registration = parameters[
                "rotation_weighting_challenger_registration"
            ]
            del registration["target_signals"][0][
                "target_weight_pct"
            ]
            run.parameters_json = json.dumps(parameters)
            assert (
                StrategyV2PortfolioService
                ._validated_inverse_volatility_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
                == {}
            )

            self._validated_inverse_volatility_registration(db)
            db.refresh(run)
            parameters = json.loads(run.parameters_json)
            parameters["rotation_evaluation"]["variants"][0][
                "expanding_validation_passed"
            ] = False
            run.parameters_json = json.dumps(parameters)
            assert (
                StrategyV2PortfolioService
                ._validated_inverse_volatility_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
                == {}
            )

            self._validated_inverse_volatility_registration(db)
            db.refresh(run)
            run.status = "DEGRADED"
            assert (
                StrategyV2PortfolioService
                ._validated_inverse_volatility_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
                == {}
            )

    def test_point_in_time_shrinkage_routes_compare_priority(self) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._validated_point_in_time_shrinkage_registration(db)
            for symbol in ("IBM.US", "MSFT.US"):
                self._version(db, symbol)
                self._quant(
                    db,
                    symbol,
                    "WATCH",
                    49,
                    estimated_cost_bps=20,
                )
            self._signal(
                db,
                "IBM.US",
                _FIRST_SIGNAL,
                close_price=99.7,
                vwap_1m=100,
                vwap_5m=100,
                zscore_1m=-2.5,
                zscore_5m=-2.0,
            )
            self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.5,
                vwap_1m=100,
                vwap_5m=100,
                zscore_1m=-1.5,
                zscore_5m=-1.2,
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
            weighted = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "PIT_SHRINK_WEIGHTED_ZSCORE_POOL"
                ].id
            ).one()
            net_edge = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "PIT_SHRINK_NET_EDGE_ZSCORE_POOL"
                ].id
            ).one()

            assert weighted.status == "PENDING_ENTRY"
            assert weighted.selected_symbol == "IBM.US"
            assert net_edge.selected_symbol == "MSFT.US"
            candidates = json.loads(weighted.candidates_json)
            assert [row["symbol"] for row in candidates] == [
                "IBM.US",
                "MSFT.US",
            ]
            assert candidates[0]["rotation_target_weight_pct"] == 15
            assert candidates[1]["rotation_target_weight_pct"] == 10

    def test_point_in_time_shrinkage_targets_fail_closed(self) -> None:
        with self._db() as db:
            self._universe(db)
            self._validated_point_in_time_shrinkage_registration(db)
            run = db.query(UniverseSelectionRun).one()
            assert set(
                StrategyV2PortfolioService
                ._validated_point_in_time_shrinkage_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
            ) == {
                "IBM.US",
                "MSFT.US",
                "CAT.US",
                "GS.US",
                "AEP.US",
                "ROST.US",
                "MRK.US",
                "GOOGL.US",
            }

            parameters = json.loads(run.parameters_json)
            parameters["rotation_point_in_time_sensitivity"][
                "membership_history"
            ]["source_version"] = ""
            run.parameters_json = json.dumps(parameters)
            assert (
                StrategyV2PortfolioService
                ._validated_point_in_time_shrinkage_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
                == {}
            )

            self._validated_point_in_time_shrinkage_registration(db)
            db.refresh(run)
            parameters = json.loads(run.parameters_json)
            parameters["rotation_point_in_time_sensitivity"][
                "evaluation"
            ]["variants"][0]["expanding_validation_passed"] = False
            run.parameters_json = json.dumps(parameters)
            assert (
                StrategyV2PortfolioService
                ._validated_point_in_time_shrinkage_targets(
                    run,
                    session_date=_FIRST_SIGNAL.date(),
                )
                == {}
            )

    def test_risk_group_routes_use_causal_inclusive_and_leave_one_out_medians(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            for symbol in ("AAPL.US", "MSFT.US", "NVDA.US"):
                self._version(db, symbol)
                self._quant(
                    db,
                    symbol,
                    "WATCH",
                    49,
                    estimated_cost_bps=24,
                )
            aapl = self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100.1,
            )
            msft = self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.8,
                vwap_1m=100,
                vwap_5m=100,
            )
            nvda = self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL,
                close_price=99.9,
                vwap_1m=100,
                vwap_5m=100,
            )
            msft.action = "WAIT"
            msft.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=4,
            )
            nvda.action = "WAIT"
            nvda.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=3,
            )
            db.add_all([msft, nvda])
            db.commit()
            self._fill(db, aapl, price=99.42)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=4)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "RISK_GROUP_REL_OBS_75BPS_POOL"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "OPEN"
            assert observation.selected_symbol == "AAPL.US"
            candidates = json.loads(observation.candidates_json)
            assert len(candidates) == 1
            assert candidates[0]["risk_group"] == (
                "Information Technology"
            )
            assert candidates[0]["risk_group_peer_count"] == 3
            assert candidates[0][
                "risk_group_relative_1m_bps"
            ] == pytest.approx(-40, abs=0.1)
            assert candidates[0][
                "risk_group_relative_5m_bps"
            ] == pytest.approx(-50, abs=0.1)
            leave_one_out_registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "RISK_GROUP_LOO_OBS_75BPS_POOL"
            ).one()
            leave_one_out_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == leave_one_out_registration.id
            ).one()
            assert leave_one_out_observation.status == "OPEN"
            assert leave_one_out_observation.selected_symbol == "AAPL.US"
            leave_one_out_candidates = json.loads(
                leave_one_out_observation.candidates_json
            )
            assert len(leave_one_out_candidates) == 1
            assert (
                leave_one_out_candidates[0]["risk_group_peer_count"]
                == 2
            )
            assert leave_one_out_candidates[0][
                "risk_group_relative_1m_bps"
            ] == pytest.approx(-45, abs=0.1)
            assert leave_one_out_candidates[0][
                "risk_group_relative_5m_bps"
            ] == pytest.approx(-55, abs=0.1)
            report = service.get_report("NVDA.US")
            challenger = next(
                row
                for row in report.variants
                if row.policy
                == "RISK_GROUP_REL_OBS_75BPS_POOL"
            )
            assert (
                challenger.edge_filter
                == "RISK_GROUP_REL_OBS_COST_TO_75BPS"
            )
            leave_one_out_challenger = next(
                row
                for row in report.variants
                if row.policy
                == "RISK_GROUP_LOO_OBS_75BPS_POOL"
            )
            assert (
                leave_one_out_challenger.edge_filter
                == "RISK_GROUP_LOO_OBS_COST_TO_75BPS"
            )

    def test_sector_leave_one_out_uses_exact_sector_peers(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._version(db, "AAPL.US")
            self._quant(
                db,
                "AAPL.US",
                "WATCH",
                49,
                estimated_cost_bps=24,
            )
            aapl = self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100,
            )
            ibm = self._signal(
                db,
                "IBM.US",
                _FIRST_SIGNAL,
                close_price=99.8,
                vwap_1m=100,
                vwap_5m=100,
            )
            csco = self._signal(
                db,
                "CSCO.US",
                _FIRST_SIGNAL,
                close_price=99.9,
                vwap_1m=100,
                vwap_5m=100,
            )
            ibm.action = "WAIT"
            ibm.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=4,
            )
            csco.action = "WAIT"
            csco.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=3,
            )
            db.add_all([ibm, csco])
            db.commit()
            self._fill(db, aapl, price=99.42)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=4)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "SECTOR_LOO_OBS_75BPS_POOL"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "OPEN"
            assert observation.selected_symbol == "AAPL.US"
            candidates = json.loads(observation.candidates_json)
            assert len(candidates) == 1
            assert candidates[0]["risk_group"] == (
                "Technology Hardware"
            )
            assert candidates[0]["risk_group_peer_count"] == 2
            assert candidates[0][
                "risk_group_relative_1m_bps"
            ] == pytest.approx(-45, abs=0.1)
            assert candidates[0][
                "risk_group_relative_5m_bps"
            ] == pytest.approx(-45, abs=0.1)
            report = service.get_report("NVDA.US")
            challenger = next(
                row
                for row in report.variants
                if row.policy == "SECTOR_LOO_OBS_75BPS_POOL"
            )
            assert (
                challenger.edge_filter
                == "SECTOR_LOO_OBS_COST_TO_75BPS"
            )
            selected_registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "SELECTED_SECTOR_LOO_OBS_75BPS_POOL"
            ).one()
            selected_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == selected_registration.id
            ).one()
            assert selected_observation.status == "OPEN"
            assert selected_observation.selected_symbol == "AAPL.US"

    def test_selected_sector_route_excludes_non_selected_signal(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._version(db, "IBM.US")
            self._quant(
                db,
                "IBM.US",
                "WATCH",
                49,
                estimated_cost_bps=24,
            )
            ibm = self._signal(
                db,
                "IBM.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100,
            )
            aapl = self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.9,
                vwap_1m=100,
                vwap_5m=100,
            )
            csco = self._signal(
                db,
                "CSCO.US",
                _FIRST_SIGNAL,
                close_price=99.8,
                vwap_1m=100,
                vwap_5m=100,
            )
            aapl.action = "WAIT"
            aapl.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=4,
            )
            csco.action = "WAIT"
            csco.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=3,
            )
            db.add_all([aapl, csco])
            db.commit()
            self._fill(db, ibm, price=99.42)

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=4)
            )

            registrations = {
                row.policy: row
                for row in db.query(
                    StrategyV2PortfolioRegistration
                ).all()
            }
            unrestricted = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations["SECTOR_LOO_OBS_75BPS_POOL"].id
            ).one()
            selected_only = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registrations[
                    "SELECTED_SECTOR_LOO_OBS_75BPS_POOL"
                ].id
            ).one()

            assert unrestricted.status == "OPEN"
            assert unrestricted.selected_symbol == "IBM.US"
            assert selected_only.status == "NO_ELIGIBLE"
            assert selected_only.selected_symbol == ""

    def test_sector_leave_one_out_excludes_late_exact_peer(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._version(db, "AAPL.US")
            self._quant(
                db,
                "AAPL.US",
                "WATCH",
                49,
                estimated_cost_bps=24,
            )
            self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100,
            )
            timely = self._signal(
                db,
                "IBM.US",
                _FIRST_SIGNAL,
                close_price=99.8,
                vwap_1m=100,
                vwap_5m=100,
            )
            late = self._signal(
                db,
                "CSCO.US",
                _FIRST_SIGNAL,
                close_price=99.9,
                vwap_1m=100,
                vwap_5m=100,
            )
            timely.action = "WAIT"
            timely.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=4,
            )
            late.action = "WAIT"
            late.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=6,
            )
            db.add_all([timely, late])
            db.commit()

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "SECTOR_LOO_OBS_75BPS_POOL"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "NO_ELIGIBLE"
            assert observation.candidate_count == 0

    def test_risk_group_relative_pool_excludes_late_peer(
        self,
    ) -> None:
        with self._db() as db:
            service = StrategyV2PortfolioService(db)
            self._register(service)
            self._universe(db)
            self._version(db, "AAPL.US")
            self._quant(
                db,
                "AAPL.US",
                "WATCH",
                49,
                estimated_cost_bps=24,
            )
            self._signal(
                db,
                "AAPL.US",
                _FIRST_SIGNAL,
                close_price=99.4,
                vwap_1m=100,
                vwap_5m=100.1,
            )
            timely = self._signal(
                db,
                "MSFT.US",
                _FIRST_SIGNAL,
                close_price=99.8,
                vwap_1m=100,
                vwap_5m=100,
            )
            late = self._signal(
                db,
                "NVDA.US",
                _FIRST_SIGNAL,
                close_price=99.9,
                vwap_1m=100,
                vwap_5m=100,
            )
            timely.action = "WAIT"
            timely.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=4,
            )
            late.action = "WAIT"
            late.observed_at = _FIRST_SIGNAL + timedelta(
                minutes=1,
                seconds=6,
            )
            db.add_all([timely, late])
            db.commit()

            service.advance(
                now=_FIRST_SIGNAL + timedelta(minutes=3)
            )

            registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "RISK_GROUP_REL_OBS_75BPS_POOL"
            ).one()
            observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == registration.id
            ).one()
            assert observation.status == "NO_ELIGIBLE"
            assert observation.candidate_count == 0
            leave_one_out_registration = db.query(
                StrategyV2PortfolioRegistration
            ).filter(
                StrategyV2PortfolioRegistration.policy
                == "RISK_GROUP_LOO_OBS_75BPS_POOL"
            ).one()
            leave_one_out_observation = db.query(
                StrategyV2PortfolioObservation
            ).filter(
                StrategyV2PortfolioObservation.registration_id
                == leave_one_out_registration.id
            ).one()
            assert leave_one_out_observation.status == "NO_ELIGIBLE"
            assert leave_one_out_observation.candidate_count == 0

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
                    + timedelta(minutes=1, seconds=6)
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
                    + timedelta(minutes=1, seconds=6)
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
