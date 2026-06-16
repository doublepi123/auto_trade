from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import LLMInteraction
from app.schemas import LLMInteractionDetail


def _json_default(obj: Any) -> Any:
    """JSON serializer for types ``json.dumps`` cannot encode natively.

    Order matters: more specific types (Decimal, datetime) are handled
    first so we never fall through to ``str()``, which would render e.g.
    ``Decimal("123.45")`` as ``'Decimal("123.45")'`` — surprising and
    impossible to round-trip. Unknown objects are coerced via ``str()`` as
    a last resort; callers that need stricter behaviour should pre-serialize
    their data.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            return str(obj)
    return str(obj)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_loads_dict(value: str | None) -> dict[str, Any]:
    """Parse a JSON text column to a dict; never raises (returns {} on failure)."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


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
        prompt_variant: str | None = None,
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
            prompt_variant=prompt_variant,
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
        self.db.commit()

    def list_recent(self, limit: int = 50) -> list[LLMInteraction]:
        return (
            self.db.query(LLMInteraction)
            .order_by(LLMInteraction.created_at.desc(), LLMInteraction.id.desc())
            .limit(limit)
            .all()
        )

    def get_detail(self, interaction_id: int) -> LLMInteractionDetail | None:
        record = self.db.get(LLMInteraction, interaction_id)
        if record is None:
            return None
        return LLMInteractionDetail(
            id=record.id,
            interaction_type=record.interaction_type,
            symbol=record.symbol,
            market=record.market,
            prompt=record.prompt,
            raw_response=record.raw_response,
            parsed_response=_json_loads_dict(record.parsed_response),
            context_snapshot=_json_loads_dict(record.context_snapshot),
            success=bool(record.success),
            error=record.error,
            order_action=record.order_action,
            order_status=record.order_status,
            order_id=record.order_id,
            applied=bool(record.applied),
            prompt_variant=record.prompt_variant,
            created_at=record.created_at,
        )
