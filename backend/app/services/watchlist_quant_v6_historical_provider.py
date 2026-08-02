from __future__ import annotations

import hashlib
import json
import logging
import math
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
    "watchlist-quant-v6-longport-quote-only-history-v2"
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
                        _history_boundary(symbol, cursor),
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
            for page_number in range(1, QUANT_V6_HISTORICAL_MAX_PAGES + 1):
                self._raise_if_cancelled()
                items = self._read_page(
                    module=module,
                    quote_context=quote_context,
                    symbol=symbol,
                    period=period,
                    adjustment=adjustment,
                    cursor=cursor,
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
                    # Longport's forward paging can repeat its inclusive
                    # boundary once the requested range is exhausted.  Treat
                    # only one exact, valid copy of the already accepted
                    # terminal bar as EOF.  Every other non-advancing response
                    # remains a fail-closed paging error.
                    if (
                        cursor + _BAR_DURATION >= end
                        and bars
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
                        "historical candlestick cursor did not advance"
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
