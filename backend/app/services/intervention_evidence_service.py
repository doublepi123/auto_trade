"""Read-only runtime intervention evidence timeline.

Projects ONLY persisted, semantically explicit, SUCCESSFUL pause/resume and
kill-switch transitions. This is evidence, not a synthesized runtime-state
history.

Authoritative source selection (finding A):
* Manual pause/resume and kill-switch transitions use ONE authoritative
  persisted source: successful ``AuditLog`` control actions
  (``PAUSE`` / ``RESUME`` / ``KILL_SWITCH`` / ``DISABLE_KILL_SWITCH`` with
  ``result = 'SUCCESS'``). These are explicit and carry pseudonymous
  actor/result evidence.
* Duplicate manual-control ``TradeEvent`` rows (``CONTROL_PAUSE`` etc.) are
  EXCLUDED from pairing/duration — they duplicate the authoritative audit row
  without a durable correlation key.
* ``RISK_PAUSED`` is EXCLUDED: the current writer uses it for generic risk
  rejections, so it is not transition proof.
* A ``TradeEvent`` automatic transition is included ONLY when its writer is
  transition-specific and successful (``RISK_AUTO_RESUMED``). Such automatic
  evidence NEVER pairs with manual audit controls (no durable correlation key)
  and therefore remains unpaired/unknown — it never contributes a duration and
  is never double-counted against the audit stream.
* ``reason`` is a FIXED reason/category code derived from the whitelisted
  action/event type. Free-form event messages and payloads are never exposed or
  truncated as a "safe reason". Actor output is pseudonymous only.

Pairing/range/limit truthfulness (finding B):
* The authoritative audit transition history is paired BEFORE date filters or
  the response limit are applied, so cross-date pairs never become synthetic
  OPEN / UNMATCHED_CLOSE states.
* The database scan is explicitly bounded by a hard cap. If the cap is
  exceeded, ``pairing_complete=False`` / ``scan_truncated=True`` are returned
  and ALL durations are suppressed (never silently partial-pair).
* Duplicate-open segments are wholly ambiguous: ``OPEN, OPEN, CLOSE`` produces
  no duration for ANY of those transitions; the ambiguous segment only resets
  after the close. Conflicting sequences remain unknown rather than repaired
  heuristically.
* Date filtering is applied AFTER global/conservative pairing; the response
  limit is applied LAST. ``total`` / ``truncated`` / pairing-context
  completeness tell callers whether rows were omitted. The summary describes
  the full filtered population before the response limit, not only returned
  rows.
* Durations are never double-counted across sources: manual durations come
  only from the authoritative audit stream; uncorrelated automatic evidence
  remains unpaired/unknown.
* Stable chronological ordering with source/ID tie-breakers is preserved, and
  ``RuntimeState`` is never read or mutated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, TradeEvent
from app.schemas import (
    InterventionEvidenceResponse,
    InterventionEvidenceRow,
    InterventionEvidenceSummary,
)

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

# Hard cap on the number of authoritative audit transitions scanned for
# pairing. If the filtered population exceeds this, complete pairing context
# cannot be guaranteed, so we report scan_truncated=True and suppress all
# durations rather than silently partial-pairing.
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
# These are the AUTHORITATIVE manual control actions.
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
# ONLY automatic, transition-specific, successful writers are included.
# RISK_PAUSED is deliberately excluded (generic risk rejection per writer).
# Manual CONTROL_* are deliberately excluded (duplicate the authoritative
# audit stream without a durable correlation key).
_TRADE_AUTO_TRANSITIONS: dict[str, tuple[_Family, _Direction, str, str]] = {
    "RISK_AUTO_RESUMED": (
        "pause",
        "close",
        "RISK_AUTO_RESUMED",
        "AUTOMATIC_RESUME",
    ),
}

# A TradeEvent automatic transition is only transition-proof when its writer
# reports success. The runner writes status="RUNNING" for a verified resume.
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

        Raises ``ValueError`` on an inverted date range. ``limit`` is bounded
        to ``[1, 1000]``. Pairing is computed over the full authoritative
        population BEFORE date filters / limit; date filtering and the
        response limit are applied afterward so cross-date pairs stay intact
        and the summary describes the full filtered population.
        """
        capped_limit = max(1, min(int(limit), _MAX_RESPONSE_ROWS))
        # Validate the response date range up front (also used to filter the
        # final paired rows). The pairing scan itself is global (unfiltered by
        # these dates) so cross-date pairs are preserved.
        _validate_range(from_date, to_date)

        # --- Collect the full authoritative + automatic populations (global) ---
        audit_all, audit_truncated = self._collect_audit_global()
        auto_all = self._collect_auto_global()

        scan_truncated = audit_truncated
        pairing_complete = not scan_truncated

        # --- Pair the authoritative audit stream globally ---
        paired_audit = self._pair_audit_stream(audit_all) if audit_all else []

        # Automatic evidence never pairs with the manual audit stream (no
        # durable correlation key). It is always reported as unpaired/unknown.
        auto_rows = [self._auto_unmatched(t) for t in auto_all]

        # --- Merge and order all rows chronologically ---
        all_rows = paired_audit + auto_rows
        all_rows.sort(
            key=lambda r: (
                _to_utc(r.timestamp).timestamp(),
                r.source,
                r.source_id,
            )
        )

        # --- Apply date filters AFTER global pairing ---
        from_dt, to_dt = _validate_range(from_date, to_date)
        filtered = [
            r
            for r in all_rows
            if (from_dt is None or _to_utc(r.timestamp) >= from_dt)
            and (to_dt is None or _to_utc(r.timestamp) <= to_dt)
        ]

        # If the scan was truncated, complete pairing context could not be
        # guaranteed: suppress ALL durations truthfully.
        if scan_truncated:
            filtered = [self._suppress_duration(r) for r in filtered]

        total = len(filtered)
        summary = self._summarize(filtered)

        # --- Apply response limit LAST ---
        truncated = len(filtered) > capped_limit
        items = filtered[:capped_limit]

        return InterventionEvidenceResponse(
            items=items,
            summary=summary,
            total=total,
            truncated=truncated,
            pairing_complete=pairing_complete,
            scan_truncated=scan_truncated,
            pairing_rule=PAIRING_RULE,
            filters=self._filters(
                from_date=from_date,
                to_date=to_date,
                limit=capped_limit,
            ),
        )

    # ------------------------------------------------------------------
    # Collection (global; date filters are applied after pairing)
    # ------------------------------------------------------------------
    def _collect_audit_global(
        self,
    ) -> tuple[list[_Transition], bool]:
        """Collect ALL successful authoritative audit control transitions.

        Returns the transitions and whether the hard scan cap was exceeded.
        Only ``result = 'SUCCESS'`` rows are transition proof.
        """
        count_stmt = (
            select(AuditLog.id)
            .where(AuditLog.action.in_(tuple(_AUDIT_TRANSITIONS)))
            .where(AuditLog.result == "SUCCESS")
        )
        population = int(self._db.execute(count_stmt).all().__len__())
        truncated = population > _MAX_SCAN_TRANSITIONS

        stmt = (
            select(AuditLog)
            .where(AuditLog.action.in_(tuple(_AUDIT_TRANSITIONS)))
            .where(AuditLog.result == "SUCCESS")
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .limit(_MAX_SCAN_TRANSITIONS + 1)
        )
        rows = list(self._db.scalars(stmt))
        out: list[_Transition] = []
        for row in rows:
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
        return out, truncated

    def _collect_auto_global(self) -> list[_Transition]:
        """Collect automatic TradeEvent transitions (transition-specific only).

        Included only when the writer reports success (``RISK_AUTO_RESUMED``
        with a success status). These never pair with the manual audit stream.
        """
        stmt = (
            select(TradeEvent)
            .where(TradeEvent.event_type.in_(tuple(_TRADE_AUTO_TRANSITIONS)))
            .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
        )
        rows = list(self._db.scalars(stmt))
        out: list[_Transition] = []
        for row in rows:
            mapped = _TRADE_AUTO_TRANSITIONS.get(row.event_type)
            if mapped is None:
                continue
            # Only count a successful automatic transition as evidence.
            status = (row.status or "").strip().upper()
            if status not in _TRADE_AUTO_SUCCESS_STATUS:
                continue
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

    # ------------------------------------------------------------------
    # Pairing (authoritative audit stream only)
    # ------------------------------------------------------------------
    def _pair_audit_stream(
        self,
        stream: list[_Transition],
    ) -> list[InterventionEvidenceRow]:
        """Conservative pairing of the authoritative audit stream.

        Pairing is within each intervention family (pause / kill_switch).
        Duplicate-open segments are wholly ambiguous: ``OPEN, OPEN, CLOSE``
        produces no duration for ANY of those transitions. The ambiguous
        segment only resets after the close. A close with no candidate open is
        UNMATCHED_CLOSE. A trailing open with no close is OPEN.
        """
        rows: list[InterventionEvidenceRow] = []
        # Pending opens per family. When more than one open accumulates before
        # a close, the whole segment is ambiguous.
        pending: dict[_Family, list[_Transition]] = {"pause": [], "kill_switch": []}

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
                        # Close did not strictly follow open -> ambiguous.
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
                    # Duplicate-open segment: every pending open AND the close
                    # are ambiguous; no duration for any of them.
                    for open_t in bucket:
                        rows.append(self._row(open_t, "AMBIGUOUS"))
                    rows.append(self._row(t, "AMBIGUOUS"))
                    bucket.clear()

        # Trailing opens with no close (per family).
        for family_bucket in pending.values():
            for idx, open_t in enumerate(family_bucket):
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
        # Duration is attached only to the close row of the pair.
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
        # Automatic evidence has no durable correlation key to the manual
        # audit stream, so it is always unpaired/unknown.
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
    def _suppress_duration(
        r: InterventionEvidenceRow,
    ) -> InterventionEvidenceRow:
        if r.duration_seconds is None and r.pairing_status != "PAIRED":
            return r
        # Pairing context is incomplete: demote any paired row to ambiguous and
        # drop its duration so no unsupported duration is reported.
        return r.model_copy(
            update={
                "pairing_status": "AMBIGUOUS",
                "paired_source": None,
                "paired_source_id": None,
                "duration_seconds": None,
            }
        )

    # ------------------------------------------------------------------
    # Summary / filters
    # ------------------------------------------------------------------
    @staticmethod
    def _summarize(
        rows: list[InterventionEvidenceRow],
    ) -> InterventionEvidenceSummary:
        paired_count = sum(1 for r in rows if r.pairing_status == "PAIRED")
        open_count = sum(1 for r in rows if r.pairing_status == "OPEN")
        unmatched_close_count = sum(
            1 for r in rows if r.pairing_status == "UNMATCHED_CLOSE"
        )
        ambiguous_count = sum(1 for r in rows if r.pairing_status == "AMBIGUOUS")
        # Only explicit, unambiguous close rows carry a duration; sum those.
        paired_duration = sum(
            r.duration_seconds or 0.0
            for r in rows
            if r.pairing_status == "PAIRED" and r.direction == "close"
        )
        return InterventionEvidenceSummary(
            total_evidence=len(rows),
            paired_count=paired_count,
            open_count=open_count,
            unmatched_close_count=unmatched_close_count,
            ambiguous_count=ambiguous_count,
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
