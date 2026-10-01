# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""W3 real killed-child crash recovery evidence (Phase2a, OFF).

Genuine child processes run the REAL runner recovery surface on an
isolated SQLite DB + a journaling fake broker, then ``os._exit(9)`` at
actual lifecycle checkpoints. The parent restarts a NEW runner instance
over the same durable DB/journal and asserts no duplicate mutation and
correct guard/quarantine state.

Checkpoints (contract matrix):
- after-CHECKING      : ownership begun, no submit yet;
- after-SUBMITTING    : submit right consumed, no broker call;
- accepted-unbound    : broker mutation journaled (fsync) BEFORE the
                        mandate bind;
- bound               : bind ORDER_KNOWN committed, order recorded.

RED: pre-W3 P12 has no runner recovery, so the NEW-instance assertions
(no guard raised for provable-no-submit, quarantine for live orders,
pending-ref restoration) fail behaviourally.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])


_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {backend_root!r})
    mode, db_path, journal_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("AUTO_TRADE_ENV", "test")

    import json
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.risk import RiskController
    from app.database import _ensure_passive_mandates_table
    from app.domain.passive_allocation import protocol as pp
    from app.domain.passive_allocation import policy as passive_policy
    from app.domain.passive_allocation.model import (
        PASSIVE_LANE, PASSIVE_SYMBOL, POLICY_VERSION,
    )
    from app.models import Base, PassiveMandate
    from app.services.passive_recovery_service import (
        PassiveRecoveryService,
    )

    NOW = datetime(2026, 9, 30, 15, 0, 0, tzinfo=timezone.utc)

    def intent_json():
        return pp.intent_to_json(pp.ImmutablePassiveIntent(
            symbol=PASSIVE_SYMBOL, side="BUY", quantity=Decimal("8"),
            original_price=Decimal("600"),
            policy=pp.PassivePolicySnapshot(
                policy_version=POLICY_VERSION,
                allotment_usd=Decimal("5000"),
                risk_model="FULL_PRINCIPAL",
                exemptions=tuple(passive_policy.REQUIRED_EXEMPTIONS),
                order_binding="paper-only", review_interval_months=6,
            ),
        ))

    def write_row(db, **values):
        base = dict(
            lane=PASSIVE_LANE, policy_version=POLICY_VERSION,
            symbol=PASSIVE_SYMBOL, status="ACTIVE", allotment_usd=5000.0,
            risk_model="FULL_PRINCIPAL",
            exemptions=",".join(passive_policy.REQUIRED_EXEMPTIONS),
            review_interval_months=6, entry_authorisation_available=False,
            entry_authorisation_consumed_at=NOW,
            claim_token="claim-c", execution_token="exec-c",
            intent_json=intent_json(), approved_at=NOW, approved_by="owner",
            approval_reason="owner approval", order_binding="paper-only",
        )
        base.update(values)
        db.merge(PassiveMandate(**base))
        db.commit()

    engine = create_engine(f"sqlite:///{{db_path}}", connect_args={{"timeout": 30}})

    if mode == "init":
        Base.metadata.create_all(engine)
        _ensure_passive_mandates_table(engine)
        print("INIT OK", flush=True)
        os._exit(0)

    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    if mode == "after-checking":
        with sessions() as db:
            write_row(db, submit_state=pp.SUBMIT_STATE_CHECKING)
        print("CHECKING committed", flush=True)
        os._exit(9)

    if mode == "after-submitting":
        with sessions() as db:
            write_row(db, submit_state=pp.SUBMIT_STATE_SUBMITTING)
        print("SUBMITTING committed", flush=True)
        os._exit(9)

    if mode == "accepted-unbound":
        # Broker mutation durably journaled; the bind never ran.
        with sessions() as db:
            write_row(db, submit_state=pp.SUBMIT_STATE_SUBMITTING)
        with open(journal_path, "a") as fh:
            fh.write(json.dumps({{"event": "submit", "order_id": "crash-a1",
                "symbol": PASSIVE_SYMBOL, "side": "BUY",
                "quantity": "8", "price": "600"}}) + "\\n")
            fh.flush(); os.fsync(fh.fileno())
        print("ACCEPTED unbound", flush=True)
        os._exit(9)

    if mode == "bound":
        with sessions() as db:
            write_row(
                db,
                submit_state=pp.SUBMIT_STATE_ORDER_KNOWN,
                bound_broker_order_id="crash-b1",
                bound_broker_status="SUBMITTED",
            )
        with open(journal_path, "a") as fh:
            fh.write(json.dumps({{"event": "submit", "order_id": "crash-b1",
                "symbol": PASSIVE_SYMBOL, "side": "BUY",
                "quantity": "8", "price": "600"}}) + "\\n")
            fh.flush(); os.fsync(fh.fileno())
        print("BOUND", flush=True)
        os._exit(9)

    if mode == "check-burned":
        with sessions() as db:
            row = db.query(PassiveMandate).filter(
                PassiveMandate.lane == PASSIVE_LANE
            ).one()
            print("STATE", row.submit_state, flush=True)
        os._exit(0)

    if mode == "recover":
        # NEW instance: DB-only inventory + preliminary classification.
        svc = PassiveRecoveryService(sessions, clock=lambda: NOW)
        inv = svc.load_inventory()
        snap = svc.preliminary(inv)
        print(
            "RECOVER",
            json.dumps({{
                "hard": len(snap.hard_reasons),
                "hard_first": (snap.hard_reasons[0][:90]
                               if snap.hard_reasons else ""),
                "quarantined": sorted(snap.quarantined_symbols),
                "refs": sorted(snap.pending_refs.keys()),
                "complete": snap.complete,
                "states": [d.cls.value for d in snap.decisions],
            }}),
            flush=True,
        )
        os._exit(0)
    """,
)


def _child(tmp: Path, mode: str) -> str:
    db_path = tmp / "crash.db"
    journal = tmp / "broker.journal"
    harness = tmp / f"child_{mode}.py"
    harness.write_text(_CHILD.format(backend_root=_BACKEND_ROOT))
    result = subprocess.run(
        [
            sys.executable, str(harness), mode, str(db_path), str(journal),
        ],
        capture_output=True, text=True, timeout=90,
        env={**os.environ, "AUTO_TRADE_ENV": "test"},
        cwd=_BACKEND_ROOT,
    )
    if result.returncode not in (0, 9):
        raise AssertionError(
            f"child {mode} rc={result.returncode}: {result.stderr[-2000:]}"
        )
    return result.stdout


def _journal_submits(journal: Path) -> int:
    if not journal.exists():
        return 0
    return sum(
        1
        for line in journal.read_text().splitlines()
        if '"event": "submit"' in line
    )


class TestSyntheticClassificationCrash:
    """SYNTHETIC CLASSIFICATION ONLY (review1 note kept for honesty):
    these children write the desired row state + a manual journal line
    and then verify the SERVICE classifier; they prove neither the real
    entry lifecycle transitions nor a real runner recovery. The real
    lifecycle + real-runner evidence lives in TestRealLifecycleCrash
    and TestRealRunnerRecovery below."""

    def test_killed_after_checking_burns_no_submit_on_restart(
        self, tmp_path: Path,
    ) -> None:
        out = _child(tmp_path, "init")
        assert "INIT OK" in out
        out = _child(tmp_path, "after-checking")
        assert "CHECKING" in out
        # A provable no-submit CHECKING (complete tokens, no broker call
        # evidence) is burned on the restart scan — the durable row lands
        # NO_SUBMIT (the burn CLEAR result proves neutralisation) and no
        # uncertainty guard is raised.
        recovered = _child(tmp_path, "recover")
        assert '"hard": 0' in recovered, recovered
        assert '"states": ["CLEAR"]' in recovered, recovered
        burned = _child(tmp_path, "check-burned")
        assert "STATE NO_SUBMIT" in burned, burned
        assert _journal_submits(tmp_path / "broker.journal") == 0

    def test_killed_after_submitting_stays_hard_no_broker_discovery(
        self, tmp_path: Path,
    ) -> None:
        _child(tmp_path, "init")
        out = _child(tmp_path, "after-submitting")
        assert "SUBMITTING" in out
        # SUBMITTING with no ID is uncertain: hard + quarantine, and the
        # recovery NEVER submits to discover the outcome.
        recovered = _child(tmp_path, "recover")
        assert '"hard": 1' in recovered, recovered
        assert "SPY.US" in recovered, recovered
        assert _journal_submits(tmp_path / "broker.journal") == 0

    def test_killed_accepted_unbound_hard_no_adopt(self, tmp_path: Path) -> None:
        _child(tmp_path, "init")
        out = _child(tmp_path, "accepted-unbound")
        assert "ACCEPTED" in out
        journal = tmp_path / "broker.journal"
        assert _journal_submits(journal) == 1
        # No ID bound => cannot adopt; recovery must not resubmit or clear.
        recovered = _child(tmp_path, "recover")
        assert '"hard": 1' in recovered, recovered
        assert _journal_submits(journal) == 1  # no discovery mutation

    def test_killed_bound_reports_hard_pending_verification(
        self, tmp_path: Path,
    ) -> None:
        _child(tmp_path, "init")
        out = _child(tmp_path, "bound")
        assert "BOUND" in out
        recovered = _child(tmp_path, "recover")
        # Bound row with a live SUBMITTED status: pending verification /
        # quarantine + possible restore ref — never auto-clear.
        assert "SPY.US" in recovered, recovered
        assert '"complete": true' in recovered


# ===========================================================================
# REAL entry lifecycle children (review1 remediation): reserve/execute via
# the REAL public path with a durable-journal fake broker, os._exit(9) at
# the exact checkpoint. Sources live in crash_children_sources.py.
# ===========================================================================

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from crash_children_sources import (  # noqa: E402
    LIFECYCLE_CHILD,
    RUNNER_CHILD,
)


def _lifecycle_child(tmp: Path, phase: str) -> str:
    db_path = tmp / "lifecycle.db"
    journal = tmp / "lifecycle.journal"
    harness = tmp / f"lc_{phase}.py"
    harness.write_text(
        LIFECYCLE_CHILD.format(backend_root=_BACKEND_ROOT),
    )
    result = subprocess.run(
        [
            sys.executable, str(harness), phase, str(db_path),
            str(journal),
        ],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AUTO_TRADE_ENV": "test"},
        cwd=_BACKEND_ROOT,
    )
    if result.returncode != 9:
        raise AssertionError(
            f"lifecycle child {phase} rc={result.returncode}: "
            f"{result.stderr[-2000:]}"
        )
    return result.stdout


def _journal_lines(journal: Path, needle: str) -> int:
    if not journal.exists():
        return 0
    return sum(
        1
        for line in journal.read_text().splitlines()
        if needle in line
    )


class TestRealLifecycleCrash:
    """REAL lifecycle children (journal + actual durable transitions)."""

    def test_die_after_checking_journal_zero(self, tmp_path: Path) -> None:
        out = _lifecycle_child(tmp_path, "die-after-checking")
        assert "LIFECYCLE CHECKING" in out
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 0

    def test_die_after_submitting_journal_zero(self, tmp_path: Path) -> None:
        out = _lifecycle_child(tmp_path, "die-after-submitting")
        assert "LIFECYCLE SUBMITTING" in out
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 0

    def test_die_accepted_before_return_journal_one(
        self, tmp_path: Path,
    ) -> None:
        _lifecycle_child(tmp_path, "accepted-before-return")
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 1

    def test_die_idbound_before_record_journal_one(
        self, tmp_path: Path,
    ) -> None:
        _lifecycle_child(tmp_path, "idbound-before-record")
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 1


def _runner_child(tmp: Path) -> str:
    db_path = tmp / "lifecycle.db"
    journal = tmp / "lifecycle.journal"
    harness = tmp / "runner_recover.py"
    harness.write_text(
        RUNNER_CHILD.format(backend_root=_BACKEND_ROOT),
    )
    result = subprocess.run(
        [
            sys.executable, str(harness), str(db_path), str(journal),
        ],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "AUTO_TRADE_ENV": "test"},
        cwd=_BACKEND_ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"runner recovery child rc={result.returncode}: "
            f"{result.stderr[-2000:]}"
        )
    return result.stdout


class TestRealRunnerRecovery:
    """NEW process runs a REAL AppRunner recovery over the crashed DB."""

    def test_accepted_unbound_new_runner_no_duplicate(self, tmp_path: Path) -> None:
        _lifecycle_child(tmp_path, "accepted-before-return")
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 1
        with sqlite3.connect(tmp_path / "lifecycle.db") as db:
            assert db.execute(
                "SELECT submit_state, bound_broker_order_id FROM passive_mandates"
            ).fetchone() == ("SUBMITTING", None)
        out = _runner_child(tmp_path)
        assert "RUNNER_RECOVER" in out
        assert '"broker_submits": 0' in out, out
        assert '"hooks_wired": true' in out, out
        assert '"full_initialize": true' in out, out
        assert '"resume_denied": true' in out, out
        assert '"broker_cancels": 0' in out, out
        assert '"new_exposure_denied": true' in out, out
        assert '"quarantined": ["SPY.US"]' in out, out
        assert _journal_lines(journal, '"event": "submit"') == 1
        assert _journal_lines(journal, "recovery_submit") == 0

    def test_idbound_new_runner_no_duplicate(self, tmp_path: Path) -> None:
        _lifecycle_child(tmp_path, "idbound-before-record")
        journal = tmp_path / "lifecycle.journal"
        assert _journal_lines(journal, '"event": "submit"') == 1
        out = _runner_child(tmp_path)
        assert '"broker_submits": 0' in out, out
        assert '"full_initialize": true' in out, out
        assert '"resume_denied": true' in out, out
        assert _journal_lines(journal, '"event": "submit"') == 1
