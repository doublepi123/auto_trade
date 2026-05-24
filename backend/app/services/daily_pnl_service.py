from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable


_ZERO = Decimal("0")
_FILLED_STATUS = "FILLED"

ToTradeDay = Callable[[datetime], date]


def _utc_date(instant: datetime) -> date:
    return instant.astimezone(timezone.utc).date()


@dataclass(frozen=True)
class RealizedTrade:
    broker_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    pnl: float
    filled_at: datetime


@dataclass(frozen=True)
class DailyPnlResult:
    trade_day: date
    realized_pnl: float
    consecutive_losses: int
    trades: list[RealizedTrade]


@dataclass(frozen=True)
class _Fill:
    id: int
    broker_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass
class _LedgerPosition:
    long_quantity: Decimal = _ZERO
    long_cost: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    short_proceeds: Decimal = _ZERO


class DailyPnlService:
    """Recompute realized daily P&L from recorded broker fills.

    The risk controller is an in-memory accumulator, while broker order sync
    can discover fills after the fact. Replaying the order ledger makes P&L
    idempotent across restarts and late status updates.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def calculate(
        self,
        *,
        trade_day: date | None = None,
        symbol: str | None = None,
        to_trade_day: ToTradeDay | None = None,
    ) -> DailyPnlResult:
        from app.models import OrderRecord

        resolve_day: ToTradeDay = to_trade_day or _utc_date
        target_day = trade_day or resolve_day(datetime.now(timezone.utc))
        query = self._db.query(OrderRecord)
        if symbol:
            query = query.filter(OrderRecord.symbol == symbol)

        latest_orders: dict[str, Any] = {}
        for order in query.all():
            key = order.broker_order_id or f"local:{order.id}"
            existing = latest_orders.get(key)
            if existing is None or order.id > existing.id:
                latest_orders[key] = order

        fills = [
            fill
            for order in latest_orders.values()
            if (fill := self._fill_from_order(order)) is not None and resolve_day(fill.filled_at) <= target_day
        ]
        fills.sort(key=lambda item: (item.filled_at, item.id))

        positions: dict[str, _LedgerPosition] = {}
        trades: list[RealizedTrade] = []
        realized_pnl = _ZERO
        consecutive_losses = 0

        for fill in fills:
            position = positions.setdefault(fill.symbol, _LedgerPosition())
            matched_quantity, pnl = self._apply_fill(position, fill)
            if matched_quantity <= 0 or resolve_day(fill.filled_at) != target_day:
                continue

            realized_pnl += pnl
            if pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            trades.append(RealizedTrade(
                broker_order_id=fill.broker_order_id,
                symbol=fill.symbol,
                side=fill.side,
                quantity=float(matched_quantity),
                price=float(fill.price),
                pnl=float(pnl),
                filled_at=fill.filled_at,
            ))

        return DailyPnlResult(
            trade_day=target_day,
            realized_pnl=float(realized_pnl),
            consecutive_losses=consecutive_losses,
            trades=trades,
        )

    def _fill_from_order(self, order: Any) -> _Fill | None:
        quantity = self._executed_quantity(order)
        price = self._executed_price(order)
        if quantity <= 0 or price <= 0:
            return None

        symbol = str(getattr(order, "symbol", "") or "").upper()
        side = str(getattr(order, "side", "") or "").upper()
        if not symbol or side not in {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}:
            return None

        filled_at = self._coerce_datetime(getattr(order, "filled_at", None) or getattr(order, "created_at", None))
        if filled_at is None:
            return None

        return _Fill(
            id=int(getattr(order, "id", 0) or 0),
            broker_order_id=str(getattr(order, "broker_order_id", "") or ""),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
        )

    @staticmethod
    def _executed_quantity(order: Any) -> Decimal:
        executed_quantity = DailyPnlService._decimal(getattr(order, "executed_quantity", None))
        if executed_quantity > 0:
            return executed_quantity
        status = str(getattr(order, "status", "") or "").upper()
        if status == _FILLED_STATUS:
            return DailyPnlService._decimal(getattr(order, "quantity", None))
        return _ZERO

    @staticmethod
    def _executed_price(order: Any) -> Decimal:
        executed_price = DailyPnlService._decimal(getattr(order, "executed_price", None))
        if executed_price > 0:
            return executed_price
        return DailyPnlService._decimal(getattr(order, "price", None))

    @staticmethod
    def _apply_fill(position: _LedgerPosition, fill: _Fill) -> tuple[Decimal, Decimal]:
        if fill.side == "BUY":
            remaining, matched_quantity, pnl = DailyPnlService._close_short(position, fill.quantity, fill.price)
            DailyPnlService._open_long(position, remaining, fill.price)
            return matched_quantity, pnl
        if fill.side == "BUY_TO_COVER":
            remaining, matched_quantity, pnl = DailyPnlService._close_short(position, fill.quantity, fill.price)
            DailyPnlService._open_long(position, remaining, fill.price)
            return matched_quantity, pnl
        if fill.side == "SELL":
            remaining, matched_quantity, pnl = DailyPnlService._close_long(position, fill.quantity, fill.price)
            DailyPnlService._open_short(position, remaining, fill.price)
            return matched_quantity, pnl
        if fill.side == "SELL_SHORT":
            DailyPnlService._open_short(position, fill.quantity, fill.price)
            return _ZERO, _ZERO
        return _ZERO, _ZERO

    @staticmethod
    def _open_long(position: _LedgerPosition, quantity: Decimal, price: Decimal) -> None:
        if quantity <= 0:
            return
        position.long_quantity += quantity
        position.long_cost += quantity * price

    @staticmethod
    def _open_short(position: _LedgerPosition, quantity: Decimal, price: Decimal) -> None:
        if quantity <= 0:
            return
        position.short_quantity += quantity
        position.short_proceeds += quantity * price

    @staticmethod
    def _close_long(position: _LedgerPosition, quantity: Decimal, price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        if quantity <= 0 or position.long_quantity <= 0:
            return quantity, _ZERO, _ZERO

        matched_quantity = min(quantity, position.long_quantity)
        average_cost = position.long_cost / position.long_quantity
        pnl = (price - average_cost) * matched_quantity
        position.long_quantity -= matched_quantity
        position.long_cost -= average_cost * matched_quantity
        if position.long_quantity <= 0:
            position.long_quantity = _ZERO
            position.long_cost = _ZERO
        return quantity - matched_quantity, matched_quantity, pnl

    @staticmethod
    def _close_short(position: _LedgerPosition, quantity: Decimal, price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        if quantity <= 0 or position.short_quantity <= 0:
            return quantity, _ZERO, _ZERO

        matched_quantity = min(quantity, position.short_quantity)
        average_short_price = position.short_proceeds / position.short_quantity
        pnl = (average_short_price - price) * matched_quantity
        position.short_quantity -= matched_quantity
        position.short_proceeds -= average_short_price * matched_quantity
        if position.short_quantity <= 0:
            position.short_quantity = _ZERO
            position.short_proceeds = _ZERO
        return quantity - matched_quantity, matched_quantity, pnl

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        if value is None:
            return _ZERO
        try:
            return Decimal(str(value))
        except Exception:
            return _ZERO

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
