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


@dataclass
class Position:
    symbol: str
    side: str
    quantity: Decimal
    avg_price: Decimal


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
                nested = item[key]
                if nested is item:
                    continue
                return _iter_position_items(nested)
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
            except Exception:
                return Decimal("0")
    return Decimal("0")


class BrokerGateway:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._quote_callbacks: list[Callable[[Quote], None]] = []
        self._subscribed_symbol: str | None = None

    def _init_clients(self) -> None:
        with self._lock:
            if self._quote_ctx is None:
                module = _import_openapi()
                config = module.Config.from_env()
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
            if self._subscribed_symbol and self._quote_ctx is not None:
                try:
                    self._quote_ctx.unsubscribe([self._subscribed_symbol])
                except Exception:
                    logger.warning("failed to unsubscribe from %s", self._subscribed_symbol)
                self._subscribed_symbol = None
                self._quote_callbacks = []
            self._init_clients()
            module = _import_openapi()
            SubType = getattr(module, "SubType", None)

            def _on_quote(_symbol: str, _event: Any) -> None:
                quote = Quote(
                    symbol=str(getattr(_event, "symbol", _symbol)),
                    last_price=float(getattr(_event, "last_done", 0)),
                    bid=float(getattr(_event, "bid", 0)),
                    ask=float(getattr(_event, "ask", 0)),
                    timestamp=str(getattr(_event, "timestamp", "")),
                )
                for cb in list(self._quote_callbacks):
                    try:
                        cb(quote)
                    except Exception:
                        logger.exception("quote callback failed for %s", _symbol)

            self._quote_ctx.set_on_quote(_on_quote)
            topics = [SubType.Quote] if SubType else []
            self._quote_ctx.subscribe([symbol], topics)
            self._quote_callbacks = [callback]
            self._subscribed_symbol = symbol

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
        with self._lock:
            self._init_clients()
            detail = self._trade_ctx.order_detail(order_id)
            return OrderStatusResult(
                broker_order_id=str(_get_value(detail, "order_id", order_id)),
                status=_normalize_order_status(_get_value(detail, "status", "SUBMITTED")),
                executed_quantity=_decimal_attr(detail, "executed_quantity", "filled_quantity", "quantity"),
                executed_price=_decimal_attr(detail, "executed_price", "filled_price", "price"),
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
                try:
                    qty = Decimal(str(raw)) if raw is not None else Decimal("0")
                except Exception:
                    qty = Decimal("0")
                if qty == 0:
                    continue

                raw_side = str(_get_value(item, "side", "")).upper()
                side = raw_side if raw_side in {"LONG", "SHORT"} else ("SHORT" if qty < 0 else "LONG")
                raw_avg = _get_value(item, "cost_price", _get_value(item, "avg_price", "0"))
                try:
                    avg_price = Decimal(str(raw_avg))
                except Exception:
                    avg_price = Decimal("0")
                positions.append(Position(
                    symbol=str(_get_value(item, "symbol", "")),
                    side=side,
                    quantity=abs(qty),
                    avg_price=avg_price,
                ))
            return positions

    def close(self) -> None:
        with self._lock:
            self._quote_callbacks.clear()
            self._subscribed_symbol = None
            for ctx in (self._quote_ctx, self._trade_ctx):
                if ctx is not None:
                    try:
                        ctx.close()
                    except (AttributeError, TypeError):
                        pass
            self._quote_ctx = None
            self._trade_ctx = None

    def unsubscribe_quotes(self) -> None:
        with self._lock:
            if self._subscribed_symbol and self._quote_ctx is not None:
                try:
                    self._quote_ctx.unsubscribe([self._subscribed_symbol])
                except Exception:
                    logger.warning("failed to unsubscribe from %s", self._subscribed_symbol)
            self._quote_callbacks.clear()
            self._subscribed_symbol = None

    def get_cash(self, currency: str | None = None) -> Decimal:
        with self._lock:
            self._init_clients()
            try:
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
            except Exception:
                logger.exception("failed to get account balance")
                raise

    def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency: str | None = None) -> Decimal:
        with self._lock:
            self._init_clients()
            module = _import_openapi()
            OrderSide = getattr(module, "OrderSide", None)
            OrderType = getattr(module, "OrderType", None)

            side_name = _SIDE_MAP.get(side, side)
            side_enum = getattr(OrderSide, side_name, side) if OrderSide else side
            lo_type = getattr(OrderType, "LO") if OrderType else "LO"

            response = self._trade_ctx.estimate_max_purchase_quantity(
                symbol=symbol,
                order_type=lo_type,
                side=side_enum,
                price=price,
                currency=currency,
                fractional_shares=False,
            )
            return _decimal_attr(response, "margin_max_qty")

    def get_account(self) -> AccountInfo:
        with self._lock:
            self._init_clients()
            try:
                response = self._trade_ctx.account_balance()
                cash_balances: list[CashBalance] = []
                net_assets: list[NetAsset] = []
                total_assets = Decimal("0")
                items = response if isinstance(response, list) else [response]
                primary_currency = ""
                primary_total = Decimal("0")

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
                    total_assets += net_amount

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

                if primary_currency:
                    total_assets = primary_total

                return AccountInfo(
                    total_assets=total_assets,
                    cash_balances=cash_balances,
                    net_assets=net_assets,
                )
            except Exception:
                logger.exception("failed to get account balance")
                raise
