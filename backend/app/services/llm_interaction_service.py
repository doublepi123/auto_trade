from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import LLMInteraction


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class LLMInteractionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        interaction_type: str,
        symbol: str,
        market: str,
        prompt: str,
        raw_response: str = "",
        parsed_response: dict[str, Any] | None = None,
        context_snapshot: dict[str, Any] | None = None,
        success: bool,
        error: str = "",
        order_action: str = "NONE",
    ) -> LLMInteraction:
        record = LLMInteraction(
            interaction_type=interaction_type,
            symbol=symbol,
            market=market,
            prompt=prompt,
            raw_response=raw_response,
            parsed_response=_json_dumps(parsed_response or {}),
            context_snapshot=_json_dumps(context_snapshot or {}),
            success=success,
            error=error,
            order_action=order_action or "NONE",
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update_outcome(
        self,
        interaction_id: int | None,
        *,
        applied: bool | None = None,
        order_status: str | None = None,
        order_id: str | None = None,
    ) -> None:
        if interaction_id is None:
            return
        record = self.db.get(LLMInteraction, interaction_id)
        if record is None:
            return
        if applied is not None:
            record.applied = applied
        if order_status is not None:
            record.order_status = order_status
        if order_id is not None:
            record.order_id = order_id
        self.db.add(record)
        self.db.commit()

    def list_recent(self, limit: int = 50) -> list[LLMInteraction]:
        return (
            self.db.query(LLMInteraction)
            .order_by(LLMInteraction.created_at.desc(), LLMInteraction.id.desc())
            .limit(limit)
            .all()
        )
