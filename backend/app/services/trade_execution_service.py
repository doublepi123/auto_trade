from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable, Protocol

from app.core.broker import OrderResult, Position, Quote
from app.core.risk import RiskController

logger = logging.getLogger("auto_trade.trade_execution_service")

SUPPORTED_ACTIONS = {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}


class BrokerLike(Protocol):
    def get_cash(self) -> Decimal: ...

    def get_positions(self) -> list[Position]: ...

    def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> OrderResult: ...


class NotifierLike(Protocol):
    def notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> bool: ...

    def notify_risk_event(self, event_type: str, reason: str) -> bool: ...


class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float], None],
        record_risk_event: Callable[[str], None],
    ) -> None:
        self._record_order = record_order
        self._record_risk_event = record_risk_event

    def execute(
        self,
        action: str,
        symbol: str,
        quote: Quote,
        broker: BrokerLike,
        risk: RiskController,
        notifier: NotifierLike,
    ) -> bool:
        if action not in SUPPORTED_ACTIONS:
            logger.warning("unknown trade action: %s", action)
            return False

        risk_result = risk.check()
        if not risk_result.approved:
            logger.warning("risk rejected: %s", risk_result.reason)
            self._record_risk_event(risk_result.reason)
            notifier.notify_risk_event("REJECTED", risk_result.reason)
            return False

        try:
            if action == "BUY":
                return self._execute_buy(symbol, quote, broker, notifier)
            if action == "SELL":
                return self._execute_sell(symbol, quote, broker, risk, notifier)
            if action == "SELL_SHORT":
                return self._execute_sell_short(symbol, quote, broker, notifier)
            if action == "BUY_TO_COVER":
                return self._execute_buy_to_cover(symbol, quote, broker, risk, notifier)
        except Exception as exc:
            logger.exception("order execution failed: %s", exc)
            self._record_risk_event(str(exc))
            notifier.notify_risk_event("ORDER_FAILED", str(exc))
            return False

    def _execute_buy(self, symbol: str, quote: Quote, broker: BrokerLike, notifier: NotifierLike) -> bool:
        cash = broker.get_cash()
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return False
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "BUY", Decimal(qty), price)
        self._safe_record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price))
        self._safe_notify_order(notifier, "BUY", symbol, str(qty), str(price), result.broker_order_id)
        logger.info("BUY: %s qty=%s price=%s", symbol, qty, price)
        return True

    def _execute_sell(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerLike,
        risk: RiskController,
        notifier: NotifierLike,
    ) -> bool:
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return False
        pnl = float((price - long_pos.avg_price) * long_pos.quantity)
        result = broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        self._safe_record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price))
        self._safe_notify_order(notifier, "SELL", symbol, str(long_pos.quantity), str(price), result.broker_order_id)
        risk.record_trade(pnl)
        logger.info("SELL: %s qty=%s price=%s pnl=%s", symbol, long_pos.quantity, price, pnl)
        return True

    def _execute_sell_short(self, symbol: str, quote: Quote, broker: BrokerLike, notifier: NotifierLike) -> bool:
        cash = broker.get_cash()
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return False
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "SELL", Decimal(qty), price)
        self._safe_record_order(result.broker_order_id, symbol, "SELL_SHORT", float(qty), float(price))
        self._safe_notify_order(notifier, "SELL_SHORT", symbol, str(qty), str(price), result.broker_order_id)
        logger.info("SELL_SHORT: %s qty=%s price=%s", symbol, qty, price)
        return True

    def _execute_buy_to_cover(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerLike,
        risk: RiskController,
        notifier: NotifierLike,
    ) -> bool:
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return False
        pnl = float((pos.avg_price - price) * pos.quantity)
        result = broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        self._safe_record_order(result.broker_order_id, symbol, "BUY_TO_COVER", float(pos.quantity), float(price))
        self._safe_notify_order(notifier, "BUY_TO_COVER", symbol, str(pos.quantity), str(price), result.broker_order_id)
        risk.record_trade(pnl)
        logger.info("BUY_TO_COVER: %s qty=%s price=%s pnl=%s", symbol, pos.quantity, price, pnl)
        return True

    def _safe_record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        try:
            self._record_order(order_id, symbol, side, qty, price)
        except Exception:
            logger.exception("failed to record order %s for %s (broker order is still live)", order_id, symbol)

    def _safe_notify_order(
        self,
        notifier: NotifierLike,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> None:
        try:
            notifier.notify_order(side, symbol, quantity, price, order_id)
        except Exception:
            logger.exception("failed to send order notification for %s %s", side, symbol)
