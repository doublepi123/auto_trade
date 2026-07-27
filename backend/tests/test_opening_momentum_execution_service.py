from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base, OpeningMomentumExecution, OrderRecord
from app.services.opening_momentum_execution_service import (
    OpeningMomentumExecutionService,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumExecutionSignal,
    OpeningMomentumShadowService,
)


_SESSION_OPEN = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
_ENTRY_DUE = _SESSION_OPEN + timedelta(minutes=4)


class _FakeRunner:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []
        self.refresh_count = 0

    def execute_opening_momentum_entry(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return self.results.pop(0)

    def refresh_opening_execution_registry(self) -> None:
        self.refresh_count += 1


def _database() -> tuple[Engine, Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, Session(bind=engine)


def _enable_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "opening_momentum_challenger_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_paper_confirmed",
        True,
    )
    monkeypatch.setattr(
        settings,
        "full_buying_power_usage_enabled",
        True,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_max_entry_delay_seconds",
        30,
    )
    monkeypatch.setattr(
        settings,
        "opening_momentum_execution_max_price_deviation_bps",
        200.0,
    )


def _signal(service: OpeningMomentumExecutionService) -> OpeningMomentumExecutionSignal:
    identity = service._execution_identity()
    return OpeningMomentumExecutionSignal(
        session_date=date(2026, 7, 23),
        algorithm_version=identity.algorithm_version,
        config_version=identity.config_version,
        universe_source=identity.universe_source,
        selection_run_id=7,
        action="ENTER_LONG",
        reason="ENTER_LONG",
        symbol="NVDA.US",
        signal_at=_SESSION_OPEN + timedelta(minutes=2),
        entry_due_at=_ENTRY_DUE,
        universe_size=41,
        market_return_bps=5.0,
        candidate_return_bps=75.0,
        excess_return_bps=70.0,
        reference_entry_price=100.0,
        stop_loss_pct=1.0,
        max_holding_minutes=60,
        context={
            "ranking": ["NVDA.US", "AAPL.US"],
            "candidate_path_efficiency": 0.82,
        },
    )


def test_tick_submits_a_session_signal_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([{
        "executed": True,
        "status": "SUBMITTED",
        "order_id": "entry-1",
        "reason": "submitted",
    }])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        signal = _signal(service)
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: signal,
        )

        first = service.tick(now=_ENTRY_DUE)
        second = service.tick(now=_ENTRY_DUE + timedelta(seconds=10))

        assert len(runner.calls) == 1
        assert first.latest is not None
        assert runner.calls[0]["execution_id"] == first.latest.id
        assert runner.calls[0]["symbol"] == "NVDA.US"
        assert first.state == "SUBMITTED"
        assert first.latest.candidate_path_efficiency == pytest.approx(0.82)
        assert second.state == "SUBMITTED"
        assert second.latest is not None
        assert second.latest.submit_attempts == 1
        assert second.latest.entry_order_id == "entry-1"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_existing_open_execution_reserves_the_global_capital_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        old_signal = OpeningMomentumExecutionSignal(
            **{
                **_signal(service).__dict__,
                "session_date": date(2026, 7, 22),
                "signal_at": _SESSION_OPEN - timedelta(days=1),
                "entry_due_at": _ENTRY_DUE - timedelta(days=1),
            }
        )
        row = service._record_signal(
            old_signal,
            armed_at=_ENTRY_DUE - timedelta(days=1),
        )
        row.status = "OPEN"
        db.commit()
        evaluated: list[datetime | None] = []
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: evaluated.append(now),
        )

        status = service.tick(now=_ENTRY_DUE)

        assert status.state == "OPEN"
        assert status.latest is not None
        assert status.latest.id == row.id
        assert evaluated == []
        assert OpeningMomentumExecutionService.active_policies(db) == [row]
        assert runner.refresh_count >= 1
        assert db.query(OpeningMomentumExecution).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_armed_signal_is_an_active_quote_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    try:
        service = OpeningMomentumExecutionService(db)
        row = service._record_signal(_signal(service), armed_at=_ENTRY_DUE)
        db.commit()

        assert OpeningMomentumExecutionService.active_policies(db) == [row]
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_transient_quote_failure_retries_only_inside_entry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([
        {
            "executed": False,
            "status": "NO_QUOTE",
            "order_id": None,
            "reason": "quote unavailable",
        },
        {
            "executed": True,
            "status": "FILLED",
            "order_id": "entry-2",
            "reason": "filled",
        },
    ])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        signal = _signal(service)
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: signal,
        )

        armed = service.tick(now=_ENTRY_DUE)
        filled = service.tick(now=_ENTRY_DUE + timedelta(seconds=10))

        assert armed.state == "ARMED"
        assert filled.state == "OPEN"
        assert len(runner.calls) == 2
        assert filled.latest is not None
        assert filled.latest.submit_attempts == 2
        assert filled.latest.entry_order_id == "entry-2"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_submission_exception_is_fail_closed_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()

    class _RaisingRunner(_FakeRunner):
        def execute_opening_momentum_entry(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            self.calls.append(dict(kwargs))
            raise RuntimeError("unknown broker outcome")

    runner = _RaisingRunner([])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        signal = _signal(service)
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: signal,
        )

        first = service.tick(now=_ENTRY_DUE)
        second = service.tick(now=_ENTRY_DUE + timedelta(seconds=10))

        assert first.state == "UNCERTAIN"
        assert second.state == "UNCERTAIN"
        assert len(runner.calls) == 1
        assert first.latest is not None
        assert first.latest.reason == "ORDER_SUBMISSION_UNCERTAIN:RuntimeError"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_reconcile_links_entry_and_exit_by_execution_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    try:
        service = OpeningMomentumExecutionService(db)
        row = service._record_signal(_signal(service), armed_at=_ENTRY_DUE)
        row.status = "SUBMITTED"
        db.commit()
        provenance = json.dumps({
            "execution_signal": {"opening_execution_id": row.id}
        })
        entry_at = _ENTRY_DUE + timedelta(seconds=5)
        db.add(OrderRecord(
            broker_order_id="entry-linked",
            symbol="NVDA.US",
            side="BUY",
            quantity=10,
            price=100.1,
            executed_quantity=10,
            executed_price=100.1,
            status="FILLED",
            created_at=entry_at,
            filled_at=entry_at,
            config_snapshot=provenance,
        ))
        db.commit()

        OpeningMomentumExecutionService.reconcile_fill(
            db,
            symbol="NVDA.US",
            action="BUY",
        )
        db.commit()
        db.refresh(row)

        assert row.status == "OPEN"
        assert row.entry_order_id == "entry-linked"
        assert row.quantity == 10
        assert row.entry_price == 100.1

        exit_at = entry_at + timedelta(minutes=60)
        db.add(OrderRecord(
            broker_order_id="unrelated-exit",
            symbol="NVDA.US",
            side="SELL",
            quantity=10,
            price=99.0,
            executed_quantity=10,
            executed_price=99.0,
            status="FILLED",
            created_at=exit_at,
            filled_at=exit_at,
            exit_cause="MANUAL",
            net_pnl=-11.0,
        ))
        db.commit()

        OpeningMomentumExecutionService.reconcile_fill(
            db,
            symbol="NVDA.US",
            action="SELL",
        )
        db.commit()
        db.refresh(row)

        assert row.status == "OPEN"
        assert row.exit_order_id == ""

        db.add(OrderRecord(
            broker_order_id="exit-linked",
            symbol="NVDA.US",
            side="SELL",
            quantity=10,
            price=101.0,
            executed_quantity=10,
            executed_price=101.0,
            status="FILLED",
            created_at=exit_at,
            filled_at=exit_at,
            exit_cause="TIME_STOP",
            net_pnl=8.25,
            config_snapshot=provenance,
        ))
        db.commit()

        OpeningMomentumExecutionService.reconcile_fill(
            db,
            symbol="NVDA.US",
            action="SELL",
        )
        db.commit()
        db.refresh(row)

        assert row.status == "CLOSED"
        assert row.exit_order_id == "exit-linked"
        assert row.exit_price == 101.0
        assert row.net_pnl == 8.25
        assert OpeningMomentumExecutionService.active_policies(db) == []
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_skipped_signal_is_durable_and_never_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        base = _signal(service)
        skipped = OpeningMomentumExecutionSignal(
            **{
                **base.__dict__,
                "action": "SKIP",
                "reason": "MARKET_GATE",
                "symbol": None,
                "reference_entry_price": None,
            }
        )
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: skipped,
        )

        status = service.tick(now=_ENTRY_DUE)

        assert status.state == "SKIPPED"
        assert runner.calls == []
        assert db.query(OpeningMomentumExecution).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
