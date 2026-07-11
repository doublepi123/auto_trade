from __future__ import annotations

import json
import math
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


def build_order_policy_outcome(
    analysis_result: dict[str, Any],
    order_result: dict[str, Any],
) -> dict[str, Any]:
    """Build the stable audit shape for an LLM order-policy decision."""
    action = str(analysis_result.get("order_action") or "NONE").upper()
    status = str(order_result.get("status") or "NO_ACTION").upper()

    raw_disposition = order_result.get("policy_disposition") or order_result.get("disposition")
    normalized_disposition = str(raw_disposition or "").upper()
    if normalized_disposition not in {"ALLOW", "REJECT", "SHADOW"}:
        if status == "SHADOW_ONLY":
            normalized_disposition = "SHADOW"
        elif action == "NONE" or status in {
            "POLICY_REJECTED",
            "CONFIDENCE_REJECTED",
            "RUNNER_STOPPED",
            "WATCHLIST_READ_ONLY",
            "UNKNOWN_SYMBOL",
            "ERROR",
        }:
            normalized_disposition = "REJECT"
        else:
            normalized_disposition = "ALLOW"

    raw_code = order_result.get("policy_code")
    if raw_code:
        code = str(raw_code)
    elif action == "NONE":
        code = "NO_ACTION"
    elif normalized_disposition == "SHADOW":
        code = "SHADOW_MODE"
    elif normalized_disposition == "REJECT":
        code = status or "POLICY_REJECTED"
    else:
        code = "ALLOW"

    candidate_source = order_result.get("candidate_price")
    if candidate_source is None:
        candidate_source = (
            analysis_result.get("replacement_price") or analysis_result.get("order_price")
            if action == "CANCEL_REPLACE"
            else analysis_result.get("order_price")
        )

    return {
        "code": code,
        "reference_price": _finite_float_or_none(order_result.get("reference_price")),
        "candidate_price": _finite_float_or_none(candidate_source),
        "deviation_pct": _finite_float_or_none(order_result.get("deviation_pct")),
        "confidence": _finite_float_or_none(
            order_result.get("confidence")
            if order_result.get("confidence") is not None
            else analysis_result.get("confidence_score")
        ),
        "disposition": normalized_disposition,
    }


def _finite_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


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
        policy_outcome: dict[str, Any] | None = None,
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
        if policy_outcome is not None:
            parsed_response = _json_loads_dict(record.parsed_response)
            parsed_response["policy_outcome"] = dict(policy_outcome)
            record.parsed_response = _json_dumps(parsed_response)
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
