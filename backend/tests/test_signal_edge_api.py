from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_signal_edge_api_{os.getpid()}.db"
)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.strategy_shadow import router
from app.config import settings
from app.database import get_db
from app.domain.strategy_v2.signal_edge import clustered_t_test
from app.models import (
    Base,
    StrategyV2ShadowConfig,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.schemas import SignalEdgeResponse


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"],
            connect_args={"check_same_thread": False},
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(bind=cls.engine)
        cls.app = FastAPI()
        cls.app.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            with cls.session_factory() as db:
                yield db

        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        cls.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        with self.session_factory() as db:
            db.query(StrategyV2ShadowTrade).delete()
            db.query(StrategyV2ShadowVersion).delete()
            db.query(StrategyV2ShadowConfig).delete()
            db.commit()


class TestSignalEdgeApi(_Base):
    def test_get_exposes_gross_and_net_clustered_statistics(self) -> None:
        # Given: one immutable barrier cohort with distinct gross and net returns.
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            db.add(StrategyV2ShadowConfig(
                symbol="API.US",
                enabled=True,
                stop_loss_pct=0.45,
                profit_target_pct=0.80,
            ))
            db.add(StrategyV2ShadowVersion(
                symbol="API.US",
                config_version="v1",
                config_json=json.dumps({
                    "stop_loss_pct": 0.45,
                    "profit_target_pct": 0.80,
                }),
                activated_at=now,
            ))
            for index in range(4):
                exit_at = now - timedelta(days=index % 2 + 1)
                gross_pnl = 1.0 + index % 2
                db.add(StrategyV2ShadowTrade(
                    symbol="API.US",
                    config_version="v1",
                    status="CLOSED",
                    entry_at=exit_at - timedelta(minutes=30),
                    exit_at=exit_at,
                    entry_price=100.0,
                    quantity=1.0,
                    gross_pnl=gross_pnl,
                    estimated_fees=0.5,
                    net_pnl=gross_pnl - 0.5,
                    exit_reason="PROFIT_TARGET" if index < 3 else "PRICE_STOP",
                ))
            db.commit()

        # When: the public read-only endpoint is requested.
        response = self.client.get(
            "/api/strategy-shadow/signal-edge",
            params={
                "symbol": "API.US",
                "min_resolved_trades": 1,
                "min_distinct_days": 1,
                "cost_floor_bps": 1.0,
            },
        )

        # Then: both estimands expose their mean, clustered t, and confidence interval.
        assert response.status_code == 200
        body = SignalEdgeResponse.model_validate_json(response.content)
        assert body.gross.naive_mean == 1.5
        assert body.net.naive_mean == 1.0
        assert body.futility.cost_floor_bps == 10.0
        promotion = response.json().get("promotion")
        assert promotion is not None, "query parameters must not bypass promotion floors"
        assert promotion["sample_size_met"] is False
        assert promotion["eligible"] is False
        assert promotion["required_distinct_days"] == 60
        assert promotion["required_resolved_brackets"] == 180
        for estimand in (body.gross, body.net):
            assert estimand.clustered_t is not None
            assert estimand.ci_lower is not None
            assert estimand.ci_upper is not None

    def test_response_rejects_payload_without_futility(self) -> None:
        clustered = {
            "observations": 0,
            "distinct_days": 0,
            "naive_t": None,
            "clustered_t": None,
            "naive_mean": None,
            "day_mean": None,
            "clustered_standard_error": None,
            "ci_lower": None,
            "ci_upper": None,
            "inflation_factor": None,
            "significant": False,
        }
        payload = {
            "generated_at": datetime.now(timezone.utc),
            "symbol": None,
            "lookback_days": 90,
            "stop_pct": 0.45,
            "target_pct": 0.80,
            "verdict": "INSUFFICIENT_DATA",
            "reasons": [],
            "first_passage": {
                "target_hits": 0,
                "stop_hits": 0,
                "resolved": 0,
                "barrier_mismatch_excluded": 0,
                "matched_versions": 0,
                "matched_trades": 0,
                "provenance_excluded_trades": 0,
                "missing_pnl_excluded": 0,
                "time_exit_excluded": 0,
                "time_exit_fraction": None,
                "observed_rate_floor": None,
                "observed_rate_ceiling": None,
                "observed_rate": None,
                "baseline_rate": 0.36,
                "edge_pp": None,
                "p_value": None,
                "beats_baseline": False,
            },
            "gross": clustered,
            "net": clustered,
            "clustered": clustered,
        }

        with pytest.raises(ValueError):
            SignalEdgeResponse.model_validate(payload)

    def test_get_reports_unresolvable_barrier_provenance(self) -> None:
        # Given: resolved trades reference a version with no immutable snapshot.
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            db.add(StrategyV2ShadowConfig(
                symbol="MISSING.US",
                enabled=True,
                stop_loss_pct=0.45,
                profit_target_pct=0.80,
            ))
            for index, exit_reason in enumerate(("PROFIT_TARGET", "PRICE_STOP")):
                exit_at = now - timedelta(days=index + 1)
                db.add(StrategyV2ShadowTrade(
                    symbol="MISSING.US",
                    config_version="missing-v1",
                    status="CLOSED",
                    entry_at=exit_at - timedelta(minutes=30),
                    exit_at=exit_at,
                    entry_price=100.0,
                    quantity=1.0,
                    gross_pnl=1.0 if index == 0 else -0.5,
                    net_pnl=1.0 if index == 0 else -0.5,
                    exit_reason=exit_reason,
                ))
            db.commit()

        # When: the public gate assesses evidence against the current barriers.
        response = self.client.get(
            "/api/strategy-shadow/signal-edge",
            params={"symbol": "MISSING.US"},
        )

        # Then: it fails closed and identifies attribution loss, not mere scarcity.
        assert response.status_code == 200
        body = SignalEdgeResponse.model_validate_json(response.content)
        assert body.verdict == "INSUFFICIENT_DATA"
        assert body.first_passage.barrier_mismatch_excluded == 2
        assert body.first_passage.matched_versions == 0
        assert body.first_passage.matched_trades == 0
        assert body.first_passage.provenance_excluded_trades == 2
        assert body.first_passage.missing_pnl_excluded == 0
        assert (
            "2 bracket-resolved trades excluded because barrier provenance was "
            "unavailable or did not match the tested barriers"
        ) in body.reasons

    def test_get_exposes_time_exit_conditioning_and_sensitivity_bounds(self) -> None:
        # Given: the live exit mix of 44 target, 88 stop and 150 time-barrier exits.
        now = datetime.now(timezone.utc)
        with self.session_factory() as db:
            db.add(StrategyV2ShadowConfig(
                symbol="COND.US",
                enabled=True,
                stop_loss_pct=0.45,
                profit_target_pct=0.80,
            ))
            db.add(StrategyV2ShadowVersion(
                symbol="COND.US",
                config_version="v1",
                config_json=json.dumps({
                    "stop_loss_pct": 0.45,
                    "profit_target_pct": 0.80,
                }),
                activated_at=now,
            ))
            outcomes = (
                ["PROFIT_TARGET"] * 44
                + ["PRICE_STOP"] * 88
                + ["MAX_HOLD"] * 135
                + ["EOD_FLATTEN"] * 15
            )
            for index, exit_reason in enumerate(outcomes):
                exit_at = now - timedelta(days=index % 25 + 1, minutes=index)
                db.add(StrategyV2ShadowTrade(
                    symbol="COND.US",
                    config_version="v1",
                    status="CLOSED",
                    entry_at=exit_at - timedelta(minutes=30),
                    exit_at=exit_at,
                    entry_price=100.0,
                    quantity=1.0,
                    gross_pnl=0.1 if index % 2 == 0 else -0.12,
                    net_pnl=-0.4 if index % 2 == 0 else -0.62,
                    exit_reason=exit_reason,
                ))
            db.commit()

        # When: the public read-only endpoint is requested.
        response = self.client.get(
            "/api/strategy-shadow/signal-edge",
            params={"symbol": "COND.US"},
        )

        # Then: the response schema carries the conditioning and its bounds.
        assert response.status_code == 200
        body = SignalEdgeResponse.model_validate_json(response.content)
        first_passage = body.first_passage
        assert first_passage.time_exit_excluded == 150
        assert first_passage.time_exit_fraction is not None
        assert math.isclose(
            first_passage.time_exit_fraction, 150 / 282, rel_tol=1e-12
        )
        assert first_passage.observed_rate_floor is not None
        assert first_passage.observed_rate_ceiling is not None
        assert math.isclose(first_passage.observed_rate_floor, 44 / 282, rel_tol=1e-12)
        assert math.isclose(
            first_passage.observed_rate_ceiling, 194 / 282, rel_tol=1e-12
        )

        # And: the verdict math is untouched by the disclosure.
        assert body.verdict == "FAIL"
        assert first_passage.beats_baseline is False
        assert first_passage.resolved == 132


class TestSignalEdgeEstimator:
    def test_reproduces_production_shaped_gross_and_net_intervals(self) -> None:
        # Given: 232 trades over 24 balanced-shock days with a ten-bp fee drag.
        gross_observations: list[tuple[date, float]] = []
        for day_index in range(24):
            trades = 9 if day_index < 8 else 10
            shock = 0.12205 if day_index % 2 == 0 else -0.12205
            gross_return_pct = -0.0057 + shock
            gross_observations.extend(
                (date(2026, 7, 1) + timedelta(days=day_index), gross_return_pct)
                for _ in range(trades)
            )
        net_observations = [
            (trading_day, gross_return_pct - 0.1)
            for trading_day, gross_return_pct in gross_observations
        ]

        # When: both estimands use the trade-weighted day-cluster CRVE.
        gross = clustered_t_test(gross_observations)
        net = clustered_t_test(net_observations)

        # Then: means and 95% intervals match the known production-shaped figures.
        assert (gross.observations, gross.distinct_days) == (232, 24)
        assert math.isclose((gross.naive_mean or 0.0) * 100, -0.57, abs_tol=0.01)
        assert math.isclose((net.naive_mean or 0.0) * 100, -10.57, abs_tol=0.01)
        assert math.isclose((gross.ci_lower or 0.0) * 100, -5.66, abs_tol=0.02)
        assert math.isclose((gross.ci_upper or 0.0) * 100, 4.53, abs_tol=0.02)
        assert math.isclose((net.ci_lower or 0.0) * 100, -15.66, abs_tol=0.02)
        assert math.isclose((net.ci_upper or 0.0) * 100, -5.48, abs_tol=0.02)
