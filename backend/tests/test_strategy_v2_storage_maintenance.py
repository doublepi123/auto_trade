from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, StrategyV2ShadowDecision
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
