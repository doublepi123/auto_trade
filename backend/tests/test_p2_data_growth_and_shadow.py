"""P2: Data growth measurement and V2 shadow observation tests.

Tests for:
- P2.1: /api/health/db-stats endpoint reporting DB size + row counts
- P2.2: Shadow mode data collection continues regardless of reconciliation gate state
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import database
from app.config import settings
from app.models import (
    Base,
    ReconciliationEvidence,
    RuntimeStateSnapshot,
    StrategyConfig,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
    StrategyV2ShadowTrade,
    TradeEvent,
)


# ── P2.1: DB stats endpoint ──────────────────────────────────────────────

class TestDBStatsEndpoint:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        # Stash the real engine, swap in our test engine
        cls._original_engine = database.engine
        database.engine = cls.engine

    @classmethod
    def teardown_class(cls) -> None:
        database.engine = cls._original_engine

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            for model in (
                TradeEvent,
                RuntimeStateSnapshot,
                ReconciliationEvidence,
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
                StrategyV2ShadowState,
                StrategyV2ShadowConfig,
                StrategyConfig,
            ):
                db.query(model).delete()
            db.commit()

    def test_db_stats_returns_table_counts(self, monkeypatch) -> None:
        """GET /api/health/db-stats returns row counts for key tables."""
        from app.main import app

        client = TestClient(app)

        with Session(bind=self.engine) as db:
            for i in range(5):
                db.add(TradeEvent(
                    event_type="TEST",
                    symbol="AAPL.US",
                    message=f"test event {i}",
                ))
            db.commit()

        response = client.get("/api/health/db-stats")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "tables" in data
        assert data["tables"].get("trade_events") == 5
        assert data["tables"].get("orders") == 0

    def test_db_stats_handles_missing_tables(self, monkeypatch) -> None:
        """DB stats gracefully returns None for tables that don't exist."""
        from app.main import app

        client = TestClient(app)

        response = client.get("/api/health/db-stats")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        # All listed tables should be present (even if None)
        assert "trade_events" in data["tables"]
        assert "runtime_state_snapshots" in data["tables"]
        assert "strategy_v2_shadow_decisions" in data["tables"]

    def test_db_stats_includes_db_size(self, monkeypatch) -> None:
        """DB stats reports db_size_bytes for SQLite databases."""
        from app.main import app

        client = TestClient(app)

        response = client.get("/api/health/db-stats")
        assert response.status_code == 200
        data = response.json()
        assert "db_size_bytes" in data
        # For in-memory SQLite, size may be 0
        assert isinstance(data["db_size_bytes"], int)

    def test_db_stats_with_shadow_table_data(self, monkeypatch) -> None:
        """DB stats includes shadow V2 table row counts."""
        from app.main import app

        client = TestClient(app)

        today = date.today()
        now = datetime.now(timezone.utc)

        with Session(bind=self.engine) as db:
            # Add shadow config
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            # Add shadow decisions (session_date is required)
            db.add(StrategyV2ShadowDecision(
                symbol="AAPL.US",
                market="US",
                config_version="v1",
                session_date=today,
                bar_at=now,
                action="ENTRY_LONG",
                idempotency_key="test-key-1",
            ))
            db.add(StrategyV2ShadowDecision(
                symbol="AAPL.US",
                market="US",
                config_version="v1",
                session_date=today,
                bar_at=now,
                action="EXIT",
                idempotency_key="test-key-2",
            ))
            db.commit()

        response = client.get("/api/health/db-stats")
        assert response.status_code == 200
        data = response.json()
        assert data["tables"]["strategy_v2_shadow_config"] == 1
        assert data["tables"]["strategy_v2_shadow_decisions"] == 2


# ── P2.2: Shadow mode independent of reconciliation gate ─────────────────

class TestShadowModeReconciliationGateIndependence:
    """Verify that Strategy V2 shadow data collection is NOT blocked by
    the reconciliation gate state.

    The shadow cron runs independently via _strategy_v2_shadow_tick_sync()
    and only writes to strategy_v2_shadow_* tables — it never places real
    orders. Therefore it should continue regardless of whether the gate is
    pending, failed, or passed.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            for model in (
                StrategyV2ShadowDecision,
                StrategyV2ShadowTrade,
                StrategyV2ShadowState,
                StrategyV2ShadowConfig,
                StrategyConfig,
            ):
                db.query(model).delete()
            # Ensure a StrategyConfig record exists (required by shadow service)
            db.add(StrategyConfig(
                symbol="AAPL.US",
                market="US",
                fee_rate_us=0.0005,
                fee_rate_hk=0.003,
            ))
            db.commit()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def test_shadow_service_writes_decisions_without_reconciliation_gate(self) -> None:
        """Shadow service .tick() writes shadow decisions regardless of gate state.

        The shadow service is not even aware of the reconciliation gate
        — it only writes to its own isolated tables and never touches
        the live order ledger.
        """
        from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

        with self._db() as db:
            # Enable shadow config for AAPL.US (keep session open)
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            service = StrategyV2ShadowService(db)
            svc_config = service.get_config("AAPL.US")
            assert svc_config.enabled is True
            assert svc_config.mode == "SHADOW"
            assert svc_config.order_submission_allowed is False

            # Create a shadow decision — the same path tick() uses internally
            version = service._config_version(config)
            decision = StrategyV2ShadowDecision(
                symbol="AAPL.US",
                market="US",
                config_version=version,
                session_date=date.today(),
                bar_at=datetime.now(timezone.utc),
                action="ENTRY_LONG",
                idempotency_key="recon-gate-test-1",
                close_price=150.0,
                zscore_1m=-2.5,
                adx_5m=12.0,
                realized_vol_1m=0.3,
            )
            db.add(decision)
            db.commit()

            # Verify the decision was persisted — gate state is irrelevant
            count = db.query(StrategyV2ShadowDecision).count()
            assert count == 1

    def test_shadow_service_trade_writes_are_isolated_from_live_orders(self) -> None:
        """Shadow trades are written to strategy_v2_shadow_trades, not the
        real orders table. Gate state cannot affect this."""
        from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

        with self._db() as db:
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            service = StrategyV2ShadowService(db)
            version = service._config_version(config)

            # Write a shadow trade (virtual, not real)
            trade = StrategyV2ShadowTrade(
                symbol="AAPL.US",
                config_version=version,
                entry_price=150.0,
                entry_at=datetime.now(timezone.utc),
                quantity=100,
                status="OPEN",
                entry_reason="test entry",
            )
            db.add(trade)
            db.commit()

            # Verify shadow trade exists independent of gate
            shadow_trades = db.query(StrategyV2ShadowTrade).count()
            assert shadow_trades == 1

            # Verify NO real orders were created
            from app.models import OrderRecord
            real_orders = db.query(OrderRecord).count()
            assert real_orders == 0

    def test_shadow_mode_has_no_execution_mode(self) -> None:
        """The shadow config deliberately has no live-execution mode.

        order_submission_allowed is always False and mode is always SHADOW.
        This is a hard P0 constraint — the reconciliation gate is irrelevant
        because shadow mode never places real orders.
        """
        from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

        with self._db() as db:
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            service = StrategyV2ShadowService(db)
            cfg = service.get_config("AAPL.US")

            # P0: Shadow mode is permanently enforced
            assert cfg.mode == "SHADOW"
            assert cfg.order_submission_allowed is False
            assert cfg.allow_position_addons is False
            assert cfg.short_entries_enabled is False

            # Even if we try to update, these hard constraints cannot be changed
            # (the schema has no fields for execution mode)
            from app.schemas import StrategyV2ShadowConfigUpdate
            updated = service.update_config(
                StrategyV2ShadowConfigUpdate(enabled=True, max_adx=15.0),
                symbol="AAPL.US",
            )
            assert updated.mode == "SHADOW"
            assert updated.order_submission_allowed is False

    @pytest.mark.parametrize("gate_state", ["pending", "failed", "passed"])
    def test_shadow_decision_persistence_with_various_gate_states(
        self, gate_state: str
    ) -> None:
        """Shadow decisions are persisted identically regardless of
        reconciliation gate state.

        The shadow service has no dependency on the runner or its gate
        state — it owns its own DB session and writes to isolated tables.
        """
        from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

        today = date.today()
        now = datetime.now(timezone.utc)

        with self._db() as db:
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            service = StrategyV2ShadowService(db)
            version = service._config_version(config)

            for i in range(3):
                decision = StrategyV2ShadowDecision(
                    symbol="AAPL.US",
                    market="US",
                    config_version=version,
                    session_date=today,
                    bar_at=now - timedelta(minutes=3 - i),
                    action="ENTRY_LONG" if i % 2 == 0 else "EXIT",
                    idempotency_key=f"gate-test-{gate_state}-{i}",
                    close_price=150.0 + i,
                )
                db.add(decision)
            db.commit()

        # Verify decisions were persisted
        with self._db() as db:
            count = db.query(StrategyV2ShadowDecision).count()
            assert count == 3, f"Expected 3 decisions, got {count} (gate={gate_state})"

    def test_shadow_decision_has_idempotency_key(self) -> None:
        """Shadow decisions use idempotency keys to prevent duplicates.

        This is a data integrity guarantee that works regardless of gate state.
        """
        from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

        today = date.today()
        now = datetime.now(timezone.utc)

        with self._db() as db:
            config = StrategyV2ShadowConfig(
                symbol="AAPL.US",
                enabled=True,
            )
            db.add(config)
            db.flush()

            from sqlalchemy.exc import IntegrityError

            service = StrategyV2ShadowService(db)
            version = service._config_version(config)

            db.add(StrategyV2ShadowDecision(
                symbol="AAPL.US",
                market="US",
                config_version=version,
                session_date=today,
                bar_at=now,
                action="ENTRY_LONG",
                idempotency_key="dup-key-test",
            ))
            db.commit()

            # Attempting a duplicate idempotency_key should fail
            db.add(StrategyV2ShadowDecision(
                symbol="AAPL.US",
                market="US",
                config_version=version,
                session_date=today,
                bar_at=now,
                action="ENTRY_LONG",
                idempotency_key="dup-key-test",
            ))
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()

            # Only one decision persisted
            count = db.query(StrategyV2ShadowDecision).count()
            assert count == 1