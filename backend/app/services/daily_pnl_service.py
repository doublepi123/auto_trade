from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Callable


logger = logging.getLogger(__name__)


_ZERO = Decimal("0")
_FILLED_STATUS = "FILLED"
_PARTIAL_FILLED_STATUS = "PARTIAL_FILLED"

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
class ClosedRoundTrip:
    """A paired entry<->exit round trip (one per closing fill).

    Aggregate view of the FIFO entry lots a single closing fill consumed:
    ``entry_price`` is the quantity-weighted average of the matched entry lots
    and ``entry_at`` is the earliest matched lot's fill time. ``est_fees`` uses
    the *currently configured* fee schedule (a close approximation for
    historical trades — the only rate we persist), so ``net_pnl`` reflects
    take-home while ``gross_pnl`` stays comparable to the risk controller.
    """

    symbol: str
    side: str  # "long" | "short"
    entry_order_id: int
    exit_order_id: int
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    est_fees: float
    net_pnl: float
    holding_seconds: float
    exit_broker_order_id: str = ""
    fee_source: str = "ESTIMATED"
    actual_fees: float | None = None
    slippage_amount: float | None = None
    slippage_bps: float | None = None
    ack_latency_ms: float | None = None
    fill_latency_ms: float | None = None
    exit_cause: str = ""
    exit_reason: str = ""
    mfe_amount: float | None = None
    mae_amount: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None


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
    fee: Decimal | None = None
    fee_source: str = "UNKNOWN"
    actual_fee: Decimal | None = None
    slippage_amount: float | None = None
    slippage_bps: float | None = None
    ack_latency_ms: float | None = None
    fill_latency_ms: float | None = None
    exit_cause: str = ""
    exit_reason: str = ""


@dataclass
class _LedgerPosition:
    long_quantity: Decimal = _ZERO
    long_cost: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    short_proceeds: Decimal = _ZERO


@dataclass
class _Lot:
    """A single entry lot in a FIFO queue (mutable: quantity decremented as it
    is consumed by closing fills)."""

    order_id: int
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    fee_remaining: Decimal | None = None
    fee_source: str = "UNKNOWN"


class DailyPnlService:
    """Recompute realized daily P&L from recorded broker fills.

    The risk controller is an in-memory accumulator, while broker order sync
    can discover fills after the fact. Replaying the order ledger makes P&L
    idempotent across restarts and late status updates.
    """

    _missing_executed_price_warned_keys: set[str] = set()
    _missing_executed_price_warn_lock = Lock()
    _unclosed_remainder_warned_keys: set[str] = set()
    _unclosed_remainder_warn_lock = Lock()
    _round_trip_overclose_warned_keys: set[str] = set()
    _round_trip_overclose_warn_lock = Lock()

    def __init__(self, db: Any) -> None:
        self._db = db

    @staticmethod
    def reconcile_risk_state(
        current_pnl: float,
        current_consecutive_losses: int,
        current_trade_day: date | None,
        result: DailyPnlResult,
    ) -> tuple[float, int]:
        """Apply a ledger replay without making same-day risk more optimistic.

        Historical inventory drift can make a valid closing fill match stale
        entry lots and overstate profit. Live fill accounting has the exact
        tracked entry cost, so a same-day replay may replace it only when the
        replay is equally or more conservative. A newly discovered loss is
        still accepted immediately.
        """
        replay_pnl = result.realized_pnl
        replay_losses = result.consecutive_losses
        if current_trade_day != result.trade_day:
            return replay_pnl, replay_losses
        if replay_pnl > current_pnl + 1e-9:
            return current_pnl, max(current_consecutive_losses, replay_losses)
        if not result.trades and replay_losses < current_consecutive_losses:
            replay_losses = current_consecutive_losses
        return replay_pnl, replay_losses

    def calculate(
        self,
        *,
        trade_day: date | None = None,
        symbol: str | None = None,
        to_trade_day: ToTradeDay | None = None,
        fee_rate_us: float = 0.0005,
        fee_rate_hk: float = 0.003,
    ) -> DailyPnlResult:
        resolve_day: ToTradeDay = to_trade_day or _utc_date
        target_day = trade_day or resolve_day(datetime.now(timezone.utc))
        end_of_day = datetime(target_day.year, target_day.month, target_day.day, tzinfo=timezone.utc) + timedelta(days=1)
        # The 2-day window (end_of_day + 1 day) accounts for timezone boundary
        # handling: fills near midnight in the target timezone may have UTC
        # timestamps that fall on the next calendar day.
        query_end = end_of_day + timedelta(days=1)
        round_trips = self.pair_round_trips(
            symbol=symbol,
            to_dt=query_end,
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
            include_excursions=False,
        )
        trades: list[RealizedTrade] = []
        realized_pnl = _ZERO
        consecutive_losses = 0

        for trip in round_trips:
            if resolve_day(trip.exit_at) != target_day:
                continue
            pnl = Decimal(str(trip.net_pnl))
            realized_pnl += pnl
            if pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            trades.append(RealizedTrade(
                broker_order_id=trip.exit_broker_order_id or str(trip.exit_order_id),
                symbol=trip.symbol,
                side="SELL" if trip.side == "long" else "BUY_TO_COVER",
                quantity=trip.quantity,
                price=trip.exit_price,
                pnl=float(pnl),
                filled_at=trip.exit_at,
            ))

        return DailyPnlResult(
            trade_day=target_day,
            realized_pnl=float(realized_pnl),
            consecutive_losses=consecutive_losses,
            trades=trades,
        )

    def pair_round_trips(
        self,
        *,
        symbol: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        fee_rate_us: float = 0.0005,
        fee_rate_hk: float = 0.003,
        include_excursions: bool = True,
    ) -> list[ClosedRoundTrip]:
        """Pair recorded fills into closed entry<->exit round trips.

        Read-only FIFO lot ledger that generalizes ``calculate`` across all days
        and symbols. Emits one ``ClosedRoundTrip`` per closing fill (``SELL``
        closes a long lot queue; ``BUY_TO_COVER`` closes a short lot queue),
        carrying the matched entry lots' weighted-average price, earliest entry
        time, and an estimated round-trip fee from the current fee schedule.

        Date filtering is on the *exit* fill time: a round trip that closed
        inside ``[from_dt, to_dt]`` is included even when its entry pre-dates the
        window. This method writes nothing and never calls ``calculate`` /
        ``_apply_fill``, so the risk controller's source of truth is untouched.
        """
        from app.models import OrderRecord

        query = self._db.query(OrderRecord)
        if symbol:
            query = query.filter(OrderRecord.symbol == symbol.strip().upper())
        if to_dt is not None:
            # Upper-bound the fills we load: an exit after to_dt cannot be in the
            # window. Entries before from_dt are still needed (no lower bound) so
            # window-closing round trips stay fully paired.
            query = query.filter(
                (
                    (OrderRecord.filled_at.isnot(None))
                    & (OrderRecord.filled_at <= to_dt)
                )
                | (
                    (OrderRecord.filled_at.is_(None))
                    & (OrderRecord.created_at <= to_dt)
                )
            )

        latest_orders: dict[str, Any] = {}
        for order in query.all():
            key = order.broker_order_id or f"local:{order.id}"
            existing = latest_orders.get(key)
            if existing is None or order.id > existing.id:
                latest_orders[key] = order

        fills = [
            fill
            for order in latest_orders.values()
            if (fill := self._fill_from_order(order)) is not None
        ]
        fills.sort(key=lambda item: (item.filled_at, item.id))

        lots: dict[str, dict[str, list[_Lot]]] = {}
        trades: list[ClosedRoundTrip] = []
        for fill in fills:
            book = lots.setdefault(fill.symbol, {"long": [], "short": []})
            if fill.side == "BUY":
                book["long"].append(
                    _Lot(
                        fill.id,
                        fill.quantity,
                        fill.price,
                        fill.filled_at,
                        fill.fee,
                        fill.fee_source,
                    )
                )
            elif fill.side == "SELL_SHORT":
                book["short"].append(
                    _Lot(
                        fill.id,
                        fill.quantity,
                        fill.price,
                        fill.filled_at,
                        fill.fee,
                        fill.fee_source,
                    )
                )
            elif fill.side == "SELL":
                trades.extend(self._close_lots(book["long"], fill, "long", fee_rate_us, fee_rate_hk))
            elif fill.side == "BUY_TO_COVER":
                trades.extend(self._close_lots(book["short"], fill, "short", fee_rate_us, fee_rate_hk))

        filtered = [
            t for t in trades
            if (from_dt is None or t.exit_at >= from_dt)
            and (to_dt is None or t.exit_at <= to_dt)
        ]
        if not include_excursions:
            return filtered
        try:
            return self._attach_excursions(filtered)
        except (AttributeError, TypeError):
            # Lightweight read-model fakes and legacy integrations may expose
            # orders without the snapshot query surface. PnL remains usable;
            # only optional excursion enrichment is omitted.
            return filtered

    @staticmethod
    def _close_lots(
        lot_queue: list[_Lot],
        exit_fill: _Fill,
        side: str,
        fee_rate_us: float,
        fee_rate_hk: float,
    ) -> list[ClosedRoundTrip]:
        from app.core.fees import one_side_fee_rate

        remaining = exit_fill.quantity
        matched_quantity = _ZERO
        cost_basis = _ZERO
        allocated_entry_fees = _ZERO
        entry_fee_complete = True
        entry_fees_all_actual = True
        entry_order_id = 0
        first_entry_at: datetime | None = None
        while remaining > 0 and lot_queue:
            lot = lot_queue[0]
            if lot.quantity <= 0:
                lot_queue.pop(0)
                continue
            take = min(remaining, lot.quantity)
            quantity_before = lot.quantity
            matched_quantity += take
            cost_basis += take * lot.price
            if lot.fee_remaining is None:
                entry_fee_complete = False
                entry_fees_all_actual = False
            elif quantity_before > 0:
                if lot.fee_source != "ACTUAL":
                    entry_fees_all_actual = False
                allocated_fee = lot.fee_remaining * take / quantity_before
                allocated_entry_fees += allocated_fee
                lot.fee_remaining -= allocated_fee
            if entry_order_id == 0:
                entry_order_id = lot.order_id
            if first_entry_at is None or lot.filled_at < first_entry_at:
                first_entry_at = lot.filled_at
            lot.quantity -= take
            remaining -= take
            if lot.quantity <= 0:
                lot_queue.pop(0)

        if remaining > 0:
            # A close that exceeds the available entry lots (data inconsistency,
            # an unhandled split/dividend, or a short opened outside this ledger).
            # Mirrors the warning _close_long/_close_short emit in calculate().
            DailyPnlService._warn_round_trip_overclose_once(exit_fill, remaining)

        if matched_quantity <= 0 or first_entry_at is None:
            return []

        avg_entry = cost_basis / matched_quantity
        exit_price = exit_fill.price
        if side == "long":
            gross = (exit_price - avg_entry) * matched_quantity
        else:
            gross = (avg_entry - exit_price) * matched_quantity

        market = "HK" if exit_fill.symbol.endswith(".HK") else "US"
        one_side = one_side_fee_rate(
            market, Decimal(str(fee_rate_us)), Decimal(str(fee_rate_hk))
        )
        entry_fee = (
            allocated_entry_fees
            if entry_fee_complete
            else avg_entry * matched_quantity * one_side
        )
        exit_fee = (
            exit_fill.fee * matched_quantity / exit_fill.quantity
            if exit_fill.fee is not None and exit_fill.quantity > 0
            else exit_price * matched_quantity * one_side
        )
        fees = entry_fee + exit_fee
        fee_source = (
            "ACTUAL"
            if entry_fee_complete
            and entry_fees_all_actual
            and exit_fill.fee_source == "ACTUAL"
            else "MIXED"
            if (entry_fees_all_actual and entry_fee_complete)
            or exit_fill.fee_source == "ACTUAL"
            else "ESTIMATED"
        )
        holding_seconds = (exit_fill.filled_at - first_entry_at).total_seconds()
        return [ClosedRoundTrip(
            symbol=exit_fill.symbol,
            side=side,
            entry_order_id=entry_order_id,
            exit_order_id=exit_fill.id,
            entry_at=first_entry_at,
            exit_at=exit_fill.filled_at,
            entry_price=float(avg_entry),
            exit_price=float(exit_price),
            quantity=float(matched_quantity),
            gross_pnl=float(gross),
            est_fees=float(fees),
            net_pnl=float(gross - fees),
            holding_seconds=holding_seconds,
            exit_broker_order_id=exit_fill.broker_order_id,
            fee_source=fee_source,
            actual_fees=float(fees) if fee_source == "ACTUAL" else None,
            slippage_amount=exit_fill.slippage_amount,
            slippage_bps=exit_fill.slippage_bps,
            ack_latency_ms=exit_fill.ack_latency_ms,
            fill_latency_ms=exit_fill.fill_latency_ms,
            exit_cause=exit_fill.exit_cause,
            exit_reason=exit_fill.exit_reason,
        )]

    @staticmethod
    def _warn_round_trip_overclose_once(exit_fill: _Fill, remaining: Decimal) -> None:
        fill_key = exit_fill.broker_order_id or f"local:{exit_fill.id}"
        warning_key = f"{fill_key}:{exit_fill.symbol}:{exit_fill.side}:{remaining}"
        with DailyPnlService._round_trip_overclose_warn_lock:
            should_warn = warning_key not in DailyPnlService._round_trip_overclose_warned_keys
            if should_warn:
                DailyPnlService._round_trip_overclose_warned_keys.add(warning_key)
        if should_warn:
            logger.warning(
                "round-trip close of %s for %s exceeds matched entry lots; "
                "close quantity exceeds tracked position by %s — possible "
                "data inconsistency",
                exit_fill.quantity,
                exit_fill.symbol,
                remaining,
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

        status = str(getattr(order, "status", "") or "").upper()
        filled_at_raw = getattr(order, "filled_at", None)
        if status == _PARTIAL_FILLED_STATUS and not filled_at_raw:
            return None
        filled_at = self._coerce_datetime(filled_at_raw or getattr(order, "created_at", None))
        if filled_at is None:
            return None

        actual_fee_raw = getattr(order, "actual_fee", None)
        estimated_fee_raw = getattr(order, "estimated_fee", None)
        actual_fee = (
            self._decimal(actual_fee_raw) if actual_fee_raw is not None else None
        )
        estimated_fee = (
            self._decimal(estimated_fee_raw)
            if estimated_fee_raw is not None
            else None
        )
        fee = actual_fee if actual_fee is not None else estimated_fee
        return _Fill(
            id=int(getattr(order, "id", 0) or 0),
            broker_order_id=str(getattr(order, "broker_order_id", "") or ""),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
            fee=fee,
            fee_source=(
                "ACTUAL"
                if actual_fee is not None
                else "ESTIMATED"
                if estimated_fee is not None
                else "UNKNOWN"
            ),
            actual_fee=actual_fee,
            slippage_amount=getattr(order, "slippage_amount", None),
            slippage_bps=getattr(order, "slippage_bps", None),
            ack_latency_ms=getattr(order, "ack_latency_ms", None),
            fill_latency_ms=getattr(order, "fill_latency_ms", None),
            exit_cause=str(getattr(order, "exit_cause", "") or ""),
            exit_reason=str(getattr(order, "exit_reason", "") or ""),
        )

    def _attach_excursions(
        self,
        trades: list[ClosedRoundTrip],
    ) -> list[ClosedRoundTrip]:
        from app.models import RuntimeStateSnapshot

        if not trades:
            return []
        symbols = {trade.symbol for trade in trades}
        start = min(trade.entry_at for trade in trades)
        end = max(trade.exit_at for trade in trades)
        snapshots_by_symbol: dict[str, list[tuple[datetime, float]]] = {}
        for created_at, symbol, last_price in self._db.query(
            RuntimeStateSnapshot.created_at,
            RuntimeStateSnapshot.symbol,
            RuntimeStateSnapshot.last_price,
        ).filter(
            RuntimeStateSnapshot.symbol.in_(symbols),
            RuntimeStateSnapshot.created_at >= start,
            RuntimeStateSnapshot.created_at <= end,
            RuntimeStateSnapshot.last_price > 0,
        ).all():
            snapshots_by_symbol.setdefault(str(symbol), []).append(
                (self._coerce_datetime(created_at) or start, float(last_price))
            )

        enriched: list[ClosedRoundTrip] = []
        for trade in trades:
            prices = [
                price
                for captured_at, price in snapshots_by_symbol.get(trade.symbol, [])
                if trade.entry_at <= captured_at <= trade.exit_at
            ]
            prices.extend([trade.entry_price, trade.exit_price])
            if not prices or trade.entry_price <= 0:
                enriched.append(trade)
                continue
            if trade.side == "long":
                favorable_per_unit = max(prices) - trade.entry_price
                adverse_per_unit = min(prices) - trade.entry_price
            else:
                favorable_per_unit = trade.entry_price - min(prices)
                adverse_per_unit = trade.entry_price - max(prices)
            enriched.append(replace(
                trade,
                mfe_amount=favorable_per_unit * trade.quantity,
                mae_amount=adverse_per_unit * trade.quantity,
                mfe_pct=favorable_per_unit / trade.entry_price * 100,
                mae_pct=adverse_per_unit / trade.entry_price * 100,
            ))
        return enriched

    def refresh_execution_outcomes(self, *, symbol: str | None = None) -> int:
        """Persist closed-trade outcomes so the order ledger is self-contained."""
        from app.models import OrderRecord

        updated = 0
        for trade in self.pair_round_trips(symbol=symbol):
            order = self._db.query(OrderRecord).filter(
                OrderRecord.id == trade.exit_order_id
            ).first()
            if order is None:
                continue
            order.gross_pnl = trade.gross_pnl
            order.net_pnl = trade.net_pnl
            order.mfe_amount = trade.mfe_amount
            order.mae_amount = trade.mae_amount
            order.mfe_pct = trade.mfe_pct
            order.mae_pct = trade.mae_pct
            updated += 1
        if updated:
            self._db.commit()
        return updated

    @staticmethod
    def _executed_quantity(order: Any) -> Decimal:
        executed_quantity = DailyPnlService._decimal(getattr(order, "executed_quantity", None))
        if executed_quantity > 0:
            return executed_quantity
        status = str(getattr(order, "status", "") or "").upper()
        if status in {_FILLED_STATUS, _PARTIAL_FILLED_STATUS}:
            return DailyPnlService._decimal(getattr(order, "quantity", None))
        return _ZERO

    @staticmethod
    def _executed_price(order: Any) -> Decimal:
        """Return the executed price of an order.

        When the order has no executed_price, this method falls back to the
        limit price as a best-effort approximation.  This can produce
        inaccurate PnL when the actual fill deviates significantly from the
        limit — callers should treat such entries as estimates and reconcile
        against broker fill data as soon as it becomes available.
        """
        executed_price = DailyPnlService._decimal(getattr(order, "executed_price", None))
        if executed_price > 0:
            return executed_price
        price = DailyPnlService._decimal(getattr(order, "price", None))
        order_id = str(getattr(order, "id", "?") or "?")
        broker_order_id = str(getattr(order, "broker_order_id", "") or "")
        warning_key = broker_order_id or f"local:{order_id}"
        with DailyPnlService._missing_executed_price_warn_lock:
            should_warn = warning_key not in DailyPnlService._missing_executed_price_warned_keys
            if should_warn:
                DailyPnlService._missing_executed_price_warned_keys.add(warning_key)
        if should_warn:
            logger.warning(
                "order %s has no executed_price, falling back to limit price %s — PnL may be inaccurate until broker sync. Consider flagging this fill as estimated.",
                order_id, price,
            )
        return price

    @staticmethod
    def _apply_fill(position: _LedgerPosition, fill: _Fill) -> tuple[Decimal, Decimal]:
        if fill.side == "BUY":
            DailyPnlService._open_long(position, fill.quantity, fill.price)
            return _ZERO, _ZERO
        if fill.side == "BUY_TO_COVER":
            unclosed, matched_quantity, pnl = DailyPnlService._close_short(position, fill.quantity, fill.price)
            if unclosed > _ZERO:
                DailyPnlService._warn_unclosed_remainder_once(fill, unclosed)
            return matched_quantity, pnl
        if fill.side == "SELL":
            unclosed, matched_quantity, pnl = DailyPnlService._close_long(position, fill.quantity, fill.price)
            if unclosed > _ZERO:
                DailyPnlService._warn_unclosed_remainder_once(fill, unclosed)
            return matched_quantity, pnl
        if fill.side == "SELL_SHORT":
            DailyPnlService._open_short(position, fill.quantity, fill.price)
            return _ZERO, _ZERO
        return _ZERO, _ZERO

    @staticmethod
    def _warn_unclosed_remainder_once(fill: _Fill, unclosed: Decimal) -> None:
        fill_key = fill.broker_order_id or f"local:{fill.id}"
        warning_key = f"{fill_key}:{fill.symbol}:{fill.side}:{unclosed}"
        with DailyPnlService._unclosed_remainder_warn_lock:
            should_warn = warning_key not in DailyPnlService._unclosed_remainder_warned_keys
            if should_warn:
                DailyPnlService._unclosed_remainder_warned_keys.add(warning_key)
        if should_warn:
            logger.warning(
                "close quantity exceeds tracked position by %s for %s — possible data inconsistency or unhandled split/dividend",
                unclosed,
                fill.symbol,
            )

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
