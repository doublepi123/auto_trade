"""Read-only runtime intervention evidence timeline.

Projects ONLY persisted, semantically explicit, SUCCESSFUL pause/resume and
kill-switch transitions. This is evidence, not a synthesized runtime-state
history.

Source predicates (finding A.1) — ONE shared SQL predicate per source, reused
identically for population count, bounded select, and exact filtered count:
* Manual source: successful authoritative ``AuditLog`` actions only
  (``action IN (...)`` AND ``result = 'SUCCESS'``), using the actual stored
  success casing/value.
* Automatic source: transition-specific ``RISK_AUTO_RESUMED`` only with
  successful statuses, normalized IN SQL (``upper(trim(coalesce(status,'')))``)
  — not filtered later in Python. Failed/other statuses affect neither scans,
  truncation, rows, nor totals.

Snapshot consistency (finding A.2): every metadata query needed for one
response (global source counts, bounded source selects, exact date-filtered
total) executes on ONE explicit read snapshot via the shared
``open_read_snapshot`` helper. A concurrent commit between any two of those
queries cannot split metadata.

Denominators (finding A.4):
* ``pairing_context_scanned`` — global rows loaded to establish pairing context.
* ``filtered_scanned`` — scanned rows matching the requested date filters;
  feeds summary classification.
* ``returned`` — rows after the response limit.
* ``total`` — exact complete filtered population from the same snapshot.

Bounded SQL counts/selects and incomplete-context UNKNOWN behavior are
preserved from the prior hardening.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.models import AuditLog, TradeEvent
from app.schemas import (
    InterventionEvidenceResponse,
    InterventionEvidenceRow,
    InterventionEvidenceSummary,
)
from app.services.snapshot_helper import open_read_snapshot

__all__ = ["InterventionEvidenceService"]

PAIRING_RULE = (
    "Manual pause/resume and kill-switch durations come ONLY from the "
    "authoritative audit stream (successful PAUSE/RESUME/KILL_SWITCH/"
    "DISABLE_KILL_SWITCH rows). Within each intervention family, an OPEN "
    "transition pairs with the next chronologically following CLOSE "
    "transition. A duration is reported only for an explicit, unambiguous "
    "open->close pair. A duplicate OPEN before the CLOSE makes the whole "
    "segment (every OPEN and the CLOSE) AMBIGUOUS with no duration; the "
    "segment only resets after that CLOSE. A CLOSE without a matching OPEN is "
    "UNMATCHED_CLOSE. Automatic TradeEvent evidence (RISK_AUTO_RESUMED) has no "
    "durable correlation key to the manual audit stream, so it never pairs "
    "with audit controls and never contributes a duration. Pairing is computed "
    "globally before date filters or the response limit are applied; if the "
    "hard scan cap is exceeded, pairing is incomplete and all durations are "
    "suppressed."
)

_MAX_SCAN_TRANSITIONS = 5000
_MAX_RESPONSE_ROWS = 1000

_Direction = Literal["open", "close"]
_Family = Literal["pause", "kill_switch"]
_Source = Literal["audit", "trade_auto"]


@dataclass(frozen=True)
class _Transition:
    source: _Source
    source_id: int
    timestamp: datetime
    family: _Family
    kind: str
    direction: _Direction
    action: str
    reason_code: str
    actor_hash: str | None


# AuditLog action -> (family, direction, kind, fixed reason code).
_AUDIT_TRANSITIONS: dict[str, tuple[_Family, _Direction, str, str]] = {
    "PAUSE": ("pause", "open", "PAUSE", "MANUAL_PAUSE"),
    "RESUME": ("pause", "close", "RESUME", "MANUAL_RESUME"),
    "KILL_SWITCH": ("kill_switch", "open", "KILL_SWITCH", "MANUAL_KILL_SWITCH"),
    "DISABLE_KILL_SWITCH": (
        "kill_switch",
        "close",
        "DISABLE_KILL_SWITCH",
        "MANUAL_KILL_SWITCH_DISABLE",
    ),
}

# TradeEvent event_type -> (family, direction, kind, fixed reason code).
_TRADE_AUTO_TRANSITIONS: dict[str, tuple[_Family, _Direction, str, str]] = {
    "RISK_AUTO_RESUMED": (
        "pause",
        "close",
        "RISK_AUTO_RESUMED",
        "AUTOMATIC_RESUME",
    ),
}

# Successful automatic statuses, normalized to upper-case for SQL comparison.
_TRADE_AUTO_SUCCESS_STATUS: frozenset[str] = frozenset(
    {"RUNNING", "SUCCESS", "OK"}
)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_range(
    from_date: date | None,
    to_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError("from_date must be on or before to_date")
    from_dt = (
        datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        if from_date is not None
        else None
    )
    to_dt = (
        datetime.combine(to_date, time.max, tzinfo=timezone.utc)
        if to_date is not None
        else None
    )
    return from_dt, to_dt


# ---------------------------------------------------------------------
# Shared SQL predicates (finding A.1) — one per source, reused identically
# for population count, bounded select, and exact filtered count.
# ---------------------------------------------------------------------
def _audit_predicate() -> ColumnElement[bool]:
    """Successful authoritative manual AuditLog control actions only."""
    return AuditLog.action.in_(tuple(_AUDIT_TRANSITIONS)) & (
        AuditLog.result == "SUCCESS"
    )


def _auto_status_normalized() -> Any:
    """Normalize TradeEvent.status in SQL: upper(trim(coalesce(status,'')))."""
    return func.upper(func.trim(func.coalesce(TradeEvent.status, "")))


def _auto_predicate() -> ColumnElement[bool]:
    """Transition-specific RISK_AUTO_RESUMED with successful status (SQL-normalized).

    Failed/other statuses are excluded IN SQL, so they affect neither scans,
    truncation, rows, nor totals.
    """
    return (TradeEvent.event_type == "RISK_AUTO_RESUMED") & (
        _auto_status_normalized().in_(tuple(_TRADE_AUTO_SUCCESS_STATUS))
    )


def _with_date_filters(
    base: ColumnElement[bool],
    *,
    model: type[AuditLog] | type[TradeEvent],
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> list[ColumnElement[bool]]:
    """Append inclusive UTC date predicates to a base predicate."""
    clauses: list[ColumnElement[bool]] = [base]
    if from_dt is not None:
        clauses.append(model.created_at >= from_dt)  # type: ignore[attr-defined]
    if to_dt is not None:
        clauses.append(model.created_at <= to_dt)  # type: ignore[attr-defined]
    return clauses


class InterventionEvidenceService:
    """Read-only projection of explicit intervention transitions."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def build(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 500,
    ) -> InterventionEvidenceResponse:
        """Return normalized chronological intervention evidence.

        Raises ``ValueError`` on an inverted date range. All metadata queries
        for one response run on ONE read snapshot. See the module docstring for
        the denominator contract.
        """
        capped_limit = max(1, min(int(limit), _MAX_RESPONSE_ROWS))
        _validate_range(from_date, to_date)
        from_dt, to_dt = _validate_range(from_date, to_date)

        def _query(connection: Connection) -> InterventionEvidenceResponse:
            return self._build_on_snapshot(
                connection=connection,
                from_dt=from_dt,
                to_dt=to_dt,
                from_date=from_date,
                to_date=to_date,
                capped_limit=capped_limit,
            )

        return open_read_snapshot(self._db, _query)

    # ------------------------------------------------------------------
    # One-snapshot build
    # ------------------------------------------------------------------
    def _build_on_snapshot(
        self,
        *,
        connection: Connection,
        from_dt: datetime | None,
        to_dt: datetime | None,
        from_date: date | None,
        to_date: date | None,
        capped_limit: int,
    ) -> InterventionEvidenceResponse:
        # --- Global population counts (shared predicate, same snapshot) ---
        audit_population = int(
            connection.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(_audit_predicate())
            )
            or 0
        )
        auto_population = int(
            connection.scalar(
                select(func.count())
                .select_from(TradeEvent)
                .where(_auto_predicate())
            )
            or 0
        )
        audit_truncated = audit_population > _MAX_SCAN_TRANSITIONS
        auto_truncated = auto_population > _MAX_SCAN_TRANSITIONS
        scan_truncated = audit_truncated or auto_truncated
        pairing_complete = not scan_truncated

        # --- Bounded selects (shared predicate, same snapshot) ---
        audit_all = self._select_audit_bounded(connection)
        auto_all = self._select_auto_bounded(connection)
        pairing_context_scanned = len(audit_all) + len(auto_all)

        # --- Pairing ---
        if pairing_complete:
            paired_audit = (
                self._pair_audit_stream(audit_all) if audit_all else []
            )
            auto_rows = [self._auto_unmatched(t) for t in auto_all]
            all_rows = paired_audit + auto_rows
        else:
            all_rows = [self._unknown_row(t) for t in audit_all]
            all_rows.extend(self._unknown_row(t) for t in auto_all)

        all_rows.sort(
            key=lambda r: (
                _to_utc(r.timestamp).timestamp(),
                r.source,
                r.source_id,
            )
        )

        # --- Date filters AFTER global pairing ---
        filtered = [
            r
            for r in all_rows
            if (from_dt is None or _to_utc(r.timestamp) >= from_dt)
            and (to_dt is None or _to_utc(r.timestamp) <= to_dt)
        ]
        filtered_scanned = len(filtered)

        # Extension point: a test can monkeypatch this no-op method to commit a
        # writer on a separate connection AFTER the bounded scan but BEFORE the
        # exact-total query, proving both observe the same snapshot. Narrow,
        # naturally factored hook — not a broad production-only test path.
        self._after_bounded_scan()

        # --- Exact complete filtered total (shared predicate, same snapshot) ---
        exact_total = self._exact_filtered_total(
            connection, from_dt=from_dt, to_dt=to_dt
        )

        summary = self._summarize(
            filtered,
            filtered_scanned=filtered_scanned,
            exact_total=exact_total,
            classification_complete=pairing_complete,
        )

        response_truncated = len(filtered) > capped_limit
        items = filtered[:capped_limit]
        truncated = response_truncated or scan_truncated

        return InterventionEvidenceResponse(
            items=items,
            summary=summary,
            total=exact_total,
            pairing_context_scanned=pairing_context_scanned,
            filtered_scanned=filtered_scanned,
            returned=len(items),
            truncated=truncated,
            pairing_complete=pairing_complete,
            scan_truncated=scan_truncated,
            classification_complete=pairing_complete,
            pairing_rule=PAIRING_RULE,
            filters=self._filters(
                from_date=from_date,
                to_date=to_date,
                limit=capped_limit,
            ),
        )

    # ------------------------------------------------------------------
    # Bounded selects (shared predicate)
    # ------------------------------------------------------------------
    def _select_audit_bounded(
        self,
        connection: Connection,
    ) -> list[_Transition]:
        # Core column select (Connection does not perform ORM mapping).
        stmt = (
            select(
                AuditLog.id,
                AuditLog.action,
                AuditLog.actor_hash,
                AuditLog.created_at,
            )
            .where(_audit_predicate())
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .limit(_MAX_SCAN_TRANSITIONS + 1)
        )
        out: list[_Transition] = []
        for row in connection.execute(stmt).all():
            mapped = _AUDIT_TRANSITIONS.get(row.action)
            if mapped is None:
                continue
            family, direction, kind, reason_code = mapped
            out.append(
                _Transition(
                    source="audit",
                    source_id=int(row.id),
                    timestamp=_to_utc(row.created_at),
                    family=family,
                    kind=kind,
                    direction=direction,
                    action=row.action,
                    reason_code=reason_code,
                    actor_hash=row.actor_hash or None,
                )
            )
        return out

    def _select_auto_bounded(
        self,
        connection: Connection,
    ) -> list[_Transition]:
        stmt = (
            select(
                TradeEvent.id,
                TradeEvent.event_type,
                TradeEvent.status,
                TradeEvent.created_at,
            )
            .where(_auto_predicate())
            .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
            .limit(_MAX_SCAN_TRANSITIONS + 1)
        )
        out: list[_Transition] = []
        for row in connection.execute(stmt).all():
            mapped = _TRADE_AUTO_TRANSITIONS.get(row.event_type)
            if mapped is None:
                continue
            # Status success is already enforced in SQL by _auto_predicate;
            # no Python-side status filtering remains.
            family, direction, kind, reason_code = mapped
            out.append(
                _Transition(
                    source="trade_auto",
                    source_id=int(row.id),
                    timestamp=_to_utc(row.created_at),
                    family=family,
                    kind=kind,
                    direction=direction,
                    action=row.event_type,
                    reason_code=reason_code,
                    actor_hash=None,
                )
            )
        return out

    def _exact_filtered_total(
        self,
        connection: Connection,
        *,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> int:
        """Exact complete filtered population (shared predicate, same snapshot)."""
        audit_clauses = _with_date_filters(
            _audit_predicate(),
            model=AuditLog,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        audit_count = int(
            connection.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(*audit_clauses)
            )
            or 0
        )
        auto_clauses = _with_date_filters(
            _auto_predicate(),
            model=TradeEvent,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        auto_count = int(
            connection.scalar(
                select(func.count())
                .select_from(TradeEvent)
                .where(*auto_clauses)
            )
            or 0
        )
        return audit_count + auto_count

    # ------------------------------------------------------------------
    # Pairing (authoritative audit stream only)
    # ------------------------------------------------------------------
    def _pair_audit_stream(
        self,
        stream: list[_Transition],
    ) -> list[InterventionEvidenceRow]:
        rows: list[InterventionEvidenceRow] = []
        pending: dict[_Family, list[_Transition]] = {
            "pause": [],
            "kill_switch": [],
        }

        for t in stream:
            bucket = pending[t.family]
            if t.direction == "open":
                bucket.append(t)
            else:  # close
                if not bucket:
                    rows.append(self._row(t, "UNMATCHED_CLOSE"))
                elif len(bucket) == 1:
                    open_t = bucket[0]
                    duration = self._duration(open_t, t)
                    if duration is None or duration <= 0:
                        rows.append(self._row(open_t, "AMBIGUOUS"))
                        rows.append(self._row(t, "AMBIGUOUS"))
                    else:
                        rows.append(
                            self._paired_row(
                                open_t, t, duration, open_is_open=True
                            )
                        )
                        rows.append(
                            self._paired_row(
                                t, open_t, duration, open_is_open=False
                            )
                        )
                    bucket.clear()
                else:
                    for open_t in bucket:
                        rows.append(self._row(open_t, "AMBIGUOUS"))
                    rows.append(self._row(t, "AMBIGUOUS"))
                    bucket.clear()

        for family_bucket in pending.values():
            for open_t in family_bucket:
                if len(family_bucket) > 1:
                    rows.append(self._row(open_t, "AMBIGUOUS"))
                else:
                    rows.append(self._row(open_t, "OPEN"))
        return rows

    # ------------------------------------------------------------------
    # Row builders
    # ------------------------------------------------------------------
    @staticmethod
    def _duration(open_t: _Transition, close_t: _Transition) -> float | None:
        return (
            _to_utc(close_t.timestamp).timestamp()
            - _to_utc(open_t.timestamp).timestamp()
        )

    @staticmethod
    def _row(t: _Transition, status: str) -> InterventionEvidenceRow:
        return InterventionEvidenceRow(
            source=t.source,
            source_id=t.source_id,
            timestamp=t.timestamp,
            family=t.family,
            kind=t.kind,
            direction=t.direction,
            reason=t.reason_code,
            action=t.action,
            actor_hash=t.actor_hash,
            pairing_status=status,  # type: ignore[arg-type]
            paired_source=None,
            paired_source_id=None,
            duration_seconds=None,
        )

    @staticmethod
    def _paired_row(
        t: _Transition,
        other: _Transition,
        duration: float,
        *,
        open_is_open: bool,
    ) -> InterventionEvidenceRow:
        return InterventionEvidenceRow(
            source=t.source,
            source_id=t.source_id,
            timestamp=t.timestamp,
            family=t.family,
            kind=t.kind,
            direction=t.direction,
            reason=t.reason_code,
            action=t.action,
            actor_hash=t.actor_hash,
            pairing_status="PAIRED",
            paired_source=other.source,
            paired_source_id=other.source_id,
            duration_seconds=duration if not open_is_open else None,
        )

    @staticmethod
    def _auto_unmatched(t: _Transition) -> InterventionEvidenceRow:
        status = "UNMATCHED_CLOSE" if t.direction == "close" else "OPEN"
        return InterventionEvidenceRow(
            source=t.source,
            source_id=t.source_id,
            timestamp=t.timestamp,
            family=t.family,
            kind=t.kind,
            direction=t.direction,
            reason=t.reason_code,
            action=t.action,
            actor_hash=t.actor_hash,
            pairing_status=status,  # type: ignore[arg-type]
            paired_source=None,
            paired_source_id=None,
            duration_seconds=None,
        )

    @staticmethod
    def _unknown_row(t: _Transition) -> InterventionEvidenceRow:
        return InterventionEvidenceRow(
            source=t.source,
            source_id=t.source_id,
            timestamp=t.timestamp,
            family=t.family,
            kind=t.kind,
            direction=t.direction,
            reason=t.reason_code,
            action=t.action,
            actor_hash=t.actor_hash,
            pairing_status="UNKNOWN",
            paired_source=None,
            paired_source_id=None,
            duration_seconds=None,
        )

    # ------------------------------------------------------------------
    # Summary / filters
    # ------------------------------------------------------------------
    def _after_bounded_scan(self) -> None:
        """No-op extension point between the bounded scan and exact-total query.

        Both run on the same snapshot; a test monkeypatches this to commit a
        writer on a separate connection, proving the bounded scan and the
        exact-total query observe the same committed state. Narrow, naturally
        factored hook — not a broad production-only test path.
        """
        return None

    @staticmethod
    def _summarize(
        rows: list[InterventionEvidenceRow],
        *,
        filtered_scanned: int,
        exact_total: int,
        classification_complete: bool,
    ) -> InterventionEvidenceSummary:
        paired_count = sum(1 for r in rows if r.pairing_status == "PAIRED")
        open_count = sum(1 for r in rows if r.pairing_status == "OPEN")
        unmatched_close_count = sum(
            1 for r in rows if r.pairing_status == "UNMATCHED_CLOSE"
        )
        ambiguous_count = sum(1 for r in rows if r.pairing_status == "AMBIGUOUS")
        unknown_count = sum(1 for r in rows if r.pairing_status == "UNKNOWN")
        paired_duration = sum(
            r.duration_seconds or 0.0
            for r in rows
            if r.pairing_status == "PAIRED" and r.direction == "close"
        )
        return InterventionEvidenceSummary(
            total_evidence=exact_total,
            scanned_evidence=filtered_scanned,
            classification_complete=classification_complete,
            paired_count=paired_count,
            open_count=open_count,
            unmatched_close_count=unmatched_close_count,
            ambiguous_count=ambiguous_count,
            unknown_count=unknown_count,
            paired_duration_seconds=paired_duration,
        )

    @staticmethod
    def _filters(
        *,
        from_date: date | None,
        to_date: date | None,
        limit: int,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"limit": limit}
        if from_date is not None:
            snapshot["from_date"] = from_date.isoformat()
        if to_date is not None:
            snapshot["to_date"] = to_date.isoformat()
        return snapshot
