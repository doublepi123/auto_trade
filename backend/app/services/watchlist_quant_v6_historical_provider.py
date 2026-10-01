"""Quote-only historical provider.

Diagnostic decision, 2026-10-01: diagnostic-v1 adds returns-blind context ONLY
to the existing non-advancing failure, after the existing native read returns.
It changes provider source / historical evaluator digest / NEW registration
identities, NOT the v3 provider contract, domain semantics, acquisition spec,
acceptance, EOF, cohort denominator, parameters, or promotion rules. Preserve
registrations 51/52, their failures and identities, and sealed publications;
never force old plans, retry a historical cohort manually, or rewrite records.
Registration 52 (9b72868f00c8daf9c296d1b2eafc75d34a2d9c9eae832fa0e0f477c931218a36)
has EA ordinal 40: recent Aug-04 bars before Aug-05 training are NOT exact
forward-history evidence. Equality metadata does not certify EOF.

Native acquisition uses the provider daemon thread / supervisor parent fetch
thread; separate compute children do not make acquisition hard-killable or
GIL-isolated. Native 3.0.23 whole-call/request/reconnect bounds remain UNKNOWN.
This diagnostic introduces no requests, retries, contexts, timers, or logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, cast
from zoneinfo import ZoneInfo

from app.config import settings
from app.domain.watchlist_quant_v6 import (
    QUANT_V6_ACQUISITION_SPEC_DIGEST,
    QuantV6Bar,
    QuantV6SemanticError,
)
from app.services.watchlist_quant_v6_deadline import (
    QuantV6EvaluationDeadline,
    QuantV6EvaluationStoppedError,
)


logger = logging.getLogger("auto_trade.watchlist_quant_v6_historical_provider")

QUANT_V6_HISTORICAL_PROVIDER_CONTRACT_VERSION = (
    "watchlist-quant-v6-longport-quote-only-history-v3"
)
QUANT_V6_HISTORICAL_PERIOD = "MIN_5"
QUANT_V6_HISTORICAL_ADJUSTMENT_MODE = "NO_ADJUST"
QUANT_V6_HISTORICAL_PAGE_BOUNDARY = (
    "EXCLUSIVE_AFTER_CURSOR_WITH_EXACT_VALID_SINGLETON_TERMINAL_REPEAT"
)
QUANT_V6_HISTORICAL_PAGE_SIZE = 1_000
QUANT_V6_HISTORICAL_MAX_PAGES = 16
QUANT_V6_HISTORICAL_MAX_BARS = 10_000
QUANT_V6_HISTORICAL_MAX_RAW_ROWS = (
    QUANT_V6_HISTORICAL_PAGE_SIZE * QUANT_V6_HISTORICAL_MAX_PAGES
)
QUANT_V6_HISTORICAL_MAX_RANGE_DAYS = 90
QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS = int(
    Decimal(str(settings.watchlist_quant_v6_provider_page_timeout_seconds))
    * 1_000
)
QUANT_V6_HISTORICAL_RETRY_MAX = settings.broker_quote_retry_max
QUANT_V6_HISTORICAL_RETRY_BASE_MILLISECONDS = settings.broker_retry_base_ms
_BAR_DURATION = timedelta(minutes=5)
_RETRYABLE_MESSAGE_MARKERS = (
    "429",
    "500000",
    "connection",
    "error sending request",
    "internal error",
    "rate limit",
    "rate_limit",
    "throttle",
    "timeout",
    "too frequent",
    "too many requests",
    "unavailable",
    "限流",
    "频率",
)
_EXCHANGE_TIMEZONES: Mapping[str, ZoneInfo] = MappingProxyType({
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
})
_SDK_CALL_SLOT = threading.BoundedSemaphore(1)
_PROVIDER_CONTRACT: Mapping[str, object] = MappingProxyType({
    "acquisition_spec_sha256": QUANT_V6_ACQUISITION_SPEC_DIGEST,
    "adjustment_mode": QUANT_V6_HISTORICAL_ADJUSTMENT_MODE,
    "bounded_context_close": True,
    "bounded_context_creation": True,
    "fallback_allowed": False,
    "forward_paging": True,
    "max_bars": QUANT_V6_HISTORICAL_MAX_BARS,
    "max_inflight_sdk_calls": 1,
    "max_pages": QUANT_V6_HISTORICAL_MAX_PAGES,
    "max_raw_rows": QUANT_V6_HISTORICAL_MAX_RAW_ROWS,
    "max_range_days": QUANT_V6_HISTORICAL_MAX_RANGE_DAYS,
    "naive_sdk_timestamp_policy": "UTC_HOST_LOCAL_ONLY",
    "page_boundary": QUANT_V6_HISTORICAL_PAGE_BOUNDARY,
    "page_timeout_milliseconds": (
        QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS
    ),
    "page_rows_must_not_exceed_page_size": True,
    "page_size": QUANT_V6_HISTORICAL_PAGE_SIZE,
    "period": QUANT_V6_HISTORICAL_PERIOD,
    "provider_contract_version": QUANT_V6_HISTORICAL_PROVIDER_CONTRACT_VERSION,
    "quote_context_only": True,
    "retry_base_milliseconds": QUANT_V6_HISTORICAL_RETRY_BASE_MILLISECONDS,
    "retry_max": QUANT_V6_HISTORICAL_RETRY_MAX,
    "runtime_local_timezone_required": "UTC",
    "schema_version": 1,
})


class QuantV6HistoricalProviderError(RuntimeError):
    """Raised when quote-only historical acquisition cannot be trusted."""


# Diagnostic-only constants deliberately OUTSIDE _PROVIDER_CONTRACT.
_PAGING_FAILURE_PREFIX = "historical candlestick cursor did not advance"
_PAGING_DIAGNOSTIC_MARKER = " | qv6_page_diag_v1="
_PAGING_MESSAGE_MAX_BYTES = 1800
_PAGING_FIELDS = ("open", "high", "low", "close", "volume")
_PAGING_REASONS = {"OK", "TIMESTAMP_INVALID", "OFF_GRID", "NONFINITE",
                   "NONPOSITIVE_PRICE", "NEGATIVE_VOLUME", "OHLC_INCONSISTENT", "UNREADABLE"}
_PAGING_DESCRIPTOR_KEYS = {"timestamp_utc", "timestamp_kind", "naive", "bar_validation", "reason"}
_PAGING_PAYLOAD_KEYS = {"v", "code", "diagnostic_status", "symbol", "page", "request", "counts",
                      "page_time_range", "previous_raw_boundary", "previous_accepted",
                      "current_boundary", "comparison"}


@dataclass(frozen=True, repr=False)
class _BoundaryDigest:
    """Only extra cross-page state: one descriptor and five bounded private hashes.

    Never serialize this object or use its fingerprints in acceptance decisions.
    No raw SDK objects, numeric values, or numeric strings are retained here.
    """

    descriptor: dict[str, Any]
    fingerprints: tuple[str | None, ...]


def _diagnostic_utc(timestamp: datetime) -> str:
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _diagnostic_decimal(value: object) -> tuple[Decimal | None, str]:
    # Exact built-in types only: never invoke arbitrary __str__/__repr__.
    if type(value) is Decimal:
        if value.__sizeof__() > 2048:
            return None, "UNREADABLE"
        candidate = value
    elif type(value) is str:
        if len(value) > 128:
            return None, "UNREADABLE"
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            return None, "UNREADABLE"
    elif type(value) is int:
        if value.bit_length() > 512:
            return None, "UNREADABLE"
        candidate = Decimal(value)
    elif type(value) is float:
        candidate = Decimal(str(value))
    else:
        return None, "UNREADABLE"
    if not candidate.is_finite():
        return None, "NONFINITE"
    parts = candidate.as_tuple()
    if len(parts.digits) > 128 or not isinstance(parts.exponent, int) or abs(parts.exponent) > 128:
        return None, "UNREADABLE"
    return candidate, "OK"


def _diagnostic_fingerprint(value: Decimal) -> str:
    # Canonical exact decimal tuple, no context rounding / huge fixed formatting.
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = cast(int, parts.exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    canonical = "0" if not any(digits) else (
        str(parts.sign) + ":" + "".join(str(digit) for digit in digits) + ":" + str(exponent)
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _describe_boundary(item: object, timestamp: datetime, *, accepted: bool = False) -> _BoundaryDigest:
    raw_timestamp = timestamp if accepted else getattr(item, "timestamp", None)
    kind = "OTHER"
    naive: bool | None = None
    if type(raw_timestamp) is datetime:
        kind = "DATETIME"
        naive = raw_timestamp.tzinfo is None or raw_timestamp.utcoffset() is None
    elif type(raw_timestamp) is str:
        kind = "STRING"
        if len(raw_timestamp) <= 64:
            try:
                parsed = datetime.fromisoformat(raw_timestamp.strip().replace("Z", "+00:00"))
                naive = parsed.tzinfo is None or parsed.utcoffset() is None
            except ValueError:
                naive = None
    elif type(raw_timestamp) in (int, float):
        kind = "NUMBER"
    elif type(raw_timestamp) is bool:
        kind = "BOOLEAN"
    elif raw_timestamp is None:
        kind = "MISSING"
    numbers: list[Decimal | None] = []
    codes: list[str] = []
    for name in _PAGING_FIELDS:
        try:
            number, code = _diagnostic_decimal(getattr(item, name, None))
        except Exception:
            number, code = None, "UNREADABLE"
        numbers.append(number)
        codes.append(code)
    fingerprints = tuple(_diagnostic_fingerprint(n) if n is not None else None for n in numbers)
    reason = "OK"
    validation = "VALID"
    if "UNREADABLE" in codes:
        validation, reason = "UNKNOWN", "UNREADABLE"
    elif "NONFINITE" in codes:
        validation, reason = "INVALID", "NONFINITE"
    elif timestamp.second or timestamp.microsecond or timestamp.minute % 5:
        validation, reason = "INVALID", "OFF_GRID"
    else:
        opened, high, low, close, volume = cast(tuple[Decimal, Decimal, Decimal, Decimal, Decimal], tuple(numbers))
        if min(opened, high, low, close) <= 0:
            validation, reason = "INVALID", "NONPOSITIVE_PRICE"
        elif volume < 0:
            validation, reason = "INVALID", "NEGATIVE_VOLUME"
        elif not (low <= opened <= high and low <= close <= high):
            validation, reason = "INVALID", "OHLC_INCONSISTENT"
    return _BoundaryDigest({"timestamp_utc": _diagnostic_utc(timestamp), "timestamp_kind": kind,
                           "naive": naive, "bar_validation": validation, "reason": reason}, fingerprints)


def _select_boundary(
    parsed_rows: tuple[tuple[object, datetime | None], ...], cursor: datetime,
) -> tuple[str, _BoundaryDigest | None]:
    if len(parsed_rows) > QUANT_V6_HISTORICAL_PAGE_SIZE:
        raise ValueError("diagnostic row budget")
    at_cursor = [(item, ts) for item, ts in parsed_rows if ts == cursor]
    if len(at_cursor) > 1:
        return "AMBIGUOUS", None
    if len(at_cursor) == 1:
        return "CURSOR_UNIQUE", _describe_boundary(at_cursor[0][0], cursor)
    times = [ts for _item, ts in parsed_rows if ts is not None]
    if not times:
        return "NONE", None
    maximum = max(times)
    at_maximum = [(item, ts) for item, ts in parsed_rows if ts == maximum]
    if len(at_maximum) != 1:
        return "AMBIGUOUS", None
    return "MAX_UNIQUE", _describe_boundary(at_maximum[0][0], maximum)


def _remember_boundary(
    parsed_rows: tuple[tuple[object, datetime | None], ...], cursor: datetime,
) -> _BoundaryDigest | None:
    try:
        return _select_boundary(parsed_rows, cursor)[1]
    except Exception:
        # Diagnostics never change the original successful page/cursor/counters.
        return None


def _boundary_match(left: _BoundaryDigest | None, right: _BoundaryDigest | None) -> bool | None:
    if left is None or right is None:
        return None
    if left.descriptor["timestamp_utc"] != right.descriptor["timestamp_utc"]:
        return False
    if None in left.fingerprints or None in right.fingerprints:
        return None
    return left.fingerprints == right.fingerprints


def _paging_failure_payload(
    *, symbol: str, page: int, start: datetime, end: datetime, cursor: datetime,
    request_boundary: datetime, parsed_rows: tuple[tuple[object, datetime | None], ...],
    raw_total: int, rejected_total: int, accepted_bars: list[QuantV6Bar],
    previous_raw_boundary: _BoundaryDigest | None,
) -> dict[str, Any]:
    selection, current = _select_boundary(parsed_rows, cursor)
    accepted = (_describe_boundary(accepted_bars[-1], accepted_bars[-1].start_at, accepted=True)
                if accepted_bars else None)
    times = [ts for _item, ts in parsed_rows if ts is not None]
    changed: list[str] | None = None
    reference = previous_raw_boundary
    if reference is None or (current is not None and reference.descriptor["timestamp_utc"] != current.descriptor["timestamp_utc"]):
        reference = accepted
    if (current is not None and reference is not None
            and current.descriptor["timestamp_utc"] == reference.descriptor["timestamp_utc"]
            and None not in current.fingerprints and None not in reference.fingerprints):
        changed = [name for name, a, b in zip(_PAGING_FIELDS, current.fingerprints, reference.fingerprints) if a != b]
    return {
        "v": 1, "code": "CURSOR_NOT_ADVANCING", "diagnostic_status": "AVAILABLE", "symbol": symbol, "page": page,
        "request": {"start_utc": _diagnostic_utc(start), "end_utc": _diagnostic_utc(end),
                    "cursor_utc": _diagnostic_utc(cursor), "cursor_exchange": request_boundary.isoformat(),
                    "page_size": QUANT_V6_HISTORICAL_PAGE_SIZE},
        "counts": {"page_rows": len(parsed_rows), "raw_total": raw_total, "rejected_total": rejected_total,
                   "accepted_total": len(accepted_bars), "before_cursor": sum(t < cursor for t in times),
                   "at_cursor": sum(t == cursor for t in times), "after_cursor": sum(t > cursor for t in times),
                   "unparsed": len(parsed_rows) - len(times)},
        "page_time_range": {"min_utc": _diagnostic_utc(min(times)) if times else None,
                            "max_utc": _diagnostic_utc(max(times)) if times else None},
        "previous_raw_boundary": previous_raw_boundary.descriptor if previous_raw_boundary is not None else None,
        "previous_accepted": accepted.descriptor if accepted is not None else None,
        "current_boundary": {"selection": selection, "descriptor": current.descriptor if current is not None else None},
        "comparison": {"raw_match": _boundary_match(current, previous_raw_boundary),
                       "accepted_match": _boundary_match(current, accepted), "changed_fields": changed},
    }


def _diagnostic_timestamp_valid(value: object, *, exchange: bool = False) -> bool:
    if type(value) is not str or len(value) > 32:
        return False
    suffix = r"[+-]\d{2}:\d{2}" if exchange else "Z"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?" + suffix, value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _diagnostic_descriptor_valid(value: object) -> bool:
    if value is None:
        return True
    if type(value) is not dict or set(value) != _PAGING_DESCRIPTOR_KEYS:
        return False
    return (
        (value["timestamp_utc"] is None or _diagnostic_timestamp_valid(value["timestamp_utc"]))
        and type(value["timestamp_kind"]) is str
        and value["timestamp_kind"] in {"DATETIME", "STRING", "NUMBER", "BOOLEAN", "MISSING", "OTHER"}
        and (value["naive"] is None or type(value["naive"]) is bool)
        and type(value["bar_validation"]) is str and value["bar_validation"] in {"VALID", "INVALID", "UNKNOWN"}
        and type(value["reason"]) is str and value["reason"] in _PAGING_REASONS
    )


def _diagnostic_payload_valid(data: object) -> bool:
    if type(data) is not dict or type(data.get("v")) is not int or data["v"] != 1:
        return False
    if data.get("diagnostic_status") == "UNAVAILABLE":
        return set(data) == {"v", "code", "diagnostic_status"} and data["code"] in {"BUILD_FAILED", "SIZE_LIMIT"}
    if set(data) != _PAGING_PAYLOAD_KEYS or data["diagnostic_status"] != "AVAILABLE" or data["code"] != "CURSOR_NOT_ADVANCING":
        return False
    if type(data["symbol"]) is not str or not re.fullmatch(r"[A-Z0-9._-]{1,47}\.(?:US|HK)", data["symbol"]):
        return False
    if type(data["page"]) is not int or not 1 <= data["page"] <= QUANT_V6_HISTORICAL_MAX_PAGES:
        return False
    request = data["request"]
    if type(request) is not dict or set(request) != {"start_utc", "end_utc", "cursor_utc", "cursor_exchange", "page_size"}:
        return False
    if (type(request["page_size"]) is not int or request["page_size"] != QUANT_V6_HISTORICAL_PAGE_SIZE
            or not all(_diagnostic_timestamp_valid(request[k]) for k in ("start_utc", "end_utc", "cursor_utc"))
            or not _diagnostic_timestamp_valid(request["cursor_exchange"], exchange=True)):
        return False
    counts = data["counts"]
    if type(counts) is not dict or set(counts) != {"page_rows", "raw_total", "rejected_total", "accepted_total", "before_cursor", "at_cursor", "after_cursor", "unparsed"}:
        return False
    if any(type(n) is not int or not 0 <= n <= QUANT_V6_HISTORICAL_MAX_RAW_ROWS for n in counts.values()):
        return False
    if (counts["page_rows"] > QUANT_V6_HISTORICAL_PAGE_SIZE or counts["accepted_total"] > QUANT_V6_HISTORICAL_MAX_BARS
            or sum(counts[k] for k in ("before_cursor", "at_cursor", "after_cursor", "unparsed")) != counts["page_rows"]):
        return False
    time_range = data["page_time_range"]
    if type(time_range) is not dict or set(time_range) != {"min_utc", "max_utc"}:
        return False
    if not all(v is None or _diagnostic_timestamp_valid(v) for v in time_range.values()):
        return False
    if not all(_diagnostic_descriptor_valid(data[k]) for k in ("previous_raw_boundary", "previous_accepted")):
        return False
    boundary = data["current_boundary"]
    if type(boundary) is not dict or set(boundary) != {"selection", "descriptor"}:
        return False
    if type(boundary["selection"]) is not str or boundary["selection"] not in {"CURSOR_UNIQUE", "MAX_UNIQUE", "AMBIGUOUS", "NONE"}:
        return False
    if not _diagnostic_descriptor_valid(boundary["descriptor"]) or ((boundary["descriptor"] is None) != (boundary["selection"] in {"AMBIGUOUS", "NONE"})):
        return False
    comparison = data["comparison"]
    if type(comparison) is not dict or set(comparison) != {"raw_match", "accepted_match", "changed_fields"}:
        return False
    if any(comparison[k] is not None and type(comparison[k]) is not bool for k in ("raw_match", "accepted_match")):
        return False
    fields = comparison["changed_fields"]
    return fields is None or (type(fields) is list and len(fields) <= 5
        and all(type(f) is str and f in _PAGING_FIELDS for f in fields) and len(set(fields)) == len(fields))


def parse_quant_v6_paging_failure(message: str) -> Mapping[str, Any] | None:
    """Strict bounded metadata parser, diagnostics ONLY; never authorizes EOF.

    Exact public keys are declared above; descriptors expose normalized time,
    fixed type/validity codes only. Accept only the original provider prefix or
    the existing supervisor's single worker/ordinal prefix, not arbitrary text.
    """
    if type(message) is not str or len(message) > 2048 or not message.isascii() or "\n" in message or "\r" in message:
        return None
    if message.count(_PAGING_DIAGNOSTIC_MARKER) != 1:
        return None
    prefix, encoded = message.split(_PAGING_DIAGNOSTIC_MARKER)
    if not re.fullmatch(r"(?:quant-v6 (?:worker|candidate ordinal (?:0|[1-9][0-9]{0,8})): )?" + re.escape(_PAGING_FAILURE_PREFIX), prefix):
        return None
    if len(_PAGING_FAILURE_PREFIX + _PAGING_DIAGNOSTIC_MARKER + encoded) > _PAGING_MESSAGE_MAX_BYTES:
        return None

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate diagnostic key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> Any:
        raise ValueError("nonfinite diagnostic value")

    try:
        data = json.loads(encoded, object_pairs_hook=unique, parse_constant=nonfinite)
        return data if _diagnostic_payload_valid(data) else None
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


def _paging_failure_message(**context: Any) -> str:
    """Preserve the original error; complete tiny JSON on any build/size failure."""
    failure_code = "BUILD_FAILED"
    try:
        payload = _paging_failure_payload(**context)
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        message = _PAGING_FAILURE_PREFIX + _PAGING_DIAGNOSTIC_MARKER + encoded
        if len(message.encode("utf-8")) > _PAGING_MESSAGE_MAX_BYTES:
            failure_code = "SIZE_LIMIT"
        elif _diagnostic_payload_valid(payload):
            return message
    except Exception:
        failure_code = "BUILD_FAILED"
    # No serialization dependency on the fallback path, and no exception text.
    return (_PAGING_FAILURE_PREFIX + _PAGING_DIAGNOSTIC_MARKER
            + '{"v":1,"code":"' + failure_code + '","diagnostic_status":"UNAVAILABLE"}')


@dataclass(frozen=True)
class QuantV6HistoricalBarFetch:
    bars: tuple[QuantV6Bar, ...]
    pages: int
    raw_rows: int
    rejected_rows: int


def quant_v6_historical_provider_contract() -> dict[str, object]:
    return dict(_PROVIDER_CONTRACT)


def quant_v6_historical_provider_digest_sha256() -> str:
    encoded = json.dumps(
        dict(_PROVIDER_CONTRACT),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_openapi() -> Any:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["Config"])
        except ImportError:
            continue
    raise QuantV6HistoricalProviderError(
        "Longbridge quote SDK is not installed"
    )


def _canonical_symbol(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value != value.strip().upper():
        raise QuantV6HistoricalProviderError("symbol must be canonical uppercase text")
    parts = value.rsplit(".", 1)
    if len(parts) != 2 or not parts[0] or parts[1] not in _EXCHANGE_TIMEZONES:
        raise QuantV6HistoricalProviderError("symbol must identify a US or HK security")
    if len(value) > 50 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    ):
        raise QuantV6HistoricalProviderError("symbol contains unsupported characters")
    return value, parts[1]


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise QuantV6HistoricalProviderError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _history_boundary(symbol: str, value: datetime) -> datetime:
    market = symbol.rsplit(".", 1)[-1]
    return value.astimezone(_EXCHANGE_TIMEZONES[market])


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        if text_value.endswith("Z"):
            text_value = f"{text_value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text_value)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _runtime_local_timezone_is_utc() -> bool:
    current_year = datetime.now().year
    probes = (
        datetime(current_year, 1, 1),
        datetime(current_year, 7, 1),
    )
    return all(
        value.astimezone().utcoffset() == timedelta(0)
        for value in probes
    )


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return candidate if candidate.is_finite() else None


def _coerce_bar(item: object, timestamp: datetime) -> QuantV6Bar | None:
    values = {
        field_name: _decimal(getattr(item, field_name, None))
        for field_name in ("open", "high", "low", "close", "volume")
    }
    if any(value is None for value in values.values()):
        return None
    try:
        return QuantV6Bar(
            start_at=timestamp,
            open=cast(Decimal, values["open"]),
            high=cast(Decimal, values["high"]),
            low=cast(Decimal, values["low"]),
            close=cast(Decimal, values["close"]),
            volume=cast(Decimal, values["volume"]),
        )
    except QuantV6SemanticError:
        return None


def _response_items(response: object) -> tuple[object, ...]:
    if response is None:
        return ()
    if isinstance(response, list):
        return tuple(response)
    if isinstance(response, tuple):
        return response
    return (response,)


class QuantV6HistoricalBarProvider:
    """Narrow Longport history reader that can never acquire order authority."""

    def __init__(
        self,
        *,
        module_loader: Callable[[], Any] = _load_openapi,
        sleep: Callable[[float], None] = time.sleep,
        cancel_event: threading.Event | None = None,
        evaluation_deadline: QuantV6EvaluationDeadline | None = None,
    ) -> None:
        if cancel_event is not None and evaluation_deadline is not None:
            raise ValueError(
                "cancel_event and evaluation_deadline are mutually exclusive"
            )
        self._module_loader = module_loader
        self._sleep = sleep
        self._cancel_event = cancel_event
        self._evaluation_deadline = evaluation_deadline
        self._lock = threading.Lock()
        self._module: Any = None
        self._quote_context: Any = None
        self._abandoned_context = False

    def supports_quant_v6_spawn_fetch(
        self,
        *,
        evaluation_deadline: QuantV6EvaluationDeadline,
    ) -> bool:
        """Confirm spawn fetches share the caller's bounded cancellation token."""
        return self._evaluation_deadline is evaluation_deadline

    def _cancelled(self) -> bool:
        return (
            (
                self._evaluation_deadline is not None
                and self._evaluation_deadline.is_stopped()
            )
            or (
                self._cancel_event is not None
                and self._cancel_event.is_set()
            )
        )

    def _raise_if_cancelled(self) -> None:
        if self._evaluation_deadline is not None:
            self._evaluation_deadline.checkpoint()
        if self._cancelled():
            raise QuantV6HistoricalProviderError(
                "quote-only historical acquisition was cancelled"
            )

    def _remaining_call_seconds(self, call_deadline: float) -> float:
        self._raise_if_cancelled()
        remaining = call_deadline - time.monotonic()
        if self._evaluation_deadline is not None:
            remaining = min(
                remaining,
                self._evaluation_deadline.remaining_seconds(),
            )
        return remaining

    def _wait_before_retry(self, delay_seconds: float) -> None:
        if self._evaluation_deadline is not None:
            self._evaluation_deadline.wait(delay_seconds)
            return
        if self._cancel_event is not None:
            if self._cancel_event.wait(delay_seconds):
                raise QuantV6HistoricalProviderError(
                    "quote-only historical acquisition was cancelled"
                )
            return
        self._sleep(delay_seconds)

    def _bounded_call(
        self,
        call: Callable[[], object],
        *,
        timeout_seconds: float,
        label: str,
        honor_evaluation_stop: bool = True,
        abandoned_cleanup: Callable[[object | None], None] | None = None,
    ) -> object:
        results: list[object] = []
        errors: list[Exception] = []
        state_lock = threading.Lock()
        completed = False
        abandoned = False

        def abandon_if_running() -> bool:
            nonlocal abandoned
            with state_lock:
                if completed:
                    return False
                abandoned = True
                return True

        def invoke() -> None:
            nonlocal completed
            result: object | None = None
            try:
                result = call()
                results.append(result)
            except Exception as exc:
                errors.append(exc)
            finally:
                with state_lock:
                    completed = True
                    should_cleanup = abandoned
                try:
                    if should_cleanup and abandoned_cleanup is not None:
                        abandoned_cleanup(result)
                except Exception as exc:
                    logger.warning(
                        "quote-only historical %s deferred cleanup failed: %s",
                        label,
                        type(exc).__name__,
                    )
                finally:
                    # Keep the singleton SDK slot until an abandoned call has
                    # really stopped and its context cleanup has completed.
                    # This prevents another provider from entering the SDK
                    # while ``close()`` is still draining the prior context.
                    _SDK_CALL_SLOT.release()

        if honor_evaluation_stop:
            self._raise_if_cancelled()
        if not _SDK_CALL_SLOT.acquire(blocking=False):
            raise QuantV6HistoricalProviderError(
                "a previous quote-only historical SDK call is still running"
            )
        worker = threading.Thread(
            target=invoke,
            name=f"quant-v6-{label}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            _SDK_CALL_SLOT.release()
            raise
        deadline = time.monotonic() + timeout_seconds
        while worker.is_alive():
            if honor_evaluation_stop:
                try:
                    remaining = self._remaining_call_seconds(deadline)
                except (
                    QuantV6EvaluationStoppedError,
                    QuantV6HistoricalProviderError,
                ):
                    if abandon_if_running():
                        self._abandoned_context = True
                        raise
                    worker.join()
                    break
            else:
                remaining = deadline - time.monotonic()
            if remaining <= 0:
                if abandon_if_running():
                    self._abandoned_context = True
                    raise QuantV6HistoricalProviderError(
                        f"quote-only historical {label} timed out"
                    )
                worker.join()
                break
            worker.join(min(0.1, remaining))
        if errors:
            raise errors[0]
        if len(results) != 1:
            raise QuantV6HistoricalProviderError(
                f"quote-only historical {label} returned no result"
            )
        return results[0]

    @staticmethod
    def _close_abandoned_context(
        quote_context: object,
        *,
        label: str,
    ) -> None:
        close = getattr(quote_context, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            logger.warning(
                "quote-only historical %s context deferred close failed: %s",
                label,
                type(exc).__name__,
            )

    def _close_abandoned_created_context(
        self,
        created: object | None,
    ) -> None:
        if type(created) is not tuple or len(created) != 2:
            return
        self._close_abandoned_context(
            created[1],
            label="context creation",
        )

    def _context(self) -> tuple[Any, Any]:
        if self._quote_context is None:
            def create_context() -> tuple[Any, Any]:
                module = self._module_loader()
                config_factory = getattr(module, "Config", None)
                quote_context_factory = getattr(module, "QuoteContext", None)
                if (
                    config_factory is None
                    or not hasattr(config_factory, "from_env")
                ):
                    raise QuantV6HistoricalProviderError(
                        "quote SDK Config.from_env is unavailable"
                    )
                if not callable(quote_context_factory):
                    raise QuantV6HistoricalProviderError(
                        "quote SDK QuoteContext is unavailable"
                    )
                config = config_factory.from_env()
                return module, quote_context_factory(config)

            created = self._bounded_call(
                create_context,
                timeout_seconds=(
                    QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS / 1_000
                ),
                label="context creation",
                abandoned_cleanup=self._close_abandoned_created_context,
            )
            if type(created) is not tuple or len(created) != 2:
                raise QuantV6HistoricalProviderError(
                    "quote-only historical context creation returned an "
                    "invalid result"
                )
            self._module, self._quote_context = created
        return self._module, self._quote_context

    @staticmethod
    def _retryable(module: Any, exc: Exception) -> bool:
        if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
            return True
        exception_type = getattr(module, "OpenApiException", None)
        if not isinstance(exception_type, type) or not isinstance(exc, exception_type):
            return False
        lowered = str(exc).lower()
        return any(marker in lowered for marker in _RETRYABLE_MESSAGE_MARKERS)

    def _read_page(
        self,
        *,
        module: Any,
        quote_context: Any,
        symbol: str,
        period: object,
        adjustment: object,
        cursor: datetime,
        request_boundary: datetime,
    ) -> tuple[object, ...]:
        reader = getattr(quote_context, "history_candlesticks_by_offset", None)
        if not callable(reader):
            raise QuantV6HistoricalProviderError(
                "quote context lacks historical candlestick paging"
            )
        retry_limit = QUANT_V6_HISTORICAL_RETRY_MAX
        for attempt in range(retry_limit + 1):
            try:
                response = self._bounded_call(
                    lambda: reader(
                        symbol,
                        period,
                        adjustment,
                        True,
                        QUANT_V6_HISTORICAL_PAGE_SIZE,
                        request_boundary,
                    ),
                    timeout_seconds=(
                        QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS / 1_000
                    ),
                    label="page read",
                    abandoned_cleanup=lambda _result: self._close_abandoned_context(
                        quote_context,
                        label="page read",
                    ),
                )
                return _response_items(response)
            except (
                QuantV6EvaluationStoppedError,
                QuantV6HistoricalProviderError,
            ):
                raise
            except Exception as exc:
                if not self._retryable(module, exc) or attempt >= retry_limit:
                    raise QuantV6HistoricalProviderError(
                        "quote-only historical page acquisition failed"
                    ) from exc
                delay = (
                    QUANT_V6_HISTORICAL_RETRY_BASE_MILLISECONDS / 1_000
                ) * (2**attempt)
                self._wait_before_retry(delay)
        raise QuantV6HistoricalProviderError("historical retry state is invalid")

    def fetch_five_minute_no_adjust(
        self,
        symbol: str,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> QuantV6HistoricalBarFetch:
        self._raise_if_cancelled()
        symbol, _market = _canonical_symbol(symbol)
        start = _aware_utc(start_at, label="start_at")
        end = _aware_utc(end_at, label="end_at")
        if end <= start:
            raise QuantV6HistoricalProviderError("end_at must follow start_at")
        if end - start > timedelta(days=QUANT_V6_HISTORICAL_MAX_RANGE_DAYS):
            raise QuantV6HistoricalProviderError("historical range exceeds the limit")
        if not _runtime_local_timezone_is_utc():
            raise QuantV6HistoricalProviderError(
                "quote-only historical evidence requires a UTC process timezone"
            )

        with self._lock:
            module, quote_context = self._context()
            period = getattr(
                getattr(module, "Period", None),
                "Min_5",
                None,
            )
            adjustment = getattr(
                getattr(module, "AdjustType", None),
                "NoAdjust",
                None,
            )
            if period is None:
                raise QuantV6HistoricalProviderError(
                    "quote SDK Period.Min_5 is unavailable"
                )
            if adjustment is None:
                raise QuantV6HistoricalProviderError(
                    "quote SDK AdjustType.NoAdjust is unavailable"
                )

            cursor = start - _BAR_DURATION
            bars: list[QuantV6Bar] = []
            raw_rows = 0
            rejected_rows = 0
            seen_timestamps: set[datetime] = set()
            previous_raw_boundary: _BoundaryDigest | None = None
            for page_number in range(1, QUANT_V6_HISTORICAL_MAX_PAGES + 1):
                self._raise_if_cancelled()
                # Capture the exact exchange-local argument sent by _read_page.
                request_boundary = _history_boundary(symbol, cursor)
                items = self._read_page(
                    module=module,
                    quote_context=quote_context,
                    symbol=symbol,
                    period=period,
                    adjustment=adjustment,
                    cursor=cursor,
                    request_boundary=request_boundary,
                )
                if not items:
                    return QuantV6HistoricalBarFetch(
                        bars=tuple(bars),
                        pages=page_number,
                        raw_rows=raw_rows,
                        rejected_rows=rejected_rows,
                    )
                if len(items) > QUANT_V6_HISTORICAL_PAGE_SIZE:
                    raise QuantV6HistoricalProviderError(
                        "historical page exceeds the row limit"
                    )
                raw_rows += len(items)
                if raw_rows > QUANT_V6_HISTORICAL_MAX_RAW_ROWS:
                    raise QuantV6HistoricalProviderError(
                        "historical response exceeds the raw row limit"
                    )
                parsed_rows = tuple(
                    (item, _parse_timestamp(getattr(item, "timestamp", None)))
                    for item in items
                )
                advancing = tuple(sorted(
                    (
                        (item, timestamp)
                        for item, timestamp in parsed_rows
                        if timestamp is not None and timestamp > cursor
                    ),
                    key=lambda value: value[1],
                ))
                rejected_rows += sum(
                    1 for _item, timestamp in parsed_rows if timestamp is None
                )
                if not advancing:
                    # Longport's forward paging repeats its inclusive
                    # boundary once the available history is exhausted.
                    # Treat only one exact, valid copy of the already
                    # accepted terminal bar as EOF, wherever the stream
                    # ends: a halted or delisted symbol can exhaust its
                    # exchange history before the requested window closes,
                    # and the evaluator records those absent sessions as
                    # honest SESSION_MISSING evidence.  Every other
                    # non-advancing response remains a fail-closed paging
                    # error.
                    if (
                        bars
                        and bars[-1].start_at == cursor
                        and len(parsed_rows) == 1
                    ):
                        repeated_item, repeated_timestamp = parsed_rows[0]
                        if repeated_timestamp is not None:
                            repeated_bar = _coerce_bar(
                                repeated_item,
                                repeated_timestamp,
                            )
                            if (
                                repeated_timestamp == cursor
                                and repeated_bar == bars[-1]
                            ):
                                return QuantV6HistoricalBarFetch(
                                    bars=tuple(bars),
                                    pages=page_number,
                                    raw_rows=raw_rows,
                                    rejected_rows=rejected_rows,
                                )
                    raise QuantV6HistoricalProviderError(
                        _paging_failure_message(
                            symbol=symbol, page=page_number, start=start, end=end,
                            cursor=cursor, request_boundary=request_boundary,
                            parsed_rows=parsed_rows, raw_total=raw_rows,
                            rejected_total=rejected_rows, accepted_bars=bars,
                            previous_raw_boundary=previous_raw_boundary,
                        )
                    )
                page_timestamps = tuple(
                    timestamp for _item, timestamp in advancing
                )
                if (
                    len(set(page_timestamps)) != len(page_timestamps)
                    or any(
                        timestamp in seen_timestamps
                        for timestamp in page_timestamps
                    )
                ):
                    raise QuantV6HistoricalProviderError(
                        "historical response contains duplicate timestamps"
                    )
                seen_timestamps.update(page_timestamps)
                latest = max(timestamp for _item, timestamp in advancing)
                for item, timestamp in advancing:
                    if not start <= timestamp < end:
                        continue
                    bar = _coerce_bar(item, timestamp)
                    if bar is None:
                        rejected_rows += 1
                    else:
                        bars.append(bar)
                        if len(bars) > QUANT_V6_HISTORICAL_MAX_BARS:
                            raise QuantV6HistoricalProviderError(
                                "historical response exceeds the bar limit"
                            )
                cursor = latest
                if latest >= end:
                    return QuantV6HistoricalBarFetch(
                        bars=tuple(bars),
                        pages=page_number,
                        raw_rows=raw_rows,
                        rejected_rows=rejected_rows,
                    )
                previous_raw_boundary = _remember_boundary(parsed_rows, cursor)
            raise QuantV6HistoricalProviderError(
                "historical response exceeds the page limit"
            )

    def close(self) -> None:
        with self._lock:
            quote_context = self._quote_context
            self._quote_context = None
            self._module = None
        close = getattr(quote_context, "close", None)
        if self._abandoned_context:
            if quote_context is not None:
                logger.warning(
                    "quote-only historical context abandoned after "
                    "timeout or cancellation"
                )
            return
        if callable(close):
            try:
                self._bounded_call(
                    close,
                    timeout_seconds=(
                        QUANT_V6_HISTORICAL_PAGE_TIMEOUT_MILLISECONDS / 1_000
                    ),
                    label="context close",
                    honor_evaluation_stop=False,
                )
            except Exception as exc:
                logger.warning(
                    "quote-only historical context close failed: %s",
                    type(exc).__name__,
                )
