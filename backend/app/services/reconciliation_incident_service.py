from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy.orm import Session

from app.core.position_probe_diagnostics import PositionProbeDiagnostics
from app.models import ReconciliationIncident
from app.services.trade_event_service import record_trade_event

RECONCILIATION_ALERT_EVENT_BOUND: Final = 10
_THIRTY_MINUTES_SECONDS: Final = 1_800.0
_ONE_HOUR_SECONDS: Final = 3_600.0


@dataclass(frozen=True, slots=True)
class ReconciliationFailure:
    source: str
    category: str
    symbols: tuple[str, ...]
    message: str
    error_type: str
    diagnostics: PositionProbeDiagnostics | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationIncidentResult:
    should_notify: bool
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class ReconciliationRecovery:
    message: str
    occurrence_count: int


class ReconciliationIncidentService:
    def __init__(self, first_reminder_seconds: float) -> None:
        self._first_reminder_seconds = max(0.0, first_reminder_seconds)

    def record_failure(
        self,
        db: Session,
        failure: ReconciliationFailure,
        *,
        now: datetime | None = None,
    ) -> ReconciliationIncidentResult:
        observed_at = now or datetime.now(timezone.utc)
        source = failure.source.strip().lower()
        category = failure.category.strip().upper()
        symbols_json = json.dumps(
            sorted(set(failure.symbols)),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        incident = db.query(ReconciliationIncident).filter_by(
            source=source,
            failure_category=category,
            symbols_json=symbols_json,
        ).first()
        if incident is None or incident.recovered_at is not None:
            if incident is None:
                incident = ReconciliationIncident(
                    source=source,
                    failure_category=category,
                    symbols_json=symbols_json,
                )
                db.add(incident)
            incident.occurrence_count = 1
            incident.alert_count = 1
            incident.first_seen_at = observed_at
            incident.last_seen_at = observed_at
            incident.last_alerted_at = observed_at
            incident.next_alert_at = observed_at + timedelta(
                seconds=self._first_reminder_seconds
            )
            incident.recovered_at = None
            self._apply_failure_details(incident, failure)
            self._record_alert_event(db, incident, event_type="TRACKED_ENTRY_RECOVERY_FAILED")
            return ReconciliationIncidentResult(True, 1)

        incident.occurrence_count += 1
        incident.last_seen_at = observed_at
        self._apply_failure_details(incident, failure)
        if observed_at < self._as_utc(incident.next_alert_at):
            return ReconciliationIncidentResult(
                False,
                incident.occurrence_count,
            )

        incident.alert_count += 1
        incident.last_alerted_at = observed_at
        incident.next_alert_at = self._next_alert_at(incident, observed_at)
        self._record_alert_event(
            db,
            incident,
            event_type="TRACKED_ENTRY_RECOVERY_REMINDER",
        )
        return ReconciliationIncidentResult(True, incident.occurrence_count)

    def record_recovery(
        self,
        db: Session,
        *,
        source: str,
        category: str,
        now: datetime | None = None,
    ) -> tuple[ReconciliationRecovery, ...]:
        observed_at = now or datetime.now(timezone.utc)
        incidents = db.query(ReconciliationIncident).filter_by(
            source=source.strip().lower(),
            failure_category=category.strip().upper(),
            recovered_at=None,
        ).all()
        recoveries: list[ReconciliationRecovery] = []
        for incident in incidents:
            incident.recovered_at = observed_at
            incident.last_seen_at = observed_at
            symbols = json.loads(incident.symbols_json)
            message = (
                "broker position reconciliation recovered for "
                f"{', '.join(symbols)} after "
                f"{incident.occurrence_count} failed probes"
            )
            record_trade_event(
                db,
                event_type="TRACKED_ENTRY_RECOVERY_RECOVERED",
                status="RECOVERED",
                message=message,
                payload={
                    "source": incident.source,
                    "failure_category": incident.failure_category,
                    "symbols": symbols,
                    "occurrence_count": incident.occurrence_count,
                    "first_seen_at": incident.first_seen_at,
                    "last_seen_at": incident.last_seen_at,
                    "recovered_at": observed_at,
                },
            )
            recoveries.append(
                ReconciliationRecovery(message, incident.occurrence_count)
            )
        return tuple(recoveries)

    def _next_alert_at(
        self,
        incident: ReconciliationIncident,
        observed_at: datetime,
    ) -> datetime:
        first_seen_at = self._as_utc(incident.first_seen_at)
        if incident.alert_count == 2:
            return max(
                observed_at,
                first_seen_at + timedelta(seconds=_THIRTY_MINUTES_SECONDS),
            )
        return max(
            observed_at + timedelta(seconds=_ONE_HOUR_SECONDS),
            first_seen_at + timedelta(seconds=_ONE_HOUR_SECONDS),
        )

    @staticmethod
    def _record_alert_event(
        db: Session,
        incident: ReconciliationIncident,
        *,
        event_type: str,
    ) -> None:
        symbols = json.loads(incident.symbols_json)
        record_trade_event(
            db,
            event_type=event_type,
            status="ERROR",
            message=incident.message,
            payload={
                "source": incident.source,
                "failure_category": incident.failure_category,
                "symbols": symbols,
                "occurrence_count": incident.occurrence_count,
                "alert_count": incident.alert_count,
                "first_seen_at": incident.first_seen_at,
                "last_seen_at": incident.last_seen_at,
                "position_snapshot_error_type": incident.error_type,
                "error_type": incident.error_type,
                "sdk_error_code": incident.sdk_error_code,
                "sdk_error_category": incident.sdk_error_category,
                "error_message": incident.error_message,
                "probe_duration_ms": incident.probe_duration_ms,
                "exit_code": incident.exit_code,
                "retry_count": incident.retry_count,
                "stderr": incident.stderr,
            },
        )

    @staticmethod
    def _apply_failure_details(
        incident: ReconciliationIncident,
        failure: ReconciliationFailure,
    ) -> None:
        diagnostics = failure.diagnostics
        incident.message = failure.message
        incident.error_type = (
            diagnostics.error_type
            if diagnostics is not None
            else failure.error_type
        )
        incident.sdk_error_code = (
            diagnostics.sdk_error_code if diagnostics is not None else ""
        )
        incident.sdk_error_category = (
            diagnostics.sdk_error_category if diagnostics is not None else ""
        )
        incident.error_message = (
            diagnostics.error_message if diagnostics is not None else ""
        )
        incident.probe_duration_ms = (
            diagnostics.probe_duration_ms if diagnostics is not None else None
        )
        incident.exit_code = (
            diagnostics.exit_code if diagnostics is not None else None
        )
        incident.retry_count = (
            diagnostics.retry_count if diagnostics is not None else 0
        )
        incident.stderr = diagnostics.stderr if diagnostics is not None else ""

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
