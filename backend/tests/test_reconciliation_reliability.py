from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_DB_PATH = os.path.join(
    tempfile.gettempdir(),
    f"auto_trade_order_lifecycle_reliability_{os.getpid()}.db",
)
_DB_URL = f"sqlite:///{_DB_PATH}"
if os.environ.get("AUTO_TRADE_DATABASE_URL") != _DB_URL:
    for _path in (_DB_PATH, f"{_DB_PATH}-wal", f"{_DB_PATH}-shm"):
        if os.path.exists(_path):
            os.remove(_path)
    os.environ["AUTO_TRADE_DATABASE_URL"] = _DB_URL

from app import database
from app import runner as runner_module
from app.core.broker import BrokerGateway, Position
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.models import OrderRecord, ReconciliationIncident, TrackedEntry, TradeEvent
from app.runner import AppRunner
from app.services.reconciliation_incident_service import (
    RECONCILIATION_ALERT_EVENT_BOUND,
    ReconciliationFailure,
    ReconciliationIncidentService,
)


class _SequencedPositionBroker(BrokerGateway):
    def __init__(self, failures: int) -> None:
        self.calls = 0
        self._failures = failures

    def get_positions(self) -> list[Position]:
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("expired access token")
        return []


class _RecordingNotifier(MultiChannelNotifier):
    def __init__(self) -> None:
        super().__init__([])
        self.risk_events: list[tuple[str, str, str | None]] = []

    def notify_risk_event(
        self,
        event_type: str,
        reason: str,
        *,
        severity: str | None = None,
    ) -> bool:
        self.risk_events.append((event_type, reason, severity))
        return True


def _clear_reconciliation_state() -> None:
    database.init_db()
    with database.SessionLocal() as db:
        db.query(OrderRecord).delete()
        db.query(ReconciliationIncident).delete()
        db.query(TradeEvent).delete()
        db.query(TrackedEntry).delete()
        db.commit()


def test_identical_reconciliation_failure_storm_is_bounded() -> None:
    # Given
    _clear_reconciliation_state()
    runner = AppRunner()
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
    notifier = _RecordingNotifier()
    runner.notifier = notifier

    # When
    with database.SessionLocal() as db:
        for _ in range(1_000):
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="runtime_position_reconcile",
                position_snapshot_error=RuntimeError("expired access token"),
            )
        persisted_alert_events = db.query(TradeEvent).filter_by(
            event_type="TRACKED_ENTRY_RECOVERY_FAILED"
        ).count()

    # Then
    assert persisted_alert_events <= RECONCILIATION_ALERT_EVENT_BOUND, (
        "1,000 identical failures exceeded the declared persisted-alert bound "
        f"N={RECONCILIATION_ALERT_EVENT_BOUND}: {persisted_alert_events}"
    )
    assert notifier.risk_events == [
        ("POSITION_RECONCILIATION_FAILED", runner.risk.pause_reason, "CRITICAL")
    ]


@pytest.mark.parametrize(
    "snapshot_error",
    [
        TimeoutError("hard subprocess deadline"),
        ConnectionError("retryable broker transport failure"),
        RuntimeError("non-retryable expired access token"),
    ],
    ids=["timeout", "retryable", "non_retryable"],
)
def test_first_aggregate_event_preserves_snapshot_error_type(
    snapshot_error: Exception,
) -> None:
    # Given
    _clear_reconciliation_state()
    runner = AppRunner()
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
    runner.notifier = _RecordingNotifier()

    # When
    with database.SessionLocal() as db:
        for _ in range(2):
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="runtime_position_reconcile",
                position_snapshot_error=snapshot_error,
            )
        events = db.query(TradeEvent).filter_by(
            event_type="TRACKED_ENTRY_RECOVERY_FAILED"
        ).all()
        incident = db.query(ReconciliationIncident).one()

    # Then
    assert len(events) == 1, "forensic payload restoration defeated aggregation"
    payload = json.loads(events[0].payload_json)
    expected_type = type(snapshot_error).__name__
    assert payload["source"] == "runtime_position_reconcile"
    assert payload["symbols"] == ["NVDA.US"]
    assert payload["position_snapshot_error_type"] == expected_type
    assert payload["error_type"] == expected_type
    assert incident.occurrence_count == 2


def test_reconciliation_dedupe_alerts_for_a_different_symbol() -> None:
    # Given
    _clear_reconciliation_state()
    runner = AppRunner()
    notifier = _RecordingNotifier()
    runner.notifier = notifier

    # When
    with database.SessionLocal() as db:
        for symbol in ("AAPL.US", "NVDA.US"):
            runner.engine.params = StrategyParams(symbol=symbol, market="US")
            runner._reconcile_tracked_entries_with_broker(
                db,
                source="runtime_position_reconcile",
                position_snapshot_error=RuntimeError("expired access token"),
            )

    # Then
    assert len(notifier.risk_events) == 2, (
        "a different reconciliation symbol was over-suppressed"
    )


def test_reconciliation_incident_reminders_decay_on_fixed_tiers() -> None:
    # Given
    _clear_reconciliation_state()
    service = ReconciliationIncidentService(first_reminder_seconds=300.0)
    failure = ReconciliationFailure(
        source="runtime_position_reconcile",
        category="BROKER_POSITION_SNAPSHOT_FAILED",
        symbols=("NVDA.US",),
        message="position reconciliation failed",
        error_type="RuntimeError",
    )
    started_at = datetime(2026, 8, 30, tzinfo=timezone.utc)

    # When
    with database.SessionLocal() as db:
        decisions = []
        for seconds in (0, 299, 300, 1_799, 1_800, 5_399, 5_400):
            result = service.record_failure(
                db,
                failure,
                now=started_at + timedelta(seconds=seconds),
            )
            decisions.append(result.should_notify)
            db.commit()
        event_types = [
            event.event_type
            for event in db.query(TradeEvent).order_by(TradeEvent.id).all()
        ]

    # Then
    assert decisions == [True, False, True, False, True, False, True]
    assert event_types == [
        "TRACKED_ENTRY_RECOVERY_FAILED",
        "TRACKED_ENTRY_RECOVERY_REMINDER",
        "TRACKED_ENTRY_RECOVERY_REMINDER",
        "TRACKED_ENTRY_RECOVERY_REMINDER",
    ]


def test_reconciliation_backoff_keeps_entry_gate_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _clear_reconciliation_state()
    now = [100.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: now[0])
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
    broker = _SequencedPositionBroker(failures=10)
    runner.broker = broker
    runner.notifier = _RecordingNotifier()

    # When
    runner._reconcile_runtime_positions()
    now[0] = 120.0
    runner._reconcile_runtime_positions()
    now[0] = 140.0
    runner._reconcile_runtime_positions()

    # Then
    assert broker.calls == 2, "persistent reconciliation failure did not back off"
    assert runner._check_reconciliation_gate() is False
    assert runner.risk.check().approved is False


def test_successful_reconciliation_emits_one_recovery_and_restores_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _clear_reconciliation_state()
    now = [100.0]
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: now[0])
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
    broker = _SequencedPositionBroker(failures=2)
    notifier = _RecordingNotifier()
    runner.broker = broker
    runner.notifier = notifier

    # When
    runner._reconcile_runtime_positions()
    now[0] = 120.0
    runner._reconcile_runtime_positions()
    now[0] = 500.0
    runner._reconcile_runtime_positions()
    now[0] = 516.0
    runner._reconcile_runtime_positions()

    # Then
    recovery_events = [
        event
        for event in notifier.risk_events
        if event[0] == "POSITION_RECONCILIATION_RECOVERED"
    ]
    assert len(recovery_events) == 1, (
        "successful reconciliation did not emit exactly one recovery notification"
    )
    assert runner._check_reconciliation_gate() is True
    assert runner.risk.check().approved is True
    assert broker.calls == 4, "healthy reconciliation cadence was not restored"
