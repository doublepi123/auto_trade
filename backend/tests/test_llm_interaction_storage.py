from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.models import Base, ExperimentResult, LLMInteraction
from app.services.llm_interaction_service import LLMInteractionService


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return Session(bind=engine)


def _interaction(
    *,
    created_at: datetime,
    success: bool = True,
    order_action: str = "NONE",
    applied: bool = False,
    order_id: str | None = None,
    order_status: str | None = None,
    prompt_variant: str | None = None,
    context_snapshot: str = '{"current_price": 100}',
) -> LLMInteraction:
    return LLMInteraction(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        parsed_response="{}",
        context_snapshot=context_snapshot,
        success=success,
        order_action=order_action,
        applied=applied,
        order_id=order_id,
        order_status=order_status,
        prompt_variant=prompt_variant,
        created_at=created_at,
    )


def test_create_samples_large_recent_price_context_and_keeps_audit_fields() -> None:
    db = _session()
    prices = [
        {
            "symbol": "NVDA.US",
            "last_price": 100 + index / 10,
            "bid": 99 + index / 10,
            "ask": 101 + index / 10,
            "timestamp": f"2026-07-16T00:{index:02d}:00+00:00",
            "observed_at": f"2026-07-16T00:{index:02d}:01+00:00",
        }
        for index in range(100)
    ]

    record = LLMInteractionService(
        db,
        context_max_bytes=4096,
        recent_price_points=8,
    ).create(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        prompt="prompt",
        parsed_response={"order_action": "NONE"},
        context_snapshot={
            "symbol": "NVDA.US",
            "current_price": 109.9,
            "recent_prices": prices,
            "account_context": {"available_cash": 1234.5},
        },
        success=True,
    )

    stored = json.loads(record.context_snapshot)
    assert len(record.context_snapshot.encode("utf-8")) <= 4096
    assert len(stored["recent_prices"]) == 8
    assert stored["recent_prices"][0]["last_price"] == 100
    assert stored["recent_prices"][-1]["last_price"] == 109.9
    assert all("symbol" not in point for point in stored["recent_prices"])
    assert stored["current_price"] == 109.9
    assert stored["account_context"]["available_cash"] == 1234.5
    assert stored["_storage"]["original_bytes"] > 4096
    assert stored["_storage"]["recent_price_points"] == {
        "original": 100,
        "stored": 8,
    }


def test_create_leaves_small_custom_context_unchanged() -> None:
    db = _session()
    record = LLMInteractionService(db, context_max_bytes=4096).create(
        interaction_type="preview",
        symbol="AAPL.US",
        market="US",
        prompt="prompt",
        context_snapshot={"price": 120, "position": "flat"},
        success=True,
    )

    assert json.loads(record.context_snapshot) == {"price": 120, "position": "flat"}


def test_context_byte_limit_holds_for_large_multibyte_and_timestamp_values() -> None:
    db = _session()
    record = LLMInteractionService(
        db,
        context_max_bytes=2048,
        recent_price_points=4,
    ).create(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        prompt="prompt",
        context_snapshot={
            "symbol": "NVDA.US",
            "current_price": 100.0,
            "recent_analysis": {"analysis": "趋势" * 10000},
            "recent_prices": [
                {"last_price": 99.0, "observed_at": "x" * 10000},
                {"last_price": 100.0, "observed_at": "y" * 10000},
            ],
        },
        success=True,
    )

    stored = json.loads(record.context_snapshot)
    assert len(record.context_snapshot.encode("utf-8")) <= 2048
    assert [point["last_price"] for point in stored["recent_prices"]] == [99.0, 100.0]


def test_context_hard_limit_holds_for_multibyte_core_fields() -> None:
    db = _session()
    core_keys = (
        "symbol",
        "market",
        "current_price",
        "current_buy_low",
        "current_sell_high",
        "short_selling",
        "current_position",
        "position_quantity",
        "position_avg_price",
        "unrealized_pnl_pct",
        "min_profit_amount",
    )
    record = LLMInteractionService(
        db,
        context_max_bytes=2048,
        recent_price_points=4,
    ).create(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        prompt="prompt",
        context_snapshot={key: '趋势"\n' * 10_000 for key in core_keys},
        success=True,
    )

    assert len(record.context_snapshot.encode("utf-8")) <= 2048
    assert isinstance(json.loads(record.context_snapshot), dict)


def test_context_hard_limit_drops_non_dict_prices_and_bounds_price_scalars() -> None:
    db = _session()
    record = LLMInteractionService(
        db,
        context_max_bytes=2048,
        recent_price_points=4,
    ).create(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        prompt="prompt",
        context_snapshot={
            "symbol": "NVDA.US",
            "recent_prices": [
                "中" * 10_000,
                ["x" * 10_000],
                {
                    "last_price": "中" * 10_000,
                    "bid": "买" * 10_000,
                    "ask": "卖" * 10_000,
                },
                {"last_price": 100.0},
            ],
        },
        success=True,
    )

    stored = json.loads(record.context_snapshot)
    assert len(record.context_snapshot.encode("utf-8")) <= 2048
    assert all(isinstance(point, dict) for point in stored["recent_prices"])
    assert any(point.get("last_price") == 100.0 for point in stored["recent_prices"])


def test_context_hard_limit_omits_nested_core_containers() -> None:
    db = _session()
    record = LLMInteractionService(
        db,
        context_max_bytes=2048,
        recent_price_points=4,
    ).create(
        interaction_type="analyze",
        symbol="NVDA.US",
        market="US",
        prompt="prompt",
        context_snapshot={
            "symbol": {"nested": ["x" * 100_000]},
            "market": ["x" * 100_000],
            "current_price": {"nested": {"value": "x" * 100_000}},
        },
        success=True,
    )

    stored = json.loads(record.context_snapshot)
    assert len(record.context_snapshot.encode("utf-8")) <= 2048
    for key in ("symbol", "market", "current_price"):
        assert not isinstance(stored.get(key), (dict, list))


def test_context_limit_validation_matches_runtime_settings() -> None:
    db = _session()
    with pytest.raises(ValueError, match="at least 2048"):
        LLMInteractionService(db, context_max_bytes=2047)


def test_prune_uses_short_window_only_for_routine_no_action_rows() -> None:
    db = _session()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    rows = {
        "routine_old": _interaction(created_at=now - timedelta(days=20)),
        "routine_recent": _interaction(created_at=now - timedelta(days=5)),
        "action_old": _interaction(
            created_at=now - timedelta(days=20), order_action="BUY_NOW"
        ),
        "failure_old": _interaction(
            created_at=now - timedelta(days=20), success=False
        ),
        "applied_old": _interaction(
            created_at=now - timedelta(days=20), applied=True
        ),
        "linked_old": _interaction(
            created_at=now - timedelta(days=20),
            order_id="broker-1",
            order_status="FILLED",
        ),
        "expired_action": _interaction(
            created_at=now - timedelta(days=100), order_action="SELL_NOW"
        ),
        "variant_old": _interaction(
            created_at=now - timedelta(days=20), prompt_variant="experiment-a"
        ),
        "variant_expired": _interaction(
            created_at=now - timedelta(days=100), prompt_variant="experiment-b"
        ),
        "referenced_expired": _interaction(
            created_at=now - timedelta(days=100)
        ),
    }
    db.add_all(rows.values())
    db.flush()
    db.add(ExperimentResult(
        experiment_name="retention-test",
        variant_name="referenced",
        interaction_id=rows["referenced_expired"].id,
    ))
    db.commit()
    ids = {name: row.id for name, row in rows.items()}

    result = LLMInteractionService(db).prune_expired(
        retention_days=90,
        no_action_retention_days=14,
        batch_size=10,
        now=now,
    )

    assert result.deleted == 3
    assert db.get(LLMInteraction, ids["routine_old"]) is None
    assert db.get(LLMInteraction, ids["expired_action"]) is None
    assert db.get(LLMInteraction, ids["variant_expired"]) is None
    for name in (
        "routine_recent",
        "action_old",
        "failure_old",
        "applied_old",
        "linked_old",
        "variant_old",
        "referenced_expired",
    ):
        assert db.get(LLMInteraction, ids[name]) is not None


def test_prune_limits_each_online_run_to_bounded_batches() -> None:
    db = _session()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    db.add_all(
        [_interaction(created_at=now - timedelta(days=20)) for _ in range(5)]
    )
    db.commit()

    result = LLMInteractionService(db).prune_expired(
        retention_days=90,
        no_action_retention_days=14,
        batch_size=2,
        max_batches=1,
        now=now,
    )

    assert result.deleted == 2
    assert result.batches == 1
    assert db.query(LLMInteraction).count() == 3


def test_prune_rechecks_expiration_predicate_before_delete(
    monkeypatch,
) -> None:
    db = _session()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    target = _interaction(created_at=now - timedelta(days=20))
    db.add(target)
    db.commit()
    original_delete = Query.delete
    mutated = False

    def mutate_before_delete(query, *args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            db.query(LLMInteraction).filter(
                LLMInteraction.id == target.id
            ).update({LLMInteraction.applied: True}, synchronize_session=False)
            db.flush()
        return original_delete(query, *args, **kwargs)

    monkeypatch.setattr(Query, "delete", mutate_before_delete)

    result = LLMInteractionService(db).prune_expired(
        retention_days=90,
        no_action_retention_days=14,
        batch_size=10,
        now=now,
    )

    assert mutated is True
    assert result.deleted == 0
    assert db.get(LLMInteraction, target.id) is not None


def test_compact_oversized_contexts_rewrites_legacy_rows_in_batches() -> None:
    db = _session()
    prices = [
        {"symbol": "NVDA.US", "last_price": float(index), "padding": "x" * 100}
        for index in range(80)
    ]
    db.add(
        LLMInteraction(
            interaction_type="analyze",
            symbol="NVDA.US",
            market="US",
            context_snapshot=json.dumps(
                {"current_price": 79.0, "recent_prices": prices}
            ),
            success=True,
        )
    )
    db.commit()

    result = LLMInteractionService(db).compact_oversized_contexts(
        max_bytes=2048,
        recent_price_points=4,
        batch_size=1,
    )

    record = db.query(LLMInteraction).one()
    stored = json.loads(record.context_snapshot)
    assert result.compacted == 1
    assert result.batches == 1
    assert len(record.context_snapshot.encode("utf-8")) <= 2048
    assert [point["last_price"] for point in stored["recent_prices"]] == [
        0.0,
        26.0,
        53.0,
        79.0,
    ]


def test_compaction_rewrites_bad_prefix_without_starving_later_rows() -> None:
    db = _session()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    malformed = '{"broken":' + "x" * 3000
    nested = json.dumps(
        {
            "symbol": {"nested": ["x" * 3000]},
            "recent_prices": ["中" * 1000],
        },
        ensure_ascii=False,
    )
    rows = [
        _interaction(created_at=now, context_snapshot=malformed)
        for _ in range(125)
    ]
    rows.extend(
        _interaction(created_at=now, context_snapshot=nested)
        for _ in range(125)
    )
    trailing_prices = [
        {"symbol": "NVDA.US", "last_price": float(index), "padding": "x" * 100}
        for index in range(80)
    ]
    trailing = _interaction(
        created_at=now,
        context_snapshot=json.dumps(
            {"current_price": 79.0, "recent_prices": trailing_prices}
        ),
    )
    rows.append(trailing)
    db.add_all(rows)
    db.commit()

    service = LLMInteractionService(db)
    first = service.compact_oversized_contexts(
        max_bytes=2048,
        recent_price_points=4,
        batch_size=25,
        max_rows=250,
    )

    assert first.inspected == 250
    assert first.compacted == 250
    assert first.batches == 10
    first_page = (
        db.query(LLMInteraction)
        .order_by(LLMInteraction.id.asc())
        .limit(250)
        .all()
    )
    assert all(
        len(row.context_snapshot.encode("utf-8")) <= 2048
        for row in first_page
    )
    assert all(isinstance(json.loads(row.context_snapshot), dict) for row in first_page)
    invalid_metadata = json.loads(first_page[0].context_snapshot)["_storage"]
    assert invalid_metadata["parse_error"] == "invalid_json"
    assert "invalid_json" in invalid_metadata["truncated_fields"]
    db.refresh(trailing)
    assert len(trailing.context_snapshot.encode("utf-8")) > 2048

    second = service.compact_oversized_contexts(
        max_bytes=2048,
        recent_price_points=4,
        batch_size=25,
        max_rows=250,
    )
    db.refresh(trailing)
    assert second.inspected == 1
    assert second.compacted == 1
    assert second.batches == 1
    assert len(trailing.context_snapshot.encode("utf-8")) <= 2048

    third = service.compact_oversized_contexts(
        max_bytes=2048,
        recent_price_points=4,
        batch_size=25,
        max_rows=250,
    )
    assert third.inspected == 0
    assert third.compacted == 0
    assert third.batches == 0


def test_compaction_treats_whitespace_wrapped_empty_object_as_valid_json() -> None:
    db = _session()
    row = _interaction(
        created_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        context_snapshot=" " * 3000 + "{}",
    )
    db.add(row)
    db.commit()

    result = LLMInteractionService(db).compact_oversized_contexts(
        max_bytes=2048,
        batch_size=1,
    )

    db.refresh(row)
    assert result.compacted == 1
    assert row.context_snapshot == "{}"


def test_storage_settings_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_TRADE_LLM_INTERACTION_RETENTION_DAYS", "120")
    monkeypatch.setenv("AUTO_TRADE_LLM_NO_ACTION_RETENTION_DAYS", "21")
    monkeypatch.setenv("AUTO_TRADE_LLM_CONTEXT_SNAPSHOT_MAX_BYTES", "32768")
    monkeypatch.setenv("AUTO_TRADE_LLM_STORAGE_MAINTENANCE_INTERVAL_MINUTES", "720")
    monkeypatch.setenv("AUTO_TRADE_LLM_STORAGE_MAINTENANCE_BATCH_SIZE", "500")

    configured = Settings()

    assert configured.llm_interaction_retention_days == 120
    assert configured.llm_no_action_retention_days == 21
    assert configured.llm_context_snapshot_max_bytes == 32768
    assert configured.llm_storage_maintenance_interval_minutes == 720
    assert configured.llm_storage_maintenance_batch_size == 500
