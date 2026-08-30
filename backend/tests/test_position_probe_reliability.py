from __future__ import annotations

import json
import os
import subprocess
import tempfile

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
from app.core import broker as broker_module
from app.core.broker import BrokerGateway
from app.core.engine import StrategyParams
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.core.position_probe_diagnostics import (
    PositionProbeDiagnostics,
    build_position_probe_error_payload,
)
from app.models import ReconciliationIncident, TrackedEntry, TradeEvent
from app.runner import AppRunner


class _RecordingNotifier(MultiChannelNotifier):
    def __init__(self) -> None:
        super().__init__([])

    def notify_risk_event(
        self,
        event_type: str,
        reason: str,
        *,
        severity: str | None = None,
    ) -> bool:
        del event_type, reason, severity
        return True


def test_position_probe_child_payload_keeps_redacted_sdk_root_cause() -> None:
    # Given
    class _SdkError(RuntimeError):
        code = 401
        category = "AUTHENTICATION"

    error = _SdkError(
        "access token expired; access_token=child-secret-value"
    )

    # When
    payload = build_position_probe_error_payload(error, retryable=False)

    # Then
    assert payload["error_type"] == "_SdkError"
    assert payload["sdk_error_code"] == "401"
    assert payload["sdk_error_category"] == "AUTHENTICATION"
    assert "expired" in payload["error_message"]
    assert "child-secret-value" not in payload["error_message"]
    assert "[REDACTED]" in payload["error_message"]


def test_position_probe_parent_captures_bounded_retry_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    calls = 0
    child_payload = json.dumps(
        {
            "status": "error",
            "error_type": "OpenApiException",
            "retryable": True,
            "sdk_error_code": "401",
            "sdk_error_category": "AUTHENTICATION",
            "error_message": "access token expired",
        }
    )

    def fake_run(
        command: tuple[str, ...],
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            17,
            stdout=child_payload,
            stderr=(
                "Authorization: Bearer parent-secret-value\n"
                + "sdk stderr " * 500
            ),
        )

    monkeypatch.setattr(
        broker_module.settings,
        "broker_position_snapshot_isolation_enabled",
        True,
    )
    monkeypatch.setattr(broker_module.settings, "broker_retry_max", 2)
    monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
    monkeypatch.setattr(broker_module.subprocess, "run", fake_run)

    # When
    with pytest.raises((RuntimeError, ConnectionError)) as captured:
        BrokerGateway().get_positions()

    # Then
    assert calls == 3, "probe diagnostics lost the actual bounded retry count"
    diagnostics = getattr(captured.value, "diagnostics", None)
    assert isinstance(diagnostics, PositionProbeDiagnostics)
    assert diagnostics.error_type == "OpenApiException"
    assert diagnostics.sdk_error_code == "401"
    assert diagnostics.sdk_error_category == "AUTHENTICATION"
    assert diagnostics.error_message == "access token expired"
    assert diagnostics.probe_duration_ms >= 0
    assert diagnostics.exit_code == 17
    assert diagnostics.retry_count == 2
    assert len(diagnostics.stderr) <= 1_024
    assert "parent-secret-value" not in diagnostics.stderr
    assert "[REDACTED]" in diagnostics.stderr


def test_position_probe_diagnostics_persist_in_reconciliation_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    child_payload = json.dumps(
        {
            "status": "error",
            "error_type": "OpenApiException",
            "retryable": False,
            "sdk_error_code": "401",
            "sdk_error_category": "AUTHENTICATION",
            "error_message": "access token expired",
        }
    )

    def fake_run(
        command: tuple[str, ...],
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=child_payload,
            stderr="access_token=incident-secret sdk failure",
        )

    monkeypatch.setattr(
        broker_module.settings,
        "broker_position_snapshot_isolation_enabled",
        True,
    )
    monkeypatch.setattr(broker_module.settings, "broker_retry_max", 3)
    monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
    monkeypatch.setattr(broker_module.subprocess, "run", fake_run)
    database.init_db()
    with database.SessionLocal() as db:
        db.query(ReconciliationIncident).delete()
        db.query(TradeEvent).delete()
        db.query(TrackedEntry).delete()
        db.commit()
    runner = AppRunner()
    runner._running = True
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US")
    runner.broker = BrokerGateway()
    runner.notifier = _RecordingNotifier()

    # When
    runner._reconcile_runtime_positions()

    # Then
    with database.SessionLocal() as db:
        incident = db.query(ReconciliationIncident).one()
    assert incident.error_type == "OpenApiException"
    assert incident.sdk_error_code == "401"
    assert incident.sdk_error_category == "AUTHENTICATION"
    assert incident.error_message == "access token expired"
    assert incident.probe_duration_ms is not None
    assert incident.probe_duration_ms >= 0
    assert incident.exit_code == 1
    assert incident.retry_count == 0
    assert len(incident.stderr) <= 1_024
    assert "incident-secret" not in incident.stderr
    assert "[REDACTED]" in incident.stderr
