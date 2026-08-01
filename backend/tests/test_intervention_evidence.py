"""Runtime intervention evidence timeline — service + API. Per-file sqlite.

Hardened semantics (Gate 2):
* Manual pause/resume and kill-switch durations come ONLY from successful
  AuditLog control actions (the authoritative stream).
* Duplicate manual-control TradeEvent rows are excluded from pairing.
* ``RISK_PAUSED`` is excluded (generic risk rejection per writer).
* ``RISK_AUTO_RESUMED`` is included as automatic evidence only when successful,
  and never pairs with manual audit controls (no durable correlation key).
* ``reason`` is a fixed code derived from the action/event type; free-form
  messages/payloads are never exposed.
* Pairing is global (before date filters / limit); date filters apply after;
  response limit applies last; ``total``/``truncated``/``scan_truncated``
  describe completeness.
* Duplicate-open segments are wholly ambiguous (no duration for any row).
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_intervention_evidence_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AuditLog, Base, RiskEvent, RuntimeState, TradeEvent
from app.services.intervention_evidence_service import (
    InterventionEvidenceService,
    _MAX_RESPONSE_ROWS,
    _MAX_SCAN_TRANSITIONS,
)


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        from sqlalchemy import event as sa_event

        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"],
            connect_args={"check_same_thread": False},
        )

        @sa_event.listens_for(cls.engine, "connect")
        def _set_wal_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        settings.api_key = ""
        db = Session(bind=self.engine)
        db.query(TradeEvent).delete()
        db.query(AuditLog).delete()
        db.query(RiskEvent).delete()
        db.query(RuntimeState).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _trade(
        self,
        event_type: str,
        created_at: datetime,
        *,
        message: str = "",
        status: str = "",
        symbol: str = "",
    ) -> int:
        db = self._db()
        row = TradeEvent(
            event_type=event_type,
            symbol=symbol,
            status=status,
            message=message,
            payload_json='{"secret": "leak"}',
            created_at=created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.close()
        return row_id

    def _audit(
        self,
        action: str,
        created_at: datetime,
        *,
        actor_hash: str = "anon",
        result: str = "SUCCESS",
        summary: str = '{"secret": "leak"}',
    ) -> int:
        db = self._db()
        row = AuditLog(
            action=action,
            severity="INFO",
            actor_hash=actor_hash,
            source_ip="10.0.0.1",
            request_summary=summary,
            result=result,
            created_at=created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.close()
        return row_id

    def _build(self, **kwargs):
        return InterventionEvidenceService(self._db()).build(**kwargs)


class TestInterventionEvidenceAuthoritative(_Base):
    def test_audit_pair_reports_duration(self) -> None:
        open_id = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        close_id = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        assert resp.summary.paired_count == 2
        assert resp.summary.paired_duration_seconds == 600.0
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[open_id].pairing_status == "PAIRED"
        assert by_id[open_id].source == "audit"
        assert by_id[open_id].duration_seconds is None
        assert by_id[close_id].pairing_status == "PAIRED"
        assert by_id[close_id].duration_seconds == 600.0
        assert by_id[close_id].paired_source_id == open_id

    def test_failed_audit_control_excluded(self) -> None:
        # A FAILED pause must not be treated as a transition.
        self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            result="FAILED",
        )
        self._audit(
            "RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        # Only the successful resume survives; it is an unmatched close.
        assert resp.summary.total_evidence == 1
        assert resp.summary.unmatched_close_count == 1
        assert resp.summary.paired_duration_seconds == 0.0

    def test_manual_trade_controls_excluded_from_pairing(self) -> None:
        # Duplicate manual-control TradeEvent rows must NOT pair or produce a
        # duration; the authoritative audit stream is the sole source.
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        assert resp.items == []
        assert resp.summary.total_evidence == 0
        assert resp.summary.paired_duration_seconds == 0.0

    def test_audit_and_trade_duplicate_manual_controls_no_double_count(self) -> None:
        # Same manual intervention in both audit (authoritative) and trade
        # (duplicate). Only the audit pair contributes a duration; the trade
        # CONTROL_* rows are excluded entirely.
        a_open = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        a_close = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 5, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 5, tzinfo=timezone.utc),
        )
        resp = self._build()
        assert resp.summary.paired_count == 2
        # Duration counted exactly once (audit only).
        assert resp.summary.paired_duration_seconds == 600.0
        sources = {r.source for r in resp.items}
        assert sources == {"audit"}
        ids = {r.source_id for r in resp.items}
        assert ids == {a_open, a_close}

    def test_risk_paused_excluded(self) -> None:
        # RISK_PAUSED is a generic risk rejection per the current writer and
        # must not be treated as a transition-proof open.
        self._trade(
            "RISK_PAUSED",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            status="PAUSED",
        )
        self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        # The resume has no authoritative open -> unmatched close, no duration.
        assert resp.summary.unmatched_close_count == 1
        assert resp.summary.paired_duration_seconds == 0.0
        assert all(r.action != "RISK_PAUSED" for r in resp.items)

    def test_automatic_resume_successful_but_unpaired(self) -> None:
        # RISK_AUTO_RESUMED is transition-specific and successful, so it is
        # included as evidence, but it has no durable correlation key to the
        # manual audit stream and must NOT pair with it.
        self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        auto_id = self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
            status="RUNNING",
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        # The audit pause is OPEN (no authoritative close); the auto resume is
        # an unmatched close. Neither contributes a duration.
        assert resp.summary.open_count == 1
        assert resp.summary.unmatched_close_count == 1
        assert resp.summary.paired_duration_seconds == 0.0
        auto_row = by_id[auto_id]
        assert auto_row.source == "trade_auto"
        assert auto_row.pairing_status == "UNMATCHED_CLOSE"
        assert auto_row.duration_seconds is None

    def test_automatic_resume_failed_excluded(self) -> None:
        # An automatic transition whose writer did not report success is not
        # transition proof and is excluded.
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
            status="FAILED",
        )
        resp = self._build()
        assert resp.items == []

    def test_secret_bearing_message_never_exposed(self) -> None:
        # Free-form messages/payloads containing secrets must never surface as
        # a "safe reason"; only the fixed reason code derived from the action.
        self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            summary='{"secret": "leak"}',
        )
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
            status="RUNNING",
            message="secret in message body",
        )
        resp = self._build()
        payload = resp.model_dump(mode="json")
        dumped = str(payload)
        assert "leak" not in dumped
        assert "secret in message body" not in dumped
        # Reasons are fixed codes only.
        reasons = {r.reason for r in resp.items}
        assert reasons <= {
            "MANUAL_PAUSE",
            "MANUAL_RESUME",
            "MANUAL_KILL_SWITCH",
            "MANUAL_KILL_SWITCH_DISABLE",
            "AUTOMATIC_RESUME",
            "",
        }
        # Pseudonymous actor only; no raw IP.
        assert "10.0.0.1" not in dumped
        assert "source_ip" not in dumped


class TestInterventionEvidencePairing(_Base):
    def test_empty(self) -> None:
        resp = self._build()
        assert resp.items == []
        assert resp.summary.total_evidence == 0
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.pairing_complete is True
        assert resp.scan_truncated is False

    def test_unmatched_open_no_duration(self) -> None:
        open_id = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        assert resp.summary.open_count == 1
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.items[0].source_id == open_id
        assert resp.items[0].pairing_status == "OPEN"

    def test_unmatched_close_no_duration(self) -> None:
        close_id = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        assert resp.summary.unmatched_close_count == 1
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.items[0].source_id == close_id
        assert resp.items[0].pairing_status == "UNMATCHED_CLOSE"

    def test_duplicate_open_segment_wholly_ambiguous(self) -> None:
        # OPEN, OPEN, CLOSE -> no duration for ANY of the three rows.
        first = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        second = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc)
        )
        close = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[first].pairing_status == "AMBIGUOUS"
        assert by_id[second].pairing_status == "AMBIGUOUS"
        assert by_id[close].pairing_status == "AMBIGUOUS"
        assert resp.summary.ambiguous_count == 3
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.summary.paired_count == 0

    def test_duplicate_open_resets_after_close(self) -> None:
        # After the ambiguous segment closes, a fresh open/close pair is valid.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc))
        good_open = self._audit(
            "PAUSE", datetime(2026, 6, 14, 11, 0, 0, tzinfo=timezone.utc)
        )
        good_close = self._audit(
            "RESUME", datetime(2026, 6, 14, 11, 10, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[good_open].pairing_status == "PAIRED"
        assert by_id[good_close].pairing_status == "PAIRED"
        assert resp.summary.paired_duration_seconds == 600.0

    def test_conflicting_close_not_strictly_after_open(self) -> None:
        open_id = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        close_id = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[open_id].pairing_status == "AMBIGUOUS"
        assert by_id[close_id].pairing_status == "AMBIGUOUS"
        assert resp.summary.paired_duration_seconds == 0.0

    def test_out_of_order_insertion_chronological_output(self) -> None:
        close_id = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc)
        )
        open_id = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        resp = self._build()
        assert [r.source_id for r in resp.items] == [open_id, close_id]
        assert resp.summary.paired_count == 2
        assert resp.summary.paired_duration_seconds == 600.0

    def test_kill_switch_family_separate_from_pause(self) -> None:
        ks_open = self._audit(
            "KILL_SWITCH",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 15, 0, tzinfo=timezone.utc))
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[ks_open].family == "kill_switch"
        assert by_id[ks_open].pairing_status == "OPEN"
        assert resp.summary.paired_duration_seconds == 600.0


class TestInterventionEvidenceRangeAndLimit(_Base):
    def test_cross_date_pair_preserved_by_global_pairing(self) -> None:
        # Open on day 1, close on day 2. Filtering to day 1 only must NOT turn
        # the open into a synthetic OPEN; global pairing keeps it PAIRED.
        open_id = self._audit(
            "PAUSE", datetime(2026, 6, 14, 23, 50, 0, tzinfo=timezone.utc)
        )
        close_id = self._audit(
            "RESUME", datetime(2026, 6, 15, 0, 10, 0, tzinfo=timezone.utc)
        )
        # Filter to day 1 only.
        resp = self._build(
            from_date=datetime(2026, 6, 14).date(),
            to_date=datetime(2026, 6, 14).date(),
        )
        by_id = {r.source_id: r for r in resp.items}
        # The open is returned and remains PAIRED (its close exists globally).
        assert by_id[open_id].pairing_status == "PAIRED"
        assert by_id[open_id].paired_source_id == close_id
        # The close is outside the filter window so it is omitted from items,
        # but the open still reports its paired duration context.
        assert close_id not in by_id

    def test_limit_split_pair_truthful_total_and_truncated(self) -> None:
        # Two independent pairs; limit to 2 rows. The summary must describe the
        # full filtered population (4 rows), and truncated must be True.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc))
        self._audit("PAUSE", datetime(2026, 6, 14, 11, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 11, 10, 0, tzinfo=timezone.utc))
        resp = self._build(limit=2)
        assert resp.total == 4
        assert resp.truncated is True
        assert len(resp.items) == 2
        # Summary reflects the FULL filtered population, not just returned rows.
        assert resp.summary.total_evidence == 4
        assert resp.summary.paired_count == 4
        assert resp.summary.paired_duration_seconds == 1200.0

    def test_date_filters_boundaries(self) -> None:
        self._audit("PAUSE", datetime(2026, 6, 14, 23, 59, 0, tzinfo=timezone.utc))
        self._audit("PAUSE", datetime(2026, 6, 15, 0, 1, 0, tzinfo=timezone.utc))
        resp = self._build(
            from_date=datetime(2026, 6, 15).date(),
            to_date=datetime(2026, 6, 15).date(),
        )
        assert resp.total == 1
        assert resp.items[0].timestamp.date().isoformat() == "2026-06-15"

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            self._build(
                from_date=datetime(2026, 6, 17).date(),
                to_date=datetime(2026, 6, 16).date(),
            )

    def test_deterministic_tie_ordering(self) -> None:
        # Two audit rows and one auto row at the same timestamp.
        a_open = self._audit(
            "PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        a_close = self._audit(
            "RESUME", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
        )
        auto_id = self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            status="RUNNING",
        )
        resp = self._build()
        ordered = [(r.source, r.source_id) for r in resp.items]
        # Tie-break: timestamp asc, then source asc ("audit" < "trade_auto"),
        # then source_id asc.
        assert ordered[0] == ("audit", min(a_open, a_close))
        assert ordered[-1] == ("trade_auto", auto_id)
        audit_ids = [sid for src, sid in ordered if src == "audit"]
        assert audit_ids == sorted([a_open, a_close])


class TestInterventionEvidenceScanCap(_Base):
    def test_scan_cap_suppresses_durations_and_reports_truncated(self) -> None:
        # Exceed the hard scan cap with successful audit transitions.
        for i in range(_MAX_SCAN_TRANSITIONS + 5):
            self._audit(
                "PAUSE",
                datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc),
            )
        resp = self._build()
        assert resp.scan_truncated is True
        assert resp.pairing_complete is False
        # All durations suppressed because complete pairing context is unknown.
        assert resp.summary.paired_count == 0
        assert resp.summary.paired_duration_seconds == 0.0
        assert all(r.duration_seconds is None for r in resp.items)
        # Context-dependent manual states are UNKNOWN, not OPEN/PAIRED.
        assert all(r.pairing_status == "UNKNOWN" for r in resp.items)
        # Summary status counts use the filtered_scanned denominator.
        assert resp.summary.unknown_count == resp.filtered_scanned
        assert resp.summary.open_count == 0
        assert resp.summary.classification_complete is False
        # Exact total is truthful (independent of the scan cap).
        assert resp.total == _MAX_SCAN_TRANSITIONS + 5
        # Pairing-context scanned population is bounded (cap + 1 rows fetched).
        assert resp.pairing_context_scanned == _MAX_SCAN_TRANSITIONS + 1
        assert resp.truncated is True

    def test_scan_cap_open_at_cap_plus_1_close_at_cap_plus_2_no_open_claim(
        self,
        monkeypatch,
    ) -> None:
        # Small cap via monkeypatch: open at cap+1, close at cap+2. The open
        # must NOT be claimed as OPEN (omitted history could change it); it is
        # UNKNOWN with no duration. Exact total is truthful.
        import app.services.intervention_evidence_service as svc_mod

        cap = 3
        monkeypatch.setattr(svc_mod, "_MAX_SCAN_TRANSITIONS", cap)
        open_id = self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Fill the first scan slots so the real open/close fall beyond.
        for i in range(cap):
            self._audit(
                "PAUSE",
                datetime(2026, 6, 14, i, 0, 0, tzinfo=timezone.utc),
            )
        close_id = self._audit(
            "RESUME",
            datetime(2026, 6, 14, 13, 0, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        assert resp.scan_truncated is True
        assert resp.pairing_complete is False
        by_id = {r.source_id: r for r in resp.items}
        # No row claims OPEN or PAIRED; all are UNKNOWN with no duration.
        assert all(r.pairing_status == "UNKNOWN" for r in resp.items)
        assert all(r.duration_seconds is None for r in resp.items)
        if open_id in by_id:
            assert by_id[open_id].pairing_status == "UNKNOWN"
        if close_id in by_id:
            assert by_id[close_id].pairing_status == "UNKNOWN"
        assert resp.summary.paired_duration_seconds == 0.0
        # Exact total counts all 5 audit rows truthfully.
        assert resp.total == 5
        # Pairing-context scanned population is bounded (cap + 1 fetched).
        assert resp.pairing_context_scanned == cap + 1
        assert resp.truncated is True

    def test_scan_cap_metadata_truthful_when_complete(self) -> None:
        # Under the cap: pairing complete, denominators consistent, not truncated.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc))
        resp = self._build()
        assert resp.pairing_complete is True
        assert resp.scan_truncated is False
        assert resp.total == 2
        assert resp.pairing_context_scanned == 2
        assert resp.filtered_scanned == 2
        assert resp.returned == 2
        assert resp.truncated is False
        assert resp.classification_complete is True
        assert resp.summary.classification_complete is True
        assert resp.summary.scanned_evidence == 2

    def test_automatic_source_scan_cap_bounded(self, monkeypatch) -> None:
        # Prove the automatic source is also bounded: exceed its cap and verify
        # no unbounded .all()/ID materialization behavior — the scan population
        # is capped and durations/classifications are suppressed.
        import app.services.intervention_evidence_service as svc_mod

        cap = 2
        monkeypatch.setattr(svc_mod, "_MAX_SCAN_TRANSITIONS", cap)
        for i in range(5):
            self._trade(
                "RISK_AUTO_RESUMED",
                datetime(2026, 6, 14, i, 0, 0, tzinfo=timezone.utc),
                status="RUNNING",
            )
        resp = self._build()
        assert resp.scan_truncated is True
        assert resp.pairing_complete is False
        # Pairing-context scanned automatic rows are bounded (cap + 1 fetched).
        assert resp.pairing_context_scanned == cap + 1
        # Exact total is truthful (all 5 automatic rows).
        assert resp.total == 5
        assert resp.truncated is True
        assert all(r.pairing_status == "UNKNOWN" for r in resp.items)
        assert all(r.duration_seconds is None for r in resp.items)


class TestInterventionEvidenceSharedPredicate(_Base):
    """Finding A.1: one shared SQL predicate per source, normalized in SQL."""

    def test_lowercase_successful_auto_status_normalized_in_sql(self) -> None:
        # The auto status is normalized in SQL (upper/trim/coalesce), so a
        # lowercase/whitespace-padded success status counts as evidence.
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            status="  running  ",
        )
        resp = self._build()
        assert resp.total == 1
        assert resp.pairing_context_scanned == 1
        assert len(resp.items) == 1
        assert resp.items[0].source == "trade_auto"
        assert resp.items[0].action == "RISK_AUTO_RESUMED"

    def test_failed_auto_status_absent_everywhere(self) -> None:
        # A failed/non-success auto status must affect neither scans,
        # truncation, rows, nor totals.
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            status="FAILED",
        )
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 11, 0, 0, tzinfo=timezone.utc),
            status="",
        )
        resp = self._build()
        assert resp.total == 0
        assert resp.pairing_context_scanned == 0
        assert resp.items == []
        assert resp.scan_truncated is False


class TestInterventionEvidenceOneSnapshot(_Base):
    """Finding A.2: all metadata queries on ONE read snapshot."""

    def test_writer_between_scan_and_exact_total_does_not_split(self) -> None:
        # Seed one audit pair on the committed state.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc))

        service_db = self._db()
        service = InterventionEvidenceService(service_db)
        writer = self._db()
        committed = []

        def _commit_during_snapshot() -> None:
            # Writer commits a new audit row AFTER the bounded scan but BEFORE
            # the exact-total query, on a separate session.
            writer.add(
                AuditLog(
                    action="PAUSE",
                    severity="INFO",
                    actor_hash="concurrent",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                    created_at=datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc),
                )
            )
            writer.commit()
            committed.append(True)

        service._after_bounded_scan = _commit_during_snapshot  # type: ignore[method-assign]
        try:
            resp = service.build()
            # The bounded scan and exact-total both ran on the same snapshot,
            # established before the writer committed. The exact total reflects
            # the pre-commit state (2), not the post-commit state (3).
            assert committed == [True]
            assert resp.total == 2
            assert resp.pairing_context_scanned == 2
        finally:
            writer.close()
            service_db.close()

    def test_no_unsupported_open_when_metadata_changes_concurrently(self) -> None:
        # A single PAUSE exists on the committed state. A writer commits a
        # RESUME during the snapshot. Because the bounded scan and exact-total
        # share one snapshot, the open must NOT become a spurious OPEN from a
        # split metadata view; it stays consistent with the snapshot.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))

        service_db = self._db()
        service = InterventionEvidenceService(service_db)
        writer = self._db()

        def _commit_during_snapshot() -> None:
            writer.add(
                AuditLog(
                    action="RESUME",
                    severity="INFO",
                    actor_hash="concurrent",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                    created_at=datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
                )
            )
            writer.commit()

        service._after_bounded_scan = _commit_during_snapshot  # type: ignore[method-assign]
        try:
            resp = service.build()
            # Snapshot taken before the writer's RESUME: the PAUSE is an
            # unmatched OPEN (no close in the scanned snapshot), and the total
            # is 1 (not 2). The concurrent RESUME is not visible.
            assert resp.total == 1
            assert resp.pairing_context_scanned == 1
            assert resp.items[0].pairing_status == "OPEN"
        finally:
            writer.close()
            service_db.close()


class TestInterventionEvidenceDenominators(_Base):
    """Finding A.4: exact total / scanned / returned denominators."""

    def test_denominators_with_date_filter_and_limit(self) -> None:
        # Two pairs on different days; filter to one day, limit to 1 row.
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc))
        self._audit("PAUSE", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 15, 10, 10, 0, tzinfo=timezone.utc))
        resp = self._build(
            from_date=datetime(2026, 6, 15).date(),
            to_date=datetime(2026, 6, 15).date(),
            limit=1,
        )
        # Exact total: only the 2 rows on 2026-06-15.
        assert resp.total == 2
        # Pairing context scanned globally (all 4 rows loaded for pairing).
        assert resp.pairing_context_scanned == 4
        # Filtered scanned: 2 rows match the date filter.
        assert resp.filtered_scanned == 2
        # Returned: 1 row after the limit.
        assert resp.returned == 1
        assert resp.truncated is True
        # Summary uses filtered_scanned, not pairing_context_scanned.
        assert resp.summary.scanned_evidence == 2
        assert resp.summary.total_evidence == 2


class TestInterventionEvidenceRuntimeState(_Base):
    def test_no_runtime_state_inference_or_mutation(self) -> None:
        db = self._db()
        db.add(
            RuntimeState(
                symbol="AAPL.US",
                engine_state="flat",
                paused=True,
                kill_switch=True,
                pause_reason="should-be-ignored",
            )
        )
        db.commit()
        db.close()
        resp = self._build()
        assert resp.items == []
        assert resp.summary.total_evidence == 0
        db = self._db()
        state = db.query(RuntimeState).filter(RuntimeState.symbol == "AAPL.US").first()
        db.close()
        assert state is not None
        assert state.paused is True
        assert state.kill_switch is True


class TestInterventionEvidenceAPI(_Base):
    def test_endpoint_empty(self) -> None:
        resp = self.client.get("/api/intervention-evidence")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["items"] == []
        assert data["summary"]["total_evidence"] == 0
        assert "pairing_rule" in data
        assert data["pairing_complete"] is True
        assert data["scan_truncated"] is False
        assert data["total"] == 0
        assert data["truncated"] is False

    def test_endpoint_audit_pair(self) -> None:
        self._audit("PAUSE", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._audit("RESUME", datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc))
        resp = self.client.get("/api/intervention-evidence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["paired_count"] == 2
        assert data["summary"]["paired_duration_seconds"] == 300.0
        assert data["total"] == 2

    def test_endpoint_invalid_date_range_422(self) -> None:
        resp = self.client.get(
            "/api/intervention-evidence",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422

    def test_endpoint_limit_bounded(self) -> None:
        for i in range(5):
            self._audit(
                "PAUSE",
                datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc),
            )
        resp = self.client.get(
            "/api/intervention-evidence",
            params={"limit": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 3
        assert data["total"] == 5
        assert data["truncated"] is True

    def test_auth_enforced_when_api_key_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret")
        assert (
            self.client.get("/api/intervention-evidence").status_code == 401
        )
        resp = self.client.get(
            "/api/intervention-evidence",
            headers={"X-API-Key": "secret"},
        )
        assert resp.status_code == 200


class TestInterventionEvidenceAPI503:
    """Finding 2: SnapshotUnavailable maps to HTTP 503 through the route.

    Uses a local FastAPI app with a StaticPool engine and a connection-bound
    session dependency so the snapshot helper rejects before aliasing. The
    response must be 503 (not 500), and no caller transaction is altered.
    """

    def test_endpoint_returns_503_for_unavailable_snapshot(self) -> None:
        from fastapi import FastAPI

        from sqlalchemy.pool import StaticPool

        from app.api import intervention_evidence as intervention_evidence_api

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        try:
            local_app = FastAPI()
            local_app.include_router(intervention_evidence_api.router)

            def override_get_db():
                # A connection-bound session dependency (the unsafe case).
                conn = engine.connect()
                try:
                    yield Session(bind=conn)
                finally:
                    conn.close()

            local_app.dependency_overrides[
                intervention_evidence_api.get_db
            ] = override_get_db
            client = TestClient(local_app)
            resp = client.get("/api/intervention-evidence")
            assert resp.status_code == 503, resp.text
            assert "snapshot unavailable" in resp.json()["detail"].lower()
            # No pool/connection internals or exception text leaked.
            detail = resp.json()["detail"].lower()
            assert "staticpool" not in detail
            assert "traceback" not in detail
            client.close()
        finally:
            engine.dispose()

    def test_endpoint_422_preserved_for_invalid_date_range(self) -> None:
        # The existing 422 mapping for ValueError must be preserved.
        from fastapi import FastAPI

        from app.api.intervention_evidence import router as intervention_router
        from app.database import get_db

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        try:
            local_app = FastAPI()
            local_app.include_router(intervention_router)

            def override_get_db():
                db = Session(bind=engine)
                try:
                    yield db
                finally:
                    db.close()

            local_app.dependency_overrides[get_db] = override_get_db
            client = TestClient(local_app)
            resp = client.get(
                "/api/intervention-evidence",
                params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
            )
            assert resp.status_code == 422
            client.close()
        finally:
            engine.dispose()
