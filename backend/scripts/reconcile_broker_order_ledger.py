#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlencode


_ORDERS_PATH = "/v1/trade/order/history"
_EXECUTIONS_PATH = "/v1/trade/execution/history"
_MAX_WINDOW = timedelta(days=90)
_STATUS_MAP = {
    "NotReported": "SUBMITTED",
    "ReplacedNotReported": "SUBMITTED",
    "ProtectedNotReported": "SUBMITTED",
    "VarietiesNotReported": "SUBMITTED",
    "WaitToNew": "SUBMITTED",
    "NewStatus": "SUBMITTED",
    "WaitToReplace": "SUBMITTED",
    "PendingReplaceStatus": "SUBMITTED",
    "ReplacedStatus": "SUBMITTED",
    "PartialFilledStatus": "PARTIAL_FILLED",
    "WaitToCancel": "SUBMITTED",
    "PendingCancelStatus": "SUBMITTED",
    "FilledStatus": "FILLED",
    "RejectedStatus": "REJECTED",
    "CanceledStatus": "CANCELLED",
    "ExpiredStatus": "CANCELLED",
    "PartialWithdrawal": "CANCELLED",
}


class LedgerError(RuntimeError): ...


class HttpTransport(Protocol):
    def request(self, method: str, path: str) -> object: ...


class HttpClientFactory(Protocol):
    @staticmethod
    def from_env() -> object: ...


@dataclass(frozen=True)
class Order:
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal | None
    executed_quantity: Decimal
    executed_price: Decimal | None
    status: str
    submitted_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReconciliationRow:
    broker_order_id: str
    verdict: str
    consistent: bool
    symbol: str
    side: str
    quantity: str
    price: str | None
    status: str
    submitted_at: str
    differences: tuple[str, ...]
    disposition: str


def _datetime_arg(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a timezone offset")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LedgerError(f"{label} must be a JSON object")
    return dict(cast(Mapping[str, object], value))


def _complete_items(
    transport: HttpTransport, path: str, items_key: str, identity_key: str
) -> tuple[list[dict[str, object]], str]:
    payload = _mapping(transport.request("get", path), path)
    if type(payload.get("has_more")) is not bool:
        raise LedgerError(f"{path} lacks boolean has_more completeness proof")
    if payload["has_more"]:
        raise LedgerError(f"{path} is incomplete: has_more=true")
    raw_items = payload.get(items_key)
    if not isinstance(raw_items, list):
        raise LedgerError(f"{path} lacks required {items_key} list")
    items = [
        _mapping(item, f"{items_key}[{index}]")
        for index, item in enumerate(cast(list[object], raw_items))
    ]
    try:
        ordered = sorted(items, key=lambda item: str(item[identity_key]))
    except KeyError as exc:
        raise LedgerError(f"{path} item lacks {identity_key}") from exc
    canonical: dict[str, object] = {"has_more": False, items_key: ordered}
    return items, _canonical_digest(canonical)


def _decimal(value: object, *, optional: bool = False) -> Decimal | None:
    normalized = str(value or "").strip()
    if optional and not normalized:
        return None
    try:
        return Decimal(normalized or "0")
    except InvalidOperation as exc:
        raise LedgerError(f"invalid decimal value {normalized!r}") from exc


def _timestamp(value: object) -> datetime:
    normalized = str(value or "").strip()
    if normalized.isdigit():
        return datetime.fromtimestamp(int(normalized), tz=timezone.utc)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _broker_order(raw: Mapping[str, object]) -> Order:
    status_raw = str(raw.get("status", ""))
    if status_raw not in _STATUS_MAP:
        raise LedgerError(f"unknown broker order status {status_raw!r}")
    side = {"Buy": "BUY", "Sell": "SELL"}.get(str(raw.get("side", "")), "")
    if not side:
        raise LedgerError(f"unsupported broker side {raw.get('side')!r}")
    return Order(
        broker_order_id=str(raw.get("order_id", "")),
        symbol=str(raw.get("symbol", "")).upper(),
        side=side,
        quantity=cast(Decimal, _decimal(raw.get("quantity"))),
        price=_decimal(raw.get("price"), optional=True),
        executed_quantity=cast(Decimal, _decimal(raw.get("executed_quantity"))),
        executed_price=_decimal(raw.get("executed_price"), optional=True),
        status=_STATUS_MAP[status_raw],
        submitted_at=_timestamp(raw.get("submitted_at")),
        updated_at=_timestamp(raw.get("updated_at")),
    )


def _local_orders(database: Path, start_at: datetime, end_at: datetime) -> list[Order]:
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = cast(list[tuple[object, ...]], connection.execute(
            """SELECT broker_order_id, symbol, side, quantity, price,
                      executed_quantity, executed_price, status, created_at,
                      filled_at, broker_submitted_at, broker_updated_at
               FROM orders WHERE created_at >= ? AND created_at <= ?
               ORDER BY created_at, id""",
            (
                start_at.strftime("%Y-%m-%d %H:%M:%S"),
                end_at.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        ).fetchall())
    result: list[Order] = []
    for row in rows:
        result.append(Order(
            broker_order_id=str(row[0] or ""),
            symbol=str(row[1]), side=str(row[2]),
            quantity=Decimal(str(row[3])), price=_decimal(row[4], optional=True),
            executed_quantity=Decimal(str(row[5] or 0)),
            executed_price=_decimal(row[6], optional=True), status=str(row[7]),
            submitted_at=_timestamp(row[10] or row[8]),
            updated_at=_timestamp(row[11] or row[9] or row[8]),
        ))
    return result


def _differences(broker: Order, local: Order) -> tuple[str, ...]:
    differences: list[str] = []
    for name in ("symbol", "side", "quantity", "price", "executed_quantity", "executed_price", "status"):
        if getattr(broker, name) != getattr(local, name):
            differences.append(f"{name}: broker={getattr(broker, name)} local={getattr(local, name)}")
    if abs((broker.submitted_at - local.submitted_at).total_seconds()) > 1:
        differences.append(f"submitted_at: broker={broker.submitted_at.isoformat()} local={local.submitted_at.isoformat()}")
    if abs((broker.updated_at - local.updated_at).total_seconds()) > 1:
        differences.append(f"updated_at: broker={broker.updated_at.isoformat()} local={local.updated_at.isoformat()}")
    return tuple(differences)


def _row(order: Order, verdict: str, differences: tuple[str, ...], disposition: str) -> ReconciliationRow:
    return ReconciliationRow(
        broker_order_id=order.broker_order_id, verdict=verdict, consistent=not differences,
        symbol=order.symbol, side=order.side, quantity=str(order.quantity),
        price=None if order.price is None else str(order.price), status=order.status,
        submitted_at=order.submitted_at.isoformat(), differences=differences, disposition=disposition,
    )


def _reconcile(broker_orders: list[Order], local_orders: list[Order]) -> list[ReconciliationRow]:
    broker_by_id = {order.broker_order_id: order for order in broker_orders}
    local_by_id = {order.broker_order_id: order for order in local_orders if order.broker_order_id}
    if len(broker_by_id) != len(broker_orders) or len(local_by_id) != sum(bool(order.broker_order_id) for order in local_orders):
        raise LedgerError("duplicate non-empty broker_order_id prevents unambiguous reconciliation")
    rows: list[ReconciliationRow] = []
    for order_id in sorted(broker_by_id.keys() | local_by_id.keys()):
        broker = broker_by_id.get(order_id)
        local = local_by_id.get(order_id)
        if broker is None:
            rows.append(_row(cast(Order, local), "UNMATCHED_LOCAL_ONLY", (), "Complete broker snapshot has no corresponding order; inspect stale or invalid local broker_order_id."))
        elif local is None:
            rows.append(_row(broker, "UNMATCHED_BROKER_ONLY", (), "Broker ledger proves the order exists but SQLite does not; manually reconstruct persistence evidence."))
        else:
            differences = _differences(broker, local)
            disposition = "Broker and local fields agree." if not differences else "Join key matches, but local fields require correction after manual review."
            rows.append(_row(broker, "MATCHED", differences, disposition))
    for index, local in enumerate(order for order in local_orders if not order.broker_order_id):
        rows.append(_row(local, "UNMATCHED_LOCAL_ONLY", (), f"Local row {index + 1} has an empty broker_order_id and cannot join to broker evidence."))
    return sorted(rows, key=lambda row: (row.submitted_at, row.broker_order_id))


def _transport_and_fingerprint() -> tuple[HttpTransport, str]:
    for canonical, alias in (("LONGPORT_APP_KEY", "LONGBRIDGE_APP_KEY"), ("LONGPORT_APP_SECRET", "LONGBRIDGE_APP_SECRET"), ("LONGPORT_ACCESS_TOKEN", "LONGBRIDGE_ACCESS_TOKEN")):
        if not os.environ.get(canonical) and os.environ.get(alias):
            os.environ[canonical] = os.environ[alias]
    credentials = tuple(os.environ.get(name, "") for name in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN"))
    if not all(credentials):
        raise LedgerError("LongPort credentials are unavailable")
    module = importlib.import_module("longport.openapi")
    client_type = cast(HttpClientFactory, getattr(module, "HttpClient"))
    transport = cast(HttpTransport, client_type.from_env())
    return transport, hashlib.sha256("\0".join(credentials).encode()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only LongPort versus SQLite order-ledger reconciliation.")
    _ = parser.add_argument("--start-at", required=True, type=_datetime_arg)
    _ = parser.add_argument("--end-at", type=_datetime_arg, default=datetime.now(timezone.utc).replace(microsecond=0))
    _ = parser.add_argument("--database", type=Path, default=Path("/app/data/auto_trade.db"))
    args = parser.parse_args(argv)
    start_at = cast(datetime, args.start_at)
    end_at = cast(datetime, args.end_at)
    database = cast(Path, args.database)
    try:
        if end_at <= start_at or end_at - start_at > _MAX_WINDOW:
            raise LedgerError("window must be positive and no longer than 90 days")
        transport, fingerprint = _transport_and_fingerprint()
        query = urlencode({"start_at": int(start_at.timestamp()), "end_at": int(end_at.timestamp())})
        orders_path, executions_path = f"{_ORDERS_PATH}?{query}", f"{_EXECUTIONS_PATH}?{query}"
        raw_orders, orders_digest = _complete_items(transport, orders_path, "orders", "order_id")
        raw_executions, executions_digest = _complete_items(transport, executions_path, "trades", "trade_id")
        broker_orders = [_broker_order(item) for item in raw_orders]
        rows = _reconcile(broker_orders, _local_orders(database, start_at, end_at))
        counts = {verdict: sum(row.verdict == verdict for row in rows) for verdict in ("MATCHED", "UNMATCHED_BROKER_ONLY", "UNMATCHED_LOCAL_ONLY")}
        proof = {"schema_version": 1, "provider": "longport_official_http_v1", "broker_identity_fingerprint": fingerprint, "start_at": start_at.isoformat(), "end_at": end_at.isoformat(), "orders_path": orders_path, "executions_path": executions_path, "orders_has_more": False, "executions_has_more": False, "order_count": len(raw_orders), "execution_count": len(raw_executions), "orders_response_digest": orders_digest, "executions_response_digest": executions_digest, "complete": True}
        artifact = {"summary": {**counts, "MATCHED_DIVERGENT": sum(row.verdict == "MATCHED" and not row.consistent for row in rows)}, "completeness_proof": proof, "rows": [asdict(row) for row in rows]}
    except (LedgerError, OSError, sqlite3.Error, ImportError, AttributeError, ValueError) as exc:
        print("RECONCILIATION_INCOMPLETE=" + json.dumps({"complete": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print("RECONCILIATION_ARTIFACT=" + json.dumps(artifact, ensure_ascii=False, sort_keys=True))
    print("verdict\tconsistent\tbroker_order_id\tsymbol\tside\tquantity\tprice\tstatus\tsubmitted_at\tdifferences\tdisposition")
    for row in rows:
        print("\t".join((row.verdict, str(row.consistent).lower(), row.broker_order_id, row.symbol, row.side, row.quantity, row.price or "", row.status, row.submitted_at, "; ".join(row.differences), row.disposition)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
