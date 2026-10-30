from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, LLMInteraction
from app.services.llm_symbol_state_service import LLMSymbolStateService


def test_hourly_budget_counts_failed_calls_but_excludes_previews_and_old_calls() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.tables["llm_interactions"].create(engine)
    now = datetime(2026, 10, 31, 1, tzinfo=timezone.utc)
    try:
        with Session(engine) as db:
            for kind, success, age in (
                ("analyze", True, 10),
                ("analyze", False, 20),
                ("analyze", False, 30),
                ("preview", True, 10),
                ("preview", False, 10),
                ("analyze", True, 61),
                ("analyze", False, 61),
            ):
                db.add(LLMInteraction(
                    interaction_type=kind,
                    symbol="TSLA.US",
                    success=success,
                    created_at=now - timedelta(minutes=age),
                ))
            db.commit()
            assert LLMSymbolStateService(db).count_analyses_last_hour(now) == 3
    finally:
        engine.dispose()
