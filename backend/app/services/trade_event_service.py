from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import TradeEvent


def encode_event_payload(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "{}"
    return json.dumps(payload, ensure_ascii=False, default=str)


def decode_event_payload(payload_json: str | None) -> dict[str, Any]:
    if not payload_json:
        return {}
    try:
        value = json.loads(payload_json)
    except json.JSONDecodeError:
        return {"raw": payload_json}
    return value if isinstance(value, dict) else {"value": value}


def record_trade_event(
    db: Session,
    *,
    event_type: str,
    symbol: str = "",
    broker_order_id: str = "",
    side: str = "",
    status: str = "",
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> TradeEvent:
    event = TradeEvent(
        event_type=event_type,
        symbol=symbol or "",
        broker_order_id=broker_order_id or "",
        side=side or "",
        status=status or "",
        message=message or "",
        payload_json=encode_event_payload(payload),
    )
    db.add(event)
    return event
