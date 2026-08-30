from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    StrategyV2ForwardEvidence,
    StrategyV2ForwardRegistration,
    StrategyV2ShadowDecision,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return Session(bind=engine)


def _decision(
    *,
    bar_at: datetime,
    suffix: str,
    action: str = "WAIT",
    reason: str = "ZSCORE_5M_NOT_OVERSOLD",
    gate_passed: bool = False,
    breach_armed: bool = False,
    state_before: str = "READY",
    state_after: str = "READY",
) -> StrategyV2ShadowDecision:
    return StrategyV2ShadowDecision(
        idempotency_key=f"decision-{suffix}",
        symbol="NVDA.US",
        market="US",
        config_version="version-a",
        session_date=bar_at.date(),
        bar_at=bar_at,
        observed_at=bar_at + timedelta(minutes=1),
        action=action,
        reason=reason,
        state_before=state_before,
        state_after=state_after,
        close_price=100.0,
        gate_passed=gate_passed,
        breach_armed=breach_armed,
        virtual_position="FLAT",
        quantity=0.0,
        exit_reason="",
        gate_reasons_json="[]",
        features_json="{}",
        created_at=bar_at + timedelta(minutes=1),
    )


def test_prune_expired_wait_decisions_keeps_material_evidence() -> None:
    db = _session()
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    old = now - timedelta(days=60)
    rows = {
        "routine_old": _decision(bar_at=old, suffix="routine-old"),
        "routine_recent": _decision(
            bar_at=now - timedelta(days=5),
            suffix="routine-recent",
        ),
        "action": _decision(
            bar_at=old,
            suffix="action",
            action="ARM_LONG",
        ),
        "eligible": _decision(
            bar_at=old,
            suffix="eligible",
            gate_passed=True,
            reason="NO_BREACH",
        ),
        "armed": _decision(
            bar_at=old,
            suffix="armed",
            breach_armed=True,
            reason="WAITING_FOR_RECLAIM",
            state_before="ARMED_LONG",
            state_after="ARMED_LONG",
        ),
        "transition": _decision(
            bar_at=old,
            suffix="transition",
            state_after="COLD",
        ),
        "incomplete": _decision(
            bar_at=old,
            suffix="incomplete",
            reason="SESSION_DATA_INCOMPLETE",
        ),
    }
    db.add_all(rows.values())
    db.commit()
    ids = {name: row.id for name, row in rows.items()}

    result = StrategyV2ShadowService(db).prune_expired_wait_decisions(
        retention_days=45,
        batch_size=10,
        now=now,
    )

    assert result.deleted == 1
    assert result.batches == 1
    assert db.get(StrategyV2ShadowDecision, ids["routine_old"]) is None
    for name in (
        "routine_recent",
        "action",
        "eligible",
        "armed",
        "transition",
        "incomplete",
    ):
        assert db.get(StrategyV2ShadowDecision, ids[name]) is not None


def test_prune_expired_wait_decisions_is_bounded_and_can_be_disabled() -> None:
    db = _session()
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db.add_all([
        _decision(
            bar_at=now - timedelta(days=60, minutes=index),
            suffix=str(index),
        )
        for index in range(5)
    ])
    db.commit()
    service = StrategyV2ShadowService(db)

    disabled = service.prune_expired_wait_decisions(
        retention_days=0,
        batch_size=2,
        now=now,
    )
    bounded = service.prune_expired_wait_decisions(
        retention_days=45,
        batch_size=2,
        max_batches=1,
        now=now,
    )

    assert disabled.deleted == 0
    assert disabled.batches == 0
    assert bounded.deleted == 2
    assert bounded.batches == 1
    assert db.query(StrategyV2ShadowDecision).count() == 3


def test_wait_prune_scopes_replay_checks_to_each_candidate_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    old = now - timedelta(days=60)
    db.add_all([
        _decision(
            bar_at=old + timedelta(minutes=index),
            suffix=f"scoped-{index}",
        )
        for index in range(3)
    ])
    db.commit()
    fence_calls = 0
    replay_check_calls: list[set[tuple[str, str, date]]] = []

    def _fence(session: Session) -> object:
        nonlocal fence_calls
        assert session is db
        assert session.in_transaction() is False
        fence_calls += 1
        return object()

    service = StrategyV2ShadowService(db, transaction_fence=_fence)

    def _scoped_replay_check(
        *,
        candidate_sessions: set[tuple[str, str, date]],
        not_before: datetime | None = None,
    ) -> set[tuple[str, str, date]]:
        assert candidate_sessions
        assert not_before is None
        assert len(candidate_sessions) <= 2
        replay_check_calls.append(set(candidate_sessions))
        return set()

    monkeypatch.setattr(
        service,
        "_forward_sessions_requiring_replay_source",
        _scoped_replay_check,
    )

    result = service.prune_expired_wait_decisions(
        retention_days=45,
        batch_size=2,
        max_batches=None,
        now=now,
    )

    assert result.deleted == 3
    assert result.batches == 2
    assert fence_calls == 2
    assert len(replay_check_calls) == 4
    assert replay_check_calls[0] == replay_check_calls[1]
    assert replay_check_calls[2] == replay_check_calls[3]


def test_replay_protection_validates_only_requested_candidate_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _session()
    target_day = date(2026, 5, 20)
    registered_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    candidate_registration = StrategyV2ForwardRegistration(
        symbol="NVDA.US",
        market="US",
        candidate_algorithm_version="candidate-v1",
        source_config_version="version-a",
        evaluator_digest="evaluator-a",
        candidate_spec_json="{}",
        registered_at=registered_at,
        eligible_after=registered_at,
    )
    unrelated_registration = StrategyV2ForwardRegistration(
        symbol="AAPL.US",
        market="US",
        candidate_algorithm_version="candidate-v1",
        source_config_version="version-b",
        evaluator_digest="evaluator-b",
        candidate_spec_json="{}",
        registered_at=registered_at,
        eligible_after=registered_at,
    )
    db.add_all([candidate_registration, unrelated_registration])
    db.flush()

    def _evidence(
        registration_id: int,
        session_day: date,
    ) -> StrategyV2ForwardEvidence:
        return StrategyV2ForwardEvidence(
            registration_id=registration_id,
            target_session_date=session_day,
            seed_session_date=session_day - timedelta(days=1),
            target_open_at=datetime.combine(
                session_day,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            evaluated_at=datetime.combine(
                session_day,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            disposition="INCLUDED",
        )

    candidate = _evidence(candidate_registration.id, target_day)
    unrelated_symbol = _evidence(unrelated_registration.id, target_day)
    unrelated_day = _evidence(
        candidate_registration.id,
        target_day + timedelta(days=30),
    )
    db.add_all([candidate, unrelated_symbol, unrelated_day])
    db.commit()

    checked_evidence_ids: list[int] = []

    def _not_prune_safe(
        evidence: StrategyV2ForwardEvidence,
        _registration: StrategyV2ForwardRegistration,
    ) -> bool:
        checked_evidence_ids.append(evidence.id)
        return False

    service = StrategyV2ShadowService(db)
    monkeypatch.setattr(
        service,
        "_forward_replay_artifact_is_prune_safe",
        _not_prune_safe,
    )
    candidate_session = ("NVDA.US", "version-a", target_day)

    protected = service._forward_sessions_requiring_replay_source(
        candidate_sessions={candidate_session},
    )

    assert protected == {candidate_session}
    assert checked_evidence_ids == [candidate.id]


def test_prune_expired_wait_decisions_validates_limits() -> None:
    service = StrategyV2ShadowService(_session())

    with pytest.raises(ValueError, match="non-negative"):
        service.prune_expired_wait_decisions(
            retention_days=-1,
            batch_size=10,
        )
    with pytest.raises(ValueError, match="positive"):
        service.prune_expired_wait_decisions(
            retention_days=45,
            batch_size=0,
        )


def _forward_registration(db: Session) -> StrategyV2ForwardRegistration:
    registered_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    registration = StrategyV2ForwardRegistration(
        symbol="NVDA.US",
        market="US",
        candidate_algorithm_version="strategy-v2-causal-trend-prewarm-v1",
        source_config_version="version-a",
        evaluator_digest="evaluator-a",
        candidate_spec_json="{}",
        registered_at=registered_at,
        eligible_after=registered_at,
    )
    db.add(registration)
    db.flush()
    return registration


def _included_evidence(
    db: Session,
    registration_id: int,
    *,
    session_day: date,
    evaluated_at: datetime,
) -> StrategyV2ForwardEvidence:
    evidence = StrategyV2ForwardEvidence(
        registration_id=registration_id,
        target_session_date=session_day,
        seed_session_date=session_day,
        target_open_at=datetime.combine(
            session_day,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        evaluated_at=evaluated_at,
        disposition="INCLUDED",
    )
    db.add(evidence)
    db.flush()
    return evidence


def test_prune_expired_diagnostic_wait_decisions_deletes_old_wait_keeps_actions() -> None:
    db = _session()
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    old = now - timedelta(days=120)
    rows = {
        "gate_old": _decision(
            bar_at=old, suffix="gate-old", gate_passed=True, reason="NO_BREACH"
        ),
        "armed_old": _decision(
            bar_at=old,
            suffix="armed-old",
            breach_armed=True,
            reason="WAITING_FOR_RECLAIM",
            state_before="ARMED_LONG",
            state_after="ARMED_LONG",
        ),
        "transition_old": _decision(
            bar_at=old, suffix="transition-old", state_after="COLD"
        ),
        "incomplete_old": _decision(
            bar_at=old,
            suffix="incomplete-old",
            reason="SESSION_DATA_INCOMPLETE",
        ),
        "routine_old": _decision(bar_at=old, suffix="routine-old"),
        "gate_recent": _decision(
            bar_at=now - timedelta(days=5),
            suffix="gate-recent",
            gate_passed=True,
            reason="NO_BREACH",
        ),
        "action_arm": _decision(
            bar_at=old, suffix="action-arm", action="ARM_LONG"
        ),
        "action_fill": _decision(
            bar_at=old, suffix="action-fill", action="FILL_ENTRY"
        ),
    }
    db.add_all(rows.values())
    db.commit()
    ids = {name: row.id for name, row in rows.items()}

    result = StrategyV2ShadowService(db).prune_expired_diagnostic_wait_decisions(
        retention_days=90,
        batch_size=10,
        now=now,
    )

    # Then: every old WAIT class (gate/armed/transition/incomplete/routine)
    # is deleted by the diagnostic window...
    assert result.deleted == 5
    for name in (
        "gate_old",
        "armed_old",
        "transition_old",
        "incomplete_old",
        "routine_old",
    ):
        assert db.get(StrategyV2ShadowDecision, ids[name]) is None
    # ...and recent WAIT rows plus all action rows survive.
    assert db.get(StrategyV2ShadowDecision, ids["gate_recent"]) is not None
    assert db.get(StrategyV2ShadowDecision, ids["action_arm"]) is not None
    assert db.get(StrategyV2ShadowDecision, ids["action_fill"]) is not None


def test_prune_expired_diagnostic_wait_decisions_zero_disables() -> None:
    db = _session()
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    db.add(_decision(bar_at=now - timedelta(days=120), suffix="old"))
    db.commit()

    result = StrategyV2ShadowService(db).prune_expired_diagnostic_wait_decisions(
        retention_days=0,
        batch_size=10,
        now=now,
    )

    assert result.deleted == 0
    assert db.query(StrategyV2ShadowDecision).count() == 1


def test_prune_expired_diagnostic_wait_decisions_validates_limits() -> None:
    service = StrategyV2ShadowService(_session())

    with pytest.raises(ValueError, match="non-negative"):
        service.prune_expired_diagnostic_wait_decisions(
            retention_days=-1,
            batch_size=10,
        )
    with pytest.raises(ValueError, match="positive"):
        service.prune_expired_diagnostic_wait_decisions(
            retention_days=90,
            batch_size=0,
        )


def test_diagnostic_prune_respects_and_lifts_forward_replay_protection() -> None:
    db = _session()
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    old = now - timedelta(days=120)
    session_day = old.date()
    registration = _forward_registration(db)
    _included_evidence(
        db,
        registration.id,
        session_day=session_day,
        evaluated_at=now - timedelta(days=10),
    )
    db.add(
        _decision(
            bar_at=old,
            suffix="protected",
            gate_passed=True,
            reason="NO_BREACH",
        )
    )
    db.commit()

    service = StrategyV2ShadowService(db)
    protected = service.prune_expired_diagnostic_wait_decisions(
        retention_days=90,
        batch_size=10,
        now=now,
    )

    # Then: an unarchived INCLUDED session still protects its decision rows.
    assert protected.deleted == 0
    assert db.query(StrategyV2ShadowDecision).count() == 1

    lifted = service.prune_expired_diagnostic_wait_decisions(
        retention_days=90,
        batch_size=10,
        now=now,
        replay_source_retention_days=5,
    )

    # Then: evidence older than the replay-source window no longer protects.
    assert lifted.deleted == 1
    assert db.query(StrategyV2ShadowDecision).count() == 0


def test_routine_wait_prune_honors_replay_source_retention_window() -> None:
    db = _session()
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    old = now - timedelta(days=120)
    session_day = old.date()
    registration = _forward_registration(db)
    _included_evidence(
        db,
        registration.id,
        session_day=session_day,
        evaluated_at=now - timedelta(days=60),
    )
    db.add(_decision(bar_at=old, suffix="routine"))
    db.commit()

    service = StrategyV2ShadowService(db)
    protected = service.prune_expired_wait_decisions(
        retention_days=14,
        batch_size=10,
        now=now,
    )
    lifted = service.prune_expired_wait_decisions(
        retention_days=14,
        batch_size=10,
        now=now,
        replay_source_retention_days=30,
    )

    # Then: the replay-source window lifts protection for old evidence only
    # when explicitly configured; the default behavior is unchanged.
    assert protected.deleted == 0
    assert lifted.deleted == 1
    assert db.query(StrategyV2ShadowDecision).count() == 0
