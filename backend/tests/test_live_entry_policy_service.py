from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    OrderRecord,
    StrategyV2ShadowConfig,
    StrategyV2ShadowDecision,
    StrategyV2ShadowState,
)
from app.services.live_entry_policy_service import LiveEntryPolicyService

_NOW = datetime(2026, 7, 23, 15, 0, tzinfo=timezone.utc)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _service(
    db: Session,
    *,
    gate: bool = True,
    max_entries: int = 2,
) -> LiveEntryPolicyService:
    return LiveEntryPolicyService(
        db,
        regime_gate_enabled=gate,
        max_data_age_seconds=600,
        max_entries_per_symbol_per_day=max_entries,
        now_provider=lambda: _NOW,
    )


def _add_shadow_evidence(
    db: Session,
    *,
    gate_passed: bool = True,
    bar_at: datetime | None = None,
    session_date: date = date(2026, 7, 23),
    last_poll_error: str = "",
) -> None:
    version = "v1"
    db.add(
        StrategyV2ShadowConfig(
            symbol="NVDA.US",
            enabled=True,
        )
    )
    db.add(
        StrategyV2ShadowState(
            symbol="NVDA.US",
            config_version=version,
            session_date=session_date,
            last_bar_at=bar_at or _NOW - timedelta(minutes=1),
            last_poll_error=last_poll_error,
        )
    )
    db.add(
        StrategyV2ShadowDecision(
            idempotency_key="decision-1",
            symbol="NVDA.US",
            market="US",
            config_version=version,
            session_date=session_date,
            bar_at=bar_at or _NOW - timedelta(minutes=1),
            observed_at=_NOW,
            gate_passed=gate_passed,
            gate_reasons_json=json.dumps(
                [] if gate_passed else ["ADX_REGIME_BLOCKED"]
            ),
            adx_5m=24.0,
        )
    )
    db.commit()


def test_gate_disabled_allows_entry_under_daily_cap() -> None:
    db = _db()
    try:
        assert _service(db, gate=False).evaluate(
            "NVDA.US",
            "BUY",
            "US",
        ) is None
    finally:
        db.close()


def test_daily_entry_cap_blocks_before_regime_lookup() -> None:
    db = _db()
    try:
        for index in range(2):
            db.add(
                OrderRecord(
                    broker_order_id=f"entry-{index}",
                    symbol="NVDA.US",
                    side="BUY",
                    quantity=1,
                    price=200,
                    executed_quantity=1,
                    executed_price=200,
                    status="FILLED",
                    filled_at=_NOW - timedelta(hours=index + 1),
                )
            )
        db.commit()

        result = _service(db).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.skip_category == "COOLDOWN"
        assert result.details["entry_policy"] == "DAILY_ENTRY_CAP"
        assert result.details["entries_today"] == 2
    finally:
        db.close()


def test_daily_entry_cap_counts_cancelled_partial_fill() -> None:
    db = _db()
    try:
        db.add(
            OrderRecord(
                broker_order_id="partial-then-cancelled",
                symbol="NVDA.US",
                side="BUY",
                quantity=10,
                price=200,
                executed_quantity=3,
                executed_price=200,
                status="CANCELLED",
                broker_updated_at=_NOW - timedelta(minutes=30),
            )
        )
        db.commit()

        result = _service(
            db,
            gate=False,
            max_entries=1,
        ).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.skip_category == "COOLDOWN"
        assert result.details["entries_today"] == 1
    finally:
        db.close()


def test_cancelled_partial_execution_counts_toward_daily_cap() -> None:
    db = _db()
    try:
        db.add(
            OrderRecord(
                broker_order_id="cancelled-partial-entry",
                symbol="NVDA.US",
                side="BUY",
                quantity=100,
                price=200,
                executed_quantity=10,
                executed_price=200,
                status="CANCELLED",
                filled_at=_NOW - timedelta(hours=1),
            )
        )
        db.commit()

        result = _service(
            db,
            gate=False,
            max_entries=1,
        ).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.details["entries_today"] == 1
    finally:
        db.close()


def test_zero_execution_partial_status_does_not_count_as_entry() -> None:
    db = _db()
    try:
        db.add(
            OrderRecord(
                broker_order_id="empty-partial-entry",
                symbol="NVDA.US",
                side="BUY",
                quantity=100,
                price=200,
                executed_quantity=0,
                status="PARTIAL_FILLED",
                filled_at=_NOW - timedelta(hours=1),
            )
        )
        db.commit()

        assert _service(
            db,
            gate=False,
            max_entries=1,
        ).evaluate("NVDA.US", "BUY", "US") is None
    finally:
        db.close()


def test_missing_shadow_state_fails_closed() -> None:
    db = _db()
    try:
        result = _service(db).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.skip_category == "REGIME"
        assert result.details["policy_reason"] == "SHADOW_GATE_DISABLED"
    finally:
        db.close()


def test_fresh_passing_shadow_gate_allows_entry() -> None:
    db = _db()
    try:
        _add_shadow_evidence(db)

        assert _service(db).evaluate(
            "NVDA.US",
            "BUY",
            "US",
        ) is None
    finally:
        db.close()


def test_rejected_shadow_gate_exposes_regime_evidence() -> None:
    db = _db()
    try:
        _add_shadow_evidence(db, gate_passed=False)

        result = _service(db).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.skip_category == "REGIME"
        assert result.details["policy_reason"] == "SHADOW_REGIME_REJECTED"
        assert result.details["gate_reasons"] == ["ADX_REGIME_BLOCKED"]
        assert result.details["adx_5m"] == 24.0
    finally:
        db.close()


def test_stale_shadow_bar_fails_closed() -> None:
    db = _db()
    try:
        _add_shadow_evidence(db, bar_at=_NOW - timedelta(minutes=11))

        result = _service(db).evaluate("NVDA.US", "BUY", "US")

        assert result is not None
        assert result.details["policy_reason"] == "SHADOW_DECISION_STALE"
    finally:
        db.close()


def test_position_reduction_bypasses_policy() -> None:
    db = _db()
    try:
        assert _service(db).evaluate(
            "NVDA.US",
            "SELL",
            "US",
        ) is None
    finally:
        db.close()
