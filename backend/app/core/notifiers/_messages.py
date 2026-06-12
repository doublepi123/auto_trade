from __future__ import annotations

from typing import Optional


def render_order_title(side: str) -> str:
    return f"[Auto Trade] {side} Order Submitted"


def render_order_body(side: str, symbol: str, quantity: str, price: str, order_id: str) -> str:
    return f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}\nOrder ID: {order_id}"


def render_fill_title() -> str:
    return "[Auto Trade] Order Filled"


def render_fill_body(symbol: str, side: str, quantity: str, price: str) -> str:
    return f"Symbol: {symbol}\nSide: {side}\nQuantity: {quantity}\nPrice: {price}"


def render_risk_title(event_type: str) -> str:
    return f"[Auto Trade] Risk Event: {event_type}"


def render_risk_body(event_type: str, reason: str) -> str:
    return f"Type: {event_type}\nReason: {reason}"


def severity_for_risk_event(event_type: str) -> str:
    return {
        "KILL_SWITCH": "CRITICAL",
        "ORDER_PERSISTENCE_FAILED": "CRITICAL",
        "ORDER_FAILED": "WARNING",
        "ORDER_TIMEOUT": "WARNING",
        "REJECTED": "WARNING",
        "DAILY_LOSS": "WARNING",
    }.get(event_type, "WARNING")


def resolve_risk_severity(event_type: str, severity: Optional[str]) -> str:
    return severity if severity is not None else severity_for_risk_event(event_type)
