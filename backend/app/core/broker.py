from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.config import settings


def _import_openapi() -> Any:
    for name in ("longport.openapi", "longbridge.openapi"):
        try:
            return __import__(name, fromlist=["Config"])
        except ModuleNotFoundError:
            continue
    raise RuntimeError("Longbridge SDK not installed. Install longport or longbridge.")


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


class BrokerGateway:
    def __init__(self) -> None:
        self._quote_ctx: Any = None
        self._trade_ctx: Any = None
        self._quote_callbacks: list[Callable[[Quote], None]] = []
        self._subscribed_symbol: str | None = None

    def _init_clients(self) -> None:
        if self._quote_ctx is None:
            module = _import_openapi()
            app_key = settings.longbridge_app_key or os.getenv("LONGPORT_APP_KEY", "")
            app_secret = settings.longbridge_app_secret or os.getenv("LONGPORT_APP_SECRET", "")
            access_token = settings.longbridge_access_token or os.getenv("LONGPORT_ACCESS_TOKEN", "")

            config = module.Config.from_apikey(app_key, app_secret, access_token)
            self._quote_ctx = module.QuoteContext(config)
            self._trade_ctx = module.TradeContext(config)

    def get_quote(self, symbol: str) -> Quote:
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
        if self._subscribed_symbol == symbol:
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
        self._init_clients()
        module = _import_openapi()
        OrderSide = getattr(module, "OrderSide", None)
        OrderType = getattr(module, "OrderType", None)
        TimeInForceType = getattr(module, "TimeInForceType", None)

        side_enum = getattr(OrderSide, side.capitalize()) if OrderSide else side
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
        self._init_clients()
        response = self._trade_ctx.stock_positions()
        items = response if isinstance(response, list) else getattr(response, "channels", [response])
        positions: list[Position] = []
        for item in items:
            raw = getattr(item, "available_quantity", None)
            if raw is None:
                raw = getattr(item, "quantity", 0)
            qty = Decimal(str(raw)) if raw is not None else Decimal("0")
            if qty > 0:
                # NOTE: hardcoded side="LONG" because the SDK response does not
                # expose a side field. Short positions will be incorrectly labeled.
                positions.append(Position(
                    symbol=str(getattr(item, "symbol", "")),
                    side="LONG",
                    quantity=qty,
                    avg_price=Decimal(str(getattr(item, "cost_price", "0"))),
                ))
        return positions

    def close(self) -> None:
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
            import logging
            logging.getLogger("auto_trade.broker").exception("failed to get account balance")
            raise
