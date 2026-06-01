from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/test_llm_eval_{os.getpid()}.db"
)

from app.database import SessionLocal, engine as db_engine
from app.models import Base, LLMInteraction, OrderRecord, RuntimeStateSnapshot
from app.services.llm_recommendation_evaluator import LLMRecommendationEvaluator

Base.metadata.create_all(bind=db_engine)


def _clean_db() -> None:
    db = SessionLocal()
    try:
        for model in (OrderRecord, RuntimeStateSnapshot, LLMInteraction):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


from typing import Generator

@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    _clean_db()
    yield
    _clean_db()


def _make_interaction(
    db,
    *,
    symbol: str = "AAPL.US",
    order_action: str = "BUY_NOW",
    order_price: float | None = None,
    current_price: float = 100.0,
    recent_prices: list[float] | None = None,
    success: bool = True,
    created_at: datetime | None = None,
) -> LLMInteraction:
    parsed: dict[str, object] = {"order_action": order_action}
    if order_price is not None:
        parsed["order_price"] = order_price
    context: dict[str, object] = {
        "symbol": symbol,
        "current_price": current_price,
        "recent_prices": recent_prices or [],
    }
    record = LLMInteraction(
        interaction_type="analyze",
        symbol=symbol,
        market="US",
        prompt="test",
        raw_response="{}",
        parsed_response=json.dumps(parsed),
        context_snapshot=json.dumps(context),
        success=success,
        order_action=order_action,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _make_snapshot(
    db,
    *,
    last_price: float,
    created_at: datetime,
) -> RuntimeStateSnapshot:
    record = RuntimeStateSnapshot(
        engine_state="FLAT",
        paused=False,
        kill_switch=False,
        daily_pnl=0.0,
        consecutive_losses=0,
        last_price=last_price,
        last_trigger_price=0.0,
        created_at=created_at,
    )
    db.add(record)
    db.commit()
    return record


def _make_order(
    db,
    *,
    symbol: str = "AAPL.US",
    side: str = "BUY",
    status: str = "FILLED",
    created_at: datetime,
) -> OrderRecord:
    record = OrderRecord(
        broker_order_id="test",
        symbol=symbol,
        side=side,
        quantity=10,
        price=100.0,
        status=status,
        created_at=created_at,
    )
    db.add(record)
    db.commit()
    return record


class TestLLMRecommendationEvaluator:
    def test_insufficient_data_no_action(self) -> None:
        db = SessionLocal()
        try:
            _make_interaction(db, order_action="NONE")
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US")
            assert result["sample_count"] == 1
            assert result["samples"][0]["tag"] == "INSUFFICIENT_DATA"
        finally:
            db.close()

    def test_insufficient_data_missing_price(self) -> None:
        db = SessionLocal()
        try:
            record = LLMInteraction(
                interaction_type="analyze",
                symbol="AAPL.US",
                market="US",
                prompt="test",
                raw_response="{}",
                parsed_response=json.dumps({"order_action": "BUY_NOW"}),
                context_snapshot=json.dumps({}),
                success=True,
                order_action="BUY_NOW",
                created_at=datetime.now(timezone.utc),
            )
            db.add(record)
            db.commit()
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US")
            assert result["samples"][0]["tag"] == "INSUFFICIENT_DATA"
        finally:
            db.close()

    def test_insufficient_data_no_snapshots(self) -> None:
        db = SessionLocal()
        try:
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0)
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US")
            assert result["samples"][0]["tag"] == "INSUFFICIENT_DATA"
        finally:
            db.close()

    def test_effective_buy_direction(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=101.0, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "EFFECTIVE"
            assert result["hit_rate"] == 1.0
        finally:
            db.close()

    def test_effective_sell_direction(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="SELL_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=99.0, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "EFFECTIVE"
        finally:
            db.close()

    def test_ineffective_buy(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.2, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "INEFFECTIVE"
        finally:
            db.close()

    def test_risky_buy(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=94.0, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "RISKY"
        finally:
            db.close()

    def test_too_early_buy(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=96.0, created_at=now + timedelta(minutes=2))
            _make_snapshot(db, last_price=101.0, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "TOO_EARLY"
        finally:
            db.close()

    def test_too_late_buy(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(
                db,
                order_action="BUY_NOW",
                current_price=100.0,
                recent_prices=[98.0, 99.0, 100.0],
                created_at=now,
            )
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=101.0, created_at=now + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "TOO_LATE"
        finally:
            db.close()

    def test_order_execution_mentioned(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=101.0, created_at=now + timedelta(minutes=5))
            _make_order(db, symbol="AAPL.US", side="BUY", status="FILLED", created_at=now + timedelta(minutes=2))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=now + timedelta(hours=1))
            assert result["samples"][0]["tag"] == "EFFECTIVE"
            assert "order execution" in result["samples"][0]["reason"]
        finally:
            db.close()

    def test_filters_by_date_range(self) -> None:
        db = SessionLocal()
        try:
            t1 = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=t1)
            _make_interaction(db, order_action="SELL_NOW", current_price=100.0, created_at=t2)
            _make_snapshot(db, last_price=100.0, created_at=t1)
            _make_snapshot(db, last_price=101.0, created_at=t1 + timedelta(minutes=5))
            _make_snapshot(db, last_price=100.0, created_at=t2)
            _make_snapshot(db, last_price=99.0, created_at=t2 + timedelta(minutes=5))
            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=t2, end=t2 + timedelta(hours=1))
            assert result["sample_count"] == 1
            assert result["samples"][0]["order_action"] == "SELL_NOW"
        finally:
            db.close()

    def test_hit_rate_with_mixed_tags(self) -> None:
        db = SessionLocal()
        try:
            now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
            # First interaction: goes up to 101.0, clearly effective
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=now)
            _make_snapshot(db, last_price=100.0, created_at=now)
            _make_snapshot(db, last_price=101.0, created_at=now + timedelta(minutes=5))

            # Second interaction: 2 hours later, barely moves (ineffective)
            later = now + timedelta(hours=2)
            _make_interaction(db, order_action="BUY_NOW", current_price=100.0, created_at=later)
            _make_snapshot(db, last_price=100.0, created_at=later)
            _make_snapshot(db, last_price=100.2, created_at=later + timedelta(minutes=5))

            evaluator = LLMRecommendationEvaluator(db)
            result = evaluator.evaluate("AAPL.US", start=now, end=later + timedelta(hours=1))
            assert result["hit_rate"] == 0.5
            assert result["tag_distribution"]["EFFECTIVE"] == 1
            assert result["tag_distribution"]["INEFFECTIVE"] == 1
        finally:
            db.close()
