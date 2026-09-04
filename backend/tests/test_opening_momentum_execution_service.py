from __future__ import annotations

import json
from dataclasses import replace
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


_SESSION_OPEN = datetime(2026, 7, 29, 13, 30, tzinfo=timezone.utc)
_ENTRY_DUE = _SESSION_OPEN + timedelta(minutes=4)


class _FakeRunner:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []
        self.refresh_count = 0
        self.refresh_sessions: list[object] = []

    def execute_opening_momentum_entry(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return self.results.pop(0)

    def refresh_opening_execution_registry(
        self,
        db: object = None,
    ) -> None:
        self.refresh_count += 1
        self.refresh_sessions.append(db)


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
        session_date=_SESSION_OPEN.date(),
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
            "candidate_signal_turnover": 25_000_000.0,
            "candidate_avg_dollar_volume": 1_000_000_000.0,
            "candidate_signal_turnover_ratio": 0.025,
        },
    )


def test_paper_execution_uses_the_exceptional_path_identity() -> None:
    engine, db = _database()
    try:
        identity = OpeningMomentumExecutionService(
            db,
            None,
            _FakeRunner([]),
        )._execution_identity()

        assert identity.variant == (
            "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
        )
        assert identity.universe_source == (
            "OPENING_EXECUTION_WEAK_BREADTH_EXCEPTIONAL_PATH"
        )
        assert identity.minimum_path_efficiency == 0.70
        assert identity.maximum_market_return_bps == 0.0
        assert identity.exceptional_minimum_path_efficiency == 0.90
        assert identity.exceptional_maximum_market_return_bps == 5.0
        assert identity.forward_evidence_start_date == date(2026, 7, 28)
        assert identity.effective_maximum_market_return_bps(0.89) == 0.0
        assert identity.effective_maximum_market_return_bps(0.90) == 5.0
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_status_exposes_resolved_universe_and_fails_closed_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    try:
        service = OpeningMomentumExecutionService(db)
        identity = service._execution_identity()
        symbols = tuple(
            f"TEST{index}.US"
            for index in range(
                identity.decision_config.minimum_universe_size
            )
        )
        populated = replace(
            identity,
            symbols=symbols,
            selection_run_id=7,
        )
        monkeypatch.setattr(
            service,
            "_execution_variant",
            lambda: populated,
        )

        ready = service.get_status()

        assert ready.config.universe_source == identity.universe_source
        assert ready.config.selection_run_id == 7
        assert ready.config.universe_size == len(symbols)
        assert ready.config.universe == list(symbols)
        assert ready.config.universe_ready is True
        assert ready.config.order_submission_allowed is True

        monkeypatch.setattr(
            service,
            "_execution_variant",
            lambda: replace(populated, universe_source="NONE", symbols=()),
        )

        unavailable = service.get_status()

        assert unavailable.config.universe_source == "NONE"
        assert unavailable.config.universe_size == 0
        assert unavailable.config.universe_ready is False
        assert unavailable.config.order_submission_allowed is False
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_does_not_evaluate_before_forward_evidence_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    evaluated: list[datetime | None] = []
    try:
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: evaluated.append(now),
        )

        status = OpeningMomentumExecutionService(
            db,
            None,
            runner,
        ).tick(
            now=datetime(2026, 7, 27, 13, 34, tzinfo=timezone.utc),
        )

        assert status.state == "WAITING"
        assert status.latest is None
        assert status.config.forward_evidence_start_date == date(2026, 7, 28)
        assert evaluated == []
        assert db.query(OpeningMomentumExecution).count() == 0
        assert runner.refresh_count == 1
        assert runner.refresh_sessions == [db], (
            "the registry refresh must borrow the tick's own session, not "
            "make the runner open a second one"
        )
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_records_a_missed_entry_window_once_without_market_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    evaluated: list[datetime | None] = []
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        identity = service._execution_identity()
        symbols = tuple(
            f"TEST{index}.US"
            for index in range(
                identity.decision_config.minimum_universe_size
            )
        )
        populated = replace(
            identity,
            symbols=symbols,
            selection_run_id=7,
        )
        monkeypatch.setattr(
            service,
            "_execution_variant",
            lambda: populated,
        )
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: evaluated.append(now),
        )
        observed_at = _ENTRY_DUE + timedelta(seconds=31)

        first = service.tick(now=observed_at)
        second = service.tick(now=observed_at + timedelta(seconds=1))

        assert first.state == "EXPIRED"
        assert first.latest is not None
        assert first.latest.reason == "ENTRY_WINDOW_MISSED"
        assert first.latest.selection_run_id == 7
        assert first.latest.signal_at.replace(tzinfo=timezone.utc) == (
            _SESSION_OPEN + timedelta(minutes=2)
        )
        assert first.latest.entry_due_at.replace(tzinfo=timezone.utc) == (
            _ENTRY_DUE
        )
        assert first.latest.entry_deadline_at.replace(
            tzinfo=timezone.utc,
        ) == (
            _ENTRY_DUE + timedelta(seconds=30)
        )
        assert first.latest.armed_at.replace(tzinfo=timezone.utc) == (
            observed_at
        )
        assert first.latest.universe_size == len(symbols)
        assert first.latest.signal_context == {
            "entry_deadline_at": (
                _ENTRY_DUE + timedelta(seconds=30)
            ).isoformat(),
            "entry_window_missed": True,
            "observed_at": observed_at.isoformat(),
            "universe": list(symbols),
            "universe_ready": True,
        }
        assert second.state == "EXPIRED"
        assert second.latest is not None
        assert second.latest.id == first.latest.id
        assert evaluated == []
        assert runner.calls == []
        assert db.query(OpeningMomentumExecution).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_at_entry_deadline_still_evaluates_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    evaluated: list[datetime | None] = []
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

        def evaluate(
            _self: OpeningMomentumShadowService,
            *,
            now: datetime | None = None,
        ) -> OpeningMomentumExecutionSignal:
            evaluated.append(now)
            return skipped

        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            evaluate,
        )
        deadline = _ENTRY_DUE + timedelta(seconds=30)

        status = service.tick(now=deadline)

        assert status.state == "SKIPPED"
        assert status.latest is not None
        assert status.latest.reason == "MARKET_GATE"
        assert evaluated == [deadline]
        assert runner.calls == []
        assert db.query(OpeningMomentumExecution).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_tick_records_unavailable_preopen_universe_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    evaluated: list[datetime | None] = []
    try:
        monkeypatch.setattr(
            OpeningMomentumShadowService,
            "evaluate_execution_signal",
            lambda _self, *, now=None: evaluated.append(now),
        )

        status = OpeningMomentumExecutionService(
            db,
            None,
            runner,
        ).tick(now=_ENTRY_DUE + timedelta(seconds=31))

        assert status.state == "EXPIRED"
        assert status.latest is not None
        assert status.latest.reason == "PREOPEN_UNIVERSE_UNAVAILABLE"
        assert status.latest.universe_source == "NONE"
        assert status.latest.selection_run_id is None
        assert status.latest.universe_size == 0
        assert status.latest.signal_context["universe_ready"] is False
        assert evaluated == []
        assert runner.calls == []
        assert db.query(OpeningMomentumExecution).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_status_ignores_execution_from_a_superseded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    try:
        service = OpeningMomentumExecutionService(db)
        legacy = service._record_signal(
            _signal(service),
            armed_at=_ENTRY_DUE,
        )
        legacy.config_version = "superseded-opening-policy"
        legacy.algorithm_version = "superseded-opening-policy"
        legacy.universe_source = "SUPERSEDED_OPENING_POLICY"
        legacy.status = "SKIPPED"
        db.commit()

        status = service.get_status()

        assert status.state == "WAITING"
        assert status.latest is None
        assert status.config.universe_source == "NONE"
        assert status.config.algorithm_version == (
            service._execution_identity().algorithm_version
        )
        assert status.config.universe_ready is False
        assert status.config.order_submission_allowed is False
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


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
        assert first.latest.candidate_signal_turnover == pytest.approx(
            25_000_000.0
        )
        assert first.latest.candidate_avg_dollar_volume == pytest.approx(
            1_000_000_000.0
        )
        assert first.latest.candidate_signal_turnover_ratio == pytest.approx(
            0.025
        )
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


@pytest.mark.parametrize("retry_status", ["NO_QUOTE", "QUOTE_DEVIATION"])
def test_transient_quote_failure_retries_only_inside_entry_window(
    monkeypatch: pytest.MonkeyPatch,
    retry_status: str,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([
        {
            "executed": False,
            "status": retry_status,
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


@pytest.mark.parametrize("retry_status", ["NO_QUOTE", "QUOTE_DEVIATION"])
def test_transient_quote_failure_expires_without_another_submission(
    monkeypatch: pytest.MonkeyPatch,
    retry_status: str,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([
        {
            "executed": False,
            "status": retry_status,
            "order_id": None,
            "reason": "transient final quote failure",
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
        assert armed.latest is not None
        deadline = armed.latest.entry_deadline_at.replace(
            tzinfo=timezone.utc
        )
        expired = service.tick(now=deadline + timedelta(seconds=1))

        assert armed.state == "ARMED"
        assert expired.state == "EXPIRED"
        assert expired.latest is not None
        assert expired.latest.reason == "ENTRY_WINDOW_EXPIRED"
        assert expired.latest.submit_attempts == 1
        assert len(runner.calls) == 1
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_unrelated_skipped_order_is_rejected_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([
        {
            "executed": False,
            "status": "SKIPPED",
            "order_id": None,
            "reason": "an unrelated order policy rejected submission",
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

        rejected = service.tick(now=_ENTRY_DUE)
        unchanged = service.tick(
            now=_ENTRY_DUE + timedelta(seconds=10)
        )

        assert rejected.state == "REJECTED"
        assert unchanged.state == "REJECTED"
        assert rejected.latest is not None
        assert rejected.latest.reason == (
            "an unrelated order policy rejected submission"
        )
        assert rejected.latest.submit_attempts == 1
        assert len(runner.calls) == 1
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


def test_stale_submitting_with_unresolved_order_id_becomes_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_execution(monkeypatch)
    engine, db = _database()
    runner = _FakeRunner([])
    try:
        service = OpeningMomentumExecutionService(db, None, runner)
        row = service._record_signal(_signal(service), armed_at=_ENTRY_DUE)
        row.status = "SUBMITTING"
        row.reason = "ENTRY_SUBMITTING"
        row.requested_at = _ENTRY_DUE
        row.entry_order_id = "broker-order-not-in-local-ledger"
        db.commit()
        deadline = row.entry_deadline_at.replace(tzinfo=timezone.utc)

        status = service.tick(now=deadline + timedelta(seconds=61))

        assert status.state == "UNCERTAIN"
        assert status.latest is not None
        assert status.latest.id == row.id
        assert status.latest.entry_order_id == (
            "broker-order-not-in-local-ledger"
        )
        assert status.latest.reason == "SUBMISSION_RESULT_UNAVAILABLE"
        assert runner.calls == []
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
