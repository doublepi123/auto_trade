from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.market_calendar import get_session
from app.models import (
    Base,
    LiveExitChallengerRegistration,
    LiveExitChallengerTrade,
    OpeningMomentumExecution,
    OpeningMomentumShadowRun,
    OrderRecord,
    RuntimeState,
    StrategyConfig,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardRegistration,
    StrategyV2ExitChallengerRegistration,
    StrategyV2ExitChallengerTrade,
    StrategyV2PortfolioObservation,
    StrategyV2PortfolioRegistration,
    StrategyV2ShadowDecision,
    StrategyV2ShadowConfig,
    StrategyV2ShadowTrade,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
    WatchlistItem,
    WatchlistScore,
)
from app.services.research_observation_health_service import (
    ResearchObservationHealthService,
)
from app.services.live_exit_challenger_service import (
    LiveExitChallengerService,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)
from app.services.opening_momentum_execution_service import (
    OpeningMomentumExecutionService,
)
from app.services.strategy_v2_portfolio_service import (
    StrategyV2PortfolioService,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.strategy_v2_exit_challenger_service import (
    STRATEGY_V2_EXIT_ALGORITHM_VERSIONS,
    StrategyV2ExitChallengerService,
)
from app.services.watchlist_quant_service import (
    QUANT_ERROR_SOURCE,
    QUANT_SCORE_SOURCE,
    QUANT_WARMUP_SOURCE,
)


_NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


def test_live_interval_health_warns_after_price_invalidates_entry_band() -> None:
    db = _db()
    try:
        db.add(StrategyConfig(
            symbol="NVDA.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
            stop_loss_pct=1.0,
        ))
        state = RuntimeState(
            symbol="NVDA.US",
            engine_state="flat",
            last_price=99.01,
            updated_at=_NOW,
        )
        db.add(state)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        healthy = service._live_interval_component()
        assert healthy.status == "HEALTHY"
        assert healthy.blockers == []

        state.last_price = 99.0
        state.updated_at = _NOW
        db.commit()
        warning = service._live_interval_component()
        assert warning.status == "WARNING"
        assert warning.blockers == [
            "CURRENT_PRICE_BELOW_LONG_ENTRY_FLOOR"
        ]

        state.last_price = 105.0
        state.updated_at = _NOW - timedelta(minutes=16)
        db.commit()
        stale = service._live_interval_component()
        assert stale.status == "DEGRADED"
        assert stale.blockers == ["CURRENT_PRICE_STALE"]

        state.updated_at = _NOW + timedelta(seconds=1)
        db.commit()
        future = service._live_interval_component()
        assert future.status == "DEGRADED"
        assert future.blockers == ["CURRENT_PRICE_TIMESTAMP_IN_FUTURE"]
    finally:
        db.close()


def test_live_interval_pre_market_rejects_early_latest_session_quote() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(StrategyConfig(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
            stop_loss_pct=1.0,
        ))
        db.add(RuntimeState(
            symbol="AAPL.US",
            engine_state="flat",
            last_price=105.0,
            updated_at=datetime(
                2026, 7, 31, 14, 0, tzinfo=timezone.utc
            ),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._live_interval_component()

        assert component.status == "DEGRADED"
        assert component.latest_session_date == date(2026, 7, 31)
        assert component.expected_session_date == date(2026, 7, 31)
        assert component.blockers == ["CURRENT_PRICE_STALE"]
    finally:
        db.close()


def test_growth_satellite_health_requires_four_distinct_low_cost_candidates() -> None:
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=date(2026, 7, 29),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            candidate_count=8,
            evaluable_count=8,
            selected_count=8,
            coverage_ratio=1.0,
            completed_at=_NOW - timedelta(minutes=30),
        )
        db.add(run)
        db.flush()
        candidates = (
            ("MSFT.US", "Software"),
            ("AAPL.US", "Technology Hardware"),
            ("NFLX.US", "Communication Services"),
            ("GOOGL.US", "Communication Services"),
            ("V.US", "Financials"),
            ("GS.US", "Financials"),
            ("WMT.US", "Consumer Staples"),
            ("COST.US", "Consumer Staples"),
        )
        for rank, (symbol, sector) in enumerate(candidates, start=1):
            db.add(UniverseSelectionCandidate(
                run_id=run.id,
                symbol=symbol,
                market="US",
                alias=symbol,
                sector=sector,
                memberships_json='["NASDAQ_100"]',
                selected=True,
                rank=rank,
                score=100 - rank,
                metrics_json="{}",
                exclusion_reasons_json="[]",
            ))
            db.add(StrategyV2ShadowConfig(symbol=symbol, enabled=True))
            db.add(WatchlistScore(
                symbol=symbol,
                market="US",
                source=QUANT_SCORE_SOURCE,
                score=60,
                confidence=0.8,
                recommended_action="WATCH",
                estimated_round_trip_cost_bps=15.0,
                created_at=_NOW - timedelta(minutes=5),
                expires_at=_NOW + timedelta(hours=1),
            ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._growth_satellite_component()

        assert component.status == "HEALTHY"
        assert component.observed_count == 4
        assert component.expected_count == 4
        assert component.coverage_ratio == 1.0
        assert component.blockers == []
    finally:
        db.close()


def test_portfolio_health_distinguishes_idle_from_unprocessed_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        config = StrategyV2ShadowConfig(
            symbol="NVDA.US",
            enabled=True,
        )
        peer_config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
        )
        db.add_all((config, peer_config))
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="NVDA.US",
            now=_NOW - timedelta(days=1),
        )
        config_version = StrategyV2ShadowService(db)._config_version(config)
        peer_config_version = StrategyV2ShadowService(db)._config_version(
            peer_config
        )
        db.add(StrategyV2ShadowDecision(
            idempotency_key="wait-heartbeat",
            symbol="NVDA.US",
            config_version=config_version,
            session_date=_NOW.date(),
            bar_at=_NOW - timedelta(minutes=2),
            observed_at=_NOW - timedelta(minutes=1),
            action="WAIT",
        ))
        db.commit()

        service = ResearchObservationHealthService(
            db,
            now=_NOW + timedelta(hours=1),
        )
        idle = service._portfolio_component()
        assert idle.status == "WARNING"
        assert idle.blockers == ["ROUTING_IDLE_UNOBSERVABLE"]

        entry = StrategyV2ShadowDecision(
            idempotency_key="unprocessed-entry",
            symbol="NVDA.US",
            config_version=config_version,
            session_date=_NOW.date(),
            bar_at=_NOW - timedelta(minutes=25),
            observed_at=_NOW - timedelta(minutes=24),
            action="SUBMIT_ENTRY",
        )
        peer_entry = StrategyV2ShadowDecision(
            idempotency_key="unprocessed-peer-entry",
            symbol="AAPL.US",
            config_version=peer_config_version,
            session_date=_NOW.date(),
            bar_at=entry.bar_at,
            observed_at=entry.observed_at + timedelta(seconds=5),
            action="SUBMIT_ENTRY",
        )
        db.add_all((entry, peer_entry))
        db.commit()

        stale = service._portfolio_component()
        assert stale.status == "DEGRADED"
        assert stale.blockers == ["ENTRY_SIGNAL_UNPROCESSED_20"]

        registrations = db.query(
            StrategyV2PortfolioRegistration
        ).all()
        first_registration = registrations[0]
        db.add(StrategyV2PortfolioObservation(
            registration_id=first_registration.id,
            signal_at=entry.bar_at,
            observed_at=entry.observed_at + timedelta(seconds=10),
            status="NO_ELIGIBLE",
            reason="TEST_PARTIAL",
        ))
        db.commit()
        partial = service._portfolio_component()
        assert partial.status == "DEGRADED"
        assert partial.blockers == ["ENTRY_SIGNAL_UNPROCESSED_19"]

        for registration in registrations[1:]:
            db.add(StrategyV2PortfolioObservation(
                registration_id=registration.id,
                signal_at=entry.bar_at,
                observed_at=entry.observed_at + timedelta(seconds=10),
                status="NO_ELIGIBLE",
                reason="TEST_PROCESSED",
            ))
        db.commit()

        processed = service._portfolio_component()
        assert processed.status == "HEALTHY"
        assert processed.observed_count == 20
        assert processed.blockers == []
    finally:
        db.close()


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_valid_forward_registration(
    db: Session,
    config: StrategyV2ShadowConfig,
    *,
    registered_at: datetime,
) -> StrategyV2ForwardRegistration:
    shadow = StrategyV2ShadowService(db)
    version = shadow._config_version(config)
    shadow._ensure_version_snapshot(config)
    source_params = shadow._version_params(config.symbol, version)
    evaluator_spec = shadow._forward_evaluator_spec()
    registration = StrategyV2ForwardRegistration(
        symbol=config.symbol,
        market="HK" if config.symbol.endswith(".HK") else "US",
        candidate_algorithm_version=str(
            evaluator_spec["candidate_algorithm_version"]
        ),
        source_config_version=version,
        evaluator_digest=shadow._forward_evaluator_digest(),
        candidate_spec_json=json.dumps(
            shadow._forward_candidate_spec(version, source_params),
            sort_keys=True,
            separators=(",", ":"),
        ),
        registered_at=registered_at,
        eligible_after=shadow._forward_eligible_after(
            "HK" if config.symbol.endswith(".HK") else "US",
            registered_at,
        ),
    )
    db.add(registration)
    db.commit()
    return registration


def _add_complete_universe_run(
    db: Session,
    *,
    as_of_date: date,
    completed_at: datetime,
) -> UniverseSelectionRun:
    run = UniverseSelectionRun(
        as_of_date=as_of_date,
        algorithm_version=f"health-test-{as_of_date.isoformat()}",
        source_version="health-test-source",
        status="COMPLETE",
        candidate_count=1,
        evaluable_count=1,
        selected_count=1,
        coverage_ratio=1.0,
        completed_at=completed_at,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _paper_execution_variant(
    db: Session,
    *,
    session_date: date,
) -> tuple[Any, datetime, datetime, datetime, datetime]:
    observer = OpeningMomentumShadowService(db)
    session = get_session("US")
    session_open = datetime.combine(
        session_date,
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    variant = observer.paper_execution_variant(
        session_date=session_date,
        completed_before=session_open,
    )
    assert variant is not None
    signal_at, entry_due_at, entry_deadline_at = (
        OpeningMomentumExecutionService._session_entry_schedule(
            variant,
            session_date=session_date,
        )
    )
    signal_ready_at = session_open + timedelta(
        minutes=variant.decision_config.signal_minutes,
        seconds=5,
    )
    return (
        variant,
        signal_at,
        signal_ready_at,
        entry_due_at,
        entry_deadline_at,
    )


def _configure_enabled_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "universe_selection_enabled", True)
    monkeypatch.setattr(
        settings,
        "watchlist_quant_auto_score_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        False,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_shadow_enabled",
        False,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_enabled",
        False,
    )
    monkeypatch.setattr(
        settings,
        "live_exit_challenger_enabled",
        False,
    )


def test_live_exit_challenger_health_requires_all_current_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
    monkeypatch.setattr(settings, "entry_round_trip_slippage_bps", 4.0)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add(StrategyV2ShadowConfig(symbol="NVDA.US", enabled=True))
        db.commit()
        exit_service = LiveExitChallengerService(db)
        exit_service.ensure_registrations(
            symbol="NVDA.US",
            market="US",
            now=_NOW,
        )
        rows = db.query(LiveExitChallengerRegistration).order_by(
            LiveExitChallengerRegistration.id.asc()
        ).all()
        for row in rows[6:]:
            db.delete(row)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        degraded = service._live_exit_challenger_component()
        assert degraded.status == "DEGRADED"
        assert degraded.observed_count == 6
        assert degraded.expected_count == 11
        assert degraded.blockers == [
            "CURRENT_LIVE_EXIT_REGISTRATION_MISSING_5"
        ]

        exit_service.ensure_registrations(
            symbol="NVDA.US",
            market="US",
            now=_NOW,
        )
        healthy = service._live_exit_challenger_component()
        assert healthy.status == "HEALTHY"
        assert healthy.coverage_ratio == 1.0
        assert healthy.blockers == []
    finally:
        db.close()


def test_strategy_v2_exit_health_requires_every_variant_per_current_config() -> None:
    db = _db()
    try:
        configs = [
            StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True),
            StrategyV2ShadowConfig(symbol="MSFT.US", enabled=True),
        ]
        db.add_all(configs)
        db.commit()
        shadow_service = StrategyV2ShadowService(db)
        exit_service = StrategyV2ExitChallengerService(db)
        for config in configs:
            exit_service.ensure_registrations(
                symbol=config.symbol,
                market="US",
                source_config_version=shadow_service._config_version(config),
                slippage_bps=config.slippage_bps,
                now=_NOW,
            )

        service = ResearchObservationHealthService(db, now=_NOW)
        healthy = service._strategy_v2_exit_challenger_component()
        expected = 2 * len(STRATEGY_V2_EXIT_ALGORITHM_VERSIONS)
        assert healthy.status == "HEALTHY"
        assert healthy.observed_count == expected
        assert healthy.expected_count == expected
        assert healthy.coverage_ratio == 1.0

        tampered = db.query(
            StrategyV2ExitChallengerRegistration
        ).order_by(
            StrategyV2ExitChallengerRegistration.id.asc()
        ).first()
        assert tampered is not None
        original_digest = tampered.evaluator_digest
        tampered.evaluator_digest = "wrong-digest"
        db.commit()

        wrong_digest = service._strategy_v2_exit_challenger_component()
        assert wrong_digest.status == "DEGRADED"
        assert wrong_digest.observed_count == expected - 1
        assert wrong_digest.blockers == [
            "CURRENT_STRATEGY_V2_EXIT_REGISTRATION_INVALID_1"
        ]

        tampered.evaluator_digest = original_digest
        db.commit()

        missing = db.query(StrategyV2ExitChallengerRegistration).filter(
            StrategyV2ExitChallengerRegistration.symbol == "MSFT.US",
            StrategyV2ExitChallengerRegistration.algorithm_version
            == "strategy-v2-time-stop-m10-v2",
        ).one()
        db.delete(missing)
        db.commit()

        degraded = service._strategy_v2_exit_challenger_component()
        assert degraded.status == "DEGRADED"
        assert degraded.observed_count == expected - 1
        assert degraded.blockers == [
            "CURRENT_STRATEGY_V2_EXIT_REGISTRATION_MISSING_1"
        ]
    finally:
        db.close()


def test_health_reports_fresh_universe_and_quant_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    db = _db()
    try:
        db.add(UniverseSelectionRun(
            as_of_date=date(2026, 7, 29),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            candidate_count=2,
            evaluable_count=2,
            selected_count=1,
            coverage_ratio=1.0,
            completed_at=_NOW - timedelta(minutes=30),
        ))
        for symbol in ("AAPL.US", "JPM.US"):
            db.add(WatchlistItem(symbol=symbol, market="US"))
            db.add(WatchlistScore(
                symbol=symbol,
                market="US",
                source=QUANT_SCORE_SOURCE,
                score=70,
                confidence=0.8,
                recommended_action="HOLD",
                created_at=_NOW - timedelta(minutes=5),
                expires_at=_NOW + timedelta(hours=1),
            ))
        db.commit()

        report = ResearchObservationHealthService(
            db,
            now=_NOW,
        ).get_health()

        assert report.status == "HEALTHY"
        by_name = {component.name: component for component in report.components}
        assert by_name["UNIVERSE_SELECTION"].status == "HEALTHY"
        assert by_name[
            "ROTATION_FORWARD_PRECOMMITMENT"
        ].status == "HEALTHY"
        assert by_name["WATCHLIST_QUANT"].status == "HEALTHY"
        assert by_name["WATCHLIST_QUANT"].coverage_ratio == 1.0
        assert by_name["PORTFOLIO_ROUTING"].status == "DISABLED"
        assert report.order_submission_allowed is False
        assert report.automatic_promotion_allowed is False
    finally:
        db.close()


def test_health_fails_closed_when_month_end_precommitment_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    now = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(UniverseSelectionRun(
            as_of_date=date(2026, 7, 31),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            candidate_count=2,
            evaluable_count=2,
            selected_count=1,
            coverage_ratio=1.0,
            completed_at=now - timedelta(hours=12),
            parameters_json=json.dumps({
                "rotation_next_cohort_registration_status": "NOT_DUE",
            }),
        ))
        db.commit()

        report = ResearchObservationHealthService(
            db,
            now=now,
        ).get_health()

        component = next(
            row
            for row in report.components
            if row.name == "ROTATION_FORWARD_PRECOMMITMENT"
        )
        assert component.status == "DEGRADED"
        assert component.expected_session_date == date(2026, 7, 31)
        assert component.expected_count == 5
        assert component.observed_count == 0
        assert any(
            blocker.endswith("_NOT_DUE")
            for blocker in component.blockers
        )
    finally:
        db.close()


def test_health_warns_when_priority_candidate_has_quant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=date(2026, 7, 29),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            candidate_count=1,
            evaluable_count=1,
            selected_count=1,
            coverage_ratio=1.0,
            completed_at=_NOW - timedelta(minutes=30),
        )
        db.add(run)
        db.flush()
        db.add(UniverseSelectionCandidate(
            run_id=run.id,
            symbol="SPCX.US",
            market="US",
            alias="SpaceX",
            sector="Industrials",
            memberships_json='["NASDAQ_100"]',
            selected=True,
            rank=1,
            score=80,
            metrics_json="{}",
            exclusion_reasons_json="[]",
        ))
        db.add(WatchlistItem(symbol="SPCX.US", market="US"))
        db.add(WatchlistItem(symbol="AAPL.US", market="US"))
        db.add(WatchlistScore(
            symbol="SPCX.US",
            market="US",
            source=QUANT_ERROR_SOURCE,
            score=0,
            confidence=0,
            recommended_action="AVOID",
            rationale="blockers=INSUFFICIENT_DAILY_DATA",
            created_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(hours=1),
        ))
        db.add(WatchlistScore(
            symbol="AAPL.US",
            market="US",
            source=QUANT_SCORE_SOURCE,
            score=49,
            confidence=0.8,
            recommended_action="WATCH",
            created_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(hours=1),
        ))
        db.commit()

        report = ResearchObservationHealthService(
            db,
            now=_NOW,
        ).get_health()

        component = next(
            row
            for row in report.components
            if row.name == "WATCHLIST_QUANT"
        )
        assert component.status == "WARNING"
        assert component.observed_count == 1
        assert component.expected_count == 2
        assert component.coverage_ratio == 0.5
        assert component.blockers == [
            "PRIORITY_QUANT_ERROR_SPCX.US",
        ]
    finally:
        db.close()


def test_health_fails_closed_when_enabled_observers_have_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    db = _db()
    try:
        report = ResearchObservationHealthService(
            db,
            now=_NOW,
        ).get_health()

        assert report.status == "DEGRADED"
        assert "UNIVERSE_SELECTION:NO_TERMINAL_RUN" in report.blockers
        quant = next(
            row for row in report.components if row.name == "WATCHLIST_QUANT"
        )
        assert quant.status == "DISABLED"
    finally:
        db.close()


def test_quant_cache_within_ttl_is_not_a_stale_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    monkeypatch.setattr(settings, "watchlist_quant_score_ttl_minutes", 1440)
    db = _db()
    try:
        db.add(WatchlistItem(symbol="AAPL.US", market="US"))
        db.add(WatchlistScore(
            symbol="AAPL.US",
            market="US",
            source=QUANT_SCORE_SOURCE,
            score=70,
            confidence=0.8,
            recommended_action="WATCH",
            created_at=_NOW - timedelta(hours=6),
            expires_at=_NOW + timedelta(hours=18),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._quant_component()

        assert component.status == "HEALTHY"
        assert component.coverage_ratio == 1.0
        assert component.blockers == []
    finally:
        db.close()


def test_portfolio_heartbeat_is_session_aware_outside_rth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    now = datetime(2026, 7, 31, 1, 30, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.add(StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True))
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="AAPL.US",
            now=now - timedelta(days=1),
        )

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._portfolio_component()

        assert component.status == "HEALTHY"
        assert component.latest_session_date == date(2026, 7, 30)
        assert component.expected_session_date == date(2026, 7, 30)
        assert component.expected_count == 0
        assert component.blockers == []
    finally:
        db.close()


def test_strategy_v2_forward_health_tracks_current_replay_mismatches() -> None:
    db = _db()
    try:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            universe_managed=True,
        )
        db.add(config)
        db.commit()
        registration = _add_valid_forward_registration(
            db,
            config,
            registered_at=_NOW - timedelta(minutes=10),
        )

        healthy = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._strategy_v2_forward_component()

        assert healthy.status == "HEALTHY"
        assert healthy.observed_count == 1
        assert healthy.expected_count == 1
        assert healthy.coverage_ratio == 1.0
        assert healthy.expected_session_date is None
        assert healthy.blockers == []

        db.add(StrategyV2ForwardEvidence(
            registration_id=registration.id,
            target_session_date=date(2026, 7, 29),
            target_open_at=datetime(
                2026, 7, 29, 13, 30, tzinfo=timezone.utc
            ),
            evaluated_at=datetime(
                2026, 7, 29, 20, 12, tzinfo=timezone.utc
            ),
            disposition="EXCLUDED",
            exclusion_reason="BASELINE_REPLAY_MISMATCH",
            structural_failure=True,
            baseline_replay_match=False,
        ))
        db.commit()

        degraded = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._strategy_v2_forward_component()

        assert degraded.status == "DEGRADED"
        assert degraded.blockers == ["BASELINE_REPLAY_MISMATCH_1"]
    finally:
        db.close()


def test_strategy_v2_forward_health_requires_due_evidence() -> None:
    db = _db()
    try:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            universe_managed=True,
        )
        db.add(config)
        db.commit()
        _add_valid_forward_registration(
            db,
            config,
            registered_at=datetime(
                2026, 7, 29, 20, 30, tzinfo=timezone.utc
            ),
        )

        now = datetime(2026, 7, 31, 1, 30, tzinfo=timezone.utc)
        component = ResearchObservationHealthService(
            db,
            now=now,
        )._strategy_v2_forward_component()

        assert component.status == "DEGRADED"
        assert component.expected_session_date == date(2026, 7, 30)
        assert component.latest_session_date is None
        assert component.blockers == [
            "FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_1"
        ]
    finally:
        db.close()


def test_quant_health_filters_unrelated_symbols_and_rejects_warmup_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "watchlist_quant_auto_score_enabled", True)
    monkeypatch.setattr(settings, "watchlist_quant_score_ttl_minutes", 60)
    db = _db()
    try:
        db.add(WatchlistItem(symbol="AAPL.US", market="US"))
        for symbol in ("MSFT.US", "NVDA.US"):
            db.add(WatchlistScore(
                symbol=symbol,
                market="US",
                source=QUANT_SCORE_SOURCE,
                score=70,
                confidence=0.8,
                recommended_action="WATCH",
                created_at=_NOW - timedelta(minutes=5),
                expires_at=_NOW + timedelta(minutes=55),
            ))
        db.commit()

        service = ResearchObservationHealthService(db, now=_NOW)
        unrelated = service._quant_component()
        assert unrelated.status == "DEGRADED"
        assert unrelated.observed_count == 0
        assert unrelated.expected_count == 1
        assert unrelated.coverage_ratio == 0.0

        db.add(WatchlistScore(
            symbol="AAPL.US",
            market="US",
            source=QUANT_WARMUP_SOURCE,
            score=0,
            confidence=0,
            recommended_action="AVOID",
            created_at=_NOW - timedelta(minutes=4),
            expires_at=_NOW + timedelta(minutes=56),
        ))
        db.commit()
        warmup = service._quant_component()
        assert warmup.status == "WARNING"
        assert warmup.coverage_ratio == 1.0
        assert "QUANT_WARMUP_1" in warmup.blockers

        db.add(WatchlistScore(
            symbol="AAPL.US",
            market="US",
            source=QUANT_SCORE_SOURCE,
            score=80,
            confidence=0.9,
            recommended_action="WATCH",
            created_at=_NOW - timedelta(minutes=1),
            expires_at=_NOW - timedelta(seconds=1),
        ))
        db.commit()
        expired = service._quant_component()
        assert expired.status == "DEGRADED"
        assert expired.observed_count == 0
        assert expired.coverage_ratio == 0.0
    finally:
        db.close()


def test_live_exit_health_requires_enabled_primary_shadow_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
    monkeypatch.setattr(settings, "entry_round_trip_slippage_bps", 4.0)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add(StrategyV2ShadowConfig(symbol="NVDA.US", enabled=False))
        db.commit()
        LiveExitChallengerService(db).ensure_registrations(
            symbol="NVDA.US",
            market="US",
            now=_NOW,
        )

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._live_exit_challenger_component()

        assert component.status == "DEGRADED"
        assert component.blockers == ["PRIMARY_LIVE_EXIT_FEED_DISABLED"]
    finally:
        db.close()


def test_forward_health_rejects_minimal_or_wrong_frozen_spec() -> None:
    db = _db()
    try:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            universe_managed=True,
        )
        db.add(config)
        db.commit()
        shadow = StrategyV2ShadowService(db)
        version = shadow._config_version(config)
        spec = shadow._forward_evaluator_spec()
        db.add(StrategyV2ForwardRegistration(
            symbol="AAPL.US",
            market="US",
            candidate_algorithm_version=str(
                spec["candidate_algorithm_version"]
            ),
            source_config_version=version,
            evaluator_digest=shadow._forward_evaluator_digest(),
            candidate_spec_json=json.dumps({
                "evaluator_version": spec["evaluator_version"],
            }),
            registered_at=_NOW - timedelta(minutes=10),
            eligible_after=_NOW + timedelta(days=1),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._strategy_v2_forward_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == 0
        assert component.blockers == [
            "CURRENT_EVALUATOR_REGISTRATION_INVALID_1"
        ]
    finally:
        db.close()


def test_universe_health_rejects_future_terminal_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "universe_selection_enabled", True)
    db = _db()
    try:
        db.add(UniverseSelectionRun(
            as_of_date=date(2026, 7, 31),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            candidate_count=1,
            evaluable_count=1,
            selected_count=1,
            coverage_ratio=1.0,
            completed_at=_NOW + timedelta(minutes=1),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._universe_component()

        assert component.status == "DEGRADED"
        assert component.blockers == [
            "TERMINAL_RUN_COMPLETED_AT_IN_FUTURE"
        ]
    finally:
        db.close()


def test_rotation_health_rejects_missing_or_future_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "universe_selection_enabled", True)
    registered = {
        "rotation_next_cohort_registration_status": "REGISTERED",
        "rotation_next_concentration_challenger_registration_status": (
            "REGISTERED"
        ),
        "rotation_next_weighting_challenger_registration_status": (
            "REGISTERED"
        ),
        "rotation_next_shrinkage_challenger_registration_status": (
            "REGISTERED"
        ),
        "rotation_next_return_to_variance_challenger_registration_status": (
            "REGISTERED"
        ),
    }
    db = _db()
    try:
        db.add(UniverseSelectionRun(
            as_of_date=date(2026, 5, 29),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            completed_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        ))
        due = UniverseSelectionRun(
            as_of_date=date(2026, 6, 30),
            algorithm_version="test-v1",
            source_version="test-source-v1",
            status="COMPLETE",
            completed_at=None,
            parameters_json=json.dumps(registered),
        )
        db.add(due)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        missing_completion = service._rotation_precommitment_component()
        assert missing_completion.status == "DEGRADED"
        assert missing_completion.blockers == [
            "MONTH_END_RUN_COMPLETION_MISSING"
        ]

        due.completed_at = _NOW + timedelta(seconds=1)
        db.commit()
        future_completion = service._rotation_precommitment_component()
        assert future_completion.status == "DEGRADED"
        assert future_completion.blockers == [
            "MONTH_END_RUN_COMPLETED_AT_IN_FUTURE"
        ]
    finally:
        db.close()


def test_opening_shadow_health_rejects_wrong_algorithm_and_future_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)
    db = _db()
    try:
        now = _NOW + timedelta(minutes=5)
        identity = OpeningMomentumShadowService(db)._variant_identities()[0]
        row = OpeningMomentumShadowRun(
            session_date=_NOW.date(),
            algorithm_version="wrong-algorithm",
            config_version=identity.config_version,
            status="SKIPPED",
            reason="TEST",
            signal_at=_NOW - timedelta(minutes=5),
            observed_at=_NOW - timedelta(minutes=4),
            estimated_cost_bps=0.0,
            updated_at=_NOW - timedelta(minutes=4),
        )
        db.add(row)
        db.commit()
        service = ResearchObservationHealthService(db, now=now)

        wrong_algorithm = service._opening_shadow_component()
        assert wrong_algorithm.status == "DEGRADED"
        assert wrong_algorithm.blockers == [
            "CURRENT_SHADOW_VARIANT_MISSING_1",
            "CURRENT_SHADOW_VARIANT_INVALID_1",
        ]

        row.algorithm_version = identity.algorithm_version
        row.observed_at = now + timedelta(seconds=1)
        row.updated_at = now + timedelta(seconds=1)
        db.commit()
        future_row = service._opening_shadow_component()
        assert future_row.status == "DEGRADED"
        assert future_row.blockers == [
            "CURRENT_SHADOW_VARIANT_MISSING_1",
            "OPENING_SHADOW_EVIDENCE_IN_FUTURE_1",
        ]
    finally:
        db.close()


def test_opening_execution_health_rejects_current_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            identity,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        db.add(OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=identity.algorithm_version,
            config_version=identity.config_version,
            universe_source=identity.universe_source,
            selection_run_id=identity.selection_run_id,
            status="REJECTED",
            reason="TEST_REJECTION",
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            universe_size=8,
            max_price_deviation_bps=200.0,
            stop_loss_pct=1.0,
            max_holding_minutes=60,
            updated_at=_NOW,
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._opening_execution_component()

        assert component.status == "DEGRADED"
        assert component.blockers == ["OPENING_EXECUTION_REJECTED"]
    finally:
        db.close()


def test_intraday_break_and_post_close_freshness_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "watchlist_quant_auto_score_enabled", True)
    monkeypatch.setattr(settings, "watchlist_quant_score_ttl_minutes", 15)
    db = _db()
    try:
        hk_lunch = datetime(2026, 7, 30, 4, 30, tzinfo=timezone.utc)
        db.add(WatchlistItem(symbol="0700.HK", market="HK"))
        db.add(WatchlistScore(
            symbol="0700.HK",
            market="HK",
            source=QUANT_SCORE_SOURCE,
            score=70,
            confidence=0.8,
            recommended_action="WATCH",
            created_at=datetime(
                2026, 7, 30, 3, 30, tzinfo=timezone.utc
            ),
            expires_at=hk_lunch + timedelta(hours=1),
        ))
        db.commit()

        lunch = ResearchObservationHealthService(
            db,
            now=hk_lunch,
        )._quant_component()

        assert lunch.status == "DEGRADED"
        assert lunch.observed_count == 0
        assert lunch.coverage_ratio == 0.0

        db.add(StrategyConfig(
            symbol="AAPL.US",
            market="US",
            buy_low=100,
            sell_high=110,
            stop_loss_pct=1,
        ))
        db.add(RuntimeState(
            symbol="AAPL.US",
            engine_state="flat",
            last_price=105,
            updated_at=datetime(
                2026, 7, 29, 19, 59, tzinfo=timezone.utc
            ),
        ))
        db.commit()
        post_close = datetime(
            2026, 7, 30, 20, 10, tzinfo=timezone.utc
        )

        interval = ResearchObservationHealthService(
            db,
            now=post_close,
        )._live_interval_component()

        assert interval.status == "DEGRADED"
        assert interval.expected_session_date == date(2026, 7, 30)
        assert interval.blockers == ["CURRENT_PRICE_STALE"]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("now", "created_at", "expected_status"),
    (
        (
            datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc),
            "WARNING",
        ),
        (
            datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc),
            "WARNING",
        ),
        (
            datetime(2026, 7, 30, 20, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 19, 59, tzinfo=timezone.utc),
            "DEGRADED",
        ),
    ),
)
def test_quant_pre_closed_and_post_cutoffs_do_not_reuse_early_scores(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    created_at: datetime,
    expected_status: str,
) -> None:
    monkeypatch.setattr(settings, "watchlist_quant_auto_score_enabled", True)
    monkeypatch.setattr(settings, "watchlist_quant_score_ttl_minutes", 15)
    db = _db()
    try:
        db.add(WatchlistItem(symbol="AAPL.US", market="US"))
        db.add(WatchlistScore(
            symbol="AAPL.US",
            market="US",
            source=QUANT_SCORE_SOURCE,
            score=70,
            confidence=0.8,
            recommended_action="WATCH",
            created_at=created_at,
            expires_at=now + timedelta(days=1),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._quant_component()

        assert component.status == expected_status
        assert component.observed_count == 0
        assert component.coverage_ratio == 0.0
    finally:
        db.close()


def test_universe_complete_requires_coherent_nonempty_counts_and_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "universe_selection_enabled", True)
    db = _db()
    try:
        run = UniverseSelectionRun(
            as_of_date=date(2026, 7, 29),
            algorithm_version="health-invalid-v1",
            source_version="health-source-v1",
            status="COMPLETE",
            candidate_count=0,
            evaluable_count=0,
            selected_count=0,
            coverage_ratio=0.0,
            completed_at=_NOW - timedelta(minutes=30),
        )
        db.add(run)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        empty = service._universe_component()
        assert empty.status == "DEGRADED"
        assert empty.coverage_ratio is None
        assert empty.blockers == [
            "COMPLETE_RUN_COUNTS_OR_COVERAGE_INVALID"
        ]

        run.candidate_count = 2
        run.evaluable_count = 3
        run.selected_count = 1
        run.coverage_ratio = 1.0
        db.commit()
        incoherent = service._universe_component()
        assert incoherent.status == "DEGRADED"
        assert incoherent.coverage_ratio is None

        run.as_of_date = date(2026, 7, 27)
        run.candidate_count = 2
        run.evaluable_count = 2
        run.selected_count = 1
        run.coverage_ratio = 1.0
        db.commit()
        stale = service._universe_component()
        assert stale.status == "DEGRADED"
        assert stale.blockers == ["MULTIPLE_COMPLETED_SESSIONS_MISSING"]
    finally:
        db.close()


def test_forward_health_accepts_finalize_window_and_rejects_early_evidence(
) -> None:
    db = _db()
    try:
        config = StrategyV2ShadowConfig(
            symbol="AAPL.US",
            enabled=True,
            universe_managed=True,
        )
        db.add(config)
        db.commit()
        registration = _add_valid_forward_registration(
            db,
            config,
            registered_at=datetime(
                2026, 7, 29, 20, 30, tzinfo=timezone.utc
            ),
        )
        evidence = StrategyV2ForwardEvidence(
            registration_id=registration.id,
            target_session_date=date(2026, 7, 30),
            target_open_at=datetime(
                2026, 7, 30, 13, 30, tzinfo=timezone.utc
            ),
            evaluated_at=datetime(
                2026, 7, 30, 20, 12, tzinfo=timezone.utc
            ),
            disposition="EXCLUDED",
            exclusion_reason="TARGET_SESSION_INCOMPLETE",
            structural_failure=False,
        )
        db.add(evidence)
        db.flush()
        evidence.evidence_digest_sha256 = (
            StrategyV2ShadowService._forward_evidence_digest(evidence)
        )
        db.commit()
        now = datetime(2026, 7, 30, 20, 12, 30, tzinfo=timezone.utc)
        service = ResearchObservationHealthService(db, now=now)

        finalize = service._strategy_v2_forward_component()
        assert finalize.status == "HEALTHY", finalize
        assert finalize.latest_session_date == date(2026, 7, 30)
        assert finalize.expected_session_date == date(2026, 7, 30)
        assert finalize.coverage_ratio == 1.0

        evidence.evaluated_at = datetime(
            2026, 7, 30, 19, 59, tzinfo=timezone.utc
        )
        evidence.evidence_digest_sha256 = (
            StrategyV2ShadowService._forward_evidence_digest(evidence)
        )
        db.commit()
        early = service._strategy_v2_forward_component()
        assert early.status == "DEGRADED"
        assert early.observed_count == 0
        assert early.expected_count == 1
        assert early.coverage_ratio == 0.0
        assert "FORWARD_EVIDENCE_TIMING_INVALID_1" in early.blockers
        assert (
            "FORWARD_EVIDENCE_MISSING_AFTER_CLOSED_SESSION_1"
            in early.blockers
        )
    finally:
        db.close()


def test_registration_eligibility_must_match_exact_next_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
    monkeypatch.setattr(settings, "entry_round_trip_slippage_bps", 4.0)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        config = StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True)
        db.add(config)
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="AAPL.US",
            now=_NOW,
        )
        LiveExitChallengerService(db).ensure_registrations(
            symbol="AAPL.US",
            market="US",
            now=_NOW,
        )
        StrategyV2ExitChallengerService(db).ensure_registrations(
            symbol="AAPL.US",
            market="US",
            source_config_version=StrategyV2ShadowService(
                db
            )._config_version(config),
            slippage_bps=config.slippage_bps,
            now=_NOW,
        )
        portfolio_registration = db.query(
            StrategyV2PortfolioRegistration
        ).first()
        live_registration = db.query(
            LiveExitChallengerRegistration
        ).first()
        strategy_registration = db.query(
            StrategyV2ExitChallengerRegistration
        ).first()
        assert portfolio_registration is not None
        assert live_registration is not None
        assert strategy_registration is not None
        portfolio_registration.eligible_after += timedelta(days=365)
        live_registration.eligible_after += timedelta(days=365)
        strategy_registration.eligible_after += timedelta(days=365)
        db.commit()
        health = ResearchObservationHealthService(db, now=_NOW)

        portfolio = health._portfolio_component()
        live_exit = health._live_exit_challenger_component()
        strategy_exit = health._strategy_v2_exit_challenger_component()

        assert portfolio.status == "DEGRADED"
        assert "CURRENT_PORTFOLIO_REGISTRATION_INVALID_1" in portfolio.blockers
        assert live_exit.status == "DEGRADED"
        assert "CURRENT_LIVE_EXIT_REGISTRATION_INVALID_1" in live_exit.blockers
        assert strategy_exit.status == "DEGRADED"
        assert (
            "CURRENT_STRATEGY_V2_EXIT_REGISTRATION_INVALID_1"
            in strategy_exit.blockers
        )
    finally:
        db.close()


def test_portfolio_health_rejects_noncausal_decisions_and_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    signal_at = datetime(2026, 7, 30, 13, 40, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        config = StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True)
        db.add(config)
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="AAPL.US",
            now=signal_at - timedelta(days=1),
        )
        decision = StrategyV2ShadowDecision(
            idempotency_key="health-noncausal-decision",
            symbol="AAPL.US",
            config_version=StrategyV2ShadowService(
                db
            )._config_version(config),
            session_date=signal_at.date(),
            bar_at=signal_at,
            observed_at=signal_at - timedelta(seconds=1),
            action="SUBMIT_ENTRY",
        )
        db.add(decision)
        db.commit()
        service = ResearchObservationHealthService(db, now=now)

        invalid_decision = service._portfolio_component()
        assert invalid_decision.status == "DEGRADED"
        assert invalid_decision.blockers == [
            "ENTRY_SIGNAL_CAUSALITY_INVALID_1"
        ]

        decision.observed_at = signal_at + timedelta(seconds=5)
        registrations = db.query(
            StrategyV2PortfolioRegistration
        ).all()
        for registration in registrations:
            db.add(StrategyV2PortfolioObservation(
                registration_id=registration.id,
                signal_at=signal_at,
                observed_at=signal_at - timedelta(days=20),
                status="NO_ELIGIBLE",
                reason="NONCAUSAL_TEST",
            ))
        db.commit()

        invalid_observations = service._portfolio_component()
        assert invalid_observations.status == "DEGRADED"
        assert invalid_observations.observed_count == 0
        assert invalid_observations.expected_count == len(registrations)
        assert invalid_observations.coverage_ratio == 0.0
        assert (
            f"PORTFOLIO_OBSERVATION_CAUSALITY_INVALID_"
            f"{len(registrations)}"
            in invalid_observations.blockers
        )
    finally:
        db.close()


def test_portfolio_evidence_denominator_keeps_missing_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    signal_at = datetime(2026, 7, 30, 13, 40, tzinfo=timezone.utc)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        config = StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True)
        db.add(config)
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="AAPL.US",
            now=signal_at - timedelta(days=1),
        )
        db.add(StrategyV2ShadowDecision(
            idempotency_key="health-denominator-decision",
            symbol="AAPL.US",
            config_version=StrategyV2ShadowService(
                db
            )._config_version(config),
            session_date=signal_at.date(),
            bar_at=signal_at,
            observed_at=signal_at + timedelta(seconds=5),
            action="SUBMIT_ENTRY",
        ))
        registrations = db.query(
            StrategyV2PortfolioRegistration
        ).all()
        for registration in registrations:
            db.add(StrategyV2PortfolioObservation(
                registration_id=registration.id,
                signal_at=signal_at,
                observed_at=signal_at + timedelta(seconds=10),
                status="NO_ELIGIBLE",
                reason="DENOMINATOR_TEST",
            ))
        db.commit()
        removed = registrations[0]
        db.query(StrategyV2PortfolioObservation).filter(
            StrategyV2PortfolioObservation.registration_id == removed.id
        ).delete()
        db.delete(removed)
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._portfolio_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == len(registrations) - 1
        assert component.expected_count == len(registrations)
        assert component.coverage_ratio == pytest.approx(
            (len(registrations) - 1) / len(registrations)
        )
        assert "CURRENT_PORTFOLIO_REGISTRATION_MISSING_1" in component.blockers
    finally:
        db.close()


def test_portfolio_unrelated_observation_is_invalid_and_cannot_refresh_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "strategy_v2_portfolio_shadow_enabled",
        True,
    )
    now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    signal_at = datetime(2026, 7, 30, 13, 40, tzinfo=timezone.utc)
    observed_at = signal_at + timedelta(seconds=10)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        config = StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True)
        db.add(config)
        db.commit()
        StrategyV2PortfolioService(db).ensure_registrations(
            primary_symbol="AAPL.US",
            now=signal_at - timedelta(days=1),
        )
        db.add(StrategyV2ShadowDecision(
            idempotency_key="health-unrelated-observation-decision",
            symbol="AAPL.US",
            config_version=StrategyV2ShadowService(
                db
            )._config_version(config),
            session_date=signal_at.date(),
            bar_at=signal_at,
            observed_at=signal_at + timedelta(seconds=5),
            action="SUBMIT_ENTRY",
        ))
        registrations = db.query(
            StrategyV2PortfolioRegistration
        ).all()
        for registration in registrations:
            db.add(StrategyV2PortfolioObservation(
                registration_id=registration.id,
                signal_at=signal_at,
                observed_at=observed_at,
                status="NO_ELIGIBLE",
                reason="HEALTH_MATCHED",
            ))
        db.add(StrategyV2PortfolioObservation(
            registration_id=registrations[0].id,
            signal_at=signal_at + timedelta(minutes=1),
            observed_at=now,
            status="NO_ELIGIBLE",
            reason="HEALTH_UNRELATED",
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._portfolio_component()

        assert component.status == "DEGRADED"
        assert component.latest_at == observed_at
        assert component.observed_count == len(registrations)
        assert component.expected_count == len(registrations)
        assert component.coverage_ratio == 1.0
        assert component.blockers == [
            "PORTFOLIO_OBSERVATION_CAUSALITY_INVALID_1"
        ]
    finally:
        db.close()


def test_strategy_v2_exit_health_requires_trade_per_eligible_baseline() -> None:
    db = _db()
    try:
        config = StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True)
        db.add(config)
        db.commit()
        config_version = StrategyV2ShadowService(db)._config_version(config)
        challenger = StrategyV2ExitChallengerService(db)
        challenger.ensure_registrations(
            symbol="AAPL.US",
            market="US",
            source_config_version=config_version,
            slippage_bps=config.slippage_bps,
            now=_NOW - timedelta(minutes=3),
        )
        entry_at = _NOW - timedelta(minutes=1)
        decision = StrategyV2ShadowDecision(
            idempotency_key="health-strategy-exit-entry",
            symbol="AAPL.US",
            market="US",
            config_version=config_version,
            session_date=entry_at.date(),
            bar_at=entry_at,
            observed_at=entry_at + timedelta(seconds=5),
            action="FILL_ENTRY",
        )
        db.add(decision)
        db.flush()
        baseline = StrategyV2ShadowTrade(
            symbol="AAPL.US",
            config_version=config_version,
            entry_decision_id=decision.id,
            status="OPEN",
            entry_at=entry_at,
            entry_price=100.0,
            quantity=1.0,
            entry_reason="FIRST_CAUSAL_BAR_OPEN_FILL",
            estimated_fees=0.05,
            estimated_fee_rate=0.0005,
        )
        db.add(baseline)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        missing = service._strategy_v2_exit_challenger_component()

        assert missing.status == "DEGRADED"
        assert missing.observed_count == 0
        assert missing.expected_count == len(
            STRATEGY_V2_EXIT_ALGORITHM_VERSIONS
        )
        assert missing.coverage_ratio == 0.0
        assert missing.blockers == [
            "STRATEGY_V2_EXIT_EVIDENCE_MISSING_8"
        ]

        challenger._attach_eligible_entry(symbol="AAPL.US")
        trades = db.query(StrategyV2ExitChallengerTrade).all()
        assert len(trades) == len(STRATEGY_V2_EXIT_ALGORITHM_VERSIONS)
        for trade in trades:
            trade.updated_at = _NOW
        db.commit()

        healthy = service._strategy_v2_exit_challenger_component()

        assert healthy.status == "HEALTHY", healthy
        assert healthy.observed_count == len(trades)
        assert healthy.expected_count == len(trades)
        assert healthy.coverage_ratio == 1.0
        assert healthy.blockers == []

        for trade in trades:
            trade.estimated_fee_rate = -1.0
            trade.updated_at = _NOW
        db.commit()
        invalid_fee = service._strategy_v2_exit_challenger_component()
        assert invalid_fee.status == "DEGRADED"
        assert invalid_fee.observed_count == 0

        challenger_exit_at = entry_at + timedelta(seconds=30)
        challenger_exit_price = 101.0
        challenger_gross = 1.0
        challenger_fees = 0.1005
        challenger_net = challenger_gross - challenger_fees
        for trade in trades:
            trade.estimated_fee_rate = 0.0005
            trade.challenger_exit_at = challenger_exit_at
            trade.updated_at = _NOW
        db.commit()
        open_with_exit = service._strategy_v2_exit_challenger_component()
        assert open_with_exit.status == "DEGRADED"
        assert open_with_exit.observed_count == 0

        for trade in trades:
            trade.status = "CLOSED"
        db.commit()
        incomplete_closed = service._strategy_v2_exit_challenger_component()
        assert incomplete_closed.status == "DEGRADED"
        assert incomplete_closed.observed_count == 0

        for trade in trades:
            trade.challenger_exit_price = challenger_exit_price
            trade.challenger_exit_reason = "PROFIT_LOCK"
            trade.challenger_gross_pnl = challenger_gross
            trade.challenger_estimated_fees = challenger_fees
            trade.challenger_net_pnl = challenger_net
            trade.updated_at = _NOW
        db.commit()
        closed_unpaired = service._strategy_v2_exit_challenger_component()
        assert closed_unpaired.status == "HEALTHY", closed_unpaired

        for trade in trades:
            trade.baseline_exit_at = entry_at + timedelta(seconds=45)
            trade.updated_at = _NOW
        db.commit()
        partial_pair = service._strategy_v2_exit_challenger_component()
        assert partial_pair.status == "DEGRADED"
        assert partial_pair.observed_count == 0

        baseline_exit_at = entry_at + timedelta(seconds=45)
        baseline.status = "CLOSED"
        baseline.exit_at = baseline_exit_at
        baseline.exit_price = 99.0
        baseline.exit_reason = "MAX_HOLD"
        baseline.gross_pnl = -1.0
        baseline.estimated_fees = 0.0995
        baseline.net_pnl = -1.0995
        for trade in trades:
            trade.baseline_exit_at = baseline_exit_at
            trade.baseline_exit_price = 99.0
            trade.baseline_exit_reason = "MAX_HOLD"
            trade.baseline_net_pnl = baseline.net_pnl
            trade.net_pnl_delta = 0.0
            trade.paired_at = _NOW
            trade.updated_at = _NOW
        db.commit()
        invalid_delta = service._strategy_v2_exit_challenger_component()
        assert invalid_delta.status == "DEGRADED"
        assert invalid_delta.observed_count == 0

        for trade in trades:
            trade.net_pnl_delta = challenger_net - float(
                baseline.net_pnl or 0.0
            )
            trade.updated_at = _NOW
        db.commit()
        paired = service._strategy_v2_exit_challenger_component()
        assert paired.status == "HEALTHY", paired
        assert paired.observed_count == len(trades)

        preeligible_at = _NOW - timedelta(minutes=4)
        preeligible_decision = StrategyV2ShadowDecision(
            idempotency_key="health-strategy-exit-preeligible",
            symbol="AAPL.US",
            market="US",
            config_version=config_version,
            session_date=preeligible_at.date(),
            bar_at=preeligible_at,
            observed_at=preeligible_at + timedelta(seconds=5),
            action="FILL_ENTRY",
        )
        db.add(preeligible_decision)
        db.flush()
        preeligible_baseline = StrategyV2ShadowTrade(
            symbol="AAPL.US",
            config_version=config_version,
            entry_decision_id=preeligible_decision.id,
            status="OPEN",
            entry_at=preeligible_at,
            entry_price=100.0,
            quantity=1.0,
            entry_reason="FIRST_CAUSAL_BAR_OPEN_FILL",
            estimated_fees=0.05,
            estimated_fee_rate=0.0005,
        )
        db.add(preeligible_baseline)
        db.flush()
        db.add(StrategyV2ExitChallengerTrade(
            registration_id=trades[0].registration_id,
            baseline_trade_id=preeligible_baseline.id,
            symbol="AAPL.US",
            source_config_version=config_version,
            status="OPEN",
            entry_at=preeligible_at,
            entry_price=100.0,
            quantity=1.0,
            estimated_fee_rate=0.0005,
            last_bar_at=preeligible_at - timedelta(microseconds=1),
            updated_at=_NOW,
        ))
        db.commit()
        unexpected = service._strategy_v2_exit_challenger_component()
        assert unexpected.status == "DEGRADED"
        assert unexpected.observed_count == len(trades)
        assert unexpected.expected_count == len(trades)
        assert unexpected.blockers == [
            "STRATEGY_V2_EXIT_EVIDENCE_UNEXPECTED_1"
        ]
    finally:
        db.close()


def test_live_exit_health_requires_causal_trade_for_deduplicated_real_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_exit_challenger_enabled", True)
    monkeypatch.setattr(settings, "entry_round_trip_slippage_bps", 4.0)
    db = _db()
    try:
        db.add(StrategyConfig(symbol="AAPL.US", market="US"))
        db.add(StrategyV2ShadowConfig(symbol="AAPL.US", enabled=True))
        db.commit()
        LiveExitChallengerService(db).ensure_registrations(
            symbol="AAPL.US",
            market="US",
            now=_NOW,
        )
        entry_at = _NOW + timedelta(minutes=2)
        for _ in range(2):
            db.add(OrderRecord(
                broker_order_id="",
                symbol="AAPL.US",
                side="BUY",
                quantity=10,
                price=100,
                executed_quantity=10,
                executed_price=100,
                status="FILLED",
                filled_at=entry_at,
                config_version="live-health-v1",
            ))
        db.commit()
        now = _NOW + timedelta(minutes=3)
        service = ResearchObservationHealthService(db, now=now)

        missing = service._live_exit_challenger_component()
        assert missing.status == "DEGRADED"
        assert missing.observed_count == 0
        assert missing.expected_count == 11
        assert missing.coverage_ratio == 0.0
        assert missing.blockers == ["LIVE_EXIT_EVIDENCE_MISSING_11"]

        latest_entry = db.query(OrderRecord).order_by(
            OrderRecord.id.desc()
        ).first()
        assert latest_entry is not None
        registrations = db.query(
            LiveExitChallengerRegistration
        ).all()
        trades: list[LiveExitChallengerTrade] = []
        for registration in registrations:
            trade = LiveExitChallengerTrade(
                registration_id=registration.id,
                entry_order_id=latest_entry.id,
                symbol="AAPL.US",
                entry_config_version="live-health-v1",
                status="OPEN",
                entry_at=_NOW - timedelta(days=1),
                entry_price=100,
                quantity=10,
                estimated_fee_rate=0.0005,
                last_bar_at=_NOW - timedelta(days=1),
                updated_at=now,
            )
            trades.append(trade)
            db.add(trade)
        db.commit()

        noncausal = service._live_exit_challenger_component()
        assert noncausal.status == "DEGRADED"
        assert noncausal.observed_count == 0
        assert noncausal.expected_count == 11
        assert noncausal.coverage_ratio == 0.0
        assert noncausal.blockers == ["LIVE_EXIT_EVIDENCE_INVALID_11"]

        for trade in trades:
            trade.entry_at = entry_at - timedelta(seconds=1)
            trade.last_bar_at = entry_at.replace(second=0, microsecond=0)
            trade.updated_at = now
        db.commit()
        reversed_fill = service._live_exit_challenger_component()
        assert reversed_fill.status == "DEGRADED"
        assert reversed_fill.observed_count == 0
        assert reversed_fill.blockers == ["LIVE_EXIT_EVIDENCE_INVALID_11"]

        for trade in trades:
            trade.entry_at = entry_at
            trade.entry_config_version = "wrong-config"
            trade.entry_price = 99
            trade.quantity = 9
            trade.last_bar_at = entry_at.replace(second=0, microsecond=0)
            trade.updated_at = now
        db.commit()
        identity_mismatch = service._live_exit_challenger_component()
        assert identity_mismatch.status == "DEGRADED"
        assert identity_mismatch.observed_count == 0
        assert identity_mismatch.blockers == [
            "LIVE_EXIT_EVIDENCE_INVALID_11"
        ]

        for trade in trades:
            trade.entry_config_version = "live-health-v1"
            trade.entry_price = 100
            trade.quantity = 10
            trade.last_bar_at = (
                entry_at.replace(second=0, microsecond=0)
                - timedelta(minutes=1)
            )
            trade.updated_at = now
        db.commit()
        invalid_last_bar = service._live_exit_challenger_component()
        assert invalid_last_bar.status == "DEGRADED"
        assert invalid_last_bar.observed_count == 0

        for trade in trades:
            trade.last_bar_at = entry_at.replace(second=0, microsecond=0)
            trade.updated_at = entry_at - timedelta(seconds=1)
        db.commit()
        invalid_update_order = service._live_exit_challenger_component()
        assert invalid_update_order.status == "DEGRADED"
        assert invalid_update_order.observed_count == 0

        for trade in trades:
            trade.updated_at = now + timedelta(seconds=1)
        db.commit()
        future_update = service._live_exit_challenger_component()
        assert future_update.status == "DEGRADED"
        assert future_update.observed_count == 0

        for trade in trades:
            trade.updated_at = now
        db.commit()
        healthy = service._live_exit_challenger_component()
        assert healthy.status == "HEALTHY", healthy
        assert healthy.observed_count == 11
        assert healthy.expected_count == 11
        assert healthy.coverage_ratio == 1.0
        assert healthy.blockers == []

        for trade in trades:
            trade.estimated_fee_rate = -1.0
            trade.updated_at = now
        db.commit()
        invalid_fee = service._live_exit_challenger_component()
        assert invalid_fee.status == "DEGRADED"
        assert invalid_fee.observed_count == 0

        challenger_exit_at = entry_at + timedelta(seconds=30)
        challenger_exit_price = 101.0
        challenger_gross = 10.0
        challenger_fees = 1.005
        challenger_net = challenger_gross - challenger_fees
        for trade in trades:
            trade.estimated_fee_rate = 0.0005
            trade.challenger_exit_at = challenger_exit_at
            trade.updated_at = now
        db.commit()
        open_with_exit = service._live_exit_challenger_component()
        assert open_with_exit.status == "DEGRADED"
        assert open_with_exit.observed_count == 0

        for trade in trades:
            trade.status = "CLOSED"
        db.commit()
        incomplete_closed = service._live_exit_challenger_component()
        assert incomplete_closed.status == "DEGRADED"
        assert incomplete_closed.observed_count == 0

        for trade in trades:
            trade.challenger_exit_price = challenger_exit_price
            trade.challenger_exit_reason = "PROFIT_LOCK"
            trade.challenger_gross_pnl = challenger_gross
            trade.challenger_estimated_fees = challenger_fees
            trade.challenger_net_pnl = challenger_net
            trade.updated_at = now
        db.commit()
        closed_unpaired = service._live_exit_challenger_component()
        assert closed_unpaired.status == "HEALTHY", closed_unpaired

        baseline_exit_at = entry_at + timedelta(seconds=45)
        baseline = OrderRecord(
            broker_order_id="live-health-exit",
            symbol="AAPL.US",
            side="SELL",
            quantity=10,
            price=99,
            executed_quantity=10,
            executed_price=99,
            status="FILLED",
            filled_at=baseline_exit_at,
            exit_cause="TIME_STOP",
            gross_pnl=-10,
            pnl_fee=1,
            net_pnl=-11,
        )
        db.add(baseline)
        db.flush()
        for trade in trades:
            trade.baseline_exit_order_id = baseline.id
            trade.baseline_exit_at = baseline_exit_at
            trade.baseline_exit_price = 99
            trade.baseline_exit_reason = "TIME_STOP"
            trade.baseline_net_pnl = -11
            trade.net_pnl_delta = 0
            trade.paired_at = now
            trade.updated_at = now
        db.commit()
        invalid_delta = service._live_exit_challenger_component()
        assert invalid_delta.status == "DEGRADED"
        assert invalid_delta.observed_count == 0

        for trade in trades:
            trade.net_pnl_delta = challenger_net + 11
            trade.updated_at = now
        db.commit()
        paired = service._live_exit_challenger_component()
        assert paired.status == "HEALTHY", paired
        assert paired.observed_count == 11

        writer = LiveExitChallengerService(db)
        for trade in trades:
            trade.status = "OPEN"
            trade.challenger_exit_at = None
            trade.challenger_exit_price = None
            trade.challenger_exit_reason = ""
            trade.challenger_gross_pnl = None
            trade.challenger_estimated_fees = None
            trade.challenger_net_pnl = None
            trade.baseline_exit_order_id = None
            trade.baseline_exit_at = None
            trade.baseline_exit_price = None
            trade.baseline_exit_reason = ""
            trade.baseline_net_pnl = None
            trade.net_pnl_delta = None
            trade.paired_at = None
            writer._finalize_against_baseline(
                trade,
                baseline,
                paired_at=now,
            )
            trade.updated_at = now
        db.commit()
        actual_broker_fee = service._live_exit_challenger_component()
        assert actual_broker_fee.status == "HEALTHY", actual_broker_fee
        assert actual_broker_fee.observed_count == 11
        assert baseline.pnl_fee == 1
        assert (100 + 99) * 10 * 0.0005 == pytest.approx(0.995)
        assert {
            trade.challenger_estimated_fees for trade in trades
        } == {1}
        assert {
            trade.challenger_exit_reason for trade in trades
        } == {"BASELINE_TIME_STOP"}
        assert {
            trade.baseline_exit_reason for trade in trades
        } == {"TIME_STOP"}

        first_trade = trades[0]
        invalid_linkage_values: tuple[tuple[str, object], ...] = (
            ("baseline_exit_order_id", None),
            ("baseline_exit_at", None),
            ("baseline_exit_price", None),
            ("baseline_exit_reason", ""),
            ("baseline_net_pnl", None),
            ("net_pnl_delta", None),
            ("paired_at", None),
        )
        mismatched_order_values: tuple[tuple[str, object], ...] = (
            ("challenger_exit_at", baseline_exit_at + timedelta(seconds=1)),
            ("challenger_exit_price", 98.0),
            ("challenger_gross_pnl", -9.0),
            ("challenger_estimated_fees", 0.5),
            ("challenger_net_pnl", -10.5),
            ("challenger_exit_reason", "BASELINE_EXIT"),
            ("baseline_exit_at", baseline_exit_at + timedelta(seconds=1)),
            ("baseline_exit_price", 98.0),
            ("baseline_net_pnl", -10.5),
            ("baseline_exit_reason", "maximum holding time reached"),
            ("paired_at", entry_at),
        )
        for field, invalid_value in (
            *invalid_linkage_values,
            *mismatched_order_values,
        ):
            original_value = getattr(first_trade, field)
            setattr(first_trade, field, invalid_value)
            first_trade.updated_at = now
            db.commit()

            invalid_baseline_link = service._live_exit_challenger_component()

            assert invalid_baseline_link.status == "DEGRADED"
            assert invalid_baseline_link.observed_count == 10
            assert invalid_baseline_link.blockers == [
                "LIVE_EXIT_EVIDENCE_INVALID_1"
            ]
            setattr(first_trade, field, original_value)
            first_trade.updated_at = now
            db.commit()

        restored_baseline_link = service._live_exit_challenger_component()
        assert restored_baseline_link.status == "HEALTHY", restored_baseline_link
        assert restored_baseline_link.observed_count == 11

        preeligible_entry_at = _NOW - timedelta(minutes=1)
        preeligible_entry = OrderRecord(
            broker_order_id="live-health-preeligible-entry",
            symbol="AAPL.US",
            side="BUY",
            quantity=10,
            price=100,
            executed_quantity=10,
            executed_price=100,
            status="FILLED",
            filled_at=preeligible_entry_at,
            config_version="live-health-v1",
        )
        db.add(preeligible_entry)
        db.flush()
        db.add(LiveExitChallengerTrade(
            registration_id=registrations[0].id,
            entry_order_id=preeligible_entry.id,
            symbol="AAPL.US",
            entry_config_version="live-health-v1",
            status="OPEN",
            entry_at=preeligible_entry_at,
            entry_price=100,
            quantity=10,
            estimated_fee_rate=0.0005,
            last_bar_at=preeligible_entry_at,
            updated_at=now,
        ))
        db.commit()
        unexpected = service._live_exit_challenger_component()
        assert unexpected.status == "DEGRADED"
        assert unexpected.observed_count == 11
        assert unexpected.expected_count == 11
        assert unexpected.blockers == [
            "LIVE_EXIT_EVIDENCE_UNEXPECTED_1"
        ]
    finally:
        db.close()


def test_opening_shadow_requires_frozen_identity_and_causal_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        session = get_session("US")
        session_open = datetime.combine(
            session_date,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        observer = OpeningMomentumShadowService(db)
        variants = observer._universe_variants(
            session_date=session_date,
            completed_before=session_open,
        )
        assert len(variants) == 1
        variant = variants[0]
        entry_at = observer._variant_entry_at(
            variant,
            session_open=session_open,
        )
        observed_at = (
            entry_at + timedelta(minutes=1, seconds=5)
        )
        row = OpeningMomentumShadowRun(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="SKIPPED",
            reason="HEALTH_TEST",
            signal_at=observer._variant_signal_at(
                variant,
                session_open=session_open,
            ),
            observed_at=observed_at,
            estimated_cost_bps=0.0,
            updated_at=observed_at,
        )
        db.add(row)
        db.commit()
        service = ResearchObservationHealthService(db, now=observed_at)

        healthy = service._opening_shadow_component()
        assert healthy.status == "HEALTHY", healthy
        assert healthy.coverage_ratio == 1.0

        row.entry_price = 100.0
        row.updated_at = observed_at
        db.commit()
        contradictory_skip = service._opening_shadow_component()
        assert contradictory_skip.status == "DEGRADED"
        assert contradictory_skip.observed_count == 0
        assert contradictory_skip.coverage_ratio == 0.0
        assert "CURRENT_SHADOW_VARIANT_INVALID_1" in (
            contradictory_skip.blockers
        )

        row.entry_price = None
        row.universe_source = "WRONG_SOURCE"
        row.selection_run_id = 999
        row.updated_at = observed_at
        db.commit()
        wrong_identity = service._opening_shadow_component()
        assert wrong_identity.status == "DEGRADED"
        assert wrong_identity.observed_count == 0
        assert wrong_identity.coverage_ratio == 0.0
        assert "CURRENT_SHADOW_VARIANT_INVALID_1" in wrong_identity.blockers

        row.universe_source = variant.universe_source
        row.selection_run_id = variant.selection_run_id
        row.signal_at = row.signal_at - timedelta(minutes=1)
        row.updated_at = observed_at
        db.commit()
        noncausal = service._opening_shadow_component()
        assert noncausal.status == "DEGRADED"
        assert noncausal.observed_count == 0
        assert noncausal.coverage_ratio == 0.0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value", "expected_stranded"),
    (
        ("candidate_symbol", None, True),
        ("candidate_symbol", "", True),
        ("entry_at", None, True),
        ("entry_price", None, True),
        ("entry_price", 0.0, True),
        ("exit_due_at", None, True),
        ("exit_price", 101.0, False),
    ),
)
def test_opening_shadow_flags_malformed_global_open_state(
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
    invalid_value: object | None,
    expected_stranded: bool,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        session = get_session("US")
        session_open = datetime.combine(
            session_date,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        observer = OpeningMomentumShadowService(db)
        variant = observer._universe_variants(
            session_date=session_date,
            completed_before=session_open,
        )[0]
        entry_at = observer._variant_entry_at(
            variant,
            session_open=session_open,
        )
        exit_due_at = entry_at + timedelta(
            minutes=variant.decision_config.holding_minutes
        )
        values: dict[str, object | None] = {
            "candidate_symbol": "AAPL.US",
            "entry_at": entry_at,
            "entry_price": 100.0,
            "exit_due_at": exit_due_at,
            "exit_price": None,
        }
        values[invalid_field] = invalid_value
        observed_at = entry_at + timedelta(minutes=1, seconds=5)
        db.add(OpeningMomentumShadowRun(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="OPEN",
            reason="HEALTH_TEST",
            signal_at=observer._variant_signal_at(
                variant,
                session_open=session_open,
            ),
            observed_at=observed_at,
            candidate_symbol=values["candidate_symbol"],
            entry_at=values["entry_at"],
            entry_price=values["entry_price"],
            exit_due_at=values["exit_due_at"],
            exit_price=values["exit_price"],
            estimated_cost_bps=0.0,
            updated_at=observed_at,
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=observed_at,
        )._opening_shadow_component()

        assert component.status == "DEGRADED"
        assert "CURRENT_SHADOW_VARIANT_INVALID_1" in component.blockers
        assert (
            "OPENING_SHADOW_OPEN_STRANDED_1" in component.blockers
        ) is expected_stranded
        assert component.coverage_ratio == 0.0
    finally:
        db.close()


def test_opening_shadow_flags_open_state_at_settlement_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        session = get_session("US")
        session_open = datetime.combine(
            session_date,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        observer = OpeningMomentumShadowService(db)
        variant = observer._universe_variants(
            session_date=session_date,
            completed_before=session_open,
        )[0]
        entry_at = observer._variant_entry_at(
            variant,
            session_open=session_open,
        )
        exit_due_at = entry_at + timedelta(
            minutes=variant.decision_config.holding_minutes
        )
        observed_at = entry_at + timedelta(minutes=1, seconds=5)
        db.add(OpeningMomentumShadowRun(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="OPEN",
            reason="HEALTH_TEST",
            signal_at=observer._variant_signal_at(
                variant,
                session_open=session_open,
            ),
            observed_at=observed_at,
            candidate_symbol="AAPL.US",
            entry_at=entry_at,
            entry_price=100.0,
            exit_due_at=exit_due_at,
            estimated_cost_bps=0.0,
            updated_at=observed_at,
        ))
        db.commit()
        now = exit_due_at + timedelta(minutes=1, seconds=5)

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._opening_shadow_component()

        assert component.status == "DEGRADED"
        assert "OPENING_SHADOW_OPEN_STRANDED_1" in component.blockers
        assert component.observed_count == 0
        assert component.coverage_ratio == 0.0
    finally:
        db.close()


@pytest.mark.parametrize("invalid_field", ("exit_price", "net_return_bps"))
def test_opening_shadow_closed_requires_complete_finite_outcome(
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_shadow_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", False)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        session = get_session("US")
        session_open = datetime.combine(
            session_date,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        observer = OpeningMomentumShadowService(db)
        variant = observer._universe_variants(
            session_date=session_date,
            completed_before=session_open,
        )[0]
        entry_at = observer._variant_entry_at(
            variant,
            session_open=session_open,
        )
        exit_due_at = entry_at + timedelta(
            minutes=variant.decision_config.holding_minutes
        )
        observed_at = entry_at + timedelta(minutes=1, seconds=5)
        settled_at = exit_due_at + timedelta(minutes=1, seconds=5)
        row = OpeningMomentumShadowRun(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="CLOSED",
            reason="MAX_HOLD",
            signal_at=observer._variant_signal_at(
                variant,
                session_open=session_open,
            ),
            observed_at=observed_at,
            candidate_symbol="AAPL.US",
            entry_at=entry_at,
            entry_price=100.0,
            exit_due_at=exit_due_at,
            exit_at=exit_due_at,
            exit_price=101.0,
            gross_return_bps=100.0,
            estimated_cost_bps=4.0,
            net_return_bps=96.0,
            maximum_adverse_excursion_bps=0.0,
            maximum_favorable_excursion_bps=100.0,
            updated_at=settled_at,
        )
        setattr(row, invalid_field, None)
        db.add(row)
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=settled_at,
        )._opening_shadow_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == 0
        assert component.coverage_ratio == 0.0
        assert "CURRENT_SHADOW_VARIANT_INVALID_1" in component.blockers
    finally:
        db.close()


def test_opening_execution_requires_session_variant_identity_and_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            variant,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        row = OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="SKIPPED",
            reason="HEALTH_TEST",
            symbol="DIAGNOSTIC.US",
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            universe_size=8,
            max_price_deviation_bps=200.0,
            stop_loss_pct=float(
                variant.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=variant.decision_config.holding_minutes,
            submit_attempts=0,
            updated_at=signal_ready_at,
        )
        db.add(row)
        db.commit()
        service = ResearchObservationHealthService(db, now=_NOW)

        healthy = service._opening_execution_component()
        assert healthy.status == "HEALTHY"
        assert healthy.coverage_ratio == 1.0

        row.submit_attempts = 1
        row.updated_at = signal_ready_at
        db.commit()
        attempted_skip = service._opening_execution_component()
        assert attempted_skip.status == "DEGRADED"
        assert attempted_skip.observed_count == 0
        assert attempted_skip.blockers == [
            "OPENING_EXECUTION_TIMING_INVALID"
        ]

        row.submit_attempts = 0
        row.universe_source = "WRONG_SOURCE"
        row.selection_run_id = 999
        row.updated_at = signal_ready_at
        db.commit()
        wrong_identity = service._opening_execution_component()
        assert wrong_identity.status == "DEGRADED"
        assert wrong_identity.observed_count == 0
        assert wrong_identity.coverage_ratio == 0.0
        assert wrong_identity.blockers == [
            "CURRENT_EXECUTION_EVIDENCE_INVALID"
        ]

        row.universe_source = variant.universe_source
        row.selection_run_id = variant.selection_run_id
        row.signal_at = signal_at + timedelta(seconds=1)
        row.updated_at = signal_ready_at
        db.commit()
        wrong_schedule = service._opening_execution_component()
        assert wrong_schedule.status == "DEGRADED"
        assert wrong_schedule.observed_count == 0
        assert wrong_schedule.coverage_ratio == 0.0
        assert wrong_schedule.blockers == [
            "OPENING_EXECUTION_TIMING_INVALID"
        ]
    finally:
        db.close()


def test_opening_execution_accepts_causal_retry_armed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            variant,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        row = OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="ARMED",
            reason="QUOTE_DEVIATION",
            symbol="AAPL.US",
            reference_entry_price=100.0,
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            requested_at=entry_due_at,
            universe_size=8,
            max_price_deviation_bps=200.0,
            stop_loss_pct=float(
                variant.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=variant.decision_config.holding_minutes,
            submit_attempts=1,
            updated_at=entry_due_at,
        )
        db.add(row)
        db.commit()
        service = ResearchObservationHealthService(db, now=entry_due_at)

        healthy = service._opening_execution_component()
        assert healthy.status == "HEALTHY"
        assert healthy.observed_count == 1
        assert healthy.blockers == []

        row.submit_attempts = 0
        row.updated_at = entry_due_at
        db.commit()
        missing_attempt = service._opening_execution_component()
        assert missing_attempt.status == "DEGRADED"
        assert missing_attempt.observed_count == 0
        assert missing_attempt.blockers == [
            "OPENING_EXECUTION_TIMING_INVALID"
        ]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("status", "invalid_field", "invalid_value"),
    (
        ("ARMED", "symbol", ""),
        ("ARMED", "reference_entry_price", 0.0),
        ("ARMED", "max_holding_minutes", 999),
        ("ARMED", "submit_attempts", 1),
        ("OPEN", "entry_order_id", ""),
        ("OPEN", "entry_price", 0.0),
        ("OPEN", "quantity", 0.0),
        ("OPEN", "exit_order_id", "unexpected-exit"),
        ("OPEN", "submit_attempts", 0),
        ("OPEN", "net_pnl", 9.0),
        ("CLOSED", "exit_order_id", ""),
        ("CLOSED", "exit_price", 0.0),
        ("CLOSED", "net_pnl", math.inf),
    ),
)
def test_opening_execution_requires_complete_positive_order_evidence(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    invalid_field: str,
    invalid_value: object,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            variant,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        armed = status == "ARMED"
        closed = status == "CLOSED"
        exit_filled_at = (
            entry_due_at + timedelta(minutes=10) if closed else None
        )
        row = OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status=status,
            reason="HEALTH_STATUS_MATRIX",
            symbol="AAPL.US",
            reference_entry_price=100.0,
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            requested_at=(None if armed else entry_due_at),
            entry_order_id=("" if armed else "entry-health-valid"),
            entry_filled_at=(None if armed else entry_due_at),
            entry_price=(None if armed else 100.0),
            quantity=(None if armed else 10.0),
            exit_order_id=("exit-health-valid" if closed else ""),
            exit_filled_at=exit_filled_at,
            exit_price=(101.0 if closed else None),
            net_pnl=(9.0 if closed else None),
            universe_size=8,
            max_price_deviation_bps=200.0,
            stop_loss_pct=float(
                variant.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=variant.decision_config.holding_minutes,
            submit_attempts=(0 if armed else 1),
            updated_at=(
                signal_ready_at if armed else exit_filled_at or entry_due_at
            ),
        )
        setattr(row, invalid_field, invalid_value)
        db.add(row)
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=(signal_ready_at if armed else _NOW),
        )._opening_execution_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == 0
        assert component.coverage_ratio == 0.0
        assert component.blockers == [
            "OPENING_EXECUTION_TIMING_INVALID"
        ]
    finally:
        db.close()


def test_opening_execution_detects_old_active_row_blocking_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            variant,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        db.add(OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status="SKIPPED",
            reason="HEALTH_CURRENT",
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            universe_size=8,
            max_price_deviation_bps=200,
            stop_loss_pct=float(
                variant.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=variant.decision_config.holding_minutes,
            updated_at=signal_ready_at,
        ))
        db.add(OpeningMomentumExecution(
            session_date=date(2026, 7, 29),
            algorithm_version="obsolete-algorithm",
            config_version="obsolete-config",
            universe_source="NONE",
            selection_run_id=None,
            status="ARMED",
            reason="STRANDED_OLD_SESSION",
            signal_at=datetime(
                2026, 7, 29, 13, 32, tzinfo=timezone.utc
            ),
            armed_at=datetime(
                2026, 7, 29, 13, 33, tzinfo=timezone.utc
            ),
            entry_due_at=datetime(
                2026, 7, 29, 13, 34, tzinfo=timezone.utc
            ),
            entry_deadline_at=datetime(
                2026, 7, 29, 13, 35, tzinfo=timezone.utc
            ),
            universe_size=8,
            max_price_deviation_bps=200,
            stop_loss_pct=1,
            max_holding_minutes=60,
            updated_at=datetime(
                2026, 7, 29, 13, 33, tzinfo=timezone.utc
            ),
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=_NOW,
        )._opening_execution_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == 0
        assert component.expected_count == 2
        assert component.coverage_ratio == 0.0
        assert component.blockers == [
            "OPENING_EXECUTION_ACTIVE_STATE_STRANDED_1"
        ]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("status", "entry_order_id"),
    (
        ("ARMED", ""),
        ("SUBMITTING", ""),
        ("SUBMITTING", "entry-health-submitting"),
        ("SUBMITTED", "entry-health-1"),
        ("OPEN", "entry-health-1"),
        ("EXITING", "entry-health-1"),
    ),
)
def test_opening_execution_flags_each_stranded_active_state(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    entry_order_id: str,
) -> None:
    monkeypatch.setattr(settings, "opening_momentum_execution_enabled", True)
    monkeypatch.setattr(settings, "opening_momentum_challenger_enabled", True)
    db = _db()
    try:
        session_date = date(2026, 7, 30)
        _add_complete_universe_run(
            db,
            as_of_date=date(2026, 7, 29),
            completed_at=datetime(
                2026, 7, 30, 13, 0, tzinfo=timezone.utc
            ),
        )
        (
            variant,
            signal_at,
            signal_ready_at,
            entry_due_at,
            entry_deadline_at,
        ) = _paper_execution_variant(db, session_date=session_date)
        now = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
        requested_at = (
            entry_due_at if status != "ARMED" else None
        )
        entry_filled_at = (
            entry_due_at if status in {"OPEN", "EXITING"} else None
        )
        updated_at = (
            now - timedelta(seconds=61)
            if status == "EXITING"
            else entry_due_at
            if status != "ARMED"
            else signal_ready_at
        )
        db.add(OpeningMomentumExecution(
            session_date=session_date,
            algorithm_version=variant.algorithm_version,
            config_version=variant.config_version,
            universe_source=variant.universe_source,
            selection_run_id=variant.selection_run_id,
            status=status,
            reason="HEALTH_STRANDED_TEST",
            symbol="AAPL.US",
            reference_entry_price=100.0,
            signal_at=signal_at,
            armed_at=signal_ready_at,
            entry_due_at=entry_due_at,
            entry_deadline_at=entry_deadline_at,
            requested_at=requested_at,
            entry_order_id=entry_order_id,
            entry_filled_at=entry_filled_at,
            entry_price=(100.0 if entry_filled_at is not None else None),
            quantity=(10.0 if entry_filled_at is not None else None),
            universe_size=8,
            max_price_deviation_bps=200.0,
            stop_loss_pct=float(
                variant.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=variant.decision_config.holding_minutes,
            submit_attempts=(0 if status == "ARMED" else 1),
            updated_at=updated_at,
        ))
        db.commit()

        component = ResearchObservationHealthService(
            db,
            now=now,
        )._opening_execution_component()

        assert component.status == "DEGRADED"
        assert component.observed_count == 0
        assert component.coverage_ratio == 0.0
        assert component.blockers == [
            "OPENING_EXECUTION_ACTIVE_STATE_STRANDED_1"
        ]
    finally:
        db.close()


def test_health_report_remains_strictly_read_only_and_safety_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_enabled_components(monkeypatch)
    monkeypatch.setattr(settings, "universe_selection_enabled", False)
    monkeypatch.setattr(
        settings,
        "watchlist_quant_auto_score_enabled",
        False,
    )
    db = _db()
    statements: list[str] = []

    def capture_writes(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(normalized.split(maxsplit=1)[0])

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", capture_writes)
    try:
        report = ResearchObservationHealthService(
            db,
            now=_NOW,
        ).get_health()
    finally:
        event.remove(bind, "before_cursor_execute", capture_writes)
        db.close()

    assert statements == []
    assert report.order_submission_allowed is False
    assert report.automatic_promotion_allowed is False
