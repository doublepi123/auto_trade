"""Runtime intervention evidence timeline — service + API. Per-file sqlite."""
from __future__ import annotations

import json
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
from app.services.intervention_evidence_service import InterventionEvidenceService


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"],
            connect_args={"check_same_thread": False},
        )
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
            payload_json="{}",
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
    ) -> int:
        db = self._db()
        row = AuditLog(
            action=action,
            severity="INFO",
            actor_hash=actor_hash,
            source_ip="",
            request_summary=json.dumps({}),
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


class TestInterventionEvidencePairing(_Base):
    def test_empty(self) -> None:
        resp = self._build()
        assert resp.items == []
        assert resp.summary.total_evidence == 0
        assert resp.summary.paired_duration_seconds == 0.0

    def test_explicit_pair_reports_duration(self) -> None:
        open_id = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            message="manual pause",
        )
        close_id = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
            message="manual resume",
        )
        resp = self._build()
        assert resp.summary.paired_count == 2
        assert resp.summary.paired_duration_seconds == 600.0
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[open_id].pairing_status == "PAIRED"
        assert by_id[open_id].direction == "open"
        assert by_id[open_id].duration_seconds is None
        assert by_id[close_id].pairing_status == "PAIRED"
        assert by_id[close_id].direction == "close"
        assert by_id[close_id].duration_seconds == 600.0
        assert by_id[close_id].paired_source_id == open_id
        assert by_id[open_id].paired_source_id == close_id

    def test_unmatched_open_no_duration(self) -> None:
        open_id = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        assert resp.summary.open_count == 1
        assert resp.summary.paired_count == 0
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.items[0].source_id == open_id
        assert resp.items[0].pairing_status == "OPEN"
        assert resp.items[0].duration_seconds is None

    def test_unmatched_close_no_duration(self) -> None:
        close_id = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        assert resp.summary.unmatched_close_count == 1
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.items[0].source_id == close_id
        assert resp.items[0].pairing_status == "UNMATCHED_CLOSE"
        assert resp.items[0].duration_seconds is None

    def test_duplicate_open_is_ambiguous(self) -> None:
        first_open = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        second_open = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
        )
        close = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        # First open is ambiguous (duplicate before close); second open pairs.
        assert by_id[first_open].pairing_status == "AMBIGUOUS"
        assert by_id[second_open].pairing_status == "PAIRED"
        assert by_id[close].pairing_status == "PAIRED"
        # Only the unambiguous pair contributes duration.
        assert resp.summary.paired_duration_seconds == 300.0
        assert resp.summary.ambiguous_count == 1

    def test_conflicting_close_before_open_is_ambiguous(self) -> None:
        # Close timestamp not strictly after open -> both ambiguous.
        open_id = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        close_id = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[open_id].pairing_status == "AMBIGUOUS"
        assert by_id[close_id].pairing_status == "AMBIGUOUS"
        assert resp.summary.paired_duration_seconds == 0.0
        assert resp.summary.ambiguous_count == 2

    def test_out_of_order_insertion_chronological_output(self) -> None:
        # Insert close first, then open (out of insertion order).
        close_id = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        open_id = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        # Output is chronological regardless of insertion order.
        assert [r.source_id for r in resp.items] == [open_id, close_id]
        assert resp.summary.paired_count == 2
        assert resp.summary.paired_duration_seconds == 600.0

    def test_kill_switch_family_separate_from_pause(self) -> None:
        # Kill-switch open with no close; pause pair intact. Families do not
        # cross-pair.
        ks_open = self._trade(
            "CONTROL_KILL_SWITCH",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 15, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        by_id = {r.source_id: r for r in resp.items}
        assert by_id[ks_open].family == "kill_switch"
        assert by_id[ks_open].pairing_status == "OPEN"
        # Pause pair duration is reported independently.
        assert resp.summary.paired_duration_seconds == 600.0

    def test_risk_paused_and_auto_resumed_pair(self) -> None:
        self._trade(
            "RISK_PAUSED",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            status="PAUSED",
        )
        self._trade(
            "RISK_AUTO_RESUMED",
            datetime(2026, 6, 14, 10, 2, 30, tzinfo=timezone.utc),
            status="RUNNING",
        )
        resp = self._build()
        assert resp.summary.paired_count == 2
        assert resp.summary.paired_duration_seconds == 150.0


class TestInterventionEvidenceCrossSource(_Base):
    def test_cross_source_duplicate_not_merged(self) -> None:
        # Same intervention recorded in both trade and audit sources, with no
        # durable correlation key. They must NOT be merged or double-counted.
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 10, 0, tzinfo=timezone.utc),
        )
        self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 10, 0, 5, tzinfo=timezone.utc),
        )
        self._audit(
            "RESUME",
            datetime(2026, 6, 14, 10, 10, 5, tzinfo=timezone.utc),
        )
        resp = self._build()
        # Two independent pairs (one per source); both durations counted
        # separately because they are distinct evidence streams.
        assert resp.summary.paired_count == 4
        sources = {r.source for r in resp.items}
        assert sources == {"trade", "audit"}

    def test_safe_actor_and_reason_projection(self) -> None:
        self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            actor_hash="deadbeef",
        )
        resp = self._build()
        payload = resp.model_dump(mode="json")
        dumped = str(payload)
        # Pseudonymous actor hash is exposed.
        assert resp.items[0].actor_hash == "deadbeef"
        # No raw IP / payload body / exception text surface.
        assert "source_ip" not in dumped
        assert "payload" not in dumped
        assert "request_summary" not in dumped

    def test_no_runtime_state_inference_or_mutation(self) -> None:
        # Seed a RuntimeState row that claims paused=True; the evidence service
        # must NOT read or mutate it.
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
        # No evidence inferred from RuntimeState.
        assert resp.items == []
        assert resp.summary.total_evidence == 0
        # RuntimeState is unchanged.
        db = self._db()
        state = db.query(RuntimeState).filter(RuntimeState.symbol == "AAPL.US").first()
        db.close()
        assert state is not None
        assert state.paused is True
        assert state.kill_switch is True


class TestInterventionEvidenceFilters(_Base):
    def test_date_filters_boundaries(self) -> None:
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 23, 59, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 15, 0, 1, 0, tzinfo=timezone.utc),
        )
        resp = self._build(
            from_date=datetime(2026, 6, 15).date(),
            to_date=datetime(2026, 6, 15).date(),
        )
        assert resp.summary.total_evidence == 1
        assert resp.items[0].timestamp.date().isoformat() == "2026-06-15"

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            self._build(
                from_date=datetime(2026, 6, 17).date(),
                to_date=datetime(2026, 6, 16).date(),
            )

    def test_deterministic_tie_ordering(self) -> None:
        # Two trade events and one audit event at the same timestamp.
        t_open = self._trade(
            "CONTROL_PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        a_open = self._audit(
            "PAUSE",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        t_close = self._trade(
            "CONTROL_RESUME",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        resp = self._build()
        # Tie-break: timestamp asc, then source asc (audit < trade), then id asc.
        ordered = [(r.source, r.source_id) for r in resp.items]
        # audit comes before trade at the same timestamp.
        assert ordered[0][0] == "audit"
        assert ordered[0][1] == a_open
        # The two trade events keep id order.
        trade_ids = [sid for src, sid in ordered if src == "trade"]
        assert trade_ids == sorted([t_open, t_close])


class TestInterventionEvidenceAPI(_Base):
    def test_endpoint_empty(self) -> None:
        resp = self.client.get("/api/intervention-evidence")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["items"] == []
        assert data["summary"]["total_evidence"] == 0
        assert "pairing_rule" in data
        assert data["summary"]["paired_duration_seconds"] == 0.0

    def test_endpoint_pair(self) -> None:
        self._trade(
            "CONTROL_KILL_SWITCH",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
        )
        self._trade(
            "CONTROL_DISABLE_KILL_SWITCH",
            datetime(2026, 6, 14, 10, 5, 0, tzinfo=timezone.utc),
        )
        resp = self.client.get("/api/intervention-evidence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["paired_count"] == 2
        assert data["summary"]["paired_duration_seconds"] == 300.0

    def test_endpoint_invalid_date_range_422(self) -> None:
        resp = self.client.get(
            "/api/intervention-evidence",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422

    def test_endpoint_limit_bounded(self) -> None:
        for i in range(5):
            self._trade(
                "CONTROL_PAUSE",
                datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc),
            )
        resp = self.client.get(
            "/api/intervention-evidence",
            params={"limit": 3},
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

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
