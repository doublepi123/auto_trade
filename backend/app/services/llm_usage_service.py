from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import LLMInteraction
from app.schemas import (
    LLMUsageBySymbol,
    LLMUsageBySymbolResponse,
    LLMUsageDailySummary,
    LLMUsageSummaryResponse,
    LLMUsageTypeSummary,
)

# Sentinel used to represent blank symbols explicitly in aggregates, rather
# than silently dropping rows whose ``symbol`` is the empty string.
UNSPECIFIED_SYMBOL = "UNSPECIFIED"


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

    def by_symbol(self, days: int, limit: int) -> LLMUsageBySymbolResponse:
        """Aggregate LLM usage by symbol (and market) over the last ``days``.

        Blank symbols are represented explicitly as ``UNSPECIFIED`` rather
        than silently dropped. Only a safe projection is exposed — no prompt,
        raw/parsed response, errors, order ids or context. Deterministic
        ordering: total tokens desc, interactions desc, then symbol/market
        asc. ``total_groups`` is the distinct group count before ``limit``.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # Project blank symbols to the UNSPECIFIED sentinel at the SQL layer so
        # grouping is deterministic and the sentinel survives ordering.
        symbol_expr = func.coalesce(
            func.nullif(LLMInteraction.symbol, ""),
            UNSPECIFIED_SYMBOL,
        )
        base = self.db.query(
            symbol_expr.label("symbol"),
            LLMInteraction.market,
            func.count(LLMInteraction.id).label("interactions"),
            func.coalesce(
                func.sum(case((LLMInteraction.success.is_(True), 1), else_=0)),
                0,
            ).label("successful_interactions"),
            func.coalesce(func.sum(LLMInteraction.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LLMInteraction.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(LLMInteraction.total_tokens), 0).label("total_tokens"),
            func.max(LLMInteraction.created_at).label("latest_interaction_at"),
        ).filter(LLMInteraction.created_at >= cutoff)

        # total_groups is the distinct (symbol, market) count before limit.
        total_groups = base.group_by(symbol_expr, LLMInteraction.market).count()

        rows = (
            base.group_by(symbol_expr, LLMInteraction.market)
            .order_by(
                func.sum(LLMInteraction.total_tokens).desc(),
                func.count(LLMInteraction.id).desc(),
                symbol_expr.asc(),
                LLMInteraction.market.asc(),
            )
            .limit(max(1, int(limit)))
            .all()
        )
        items: list[LLMUsageBySymbol] = []
        for row in rows:
            interactions = int(row.interactions)
            successful = int(row.successful_interactions)
            success_rate = (successful / interactions) if interactions else 0.0
            items.append(
                LLMUsageBySymbol(
                    symbol=str(row.symbol),
                    market=str(row.market),
                    interactions=interactions,
                    successful_interactions=successful,
                    success_rate=success_rate,
                    prompt_tokens=int(row.prompt_tokens),
                    completion_tokens=int(row.completion_tokens),
                    total_tokens=int(row.total_tokens),
                    latest_interaction_at=row.latest_interaction_at,
                )
            )
        return LLMUsageBySymbolResponse(
            days=days,
            limit=int(limit),
            total_groups=int(total_groups),
            items=items,
        )
