from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from app.models import LLMInteraction, LLMSymbolScheduleState


class LLMSymbolStateService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_state(self, symbol: str, market: str) -> LLMSymbolScheduleState:
        state = self.db.query(LLMSymbolScheduleState).filter(LLMSymbolScheduleState.symbol == symbol).first()
        if state is None:
            state = LLMSymbolScheduleState(symbol=symbol, market=market)
            self.db.add(state)
            self.db.flush()
        else:
            state.market = market
        return state

    def record_analysis(
        self,
        symbol: str,
        market: str,
        *,
        analyzed_at: datetime,
        next_analysis_at: datetime | None,
    ) -> LLMSymbolScheduleState:
        state = self.get_state(symbol, market)
        state.last_analysis_at = analyzed_at
        state.next_analysis_at = next_analysis_at
        state.last_status = "ANALYZED"
        state.last_skip_reason = ""
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        return state

    def record_skip(
        self,
        symbol: str,
        market: str,
        reason: str,
        *,
        next_analysis_at: datetime | None,
    ) -> LLMSymbolScheduleState:
        state = self.get_state(symbol, market)
        state.next_analysis_at = next_analysis_at
        state.last_status = "SKIPPED"
        state.last_skip_reason = reason
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        return state

    def record_failure(
        self,
        symbol: str,
        market: str,
        reason: str,
        *,
        next_analysis_at: datetime | None,
    ) -> LLMSymbolScheduleState:
        state = self.get_state(symbol, market)
        state.next_analysis_at = next_analysis_at
        state.last_status = "FAILED"
        state.last_skip_reason = reason
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        return state

    def states_by_symbol(self) -> dict[str, LLMSymbolScheduleState]:
        rows = self.db.query(LLMSymbolScheduleState).all()
        return {row.symbol: row for row in rows}

    def count_analyses_last_hour(self, now: datetime) -> int:
        cutoff = now - timedelta(hours=1)
        return (
            self.db.query(LLMInteraction)
            .filter(LLMInteraction.interaction_type == "analyze")
            .filter(LLMInteraction.success.is_(True))
            .filter(LLMInteraction.created_at >= cutoff)
            .count()
        )
