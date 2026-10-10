"""Strict wire codec for parsed read-only broker results (no SDK objects)."""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.broker import AccountInfo, BrokerOrder, CashBalance, MarginInfo, NetAsset


_SCHEMAS: dict[type[Any], dict[str, Any]] = {
    BrokerOrder: {
        "broker_order_id": str, "symbol": str, "side": str, "status": str,
        "quantity": Decimal, "price": Decimal, "executed_quantity": Decimal,
        "executed_price": Decimal, "created_at": datetime, "filled_at": datetime,
    },
    AccountInfo: {
        "total_assets": Decimal, "currency": str, "cash_balances": [CashBalance],
        "net_assets": [NetAsset], "margin_infos": [MarginInfo],
    },
    CashBalance: {"currency": str, "available_cash": Decimal, "frozen_cash": Decimal},
    NetAsset: {"currency": str, "amount": Decimal},
    MarginInfo: {
        "currency": str, "risk_level": int, "margin_call": Decimal,
        "init_margin": Decimal, "maintenance_margin": Decimal,
        "max_finance_amount": Decimal, "remaining_finance_amount": Decimal,
        "buy_power": Decimal,
    },
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON constant {value}")


def strict_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _decode(value: Any, schema: Any) -> Any:
    if isinstance(schema, list):
        if type(value) is not list:
            raise ValueError("expected protocol list")
        return [_decode(item, schema[0]) for item in value]
    if schema is datetime:
        if value is None:
            return None
        if type(value) is not str or "T" not in value:
            raise ValueError("expected ISO datetime or null")
        return datetime.fromisoformat(value)
    if schema is Decimal:
        if type(value) is not str:
            raise ValueError("expected exact decimal string")
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError("nonfinite decimal")
        return result
    if schema in (str, int):
        if type(value) is not schema:
            raise ValueError("invalid scalar type")
        return value
    expected = _SCHEMAS[schema]
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("unknown or missing protocol fields")
    return schema(**{key: _decode(value[key], kind) for key, kind in expected.items()})


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite decimal")
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if type(value) in _SCHEMAS:
        return {item.name: _encode(getattr(value, item.name)) for item in fields(value)}
    if type(value) is list:
        return [_encode(item) for item in value]
    if value is None or type(value) in (str, int):
        return value
    raise ValueError("unsupported broker read value")


def encode_result(op: str, result: Any) -> Any:
    encoded = _encode(result)
    decode_result(op, encoded)
    return encoded


def decode_result(op: str, value: Any) -> list[BrokerOrder] | AccountInfo:
    if op == "today_orders":
        return _decode(value, [BrokerOrder])
    if op == "account":
        return _decode(value, AccountInfo)
    raise ValueError("unknown broker read operation")
