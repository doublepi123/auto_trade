from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.config import settings

logger = logging.getLogger("auto_trade.broker")


def _import_openapi() -> Any:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["Config"])
        except ModuleNotFoundError:
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


@dataclass
class OrderResult:
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    status: str


@dataclass
class Position:
    symbol: str
    side: str
    quantity: Decimal
    avg_price: Decimal


def _get_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _iter_position_items(item: Any) -> list[Any]:
    if item is None:
        return []
    if isinstance(item, list):
        result: list[Any] = []
        for child in item:
            result.extend(_iter_position_items(child))
        return result
    if isinstance(item, dict):
        nested = item.get("data", item)
        if nested is not item:
            return _iter_position_items(nested)
        for key in ("list", "channels", "stock_info", "positions"):
            if key in item:
                return _iter_position_items(item[key])
        return [item] if item.get("symbol") else []

    children: list[Any] = []
    for attr in ("channels", "list", "stock_info", "positions"):
        nested = getattr(item, attr, None)
        if nested is not None:
            children.extend(_iter_position_items(nested))
    if children:
        return children
    return [item] if getattr(item, "symbol", "") else []


_SIDE_MAP = {"BUY": "Buy", "SELL": "Sell", "SELL_SHORT": "Sell", "BUY_TO_COVER": "Buy"}


class BrokerGateway:
    def __init__(self, credentials: BrokerCredentials | None = None) -> None:
        self._credentials = credentials or BrokerCredentials()
        self._lock = threading.RLock()
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._quote_callbacks: list[Callable[[Quote], None]] = []
        self._subscribed_symbol: str | None = None

    def _init_clients(self) -> None:
        with self._lock:
            if self._quote_ctx is None:
                module = _import_openapi()
                app_key = self._credentials.app_key or settings.longbridge_app_key
                app_secret = self._credentials.app_secret or settings.longbridge_app_secret
                access_token = self._credentials.access_token or settings.longbridge_access_token

                config = module.Config.from_apikey(app_key, app_secret, access_token)
                self._quote_ctx = module.QuoteContext(config)
                self._trade_ctx = module.TradeContext(config)

    def get_quote(self, symbol: str) -> Quote:
        with self._lock:
            self._init_clients()
            response = self._quote_ctx.quote([symbol])
            items = response if isinstance(response, list) else [response]
            if not items:
                raise ValueError(f"no quote data for {symbol}")
            item = items[0]
            return Quote(
                symbol=str(getattr(item, "symbol", symbol)),
                last_price=float(getattr(item, "last_done", 0)),
                bid=float(getattr(item, "bid", 0)),
                ask=float(getattr(item, "ask", 0)),
                timestamp=str(getattr(item, "timestamp", "")),
            )

    def subscribe_quotes(self, symbol: str, callback: Callable[[Quote], None]) -> None:
        with self._lock:
            if self._subscribed_symbol == symbol:
                if callback not in self._quote_callbacks:
                    self._quote_callbacks.append(callback)
                return
            self._init_clients()
            self._quote_callbacks.append(callback)
            self._subscribed_symbol = symbol
            module = _import_openapi()
            TopicType = getattr(module, "TopicType", None)

            def _on_quote(_symbol: str, _event: Any) -> None:
                quote = Quote(
                    symbol=str(getattr(_event, "symbol", _symbol)),
                    last_price=float(getattr(_event, "last_done", 0)),
                    bid=float(getattr(_event, "bid", 0)),
                    ask=float(getattr(_event, "ask", 0)),
                    timestamp=str(getattr(_event, "timestamp", "")),
                )
                for cb in self._quote_callbacks:
                    cb(quote)

            self._quote_ctx.set_on_quote(_on_quote)
            topics = [TopicType.Quote] if TopicType else []
            self._quote_ctx.subscribe([symbol], topics)

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult:
        with self._lock:
            self._init_clients()
            module = _import_openapi()
            OrderSide = getattr(module, "OrderSide", None)
            OrderType = getattr(module, "OrderType", None)
            TimeInForceType = getattr(module, "TimeInForceType", None)

            side_name = _SIDE_MAP.get(side, side)
            side_enum = getattr(OrderSide, side_name, side) if OrderSide else side
            lo_type = getattr(OrderType, "LO") if OrderType else "LO"
            day_tif = getattr(TimeInForceType, "Day") if TimeInForceType else "DAY"

            response = self._trade_ctx.submit_order(
                symbol=symbol,
                order_type=lo_type,
                side=side_enum,
                submitted_quantity=quantity,
                time_in_force=day_tif,
                submitted_price=price,
                remark="auto-trade",
            )

            order_id = str(getattr(response, "order_id", getattr(response, "broker_order_id", "")))
            return OrderResult(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                status="SUBMITTED",
            )

    def get_positions(self) -> list[Position]:
        with self._lock:
            self._init_clients()
            response = self._trade_ctx.stock_positions()
            positions: list[Position] = []
            for item in _iter_position_items(response):
                raw = _get_value(item, "quantity", None)
                if raw is None:
                    raw = _get_value(item, "available_quantity", 0)
                qty = Decimal(str(raw)) if raw is not None else Decimal("0")
                if qty == 0:
                    continue

                raw_side = str(_get_value(item, "side", "")).upper()
                side = raw_side if raw_side in {"LONG", "SHORT"} else ("SHORT" if qty < 0 else "LONG")
                positions.append(Position(
                    symbol=str(_get_value(item, "symbol", "")),
                    side=side,
                    quantity=abs(qty),
                    avg_price=Decimal(str(_get_value(item, "cost_price", _get_value(item, "avg_price", "0")))),
                ))
            return positions

    def close(self) -> None:
        with self._lock:
            self._quote_callbacks.clear()
            self._subscribed_symbol = None
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception:
                    pass
            self._quote_ctx = None
            self._trade_ctx = None

    def get_cash(self) -> Decimal:
        with self._lock:
            self._init_clients()
            try:
                response = self._trade_ctx.account_balance()
                if isinstance(response, list) and response:
                    for item in response:
                        currency = getattr(item, "currency", "")
                        if currency in ("USD", "HKD"):
                            return Decimal(str(getattr(item, "available_cash", getattr(item, "cash", "0"))))
                    return Decimal(str(getattr(response[0], "available_cash", "0")))
                return Decimal(str(getattr(response, "available_cash", getattr(response, "cash", "0"))))
            except Exception:
                logger.exception("failed to get account balance")
                raise
