from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import LLMInteraction
from app.schemas import (
    LLMUsageDailySummary,
    LLMUsageSummaryResponse,
    LLMUsageTypeSummary,
)


class LLMUsageService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def summary(self, days: int) -> LLMUsageSummaryResponse:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        totals = (
            self.db.query(
                func.count(LLMInteraction.id),
                func.coalesce(
                    func.sum(case((LLMInteraction.success.is_(True), 1), else_=0)),
                    0,
                ),
                func.coalesce(func.sum(LLMInteraction.prompt_tokens), 0),
                func.coalesce(func.sum(LLMInteraction.completion_tokens), 0),
                func.coalesce(func.sum(LLMInteraction.total_tokens), 0),
            )
            .filter(LLMInteraction.created_at >= cutoff)
            .one()
        )
        day = func.date(LLMInteraction.created_at)
        day_rows = (
            self.db.query(
                day,
                func.count(LLMInteraction.id),
                func.coalesce(func.sum(LLMInteraction.prompt_tokens), 0),
                func.coalesce(func.sum(LLMInteraction.completion_tokens), 0),
                func.coalesce(func.sum(LLMInteraction.total_tokens), 0),
            )
            .filter(LLMInteraction.created_at >= cutoff)
            .group_by(day)
            .order_by(day.asc())
            .all()
        )
        type_rows = (
            self.db.query(
                LLMInteraction.interaction_type,
                func.count(LLMInteraction.id),
                func.coalesce(func.sum(LLMInteraction.total_tokens), 0),
            )
            .filter(LLMInteraction.created_at >= cutoff)
            .group_by(LLMInteraction.interaction_type)
            .order_by(LLMInteraction.interaction_type.asc())
            .all()
        )
        return LLMUsageSummaryResponse(
            days=days,
            total_interactions=int(totals[0]),
            successful_interactions=int(totals[1]),
            total_prompt_tokens=int(totals[2]),
            total_completion_tokens=int(totals[3]),
            total_tokens=int(totals[4]),
            by_day=[
                LLMUsageDailySummary(
                    date=str(row[0]),
                    interactions=int(row[1]),
                    prompt_tokens=int(row[2]),
                    completion_tokens=int(row[3]),
                    total_tokens=int(row[4]),
                )
                for row in day_rows
            ],
            by_type=[
                LLMUsageTypeSummary(
                    interaction_type=str(row[0]),
                    interactions=int(row[1]),
                    total_tokens=int(row[2]),
                )
                for row in type_rows
            ],
        )
