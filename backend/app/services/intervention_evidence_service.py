"""Read-only runtime intervention evidence timeline.

Projects ONLY persisted, semantically explicit pause/resume and kill-switch
transitions from ``trade_events`` and ``audit_logs``. This is evidence, not a
synthesized runtime-state history:

* Gaps are never inferred from ``RuntimeState`` (the current mutable state is
  not consulted and is never mutated).
* Only whitelisted, explicit transition event/action names are projected.
* A duration is reported ONLY for an explicit, unambiguous open→close pair under
  the documented conservative pairing rule. Duplicate opens/closes, conflicting
  transitions, unmatched endpoints, and cross-source duplicates (which have no
  durable correlation key) remain unknown/ambiguous and never contribute a
  duration. The summary therefore distinguishes known paired duration from
  unknown/open/ambiguous evidence rather than presenting a synthetic total.

Whitelisted explicit transitions (inspected from the live control paths in
``app/api/trade.py`` and ``app/runner.py``):

* TradeEvent ``event_type``:
    - ``CONTROL_PAUSE``            -> pause  open
    - ``CONTROL_RESUME``           -> pause  close
    - ``CONTROL_KILL_SWITCH``      -> kill   open
    - ``CONTROL_DISABLE_KILL_SWITCH`` -> kill close
    - ``RISK_PAUSED``              -> pause  open  (status ``PAUSED``)
    - ``RISK_AUTO_RESUMED``        -> pause  close (status ``RUNNING``)
* AuditLog ``action``:
    - ``PAUSE``                    -> pause  open
    - ``RESUME``                   -> pause  close
    - ``KILL_SWITCH``              -> kill   open
    - ``DISABLE_KILL_SWITCH``      -> kill   close

Each source is paired independently. Cross-source duplicates have no durable
correlation key, so they are reported as separate evidence rows and never
double-count a duration.
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
    "Within each source and intervention family, an OPEN transition pairs with "
    "the next chronologically following CLOSE transition. A duration is "
    "reported only for an explicit, unambiguous open->close pair. A second OPEN "
    "before the CLOSE, a conflicting transition, or a CLOSE without a matching "
    "OPEN yields AMBIGUOUS / UNMATCHED_CLOSE with no duration. Cross-source "
    "duplicates have no durable correlation key and are never merged; durations "
    "are never double-counted across sources."
)

_MAX_EVIDENCE_ROWS = 1000

_Direction = Literal["open", "close"]
_Family = Literal["pause", "kill_switch"]


@dataclass(frozen=True)
class _Transition:
    source: Literal["trade", "audit"]
    source_id: int
    timestamp: datetime
    family: _Family
    kind: str
    direction: _Direction
    reason: str
    action: str
    actor_hash: str | None


# TradeEvent event_type -> (family, direction, kind)
_TRADE_TRANSITIONS: dict[str, tuple[_Family, _Direction, str]] = {
    "CONTROL_PAUSE": ("pause", "open", "CONTROL_PAUSE"),
    "CONTROL_RESUME": ("pause", "close", "CONTROL_RESUME"),
    "CONTROL_KILL_SWITCH": ("kill_switch", "open", "CONTROL_KILL_SWITCH"),
    "CONTROL_DISABLE_KILL_SWITCH": (
        "kill_switch",
        "close",
        "CONTROL_DISABLE_KILL_SWITCH",
    ),
    "RISK_PAUSED": ("pause", "open", "RISK_PAUSED"),
    "RISK_AUTO_RESUMED": ("pause", "close", "RISK_AUTO_RESUMED"),
}

# AuditLog action -> (family, direction, kind)
_AUDIT_TRANSITIONS: dict[str, tuple[_Family, _Direction, str]] = {
    "PAUSE": ("pause", "open", "PAUSE"),
    "RESUME": ("pause", "close", "RESUME"),
    "KILL_SWITCH": ("kill_switch", "open", "KILL_SWITCH"),
    "DISABLE_KILL_SWITCH": ("kill_switch", "close", "DISABLE_KILL_SWITCH"),
}


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


def _safe_text(value: str | None, limit: int = 200) -> str:
    if not value:
        return ""
    # Bound reason/action text. Never expose exception bodies or payloads —
    # only the short human-facing reason/message/action already on the row.
    return value.strip()[:limit]


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
        to ``[1, 1000]``. Stable ordering is timestamp asc, then source asc,
        then source_id asc, so identical timestamps are deterministic.
        """
        capped = max(1, min(int(limit), _MAX_EVIDENCE_ROWS))
        from_dt, to_dt = _validate_range(from_date, to_date)

        transitions = self._collect(from_dt=from_dt, to_dt=to_dt)
        # Stable chronological order with source/ID tie-breakers.
        transitions.sort(
            key=lambda t: (_to_utc(t.timestamp).timestamp(), t.source, t.source_id)
        )

        rows = self._pair(transitions)
        # Apply the row cap AFTER pairing so pairing is computed over the full
        # filtered set; the cap only bounds the response payload.
        rows = rows[:capped]

        summary = self._summarize(rows)
        return InterventionEvidenceResponse(
            items=rows,
            summary=summary,
            pairing_rule=PAIRING_RULE,
            filters=self._filters(from_date=from_date, to_date=to_date, limit=capped),
        )

    def _collect(
        self,
        *,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> list[_Transition]:
        transitions: list[_Transition] = []
        transitions.extend(self._collect_trade(from_dt=from_dt, to_dt=to_dt))
        transitions.extend(self._collect_audit(from_dt=from_dt, to_dt=to_dt))
        return transitions

    def _collect_trade(
        self,
        *,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> list[_Transition]:
        stmt = select(TradeEvent).where(
            TradeEvent.event_type.in_(tuple(_TRADE_TRANSITIONS))
        )
        if from_dt is not None:
            stmt = stmt.where(TradeEvent.created_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(TradeEvent.created_at <= to_dt)
        rows = self._db.scalars(stmt)
        out: list[_Transition] = []
        for row in rows:
            mapped = _TRADE_TRANSITIONS.get(row.event_type)
            if mapped is None:
                continue
            family, direction, kind = mapped
            out.append(
                _Transition(
                    source="trade",
                    source_id=int(row.id),
                    timestamp=_to_utc(row.created_at),
                    family=family,
                    kind=kind,
                    direction=direction,
                    reason=_safe_text(row.message),
                    action=row.event_type,
                    actor_hash=None,
                )
            )
        return out

    def _collect_audit(
        self,
        *,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> list[_Transition]:
        stmt = select(AuditLog).where(
            AuditLog.action.in_(tuple(_AUDIT_TRANSITIONS))
        )
        if from_dt is not None:
            stmt = stmt.where(AuditLog.created_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(AuditLog.created_at <= to_dt)
        rows = self._db.scalars(stmt)
        out: list[_Transition] = []
        for row in rows:
            mapped = _AUDIT_TRANSITIONS.get(row.action)
            if mapped is None:
                continue
            family, direction, kind = mapped
            out.append(
                _Transition(
                    source="audit",
                    source_id=int(row.id),
                    timestamp=_to_utc(row.created_at),
                    family=family,
                    kind=kind,
                    direction=direction,
                    reason="",
                    action=row.action,
                    # Pseudonymous actor only; never raw API key/IP material.
                    actor_hash=row.actor_hash or None,
                )
            )
        return out

    def _pair(
        self,
        transitions: list[_Transition],
    ) -> list[InterventionEvidenceRow]:
        """Conservative within-source, within-family pairing.

        For each (source, family) stream, walk chronologically and pair an OPEN
        with the next CLOSE. A second OPEN before the CLOSE marks the first
        OPEN AMBIGUOUS (and the new OPEN becomes the candidate). A CLOSE with
        no candidate OPEN is UNMATCHED_CLOSE. A CLOSE that matches a candidate
        OPEN yields PAIRED for both with a duration. Any OPEN still candidate
        at end-of-stream is OPEN (no duration).
        """
        rows: list[InterventionEvidenceRow] = []

        # Group preserving already-sorted order within each group.
        streams: dict[tuple[Literal["trade", "audit"], _Family], list[_Transition]] = {}
        for t in transitions:
            streams.setdefault((t.source, t.family), []).append(t)

        results_by_key: dict[
            tuple[Literal["trade", "audit"], _Family],
            list[tuple[_Transition, InterventionEvidenceRow]],
        ] = {}
        for key, stream in streams.items():
            results_by_key[key] = self._pair_stream(stream)

        # Re-merge across streams into the original global chronological order.
        indexed: dict[tuple[str, int], InterventionEvidenceRow] = {}
        for paired_list in results_by_key.values():
            for transition, row in paired_list:
                indexed[(transition.source, transition.source_id)] = row
        for t in transitions:
            row = indexed.get((t.source, t.source_id))
            if row is not None:
                rows.append(row)
        return rows

    @staticmethod
    def _pair_stream(
        stream: list[_Transition],
    ) -> list[tuple[_Transition, InterventionEvidenceRow]]:
        """Pair one (source, family) chronological stream conservatively."""
        out: list[tuple[_Transition, InterventionEvidenceRow]] = []
        open_candidate: _Transition | None = None
        ambiguous_opens: list[_Transition] = []

        def flush_open_as_ambiguous(open_t: _Transition) -> None:
            out.append(
                (
                    open_t,
                    InterventionEvidenceRow(
                        source=open_t.source,
                        source_id=open_t.source_id,
                        timestamp=open_t.timestamp,
                        family=open_t.family,
                        kind=open_t.kind,
                        direction=open_t.direction,
                        reason=open_t.reason,
                        action=open_t.action,
                        actor_hash=open_t.actor_hash,
                        pairing_status="AMBIGUOUS",
                        paired_source=None,
                        paired_source_id=None,
                        duration_seconds=None,
                    ),
                )
            )

        for t in stream:
            if t.direction == "open":
                if open_candidate is not None:
                    # Duplicate open before a close -> previous open ambiguous.
                    ambiguous_opens.append(open_candidate)
                    flush_open_as_ambiguous(open_candidate)
                open_candidate = t
            else:  # close
                if open_candidate is None:
                    # Close with no matching open.
                    out.append(
                        (
                            t,
                            InterventionEvidenceRow(
                                source=t.source,
                                source_id=t.source_id,
                                timestamp=t.timestamp,
                                family=t.family,
                                kind=t.kind,
                                direction=t.direction,
                                reason=t.reason,
                                action=t.action,
                                actor_hash=t.actor_hash,
                                pairing_status="UNMATCHED_CLOSE",
                                paired_source=None,
                                paired_source_id=None,
                                duration_seconds=None,
                            ),
                        )
                    )
                else:
                    open_t = open_candidate
                    open_candidate = None
                    duration = (
                        _to_utc(t.timestamp).timestamp()
                        - _to_utc(open_t.timestamp).timestamp()
                    )
                    # A non-positive or non-finite duration means the close did
                    # not strictly follow the open; treat both as ambiguous.
                    if duration <= 0:
                        flush_open_as_ambiguous(open_t)
                        out.append(
                            (
                                t,
                                InterventionEvidenceRow(
                                    source=t.source,
                                    source_id=t.source_id,
                                    timestamp=t.timestamp,
                                    family=t.family,
                                    kind=t.kind,
                                    direction=t.direction,
                                    reason=t.reason,
                                    action=t.action,
                                    actor_hash=t.actor_hash,
                                    pairing_status="AMBIGUOUS",
                                    paired_source=None,
                                    paired_source_id=None,
                                    duration_seconds=None,
                                ),
                            )
                        )
                        continue
                    out.append(
                        (
                            open_t,
                            InterventionEvidenceRow(
                                source=open_t.source,
                                source_id=open_t.source_id,
                                timestamp=open_t.timestamp,
                                family=open_t.family,
                                kind=open_t.kind,
                                direction=open_t.direction,
                                reason=open_t.reason,
                                action=open_t.action,
                                actor_hash=open_t.actor_hash,
                                pairing_status="PAIRED",
                                paired_source=t.source,
                                paired_source_id=t.source_id,
                                duration_seconds=None,
                            ),
                        )
                    )
                    out.append(
                        (
                            t,
                            InterventionEvidenceRow(
                                source=t.source,
                                source_id=t.source_id,
                                timestamp=t.timestamp,
                                family=t.family,
                                kind=t.kind,
                                direction=t.direction,
                                reason=t.reason,
                                action=t.action,
                                actor_hash=t.actor_hash,
                                pairing_status="PAIRED",
                                paired_source=open_t.source,
                                paired_source_id=open_t.source_id,
                                duration_seconds=duration,
                            ),
                        )
                    )

        # Trailing open with no close.
        if open_candidate is not None:
            out.append(
                (
                    open_candidate,
                    InterventionEvidenceRow(
                        source=open_candidate.source,
                        source_id=open_candidate.source_id,
                        timestamp=open_candidate.timestamp,
                        family=open_candidate.family,
                        kind=open_candidate.kind,
                        direction=open_candidate.direction,
                        reason=open_candidate.reason,
                        action=open_candidate.action,
                        actor_hash=open_candidate.actor_hash,
                        pairing_status="OPEN",
                        paired_source=None,
                        paired_source_id=None,
                        duration_seconds=None,
                    ),
                )
            )
        return out

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
