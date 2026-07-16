from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_audit_logger
from app.api.strategy_shadow import router
from app.config import settings
from app.database import get_db
from app.models import (
    Base,
    StrategyConfig,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
    StrategyV2ShadowTrade,
    StrategyV2ShadowVersion,
)
from app.schemas import StrategyV2ShadowDailyEvidence
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


_NOW = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


class _AuditSpy:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, action: str, **details: Any) -> None:
        self.records.append((action, details))


class TestStrategyV2ShadowApi:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )
        cls.audit = _AuditSpy()
        cls.app = FastAPI()
        cls.app.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            db = cls.session_factory()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = override_get_db
        cls.app.dependency_overrides[get_audit_logger] = lambda: cls.audit
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        cls.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        self.audit.records.clear()
        with self.session_factory() as db:
            for model in (
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
                StrategyV2ShadowState,
                StrategyV2ShadowConfig,
                StrategyV2ShadowVersion,
                StrategyConfig,
            ):
                db.query(model).delete()
            db.add(
                StrategyConfig(
                    symbol="AAPL.US",
                    market="US",
                    fee_rate_us=0.0005,
                    fee_rate_hk=0.003,
                )
            )
            db.commit()

    def test_router_enforces_api_key_when_configured(self) -> None:
        settings.api_key = "shadow-secret"

        assert self.client.get("/api/strategy-shadow/config").status_code == 401
        response = self.client.get(
            "/api/strategy-shadow/config",
            headers={"X-API-Key": "shadow-secret"},
        )

        assert response.status_code == 200
        assert response.json()["mode"] == "SHADOW"

    def test_config_contract_update_audit_and_forbidden_hard_fields(self) -> None:
        response = self.client.get("/api/strategy-shadow/config")
        assert response.status_code == 200
        assert response.json()["enabled"] is False

        response = self.client.put(
            "/api/strategy-shadow/config",
            json={"enabled": True, "max_adx": 19.5},
            headers={"X-Forwarded-For": "192.0.2.10"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["max_adx"] == 19.5
        assert body["algorithm_version"] == "strategy-v2-rth-mr-v4-frozen-config"
        assert body["estimated_fee_rate_us"] == 0.0005
        assert body["mode"] == "SHADOW"
        assert body["order_submission_allowed"] is False
        assert len(self.audit.records) == 1
        action, details = self.audit.records[0]
        assert action == "STRATEGY_V2_SHADOW_UPDATE"
        assert details["result"] == "SUCCESS"
        assert set(details["request_summary"]["changed"]) == {"enabled", "max_adx"}

        rejected = self.client.put(
            "/api/strategy-shadow/config",
            json={"max_holding_minutes": 30},
        )
        assert rejected.status_code == 422
        with self.session_factory() as db:
            row = db.query(StrategyV2ShadowConfig).filter_by(symbol="AAPL.US").one()
            assert row.max_holding_minutes == 60

    def test_non_primary_shadow_configs_can_be_listed_and_disabled(self) -> None:
        created = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "MSFT.US"},
            json={"enabled": True},
        )
        assert created.status_code == 200
        assert created.json()["symbol"] == "MSFT.US"
        assert created.json()["enabled"] is True

        configs = self.client.get("/api/strategy-shadow/configs")
        assert configs.status_code == 200
        assert {item["symbol"] for item in configs.json()} == {
            "AAPL.US",
            "MSFT.US",
        }

        disabled = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "MSFT.US"},
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert self.client.get("/api/strategy-shadow/config").json()["symbol"] == "AAPL.US"

    def test_status_decision_and_trade_response_contracts(self) -> None:
        with self.session_factory() as db:
            service = StrategyV2ShadowService(db)
            config = service.get_config()
            decision = StrategyV2ShadowDecision(
                idempotency_key="api-contract-decision",
                symbol="AAPL.US",
                market="US",
                config_version=config.config_version,
                session_date=_NOW.date(),
                bar_at=_NOW,
                bar_at_5m=_NOW,
                observed_at=_NOW + timedelta(minutes=1, seconds=5),
                action="EXIT_LONG",
                reason="PROFIT_TARGET",
                state_before="LONG",
                state_after="READY",
                close_price=101.0,
                vwap_1m=100.2,
                zscore_1m=-0.2,
                vwap_5m=100.1,
                zscore_5m=-0.1,
                adx_5m=18.0,
                realized_vol_1m=0.25,
                gate_passed=False,
                breach_armed=False,
                virtual_position="FLAT",
                reference_price=101.0,
                quantity=1.0,
                gross_pnl=1.0,
                fee=0.1005,
                net_pnl=0.8995,
                exit_reason="PROFIT_TARGET",
                holding_minutes=5.0,
                mae_pct=-0.002,
                mfe_pct=0.011,
                gate_reasons_json=json.dumps(["ADX_TOO_HIGH"]),
            )
            db.add(decision)
            db.add(
                StrategyV2ShadowDecision(
                    idempotency_key="stale-config-decision",
                    symbol="AAPL.US",
                    market="US",
                    config_version="stale-config-version",
                    session_date=_NOW.date(),
                    bar_at=_NOW + timedelta(minutes=10),
                    observed_at=_NOW + timedelta(minutes=11),
                    action="WAIT",
                    reason="STALE_VERSION",
                    state_before="READY",
                    state_after="READY",
                    close_price=999.0,
                    gate_passed=False,
                    breach_armed=False,
                    virtual_position="FLAT",
                    gate_reasons_json=json.dumps(["STALE_GATE"]),
                )
            )
            db.add(
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version=config.config_version,
                    status="CLOSED",
                    entry_at=_NOW - timedelta(minutes=5),
                    exit_at=_NOW,
                    entry_price=100.0,
                    exit_price=101.0,
                    quantity=1.0,
                    entry_reason="NEXT_BAR_OPEN_FILL",
                    exit_reason="PROFIT_TARGET",
                    gross_pnl=1.0,
                    estimated_fees=0.1005,
                    net_pnl=0.8995,
                    mfe_amount=1.1,
                    mae_amount=-0.2,
                    mfe_pct=0.011,
                    mae_pct=-0.002,
                    holding_seconds=300.0,
                    fee_source="ESTIMATED",
                )
            )
            db.add(
                StrategyV2ShadowTrade(
                    symbol="AAPL.US",
                    config_version="stale-config-version",
                    status="CLOSED",
                    entry_at=_NOW,
                    exit_at=_NOW + timedelta(minutes=10),
                    entry_price=100.0,
                    exit_price=90.0,
                    quantity=1.0,
                    net_pnl=-10.0,
                    fee_source="ESTIMATED",
                )
            )
            db.commit()

        response = self.client.get("/api/strategy-shadow/status?symbol=AAPL.US")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "config",
            "latest",
            "metrics",
            "gate_counts",
            "phase",
            "last_polled_at",
            "last_poll_error",
        }
        assert set(body["latest"]) == {
            "observed_at",
            "data_age_seconds",
            "bar_timestamp_1m",
            "bar_timestamp_5m",
            "price",
            "vwap_1m",
            "zscore_1m",
            "vwap_5m",
            "zscore_5m",
            "adx",
            "realized_vol",
            "regime_eligible",
            "breach_armed",
            "virtual_position",
            "virtual_entry_price",
            "virtual_entry_at",
            "last_action",
            "last_reason",
        }
        assert body["latest"]["last_action"] == "EXIT_LONG"
        assert body["latest"]["price"] == 101.0
        assert body["latest"]["virtual_position"] == "FLAT"
        assert body["metrics"]["closed_trades"] == 1
        assert body["metrics"]["win_rate"] == 1.0
        assert body["metrics"]["net_pnl"] == 0.8995
        assert body["metrics"]["comparison_available"] is False
        assert body["metrics"]["live_action_count"] is None
        assert body["metrics"]["action_agreement_rate"] is None
        assert body["metrics"]["net_pnl_delta_vs_live"] is None
        assert body["gate_counts"] == {"ADX_TOO_HIGH": 1}

        decisions = self.client.get(
            "/api/strategy-shadow/decisions",
            params={
                "symbol": "aapl.us",
                "action": "exit_long",
                "from": (_NOW - timedelta(minutes=1)).isoformat(),
                "to": (_NOW + timedelta(minutes=1)).isoformat(),
                "page": 1,
                "page_size": 10,
            },
        )
        assert decisions.status_code == 200
        assert decisions.json()["total"] == 1
        assert decisions.json()["items"][0]["action"] == "EXIT_LONG"

        invalid_action = self.client.get(
            "/api/strategy-shadow/decisions",
            params={"symbol": "AAPL.US", "action": "BUY_NOW"},
        )
        assert invalid_action.status_code == 400
        reversed_range = self.client.get(
            "/api/strategy-shadow/decisions",
            params={
                "symbol": "AAPL.US",
                "from": (_NOW + timedelta(minutes=1)).isoformat(),
                "to": (_NOW - timedelta(minutes=1)).isoformat(),
            },
        )
        assert reversed_range.status_code == 400

        trades = self.client.get(
            "/api/strategy-shadow/trades",
            params={"symbol": "AAPL.US", "limit": 10},
        )
        assert trades.status_code == 200
        assert trades.json()[0]["fee_source"] == "ESTIMATED"

    def test_replay_is_explicitly_non_persistent(self) -> None:
        with self.session_factory() as db:
            before = self._shadow_counts(db)
        bars = [
            {
                "timestamp": (_NOW + timedelta(minutes=index)).isoformat(),
                "open": 100.0 + index * 0.1,
                "high": 100.2 + index * 0.1,
                "low": 99.8 + index * 0.1,
                "close": 100.1 + index * 0.1,
                "volume": 1000 + index,
            }
            for index in range(3)
        ]

        response = self.client.post(
            "/api/strategy-shadow/replay",
            json={"symbol": "AAPL.US", "market": "US", "bars": bars},
        )

        assert response.status_code == 200
        assert response.json()["persisted"] is False
        with self.session_factory() as db:
            assert self._shadow_counts(db) == before == (0, 0, 0, 0, 0)

    def test_adx_challenger_api_is_read_only_and_reports_missing_evidence(self) -> None:
        with self.session_factory() as db:
            empty_before = self._shadow_counts(db)
        missing_config = self.client.post(
            "/api/strategy-shadow/adx-challengers",
            json={"symbol": "AAPL.US"},
        )
        assert missing_config.status_code == 400
        with self.session_factory() as db:
            assert self._shadow_counts(db) == empty_before == (0, 0, 0, 0, 0)

        config = self.client.get(
            "/api/strategy-shadow/config",
            params={"symbol": "AAPL.US"},
        ).json()
        with self.session_factory() as db:
            before = self._shadow_counts(db)

        response = self.client.post(
            "/api/strategy-shadow/adx-challengers",
            json={
                "symbol": "AAPL.US",
                "config_version": config["config_version"],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["persisted"] is False
        assert body["mode"] == "SHADOW"
        assert body["order_submission_allowed"] is False
        assert body["evaluation_scope"] == "EXPLORATORY_IN_SAMPLE"
        assert body["promotion_eligible"] is False
        assert body["forward_validation_required"] is True
        assert body["status"] == "INSUFFICIENT_EVIDENCE"
        assert body["minimum_complete_sessions"] == 5
        assert body["observed_complete_sessions"] == 0
        assert body["baseline_replay_match"] is None
        assert body["blockers"] == ["MIN_COMPLETE_SESSIONS"]
        assert body["candidates"] == []
        with self.session_factory() as db:
            assert self._shadow_counts(db) == before

        missing = self.client.post(
            "/api/strategy-shadow/adx-challengers",
            json={
                "symbol": "AAPL.US",
                "config_version": "0" * 64,
            },
        )
        assert missing.status_code == 400

    def test_versions_and_evaluation_preserve_old_config_evidence(self) -> None:
        original = self.client.get("/api/strategy-shadow/config").json()
        old_version = original["config_version"]
        updated = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "AAPL.US"},
            json={"max_adx": 18.0},
        )
        assert updated.status_code == 200
        new_version = updated.json()["config_version"]
        assert new_version != old_version

        versions = self.client.get(
            "/api/strategy-shadow/versions",
            params={"symbol": "AAPL.US"},
        )
        assert versions.status_code == 200
        assert {item["config_version"] for item in versions.json()} == {
            old_version,
            new_version,
        }
        assert sum(item["current"] for item in versions.json()) == 1

        evaluation = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US", "config_version": old_version},
        )
        assert evaluation.status_code == 200
        body = evaluation.json()
        assert body["status"] == "COLLECTING"
        assert body["order_submission_allowed"] is False
        assert body["remaining_trading_days"] == 20
        assert body["remaining_closed_trades"] == 50

        missing = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US", "config_version": "0" * 64},
        )
        assert missing.status_code == 400

    def test_market_aware_window_capacity_validation(self) -> None:
        response = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "AAPL.US"},
            json={"zscore_window_5m_bars": 69},
        )
        assert response.status_code == 400
        assert "must not exceed 68" in response.json()["detail"]

    def test_hk_profit_target_must_cover_round_trip_costs_and_buffer(self) -> None:
        created = self.client.get(
            "/api/strategy-shadow/config",
            params={"symbol": "0700.HK"},
        )

        assert created.status_code == 200
        assert created.json()["profit_target_pct"] == pytest.approx(0.74)
        assert created.json()["order_submission_allowed"] is False

        rejected = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "0700.HK"},
            json={"profit_target_pct": 0.73},
        )
        accepted = self.client.put(
            "/api/strategy-shadow/config",
            params={"symbol": "0700.HK"},
            json={"profit_target_pct": 0.75},
        )

        assert rejected.status_code == 400
        assert "must be at least 0.7400%" in rejected.json()["detail"]
        assert accepted.status_code == 200
        assert accepted.json()["profit_target_pct"] == pytest.approx(0.75)
        assert accepted.json()["max_adx"] == pytest.approx(20.0)
        assert accepted.json()["order_submission_allowed"] is False

    def test_disabled_config_reports_disabled_even_with_state_row(self) -> None:
        self.client.get("/api/strategy-shadow/config")
        with self.session_factory() as db:
            db.add(StrategyV2ShadowState(symbol="AAPL.US", phase="READY"))
            db.commit()

        response = self.client.get(
            "/api/strategy-shadow/status",
            params={"symbol": "AAPL.US"},
        )

        assert response.status_code == 200
        assert response.json()["phase"] == "DISABLED"

    def test_legacy_evidence_version_is_backfilled_as_queryable(self) -> None:
        legacy_version = "a" * 64
        with self.session_factory() as db:
            db.add(StrategyV2ShadowDecision(
                idempotency_key="legacy-decision",
                symbol="AAPL.US",
                market="US",
                config_version=legacy_version,
                session_date=_NOW.date(),
                bar_at=_NOW,
                close_price=100.0,
            ))
            db.commit()

        versions = self.client.get(
            "/api/strategy-shadow/versions",
            params={"symbol": "AAPL.US"},
        )
        assert versions.status_code == 200
        legacy = next(
            item for item in versions.json()
            if item["config_version"] == legacy_version
        )
        assert legacy["params"]["parameters_available"] is False

        decisions = self.client.get(
            "/api/strategy-shadow/decisions",
            params={"symbol": "AAPL.US", "config_version": legacy_version},
        )
        assert decisions.status_code == 200
        assert decisions.json()["total"] == 1

    def test_partial_day_has_no_internal_gap_and_does_not_mature_sample(self) -> None:
        config = self.client.get("/api/strategy-shadow/config").json()
        with self.session_factory() as db:
            db.add(StrategyV2ShadowDecision(
                idempotency_key="partial-day-decision",
                symbol="AAPL.US",
                market="US",
                config_version=config["config_version"],
                session_date=_NOW.date(),
                bar_at=_NOW,
                close_price=100.0,
            ))
            db.commit()

        evaluation = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US"},
        ).json()

        assert evaluation["observed_trading_days"] == 0
        assert evaluation["daily"][0]["missing_internal_bars"] == 0
        assert evaluation["daily"][0]["partial_start"] is True
        assert evaluation["daily"][0]["partial_end"] is True

    def test_evaluation_requires_complete_data_and_profitable_quality(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = self.client.get("/api/strategy-shadow/config").json()

        def add_linked_trade(
            db: Session,
            *,
            key: str,
            entry_at: datetime,
            exit_at: datetime,
            exit_price: float,
            gross_pnl: float,
            net_pnl: float,
        ) -> None:
            entry = StrategyV2ShadowDecision(
                idempotency_key=f"{key}-entry",
                symbol="AAPL.US",
                market="US",
                config_version=config["config_version"],
                session_date=entry_at.date(),
                bar_at=entry_at,
                action="FILL_ENTRY",
                close_price=100.0,
            )
            exit_row = StrategyV2ShadowDecision(
                idempotency_key=f"{key}-exit",
                symbol="AAPL.US",
                market="US",
                config_version=config["config_version"],
                session_date=exit_at.date(),
                bar_at=exit_at,
                action="EXIT_LONG",
                close_price=exit_price,
            )
            db.add_all([entry, exit_row])
            db.flush()
            db.add(StrategyV2ShadowTrade(
                symbol="AAPL.US",
                config_version=config["config_version"],
                entry_decision_id=entry.id,
                exit_decision_id=exit_row.id,
                status="CLOSED",
                entry_at=entry_at,
                exit_at=exit_at,
                entry_price=100.0,
                exit_price=exit_price,
                quantity=1.0,
                gross_pnl=gross_pnl,
                estimated_fees=0.1,
                net_pnl=net_pnl,
                fee_source="ESTIMATED",
                estimated_fee_rate=0.0005,
            ))

        complete_days = [
            StrategyV2ShadowDailyEvidence(
                session_date=(_NOW - timedelta(days=index)).date(),
                first_bar_at=_NOW - timedelta(hours=1),
                last_bar_at=_NOW,
                bars=390,
                eligible_bars=10,
                expected_internal_bars=390,
                missing_internal_bars=0,
                coverage_ratio=1.0,
                trades=3,
                net_pnl=2.7,
                partial_start=False,
                partial_end=False,
                outside_session_bars=0,
                complete_session=True,
            )
            for index in range(20)
        ]
        evidence = list(complete_days)
        monkeypatch.setattr(
            StrategyV2ShadowService,
            "_daily_evidence",
            staticmethod(lambda _decisions, _trades: evidence),
        )
        with self.session_factory() as db:
            first_entry = _NOW - timedelta(minutes=90)
            for index in range(50):
                entry_at = first_entry + timedelta(minutes=index * 2)
                add_linked_trade(
                    db,
                    key=f"quality-{index}",
                    entry_at=entry_at,
                    exit_at=entry_at + timedelta(minutes=1),
                    exit_price=101.0,
                    gross_pnl=1.0,
                    net_pnl=0.9,
                )
            db.commit()

        ready = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US"},
        ).json()

        assert ready["status"] == "READY_FOR_REVIEW"
        assert ready["observed_trading_days"] == 20
        assert ready["closed_trades"] == 50
        assert ready["eligible_closed_trades"] == 50
        assert ready["excluded_closed_trades"] == 0
        assert ready["excluded_trading_days"] == 0
        assert ready["minimum_session_coverage_ratio"] == pytest.approx(0.995)
        assert ready["readiness_blockers"] == []
        assert ready["quality"]["total_net_pnl"] == pytest.approx(45.0)
        assert ready["quality"]["cost_stressed_net_pnl"] > 0

        with self.session_factory() as db:
            for trade in db.query(StrategyV2ShadowTrade).all():
                trade.exit_price = 99.0
                trade.gross_pnl = -1.0
                trade.net_pnl = -1.1
            db.commit()

        losing = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US"},
        ).json()
        assert losing["status"] == "COLLECTING"
        assert "NET_PNL_NON_POSITIVE" in losing["readiness_blockers"]

        with self.session_factory() as db:
            for trade in db.query(StrategyV2ShadowTrade).all():
                trade.exit_price = 101.0
                trade.gross_pnl = 1.0
                trade.net_pnl = 0.9
            db.commit()

        incomplete_day = _NOW + timedelta(days=3)
        evidence.append(complete_days[0].model_copy(update={
            "session_date": incomplete_day.date(),
            "coverage_ratio": 0.80,
            "partial_start": True,
            "complete_session": False,
        }))
        incomplete = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US"},
        ).json()
        assert incomplete["observed_trading_days"] == 20
        assert incomplete["excluded_trading_days"] == 1
        assert incomplete["status"] == "READY_FOR_REVIEW"
        assert "DATA_PARTIAL_SESSIONS" not in incomplete["readiness_blockers"]
        assert "DATA_SESSION_COVERAGE" not in incomplete["readiness_blockers"]
        assert any("partial session" in item for item in incomplete["data_quality_warnings"])

        with self.session_factory() as db:
            add_linked_trade(
                db,
                key="excluded-loss",
                entry_at=incomplete_day - timedelta(minutes=90),
                exit_at=incomplete_day - timedelta(minutes=89),
                exit_price=1.0,
                gross_pnl=-99.0,
                net_pnl=-99.1,
            )
            db.commit()

        excluded_loss = self.client.get(
            "/api/strategy-shadow/evaluation",
            params={"symbol": "AAPL.US"},
        ).json()
        assert excluded_loss["eligible_closed_trades"] == 50
        assert excluded_loss["excluded_closed_trades"] == 1
        assert excluded_loss["status"] == "COLLECTING"
        assert "DATA_TRADE_SESSION_INCOMPLETE" in excluded_loss["readiness_blockers"]

    @staticmethod
    def _shadow_counts(db: Session) -> tuple[int, int, int, int, int]:
        return (
            db.query(StrategyV2ShadowConfig).count(),
            db.query(StrategyV2ShadowVersion).count(),
            db.query(StrategyV2ShadowState).count(),
            db.query(StrategyV2ShadowDecision).count(),
            db.query(StrategyV2ShadowTrade).count(),
        )
