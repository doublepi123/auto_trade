from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation as _DecimalInvalidOp
from typing import TYPE_CHECKING, Any, Callable

from app.config import settings

if TYPE_CHECKING:
    from app.core.audit import AuditLogger

try:
    from longport.openapi import OpenApiException as _OpenApiException

    _retryable_exc: tuple[type[BaseException], ...] = (
        _OpenApiException,
        OSError,
        ConnectionError,
        TimeoutError,
    )
except ImportError:
    _retryable_exc = (OSError, ConnectionError, TimeoutError)
RETRYABLE_EXC = _retryable_exc

logger = logging.getLogger("auto_trade.broker")
DisconnectHook = Callable[[str], None]

_RETRYABLE_MESSAGE_MARKERS = (
    "限流",
    "频率",
    "rate limit",
    "rate_limit",
    "too many requests",
    "too frequent",
    "throttle",
    "throttled",
    "timeout",
    "connection",
    "unavailable",
    "internal error",
    "500000",
    "429",
)

_BBO_CACHE_MAX_AGE_SECONDS = 30.0


def _is_retryable_message(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RETRYABLE_MESSAGE_MARKERS)


_NETWORK_EXC: tuple[type[BaseException], ...] = (OSError, ConnectionError, TimeoutError)


def _is_retryable_exception(exc: BaseException) -> bool:
    # Network-level errors are always retryable without message check
    if isinstance(exc, _NETWORK_EXC):
        return True
    if isinstance(exc, RETRYABLE_EXC):
        return _is_retryable_message(exc)
    return False


def _import_openapi() -> Any:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["Config"])
        except ImportError:
            continue
    raise RuntimeError("Longbridge SDK not installed. Install longport or longbridge.")


@dataclass
class BrokerCredentials:
    app_key: str = ""
    app_secret: str = ""
    access_token: str = ""


@dataclass
class Quote:
    symbol: str
    last_price: float
    bid: float
    ask: float
    timestamp: str


@dataclass(frozen=True)
class BrokerCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass
class OrderResult:
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    status: str


@dataclass
class OrderStatusResult:
    broker_order_id: str
    status: str
    executed_quantity: Decimal = Decimal("0")
    executed_price: Decimal = Decimal("0")
    actual_fee: Decimal | None = None
    fee_currency: str = ""
    broker_submitted_at: datetime | None = None
    broker_updated_at: datetime | None = None


@dataclass
class BrokerOrder:
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    executed_quantity: Decimal
    executed_price: Decimal
    status: str
    created_at: datetime | None = None
    filled_at: datetime | None = None


@dataclass
class Position:
    symbol: str
    side: str
    quantity: Decimal
    avg_price: Decimal
    available_quantity: Decimal | None = None


@dataclass
class CashBalance:
    currency: str
    available_cash: Decimal
    frozen_cash: Decimal


@dataclass
class NetAsset:
    currency: str
    amount: Decimal


@dataclass
class AccountInfo:
    total_assets: Decimal
    cash_balances: list[CashBalance]
    net_assets: list[NetAsset]


def _get_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _iter_position_items(item: Any) -> list[Any]:
    if item is None:
        return []
    if isinstance(item, (list, tuple)):
        result: list[Any] = []
        for child in item:
            result.extend(_iter_position_items(child))
        return result
    if isinstance(item, dict):
        if "data" in item:
            nested = item["data"]
            if nested is None:
                raise ValueError("broker returned null position data")
            return _iter_position_items(nested)
        for key in ("list", "channels", "stock_info", "positions"):
            if key in item:
                nested = item[key]
                if nested is item:
                    continue
                if nested is None:
                    raise ValueError(f"broker returned null position {key}")
                return _iter_position_items(nested)
        position_fields = {
            "symbol",
            "quantity",
            "available_quantity",
            "cost_price",
            "avg_price",
            "average_price",
        }
        if position_fields.intersection(item):
            return [item]
        raise ValueError("broker returned an unrecognized position response")

    children: list[Any] = []
    saw_container = False
    for attr in ("channels", "list", "stock_info", "positions"):
        if not hasattr(item, attr):
            continue
        saw_container = True
        nested = getattr(item, attr)
        if nested is None:
            raise ValueError(f"broker returned null position {attr}")
        children.extend(_iter_position_items(nested))
    if saw_container:
        return children
    position_fields = (
        "symbol",
        "quantity",
        "available_quantity",
        "cost_price",
        "avg_price",
        "average_price",
    )
    if any(getattr(item, name, None) is not None for name in position_fields):
        return [item]
    raise ValueError("broker returned an unrecognized position response")


def _iter_order_items(item: Any) -> list[Any]:
    if item is None:
        return []
    if isinstance(item, (list, tuple)):
        result: list[Any] = []
        for child in item:
            result.extend(_iter_order_items(child))
        return result
    if isinstance(item, dict):
        if "data" in item:
            nested = item["data"]
            if nested is None:
                raise ValueError("broker returned null order data")
            return _iter_order_items(nested)
        for key in ("orders", "list", "items"):
            if key in item:
                nested = item[key]
                if nested is None:
                    raise ValueError(f"broker returned null order {key}")
                return _iter_order_items(nested)
        order_fields = {
            "order_id",
            "broker_order_id",
            "symbol",
            "side",
            "status",
            "submitted_quantity",
            "quantity",
            "executed_quantity",
            "filled_quantity",
        }
        if order_fields.intersection(item):
            return [item]
        raise ValueError("broker returned an unrecognized order response")

    children: list[Any] = []
    saw_container = False
    for attr in ("orders", "list", "items", "data"):
        if not hasattr(item, attr):
            continue
        nested = getattr(item, attr)
        if nested is item:
            continue
        saw_container = True
        if nested is None:
            raise ValueError(f"broker returned null order {attr}")
        children.extend(_iter_order_items(nested))
    if saw_container:
        return children
    order_fields = (
        "order_id",
        "broker_order_id",
        "symbol",
        "side",
        "status",
        "submitted_quantity",
        "quantity",
        "executed_quantity",
        "filled_quantity",
    )
    if any(getattr(item, name, None) is not None for name in order_fields):
        return [item]
    raise ValueError("broker returned an unrecognized order response")


_SIDE_MAP = {"BUY": "Buy", "SELL": "Sell", "SELL_SHORT": "Sell", "BUY_TO_COVER": "Buy"}


def _normalize_order_status(raw_status: Any) -> str:
    text = str(getattr(raw_status, "value", raw_status)).split(".")[-1]
    key = text.upper().replace("_", "").replace("-", "").replace(" ", "")
    if key == "FILLED":
        return "FILLED"
    if key == "PARTIALFILLED":
        return "PARTIAL_FILLED"
    if key == "REJECTED":
        return "REJECTED"
    if key in {"CANCELED", "CANCELLED", "EXPIRED", "PARTIALWITHDRAWAL"}:
        return "CANCELLED"
    return "SUBMITTED"


def _decimal_attr(item: Any, *names: str) -> Decimal:
    for name in names:
        value = _get_value(item, name, None)
        if value is not None:
            try:
                return Decimal(str(value))
            except (ValueError, TypeError, AttributeError, _DecimalInvalidOp):
                return Decimal("0")
    return Decimal("0")


def _nonnegative_decimal_attr(item: Any, *names: str) -> Decimal:
    for name in names:
        value = _get_value(item, name, None)
        if value is None:
            continue
        try:
            parsed = Decimal(str(value))
        except (ValueError, TypeError, AttributeError, _DecimalInvalidOp) as exc:
            raise ValueError(f"broker returned invalid {name}") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"broker returned invalid {name}")
        return parsed
    return Decimal("0")


def _require_matching_order_id(item: Any, expected_order_id: str) -> str:
    raw_order_id = _get_value(
        item,
        "order_id",
        _get_value(item, "broker_order_id", None),
    )
    order_id = str(raw_order_id or "").strip()
    if not order_id:
        raise ValueError("broker order status response is missing order_id")
    if order_id != expected_order_id:
        raise ValueError(
            "broker order status response id mismatch: "
            f"expected {expected_order_id}, got {order_id}"
        )
    return order_id


def _order_charge(item: Any) -> tuple[Decimal | None, str]:
    """Return broker-reported total charges, preserving unavailable vs zero."""
    detail = _get_value(item, "charge_detail", None)
    if detail is None:
        return None, ""
    raw_total = _get_value(detail, "total_amount", None)
    currency = str(_get_value(detail, "currency", "") or "").upper()
    if raw_total is None:
        return None, currency
    try:
        total = Decimal(str(raw_total))
    except (ValueError, TypeError, AttributeError, _DecimalInvalidOp) as exc:
        raise ValueError("broker returned invalid total order charge") from exc
    if not total.is_finite() or total < 0:
        raise ValueError("broker returned invalid total order charge")
    return total, currency


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _normalize_period_name(period: str) -> str:
    text = str(period).strip().upper().replace("-", "_")
    aliases = {
        "DAY": "Day",
        "D": "Day",
        "1D": "Day",
        "WEEK": "Week",
        "MONTH": "Month",
        "QUARTER": "Quarter",
        "YEAR": "Year",
    }
    if text in aliases:
        return aliases[text]
    if text.startswith("MIN_"):
        return "Min_" + text[len("MIN_"):]
    if text.startswith("MIN") and text[3:].isdigit():
        return "Min_" + text[3:]
    return text.capitalize()


def _parse_candle_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return _parse_datetime(value)


def _normalize_order_side(raw_side: Any) -> str:
    text = str(getattr(raw_side, "value", raw_side)).split(".")[-1]
    key = text.upper().replace("_", "").replace("-", "").replace(" ", "")
    if key in {"BUY", "BUYTOCOVER"}:
        return "BUY_TO_COVER" if key == "BUYTOCOVER" else "BUY"
    if key in {"SELL", "SELLSHORT"}:
        return "SELL_SHORT" if key == "SELLSHORT" else "SELL"
    return text.upper() or "UNKNOWN"


class BrokerGateway:
    def __init__(self, audit: AuditLogger | None = None) -> None:
        self._audit = audit
        self._lock = threading.RLock()
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._quote_callbacks: list[Callable[[Quote], None]] = []
        self._disconnect_hooks: list[DisconnectHook] = []
        self._subscribed_symbols: set[str] = set()
        self._last_trade_by_symbol: dict[str, tuple[float, str]] = {}
        self._bbo_by_symbol: dict[str, tuple[float, float, float]] = {}

    @staticmethod
    def _best_depth_price(levels: Any, *, side: str) -> float:
        values: list[float] = []
        for level in levels or ():
            try:
                price = float(getattr(level, "price", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(price) and price > 0:
                values.append(price)
        if not values:
            return 0.0
        return max(values) if side == "bid" else min(values)

    @classmethod
    def _bbo_from_depth(cls, depth: Any) -> tuple[float, float]:
        bids = getattr(depth, "bids", None)
        asks = getattr(depth, "asks", None)
        return (
            cls._best_depth_price(
                bids if bids is not None else getattr(depth, "bid", ()),
                side="bid",
            ),
            cls._best_depth_price(
                asks if asks is not None else getattr(depth, "ask", ()),
                side="ask",
            ),
        )

    def _remember_bbo(self, symbol: str, bid: float, ask: float) -> None:
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0
            or ask <= 0
            or ask < bid
        ):
            return
        self._bbo_by_symbol[symbol] = (bid, ask, time.monotonic())

    def _cached_bbo(self, symbol: str) -> tuple[float, float]:
        cached = self._bbo_by_symbol.get(symbol)
        if cached is None:
            return 0.0, 0.0
        bid, ask, observed_at = cached
        if time.monotonic() - observed_at > _BBO_CACHE_MAX_AGE_SECONDS:
            return 0.0, 0.0
        return bid, ask

    def _pull_bbo(self, symbol: str) -> tuple[float, float]:
        depth_reader = getattr(self._quote_ctx, "depth", None)
        if not callable(depth_reader):
            return self._cached_bbo(symbol)
        try:
            bid, ask = self._bbo_from_depth(depth_reader(symbol))
        except Exception as exc:
            logger.warning("broker depth fetch failed for %s: %s", symbol, exc)
            return self._cached_bbo(symbol)
        self._remember_bbo(symbol, bid, ask)
        return bid, ask

    def register_disconnect_hook(self, hook: DisconnectHook) -> None:
        """Register a broker disconnect hook, de-duplicating the same callable."""
        if hook not in self._disconnect_hooks:
            self._disconnect_hooks.append(hook)

    def _call_disconnect_hooks(self, reason: str) -> None:
        """Invoke disconnect hooks without letting one failure block others."""
        for hook in list(self._disconnect_hooks):
            try:
                hook(reason)
            except Exception as exc:
                logger.warning("disconnect_hook_failed: %s", exc)

    def _register_native_disconnect_if_available(self) -> None:
        """Attach to the SDK disconnect event when available; watchdog remains the fallback."""
        quote_ctx = self._quote_ctx
        if quote_ctx is None:
            return
        on_disconnect = getattr(quote_ctx, "on_disconnect", None)
        if not callable(on_disconnect):
            on_disconnect = getattr(quote_ctx, "set_on_disconnect", None)
        if not callable(on_disconnect):
            logger.info("broker disconnect event unavailable; falling back to quote watchdog")
            return
        try:
            on_disconnect(self._call_disconnect_hooks)
        except Exception as exc:
            logger.warning("native_disconnect_register_failed: %s", exc)

    def _call_with_retry(
        self,
        fn: Callable[[], Any],
        *,
        op: str,
        max_retries: int,
        base_ms: int,
    ) -> Any:
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except RETRYABLE_EXC as exc:
                if not _is_retryable_exception(exc):
                    raise
                if attempt >= max_retries:
                    raise
                delay_s = (base_ms / 1000.0) * (2 ** attempt)
                if self._audit:
                    self._audit.record(
                        "BROKER_RETRY",
                        severity="INFO",
                        request_summary={
                            "op": op,
                            "attempt": attempt + 1,
                            "delay_s": delay_s,
                            "exc": type(exc).__name__,
                            "message": str(exc)[:200],
                        },
                    )
                time.sleep(delay_s)
        raise RuntimeError("unreachable")

    def _init_clients(self) -> None:
        with self._lock:
            if self._quote_ctx is None:
                module = _import_openapi()
                config = module.Config.from_env()
                self._quote_ctx = module.QuoteContext(config)
                self._trade_ctx = module.TradeContext(config)
                self._register_native_disconnect_if_available()

    def get_candlesticks(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        """Fetch recent candlesticks. ``period`` is one of ``DAY``, ``MIN_1``, ``MIN_5``, etc."""
        return self._call_with_retry(
            lambda: self._get_candlesticks_inner(symbol, period, count),
            op="get_candlesticks",
            max_retries=settings.broker_quote_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def _get_candlesticks_inner(self, symbol: str, period: str, count: int) -> list[BrokerCandle]:
        if count <= 0:
            return []
        with self._lock:
            self._init_clients()
            module = _import_openapi()
            Period = getattr(module, "Period", None)
            AdjustType = getattr(module, "AdjustType", None)
            if Period is None or AdjustType is None:
                raise RuntimeError("longport SDK is missing Period/AdjustType enums")

            period_enum = getattr(Period, _normalize_period_name(period), None)
            if period_enum is None:
                raise ValueError(f"unsupported candlestick period: {period}")
            adjust_enum = getattr(AdjustType, "NoAdjust", None)
            if adjust_enum is None:
                raise RuntimeError("AdjustType.NoAdjust not found in SDK")

            quote_ctx = self._quote_ctx
            if quote_ctx is None:
                raise RuntimeError("quote context is not initialized")
            response = quote_ctx.candlesticks(symbol, period_enum, count, adjust_enum)
            items = response if isinstance(response, list) else [response]
            candles: list[BrokerCandle] = []
            dropped = 0
            for item in items:
                ts = _parse_candle_timestamp(getattr(item, "timestamp", None))
                if ts is None:
                    dropped += 1
                    continue
                try:
                    candle = BrokerCandle(
                        timestamp=ts,
                        open=float(getattr(item, "open", 0)),
                        high=float(getattr(item, "high", 0)),
                        low=float(getattr(item, "low", 0)),
                        close=float(getattr(item, "close", 0)),
                        volume=float(getattr(item, "volume", 0)),
                        turnover=float(getattr(item, "turnover", 0)),
                    )
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                prices = (candle.open, candle.high, candle.low, candle.close)
                if (
                    not all(math.isfinite(value) and value > 0 for value in prices)
                    or candle.high < max(candle.open, candle.close, candle.low)
                    or candle.low > min(candle.open, candle.close, candle.high)
                    or not math.isfinite(candle.volume)
                    or candle.volume < 0
                    or not math.isfinite(candle.turnover)
                    or candle.turnover < 0
                ):
                    dropped += 1
                    continue
                candles.append(candle)
            if dropped:
                logger.warning(
                    "dropped %d invalid %s candlesticks for %s (received=%d)",
                    dropped,
                    period,
                    symbol,
                    len(items),
                )
            candles.sort(key=lambda c: c.timestamp)
            return candles

    def get_quote(self, symbol: str) -> Quote:
        return self._call_with_retry(
            lambda: self._get_quote_inner(symbol),
            op="get_quote",
            max_retries=settings.broker_quote_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def _get_quote_inner(self, symbol: str) -> Quote:
        quotes = self._get_quotes_inner([symbol])
        if not quotes:
            raise ValueError(f"no quote data for {symbol}")
        return quotes[0]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        return self._call_with_retry(
            lambda: self._get_quotes_inner(symbols),
            op="get_quotes",
            max_retries=settings.broker_quote_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def _get_quotes_inner(self, symbols: list[str]) -> list[Quote]:
        with self._lock:
            self._init_clients()
            response = self._quote_ctx.quote(symbols)
            items = response if isinstance(response, list) else [response]
            if len(items) != len(symbols):
                if not items:
                    raise RuntimeError(f"broker returned 0 quotes for {len(symbols)} symbols")
                logger.warning("broker returned %d quotes for %d symbols", len(items), len(symbols))
            item_by_symbol: dict[str, Any] = {}
            for item in items:
                sym = str(getattr(item, "symbol", ""))
                if sym:
                    item_by_symbol[sym] = item
            # Fallback: if symbol-keyed lookup is empty (items lack symbol attr),
            # use positional pairing (single item + single symbol case)
            if not item_by_symbol and len(items) == len(symbols):
                item_by_symbol = dict(zip(symbols, items))
            quotes: list[Quote] = []
            for fallback_symbol in symbols:
                item = item_by_symbol.get(fallback_symbol)
                if item is None:
                    logger.warning("broker did not return quote for symbol %s", fallback_symbol)
                    continue
                symbol = str(getattr(item, "symbol", fallback_symbol))
                last_price = float(getattr(item, "last_done", 0))
                timestamp = str(getattr(item, "timestamp", ""))
                bid = float(getattr(item, "bid", 0))
                ask = float(getattr(item, "ask", 0))
                if bid <= 0 or ask <= 0:
                    bid, ask = self._pull_bbo(symbol)
                else:
                    self._remember_bbo(symbol, bid, ask)
                if last_price > 0:
                    self._last_trade_by_symbol[symbol] = (last_price, timestamp)
                quotes.append(Quote(
                    symbol=symbol,
                    last_price=last_price,
                    bid=bid,
                    ask=ask,
                    timestamp=timestamp,
                ))
            return quotes

    def subscribe_quotes(self, symbol: str, callback: Callable[[Quote], None]) -> None:
        self.subscribe_quotes_batch([symbol], callback)

    def subscribe_quotes_batch(self, symbols: list[str], callback: Callable[[Quote], None]) -> None:
        unique_symbols = [symbol for symbol in dict.fromkeys(symbols) if symbol]
        if not unique_symbols:
            return

        def _on_quote(_symbol: str, _event: Any) -> None:
            symbol = str(getattr(_event, "symbol", _symbol))
            last_price = float(getattr(_event, "last_done", 0))
            timestamp = str(getattr(_event, "timestamp", ""))
            with self._lock:
                if last_price > 0:
                    self._last_trade_by_symbol[symbol] = (last_price, timestamp)
                else:
                    latest = self._last_trade_by_symbol.get(symbol)
                    if latest is None:
                        return
                    last_price, timestamp = latest
                bid, ask = self._cached_bbo(symbol)
                callbacks = list(self._quote_callbacks)
            quote = Quote(symbol, last_price, bid, ask, timestamp)
            for cb in callbacks:
                try:
                    cb(quote)
                except Exception:
                    logger.exception("quote callback failed for %s", _symbol)

        def _on_depth(_symbol: str, _event: Any) -> None:
            symbol = str(getattr(_event, "symbol", _symbol))
            bid, ask = self._bbo_from_depth(_event)
            with self._lock:
                self._remember_bbo(symbol, bid, ask)
                latest = self._last_trade_by_symbol.get(symbol)
                callbacks = list(self._quote_callbacks)
            if latest is None or bid <= 0 or ask <= 0 or ask < bid:
                return
            last_price, timestamp = latest
            quote = Quote(symbol, last_price, bid, ask, timestamp)
            for cb in callbacks:
                try:
                    cb(quote)
                except Exception:
                    logger.exception("depth callback failed for %s", _symbol)

        with self._lock:
            added_callback = False
            if callback not in self._quote_callbacks:
                self._quote_callbacks.append(callback)
                added_callback = True
            missing_symbols = [symbol for symbol in unique_symbols if symbol not in self._subscribed_symbols]
            if not missing_symbols:
                # Even when no new symbols, re-register the composite handler
                # so the newly appended callback is wired into set_on_quote.
                self._init_clients()
                quote_ctx = self._quote_ctx
                if quote_ctx is not None:
                    quote_ctx.set_on_quote(_on_quote)
                    depth_handler = getattr(quote_ctx, "set_on_depth", None)
                    if callable(depth_handler):
                        depth_handler(_on_depth)
                return
            self._init_clients()
            quote_ctx = self._quote_ctx
            if quote_ctx is None:
                if added_callback:
                    self._quote_callbacks.remove(callback)
                raise RuntimeError("quote context is not initialized")
            module = _import_openapi()
            SubType = getattr(module, "SubType", None)

            quote_ctx.set_on_quote(_on_quote)
            if SubType is None:
                if added_callback and callback in self._quote_callbacks:
                    self._quote_callbacks.remove(callback)
                raise RuntimeError(
                    "longport SDK SubType not available — cannot subscribe to quotes. "
                    "Install the longport package."
                )
            topics = [SubType.Quote]
            depth_type = getattr(SubType, "Depth", None)
            depth_handler = getattr(quote_ctx, "set_on_depth", None)
            if depth_type is not None and callable(depth_handler):
                depth_handler(_on_depth)
                topics.append(depth_type)
            else:
                logger.warning(
                    "longport SDK depth subscription unavailable; executable BBO "
                    "will rely on pull depth"
                )
            try:
                quote_ctx.subscribe(missing_symbols, topics)
            except Exception:
                if added_callback and callback in self._quote_callbacks:
                    self._quote_callbacks.remove(callback)
                raise
            self._subscribed_symbols.update(missing_symbols)
    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        # NOTE: submit_limit_order 故意不使用 _call_with_retry。
        # 下单是 non-idempotent 操作 — 网络重试可能导致重复下单(双倍仓位)。
        # 网络失败由调用方(AppRunner)在下一循环重新决策,而非 broker 层重试。
        # 对比:cancel_order 用了重试,因为取消是幂等的(取消已取消订单无害)。
        return self._submit_limit_order_inner(symbol, side, quantity, price)

    def _submit_limit_order_inner(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        with self._lock:
            self._init_clients()
            module = _import_openapi()
            OrderSide = getattr(module, "OrderSide", None)
            OrderType = getattr(module, "OrderType", None)
            TimeInForceType = getattr(module, "TimeInForceType", None)

            side_name = _SIDE_MAP.get(side, side)
            side_enum = getattr(OrderSide, side_name, side) if OrderSide else side
            lo_type = getattr(OrderType, "LO", "LO") if OrderType else "LO"
            day_tif = getattr(TimeInForceType, "Day", "DAY") if TimeInForceType else "DAY"

            response = self._trade_ctx.submit_order(
                symbol=symbol,
                order_type=lo_type,
                side=side_enum,
                submitted_quantity=quantity,
                time_in_force=day_tif,
                submitted_price=price,
                remark="auto-trade",
            )

            order_id = str(getattr(response, "order_id", getattr(response, "broker_order_id", "")) or "").strip()
            if not order_id:
                raise RuntimeError("broker submit_order returned empty order_id")
            raw_status = getattr(response, "status", "SUBMITTED")
            return OrderResult(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status=_normalize_order_status(raw_status),
            )

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        def _fetch() -> OrderStatusResult:
            with self._lock:
                self._init_clients()
                detail = self._trade_ctx.order_detail(order_id)
                actual_fee, fee_currency = _order_charge(detail)
                return OrderStatusResult(
                    broker_order_id=_require_matching_order_id(detail, order_id),
                    status=_normalize_order_status(_get_value(detail, "status", "SUBMITTED")),
                    executed_quantity=_nonnegative_decimal_attr(
                        detail,
                        "executed_quantity",
                        "filled_quantity",
                    ),
                    executed_price=_nonnegative_decimal_attr(
                        detail,
                        "executed_price",
                        "filled_price",
                    ),
                    actual_fee=actual_fee,
                    fee_currency=fee_currency,
                    broker_submitted_at=_parse_datetime(
                        _get_value(detail, "submitted_at", None)
                    ),
                    broker_updated_at=_parse_datetime(
                        _get_value(detail, "updated_at", None)
                    ),
                )
        return self._call_with_retry(
            _fetch,
            op="get_order_status",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def get_today_orders(self) -> list[BrokerOrder]:
        def _fetch() -> list[BrokerOrder]:
            with self._lock:
                self._init_clients()
                response = None
                last_error: Exception | None = None
                for method_name in ("today_orders", "order_list", "stock_order_list", "orders"):
                    method = getattr(self._trade_ctx, method_name, None)
                    if method is None:
                        continue
                    try:
                        response = method()
                        break
                    except TypeError as exc:
                        last_error = exc
                        continue
                if response is None:
                    if last_error is not None:
                        raise last_error
                    raise RuntimeError("broker does not support listing today orders")

                orders: list[BrokerOrder] = []
                for item in _iter_order_items(response):
                    raw_order_id = _get_value(
                        item,
                        "order_id",
                        _get_value(item, "broker_order_id", None),
                    )
                    order_id = str(raw_order_id or "").strip()
                    if not order_id:
                        raise ValueError(
                            "broker returned an order without broker_order_id"
                        )
                    executed_quantity = _nonnegative_decimal_attr(
                        item,
                        "executed_quantity",
                        "filled_quantity",
                    )
                    status = _normalize_order_status(
                        _get_value(item, "status", "SUBMITTED")
                    )
                    raw_filled_at = _get_value(item, "filled_at", None)
                    if raw_filled_at is None and (
                        status == "FILLED" or executed_quantity > 0
                    ):
                        raw_filled_at = _get_value(item, "updated_at", None)
                    orders.append(BrokerOrder(
                        broker_order_id=order_id,
                        symbol=str(_get_value(item, "symbol", "")),
                        side=_normalize_order_side(_get_value(item, "side", "")),
                        quantity=_decimal_attr(item, "submitted_quantity", "quantity"),
                        price=_decimal_attr(item, "submitted_price", "price", "limit_price"),
                        executed_quantity=executed_quantity,
                        executed_price=_nonnegative_decimal_attr(
                            item,
                            "executed_price",
                            "filled_price",
                        ),
                        status=status,
                        created_at=_parse_datetime(_get_value(item, "created_at", _get_value(item, "submitted_at", None))),
                        filled_at=_parse_datetime(raw_filled_at),
                    ))
                return orders
        return self._call_with_retry(
            _fetch,
            op="get_today_orders",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        return self._call_with_retry(
            lambda: self._cancel_order_inner(order_id),
            op="cancel_order",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def _cancel_order_inner(self, order_id: str) -> OrderStatusResult:
        with self._lock:
            self._init_clients()
            cancel = getattr(self._trade_ctx, "cancel_order", None)
            if cancel is None:
                cancel = getattr(self._trade_ctx, "withdraw_order", None)
            if cancel is None:
                raise RuntimeError("broker does not support order cancellation")
            response = cancel(order_id)
            detail = None
            order_detail = getattr(self._trade_ctx, "order_detail", None)
            if order_detail is not None:
                try:
                    detail = order_detail(order_id)
                except Exception as exc:
                    logger.warning(
                        "cancel request accepted but order %s status could not be confirmed: %s",
                        order_id,
                        exc,
                    )
            if detail is None:
                # Longport's cancel_order contract returns None after accepting
                # the request. Acceptance is not terminal proof: the order may
                # fill before cancellation reaches the venue.
                return OrderStatusResult(
                    broker_order_id=order_id,
                    status="SUBMITTED",
                    executed_quantity=_nonnegative_decimal_attr(
                        response,
                        "executed_quantity",
                        "filled_quantity",
                    ),
                    executed_price=_nonnegative_decimal_attr(
                        response,
                        "executed_price",
                        "filled_price",
                    ),
                )
            return OrderStatusResult(
                broker_order_id=_require_matching_order_id(detail, order_id),
                status=_normalize_order_status(
                    _get_value(detail, "status", "SUBMITTED")
                ),
                executed_quantity=_nonnegative_decimal_attr(
                    detail,
                    "executed_quantity",
                    "filled_quantity",
                ),
                executed_price=_nonnegative_decimal_attr(
                    detail,
                    "executed_price",
                    "filled_price",
                ),
            )

    def get_positions(self) -> list[Position]:
        def _fetch() -> list[Position]:
            with self._lock:
                self._init_clients()
                response = self._trade_ctx.stock_positions()
                if response is None:
                    raise RuntimeError("broker returned no position snapshot")
                positions: list[Position] = []
                for item in _iter_position_items(response):
                    symbol = str(_get_value(item, "symbol", "") or "").strip()
                    if not symbol:
                        raise ValueError(
                            "broker returned a position without symbol"
                        )
                    raw = _get_value(item, "quantity", None)
                    raw_available = _get_value(item, "available_quantity", None)
                    if raw is None:
                        if raw_available is None:
                            raise ValueError(
                                f"broker returned position {symbol} without quantity"
                            )
                        raw = raw_available
                    try:
                        qty = Decimal(str(raw))
                    except (ValueError, TypeError, _DecimalInvalidOp) as exc:
                        raise ValueError(
                            f"broker returned invalid quantity for position {symbol}"
                        ) from exc
                    if not qty.is_finite():
                        raise ValueError(
                            f"broker returned invalid quantity for position {symbol}"
                        )
                    raw_side_value = _get_value(item, "side", None)
                    explicit_side = raw_side_value is not None and bool(
                        str(getattr(raw_side_value, "value", raw_side_value)).strip()
                    )
                    if explicit_side:
                        side_text = str(
                            getattr(raw_side_value, "value", raw_side_value)
                        ).split(".")[-1].strip().upper()
                        if side_text not in {"LONG", "SHORT"}:
                            raise ValueError(
                                f"broker returned invalid side for position {symbol}"
                            )
                        if qty < 0:
                            raise ValueError(
                                "broker returned signed quantity together with explicit "
                                f"side for position {symbol}"
                            )
                        side = side_text
                    else:
                        side = "SHORT" if qty < 0 else "LONG"
                    available_qty = None
                    if raw_available is not None:
                        try:
                            parsed_available = Decimal(str(raw_available))
                        except (ValueError, TypeError, _DecimalInvalidOp) as exc:
                            raise ValueError(
                                "broker returned invalid available quantity for "
                                f"position {symbol}"
                            ) from exc
                        if not parsed_available.is_finite():
                            raise ValueError(
                                "broker returned invalid available quantity for "
                                f"position {symbol}"
                            )
                        if explicit_side and parsed_available < 0:
                            raise ValueError(
                                "broker returned signed available quantity together "
                                f"with explicit side for position {symbol}"
                            )
                        if not explicit_side and (
                            (qty > 0 and parsed_available < 0)
                            or (qty < 0 and parsed_available > 0)
                        ):
                            raise ValueError(
                                "broker position quantity and available quantity have "
                                f"conflicting signs for {symbol}"
                            )
                        available_qty = abs(parsed_available)
                    if qty == 0:
                        continue
                    if available_qty is not None and available_qty > abs(qty):
                        raise ValueError(
                            "broker available quantity exceeds total quantity for "
                            f"position {symbol}"
                        )

                    raw_avg = None
                    for key in ("cost_price", "avg_price", "average_price", "avg_cost_price"):
                        val = _get_value(item, key)
                        if val is not None:
                            raw_avg = val
                            break
                    if raw_avg is None:
                        avg_price = Decimal("0")
                    else:
                        try:
                            avg_price = Decimal(str(raw_avg))
                        except (ValueError, TypeError, _DecimalInvalidOp) as exc:
                            raise ValueError(
                                "broker returned invalid average price for "
                                f"position {symbol}"
                            ) from exc
                        if not avg_price.is_finite() or avg_price <= 0:
                            raise ValueError(
                                "broker returned invalid average price for "
                                f"position {symbol}"
                            )
                    if avg_price <= 0:
                        logger.warning(
                            "position %s side=%s has avg_price=%s (raw=%s), pnl calculation may be inaccurate",
                            _get_value(item, "symbol", ""),
                            side,
                            avg_price,
                            raw_avg,
                        )
                    positions.append(Position(
                        symbol=symbol,
                        side=side,
                        quantity=abs(qty),
                        avg_price=avg_price,
                        available_quantity=available_qty,
                    ))
                return positions
        return self._call_with_retry(
            _fetch,
            op="get_positions",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def close(self) -> None:
        with self._lock:
            self._quote_callbacks.clear()
            self._subscribed_symbols.clear()
            self._last_trade_by_symbol.clear()
            self._bbo_by_symbol.clear()
            for ctx in (self._quote_ctx, self._trade_ctx):
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception as exc:
                        logger.debug("broker context close error (ignored): %s", exc)
            self._quote_ctx = None
            self._trade_ctx = None

    def unsubscribe_quotes(self) -> None:
        with self._lock:
            symbols = sorted(self._subscribed_symbols)
            if symbols and self._quote_ctx is not None:
                try:
                    self._quote_ctx.unsubscribe(symbols)
                except Exception:
                    logger.warning("failed to unsubscribe from %s", ", ".join(symbols))
            self._quote_callbacks.clear()
            self._subscribed_symbols.clear()
            self._last_trade_by_symbol.clear()
            self._bbo_by_symbol.clear()

    def get_cash(self, currency: str | None = None) -> Decimal:
        """Return available cash for the given currency.

        When ``currency`` is *None* (the default), returns the first USD or
        HKD entry found in the broker response, which may be nondeterministic
        if the account holds both.  Pass ``currency`` explicitly to avoid
        ambiguity.
        """
        def _fetch() -> Decimal:
            with self._lock:
                self._init_clients()
                response = self._trade_ctx.account_balance()
                items = response if isinstance(response, list) else [response]
                target_currency = currency.upper() if currency else None
                for item in items:
                    cash_infos = getattr(item, "cash_infos", None)
                    if cash_infos:
                        for ci in cash_infos:
                            item_currency = str(getattr(ci, "currency", ""))
                            if target_currency and item_currency == target_currency:
                                return Decimal(str(getattr(ci, "available_cash", "0")))
                            if not target_currency and item_currency in ("USD", "HKD"):
                                return Decimal(str(getattr(ci, "available_cash", "0")))
                    item_currency = str(getattr(item, "currency", ""))
                    if target_currency and item_currency == target_currency:
                        return Decimal(str(getattr(item, "total_cash", "0")))
                    if not target_currency and item_currency in ("USD", "HKD"):
                        return Decimal(str(getattr(item, "total_cash", "0")))
                logger.warning("get_cash: no %s item found in account_balance response", target_currency or "USD/HKD")
                return Decimal("0")
        return self._call_with_retry(
            _fetch,
            op="get_cash",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency: str | None = None) -> Decimal:
        def _fetch() -> Decimal:
            with self._lock:
                self._init_clients()
                module = _import_openapi()
                OrderSide = getattr(module, "OrderSide", None)
                OrderType = getattr(module, "OrderType", None)

                side_name = _SIDE_MAP.get(side, side)
                side_enum = getattr(OrderSide, side_name, side) if OrderSide else side
                lo_type = getattr(OrderType, "LO", "LO") if OrderType else "LO"

                response = self._trade_ctx.estimate_max_purchase_quantity(
                    symbol=symbol,
                    order_type=lo_type,
                    side=side_enum,
                    price=price,
                    currency=currency,
                    fractional_shares=False,
                )
                return _decimal_attr(response, "margin_max_qty")
        return self._call_with_retry(
            _fetch,
            op="estimate_margin_max_quantity",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )

    def get_account(self) -> AccountInfo:
        def _fetch() -> AccountInfo:
            with self._lock:
                self._init_clients()
                response = self._trade_ctx.account_balance()
                cash_balances: list[CashBalance] = []
                net_assets: list[NetAsset] = []
                total_assets = Decimal("0")
                items = response if isinstance(response, list) else [response]
                primary_currency = ""
                primary_total = Decimal("0")
                fallback_total = Decimal("0")

                for item in items:
                    currency = str(getattr(item, "currency", ""))
                    net_amount = Decimal(str(getattr(item, "net_assets", "0")))
                    net_assets.append(NetAsset(
                        currency=currency,
                        amount=net_amount,
                    ))
                    if currency in ("USD", "HKD") and not primary_currency:
                        primary_currency = currency
                        primary_total = net_amount
                    fallback_total += net_amount

                    cash_infos = getattr(item, "cash_infos", None)
                    if cash_infos:
                        for ci in cash_infos:
                            ci_currency = str(getattr(ci, "currency", ""))
                            ci_available = Decimal(str(getattr(ci, "available_cash", "0")))
                            ci_frozen = Decimal(str(getattr(ci, "frozen_cash", "0")))
                            cash_balances.append(CashBalance(
                                currency=ci_currency,
                                available_cash=ci_available,
                                frozen_cash=ci_frozen,
                            ))

                # total_assets uses the primary-currency (first USD/HKD) net
                # asset figure rather than a cross-currency sum, which would be
                # meaningless without FX conversion.  When no USD/HKD entry is
                # present, fall back to the single-currency total (or naive sum
                # if multiple non-primary currencies exist).
                if primary_currency:
                    total_assets = primary_total
                else:
                    total_assets = fallback_total

                return AccountInfo(
                    total_assets=total_assets,
                    cash_balances=cash_balances,
                    net_assets=net_assets,
                )
        return self._call_with_retry(
            _fetch,
            op="get_account",
            max_retries=settings.broker_retry_max,
            base_ms=settings.broker_retry_base_ms,
        )
