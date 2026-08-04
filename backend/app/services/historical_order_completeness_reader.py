from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


_ORDERS_PATH = "/v1/trade/order/history"
_EXECUTIONS_PATH = "/v1/trade/execution/history"
_MAX_WINDOW = timedelta(days=90)
_PRICE_TOLERANCE = Decimal("0.0001")
_RELATIVE_PRICE_TOLERANCE = Decimal("0.00000001")
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}\.(?:US|HK)$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
_OFFICIAL_ORDER_STATUSES = frozenset({
    "NotReported",
    "ReplacedNotReported",
    "ProtectedNotReported",
    "VarietiesNotReported",
    "FilledStatus",
    "WaitToNew",
    "NewStatus",
    "WaitToReplace",
    "PendingReplaceStatus",
    "ReplacedStatus",
    "PartialFilledStatus",
    "WaitToCancel",
    "PendingCancelStatus",
    "RejectedStatus",
    "CanceledStatus",
    "ExpiredStatus",
    "PartialWithdrawal",
})


class HistoricalPreviewError(RuntimeError):
    """Base error for a historical preview that cannot be proved safe."""


class HistoricalCompletenessError(HistoricalPreviewError):
    """The broker explicitly reported that a historical page was truncated."""


class HistoricalPayloadError(HistoricalPreviewError):
    """The raw broker payload cannot support an unambiguous ledger preview."""


class HistoricalTransportError(HistoricalPreviewError):
    """The official authenticated HTTP transport could not return a snapshot."""


class HistoricalHttpTransport(Protocol):
    def request(self, method: str, path: str) -> object: ...


@dataclass(frozen=True)
class HistoricalExecutionEvidence:
    order_id: str
    trade_id: str
    symbol: str
    quantity: Decimal
    price: Decimal
    trade_done_at: datetime
    raw_json: str


@dataclass(frozen=True)
class HistoricalFilledOrderEvidence:
    order_id: str
    symbol: str
    side: str
    submitted_quantity: Decimal
    submitted_price: Decimal | None
    executed_quantity: Decimal
    executed_price: Decimal
    submitted_at: datetime
    updated_at: datetime
    first_executed_at: datetime
    last_executed_at: datetime
    executions: tuple[HistoricalExecutionEvidence, ...]
    raw_json: str


@dataclass(frozen=True)
class HistoricalCompletenessProof:
    schema_version: int
    provider: str
    broker_identity_fingerprint: str
    symbol: str
    start_at: datetime
    end_at: datetime
    orders_path: str
    executions_path: str
    orders_has_more: bool
    executions_has_more: bool
    order_count: int
    execution_count: int
    filled_order_count: int
    orders_response_digest: str
    executions_response_digest: str
    preview_digest: str


@dataclass(frozen=True)
class HistoricalOrderPreview:
    """Read-only evidence bundle; it has no persistence or order authority."""

    proof: HistoricalCompletenessProof
    filled_orders: tuple[HistoricalFilledOrderEvidence, ...]


class LongportHistoricalCompletenessReader:
    """Read raw LongPort history through its official authenticated transport.

    LongPort's typed Python history helpers discard the HTTP response's
    ``has_more`` field.  ``HttpClient`` is also a public SDK primitive, but it
    returns the raw JSON data object after performing the official
    authentication and request signing.  This reader deliberately accepts
    only a single, explicitly complete page.  It never guesses pagination:
    either endpoint returning ``has_more=true`` fails closed.

    The resulting preview is evidence only.  This module imports no database
    model and exposes no apply/import method.
    """

    def __init__(
        self,
        transport: HistoricalHttpTransport,
        *,
        broker_identity_fingerprint: str,
    ) -> None:
        fingerprint = broker_identity_fingerprint.strip().lower()
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError(
                "broker_identity_fingerprint must be a 64-character lowercase "
                "SHA-256 hex digest"
            )
        self._transport = transport
        self._broker_identity_fingerprint = fingerprint

    def preview(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
        observed_at: datetime | None = None,
    ) -> HistoricalOrderPreview:
        normalized_symbol = _normalize_symbol(symbol)
        start_utc, end_utc = _validate_window(
            normalized_symbol,
            start_at,
            end_at,
            observed_at=observed_at,
        )
        query = urlencode({
            "symbol": normalized_symbol,
            "start_at": int(start_utc.timestamp()),
            "end_at": int(end_utc.timestamp()),
        })
        orders_path = f"{_ORDERS_PATH}?{query}"
        executions_path = f"{_EXECUTIONS_PATH}?{query}"

        orders_payload = self._request_payload(orders_path, label="orders")
        raw_orders = _complete_items(
            orders_payload,
            items_key="orders",
            label="orders",
        )
        executions_payload = self._request_payload(
            executions_path,
            label="executions",
        )
        raw_executions = _complete_items(
            executions_payload,
            items_key="trades",
            label="executions",
        )

        filled_orders = _build_filled_order_evidence(
            raw_orders,
            raw_executions,
            symbol=normalized_symbol,
            start_at=start_utc,
            end_at=end_utc,
        )
        sorted_orders = sorted(
            raw_orders,
            key=lambda item: _required_text(item, "order_id", "order"),
        )
        sorted_executions = sorted(
            raw_executions,
            key=lambda item: _required_text(item, "trade_id", "execution"),
        )
        canonical_orders_payload = {
            "has_more": False,
            "orders": sorted_orders,
        }
        canonical_executions_payload = {
            "has_more": False,
            "trades": sorted_executions,
        }
        orders_digest = _sha256_json(canonical_orders_payload)
        executions_digest = _sha256_json(canonical_executions_payload)
        preview_digest = _sha256_json({
            "schema_version": 1,
            "provider": "longport_official_http_v1",
            "broker_identity_fingerprint": self._broker_identity_fingerprint,
            "symbol": normalized_symbol,
            "start_at": start_utc.isoformat(),
            "end_at": end_utc.isoformat(),
            "orders_response_digest": orders_digest,
            "executions_response_digest": executions_digest,
        })
        proof = HistoricalCompletenessProof(
            schema_version=1,
            provider="longport_official_http_v1",
            broker_identity_fingerprint=self._broker_identity_fingerprint,
            symbol=normalized_symbol,
            start_at=start_utc,
            end_at=end_utc,
            orders_path=orders_path,
            executions_path=executions_path,
            orders_has_more=False,
            executions_has_more=False,
            order_count=len(raw_orders),
            execution_count=len(raw_executions),
            filled_order_count=len(filled_orders),
            orders_response_digest=orders_digest,
            executions_response_digest=executions_digest,
            preview_digest=preview_digest,
        )
        return HistoricalOrderPreview(
            proof=proof,
            filled_orders=filled_orders,
        )

    def _request_payload(self, path: str, *, label: str) -> dict[str, object]:
        try:
            response = self._transport.request("get", path)
        except Exception as exc:
            raise HistoricalTransportError(
                f"LongPort historical {label} request failed"
            ) from exc
        return _object(response, f"historical {label} response")


def build_longport_historical_reader_from_env(
) -> LongportHistoricalCompletenessReader:
    """Build a reader using the SDK's public, authenticated ``HttpClient``.

    Credentials remain inside the SDK transport.  Only their SHA-256 identity
    fingerprint is retained in the preview so evidence from different broker
    accounts cannot be mixed accidentally.
    """

    credentials = tuple(
        str(os.environ.get(name, ""))
        for name in (
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        )
    )
    if not all(credentials):
        raise HistoricalTransportError(
            "LONGPORT_APP_KEY, LONGPORT_APP_SECRET, and LONGPORT_ACCESS_TOKEN "
            "are all required"
        )
    module = _import_openapi()
    client_type = getattr(module, "HttpClient", None)
    from_env = getattr(client_type, "from_env", None)
    if not callable(from_env):
        raise HistoricalTransportError(
            "installed LongPort SDK does not expose HttpClient.from_env"
        )
    try:
        raw_transport = from_env()
    except Exception as exc:
        raise HistoricalTransportError(
            "LongPort authenticated HTTP transport initialization failed"
        ) from exc
    if not callable(getattr(raw_transport, "request", None)):
        raise HistoricalTransportError(
            "LongPort HttpClient transport does not expose request"
        )
    transport = cast(HistoricalHttpTransport, raw_transport)
    fingerprint = hashlib.sha256(
        "\0".join(credentials).encode("utf-8")
    ).hexdigest()
    return LongportHistoricalCompletenessReader(
        transport,
        broker_identity_fingerprint=fingerprint,
    )


def _import_openapi() -> object:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["HttpClient"])
        except ImportError:
            continue
    raise HistoricalTransportError(
        "LongPort SDK is not installed; install the pinned longport dependency"
    )


def _normalize_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol must be an explicit US or HK broker symbol")
    return normalized


def _validate_window(
    symbol: str,
    start_at: datetime,
    end_at: datetime,
    *,
    observed_at: datetime | None,
) -> tuple[datetime, datetime]:
    start_utc = _aware_second(start_at, "start_at")
    end_utc = _aware_second(end_at, "end_at")
    if end_utc <= start_utc:
        raise ValueError("end_at must be later than start_at")
    if end_utc - start_utc > _MAX_WINDOW:
        raise ValueError("historical preview window cannot exceed 90 days")

    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    market = symbol.rsplit(".", 1)[-1]
    market_timezone = _MARKET_TIMEZONES[market]
    observed_market = observed.astimezone(market_timezone)
    current_market_day_start = datetime.combine(
        observed_market.date(),
        time.min,
        tzinfo=market_timezone,
    ).astimezone(timezone.utc)
    if end_utc > current_market_day_start:
        raise ValueError(
            "historical execution preview must end before the current market day"
        )
    return start_utc, end_utc


def _aware_second(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    if value.microsecond != 0:
        raise ValueError(f"{label} must have whole-second precision")
    return value.astimezone(timezone.utc)


def _complete_items(
    payload: Mapping[str, object],
    *,
    items_key: str,
    label: str,
) -> list[dict[str, object]]:
    has_more = payload.get("has_more")
    if type(has_more) is not bool:
        raise HistoricalPayloadError(
            f"historical {label} response is missing boolean has_more proof"
        )
    if has_more:
        raise HistoricalCompletenessError(
            f"historical {label} response is truncated (has_more=true)"
        )
    if items_key not in payload:
        raise HistoricalPayloadError(
            f"historical {label} response is missing required {items_key} list"
        )
    raw_items = payload[items_key]
    if not isinstance(raw_items, list):
        raise HistoricalPayloadError(
            f"historical {label} response {items_key} must be a list"
        )
    return [
        _object(item, f"historical {label} {items_key}[{index}]")
        for index, item in enumerate(raw_items)
    ]


def _build_filled_order_evidence(
    raw_orders: list[dict[str, object]],
    raw_executions: list[dict[str, object]],
    *,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[HistoricalFilledOrderEvidence, ...]:
    orders: dict[str, dict[str, object]] = {}
    statuses: dict[str, str] = {}
    executed_quantities: dict[str, Decimal] = {}
    for index, order in enumerate(raw_orders):
        label = f"order[{index}]"
        order_id = _required_text(order, "order_id", label)
        if order_id in orders:
            raise HistoricalPayloadError(
                f"duplicate historical order_id {order_id}"
            )
        if _required_text(order, "symbol", label).upper() != symbol:
            raise HistoricalPayloadError(
                f"historical order {order_id} does not match requested symbol"
            )
        status = _required_text(order, "status", label)
        if status not in _OFFICIAL_ORDER_STATUSES:
            raise HistoricalPayloadError(
                f"historical order {order_id} has unknown status {status}"
            )
        submitted_at = _epoch_second(order.get("submitted_at"), f"{label}.submitted_at")
        _require_in_window(submitted_at, start_at, end_at, f"{label}.submitted_at")
        executed_quantity = _decimal(
            order.get("executed_quantity"),
            f"{label}.executed_quantity",
            allow_zero=True,
        )
        if status != "FilledStatus" and executed_quantity > 0:
            raise HistoricalPayloadError(
                f"historical order {order_id} is {status} with executions; "
                "FILLED-only preview cannot represent it"
            )
        orders[order_id] = order
        statuses[order_id] = status
        executed_quantities[order_id] = executed_quantity

    executions_by_order: dict[str, list[HistoricalExecutionEvidence]] = {}
    trade_ids: set[str] = set()
    for index, execution in enumerate(raw_executions):
        label = f"execution[{index}]"
        order_id = _required_text(execution, "order_id", label)
        trade_id = _required_text(execution, "trade_id", label)
        if trade_id in trade_ids:
            raise HistoricalPayloadError(
                f"duplicate historical trade_id {trade_id}"
            )
        trade_ids.add(trade_id)
        if order_id not in orders:
            raise HistoricalPayloadError(
                f"historical execution {trade_id} has no order in the proved window"
            )
        if statuses[order_id] != "FilledStatus":
            raise HistoricalPayloadError(
                f"historical execution {trade_id} belongs to non-FILLED order "
                f"{order_id}"
            )
        if _required_text(execution, "symbol", label).upper() != symbol:
            raise HistoricalPayloadError(
                f"historical execution {trade_id} does not match requested symbol"
            )
        trade_done_at = _epoch_second(
            execution.get("trade_done_at"),
            f"{label}.trade_done_at",
        )
        _require_in_window(
            trade_done_at,
            start_at,
            end_at,
            f"{label}.trade_done_at",
        )
        evidence = HistoricalExecutionEvidence(
            order_id=order_id,
            trade_id=trade_id,
            symbol=symbol,
            quantity=_decimal(
                execution.get("quantity"),
                f"{label}.quantity",
            ),
            price=_decimal(execution.get("price"), f"{label}.price"),
            trade_done_at=trade_done_at,
            raw_json=_canonical_json(execution),
        )
        executions_by_order.setdefault(order_id, []).append(evidence)

    result: list[HistoricalFilledOrderEvidence] = []
    for order_id, order in orders.items():
        if statuses[order_id] != "FilledStatus":
            continue
        label = f"order[{order_id}]"
        side_raw = _required_text(order, "side", label)
        side = {"Buy": "BUY", "Sell": "SELL"}.get(side_raw)
        if side is None:
            raise HistoricalPayloadError(
                f"historical FILLED order {order_id} has unsupported side {side_raw}"
            )
        submitted_quantity = _decimal(
            order.get("quantity"),
            f"{label}.quantity",
        )
        executed_quantity = executed_quantities[order_id]
        if executed_quantity != submitted_quantity:
            raise HistoricalPayloadError(
                f"historical FILLED order {order_id} quantity does not match "
                "executed_quantity"
            )
        executed_price = _decimal(
            order.get("executed_price"),
            f"{label}.executed_price",
        )
        submitted_price = _optional_decimal(
            order.get("price"),
            f"{label}.price",
        )
        submitted_at = _epoch_second(
            order.get("submitted_at"),
            f"{label}.submitted_at",
        )
        updated_at = _epoch_second(
            order.get("updated_at"),
            f"{label}.updated_at",
        )
        executions = sorted(
            executions_by_order.get(order_id, []),
            key=lambda item: (item.trade_done_at, item.trade_id),
        )
        if not executions:
            raise HistoricalPayloadError(
                f"historical FILLED order {order_id} has no execution evidence"
            )
        execution_quantity = sum(
            (execution.quantity for execution in executions),
            start=Decimal("0"),
        )
        if execution_quantity != executed_quantity:
            raise HistoricalPayloadError(
                f"historical FILLED order {order_id} execution quantities do not "
                "match the order"
            )
        weighted_price = sum(
            (
                execution.quantity * execution.price
                for execution in executions
            ),
            start=Decimal("0"),
        ) / execution_quantity
        tolerance = max(
            _PRICE_TOLERANCE,
            abs(executed_price) * _RELATIVE_PRICE_TOLERANCE,
        )
        if abs(weighted_price - executed_price) > tolerance:
            raise HistoricalPayloadError(
                f"historical FILLED order {order_id} execution prices do not "
                "match the order"
            )
        result.append(HistoricalFilledOrderEvidence(
            order_id=order_id,
            symbol=symbol,
            side=side,
            submitted_quantity=submitted_quantity,
            submitted_price=submitted_price,
            executed_quantity=executed_quantity,
            executed_price=executed_price,
            submitted_at=submitted_at,
            updated_at=updated_at,
            first_executed_at=executions[0].trade_done_at,
            last_executed_at=executions[-1].trade_done_at,
            executions=tuple(executions),
            raw_json=_canonical_json(order),
        ))
    result.sort(key=lambda item: (item.first_executed_at, item.order_id))
    return tuple(result)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HistoricalPayloadError(f"{label} must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise HistoricalPayloadError(f"{label} contains a non-string key")
        result[key] = item
    _canonical_json(result)
    return result


def _required_text(item: Mapping[str, object], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HistoricalPayloadError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _decimal(value: object, label: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise HistoricalPayloadError(f"{label} must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HistoricalPayloadError(f"{label} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise HistoricalPayloadError(f"{label} must be a finite {qualifier} decimal")
    return parsed


def _optional_decimal(value: object, label: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, label)


def _epoch_second(value: object, label: str) -> datetime:
    if isinstance(value, bool):
        raise HistoricalPayloadError(f"{label} must be an epoch-second integer")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and value.isdigit():
        seconds = int(value)
    else:
        raise HistoricalPayloadError(f"{label} must be an epoch-second integer")
    try:
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise HistoricalPayloadError(f"{label} is outside datetime range") from exc
    return parsed


def _require_in_window(
    value: datetime,
    start_at: datetime,
    end_at: datetime,
    label: str,
) -> None:
    if value < start_at or value > end_at:
        raise HistoricalPayloadError(f"{label} falls outside the proved window")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HistoricalPayloadError(
            "historical payload is not canonical JSON"
        ) from exc


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
