from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock
from typing import Any, Callable


logger = logging.getLogger(__name__)


_ZERO = Decimal("0")
_COST_BASIS_ABS_TOLERANCE = Decimal("0.000001")
_COST_BASIS_REL_TOLERANCE = Decimal("0.000001")
_FILLED_STATUS = "FILLED"
_PARTIAL_FILLED_STATUS = "PARTIAL_FILLED"

ToTradeDay = Callable[[datetime], date]
ToSymbolTradeDay = Callable[[str, datetime], date]


def _utc_date(instant: datetime) -> date:
    return instant.astimezone(timezone.utc).date()


def _symbol_trade_day(symbol: str, instant: datetime) -> date:
    from app.core.market_calendar import trade_day_for

    market = "HK" if symbol.upper().endswith(".HK") else "US"
    return trade_day_for(market, instant)


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


class PnlReplayIssueCode(str, Enum):
    FULL_UNMATCHED_EXIT = "FULL_UNMATCHED_EXIT"
    PARTIAL_OVERCLOSE = "PARTIAL_OVERCLOSE"
    COST_BASIS_CONFLICT = "COST_BASIS_CONFLICT"


@dataclass(frozen=True)
class PnlReplayIssue:
    issue_code: PnlReplayIssueCode
    symbol: str
    side: str
    trade_day: date
    filled_at: datetime
    exit_order_id: int
    exit_broker_order_id: str
    filled_quantity: float
    matched_quantity: float
    unmatched_quantity: float


@dataclass(frozen=True)
class DailyPnlResult:
    trade_day: date
    realized_pnl: float
    consecutive_losses: int
    trades: list[RealizedTrade]
    is_complete: bool = True
    issues: list[PnlReplayIssue] = field(default_factory=list)


@dataclass(frozen=True)
class RoundTripReplayResult:
    trades: list[ClosedRoundTrip]
    issues: list[PnlReplayIssue]


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
    estimated_fee: Decimal | None = None
    reported_fee_source: str = "UNKNOWN"
    slippage_amount: float | None = None
    slippage_bps: float | None = None
    ack_latency_ms: float | None = None
    fill_latency_ms: float | None = None
    exit_cause: str = ""
    exit_reason: str = ""
    pnl_source: str = "UNKNOWN"
    cost_basis_price: Decimal | None = None
    cost_basis_quantity: Decimal | None = None
    cost_basis_opened_at: datetime | None = None
    position_quantity_before: Decimal | None = None
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    pnl_fee: Decimal | None = None
    pnl_fee_source: str = "UNKNOWN"
    pnl_fee_rate: Decimal | None = None


@dataclass
class _LedgerPosition:
    long_quantity: Decimal = _ZERO
    long_cost: Decimal = _ZERO
    long_fees: Decimal = _ZERO
    short_quantity: Decimal = _ZERO
    short_proceeds: Decimal = _ZERO
    short_fees: Decimal = _ZERO


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
        if not result.is_complete:
            logger.error(
                "refusing incomplete daily PnL replay for %s; preserving live risk state",
                result.trade_day,
            )
            return current_pnl, current_consecutive_losses
        if current_trade_day != result.trade_day:
            return replay_pnl, replay_losses
        if replay_pnl > current_pnl + 1e-9:
            return current_pnl, max(current_consecutive_losses, replay_losses)
        return replay_pnl, max(current_consecutive_losses, replay_losses)

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
        from app.models import OrderRecord

        query = self._db.query(OrderRecord)
        if symbol:
            query = query.filter(OrderRecord.symbol == symbol.strip().upper())
        query = query.filter(
            (
                (OrderRecord.filled_at.isnot(None))
                & (OrderRecord.filled_at < query_end)
            )
            | (
                (OrderRecord.filled_at.is_(None))
                & (OrderRecord.created_at < query_end)
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
            and resolve_day(fill.filled_at) <= target_day
        ]
        fills.sort(key=lambda item: (item.filled_at, item.id))

        positions: dict[str, _LedgerPosition] = {}
        trades: list[RealizedTrade] = []
        realized_pnl = _ZERO
        consecutive_losses = 0
        is_complete = True
        issues: list[PnlReplayIssue] = []

        for fill in fills:
            position = positions.setdefault(fill.symbol, _LedgerPosition())
            fill_trade_day = resolve_day(fill.filled_at)
            authoritative_outcome = self._authoritative_outcome(fill)
            authoritative = authoritative_outcome is not None
            replay_cost_basis = (
                self._fully_covered_replay_cost_basis(position, fill)
                if authoritative
                else None
            )
            cost_basis_conflict = (
                replay_cost_basis is not None
                and self._cost_basis_conflicts(fill, replay_cost_basis)
            )
            if cost_basis_conflict:
                if fill_trade_day == target_day:
                    is_complete = False
                    issues.append(self._replay_issue(
                        fill,
                        trade_day=fill_trade_day,
                        matched_quantity=fill.quantity,
                        issue_code=PnlReplayIssueCode.COST_BASIS_CONFLICT,
                    ))
                logger.error(
                    "daily PnL replay found conflicting tracked cost basis for %s "
                    "order %s on %s: declared=%s replayed=%s position=%s; refusing "
                    "suspect realized PnL for that trade day",
                    fill.symbol,
                    fill.broker_order_id or fill.id,
                    fill_trade_day,
                    fill.cost_basis_price,
                    replay_cost_basis,
                    fill.position_quantity_before,
                )
            elif authoritative:
                self._rebase_position_from_authoritative_exit(position, fill)
            matched_quantity, replay_pnl = self._apply_fill_net(
                position,
                fill,
                fee_rate_us=fee_rate_us,
                fee_rate_hk=fee_rate_hk,
            )
            if fill.side not in {"SELL", "BUY_TO_COVER"}:
                continue
            if fill_trade_day != target_day:
                continue
            if cost_basis_conflict:
                # _apply_fill_net has already consumed the closing fill from
                # the fully represented ledger position. Keep later replay
                # state accurate, but do not credit either disputed PnL value.
                continue
            if authoritative:
                matched_quantity = fill.cost_basis_quantity or fill.quantity
                assert authoritative_outcome is not None
                pnl = authoritative_outcome[1]
            else:
                pnl = replay_pnl
                if matched_quantity < fill.quantity:
                    is_complete = False
                    issues.append(self._replay_issue(
                        fill,
                        trade_day=fill_trade_day,
                        matched_quantity=matched_quantity,
                    ))
                    logger.error(
                        "daily PnL replay is incomplete for %s order %s: matched=%s filled=%s",
                        fill.symbol,
                        fill.broker_order_id or fill.id,
                        matched_quantity,
                        fill.quantity,
                    )
                    continue
            if matched_quantity <= 0 or pnl is None:
                is_complete = False
                continue
            realized_pnl += pnl
            if pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0
            trades.append(RealizedTrade(
                broker_order_id=fill.broker_order_id or str(fill.id),
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
            is_complete=is_complete,
            issues=issues,
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
        to_trade_day: ToSymbolTradeDay | None = None,
    ) -> list[ClosedRoundTrip]:
        """Return only fully reconciled round trips for backwards compatibility."""
        return self.pair_round_trips_with_issues(
            symbol=symbol,
            from_dt=from_dt,
            to_dt=to_dt,
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
            include_excursions=include_excursions,
            to_trade_day=to_trade_day,
        ).trades

    def pair_round_trips_with_issues(
        self,
        *,
        symbol: str | None = None,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        fee_rate_us: float = 0.0005,
        fee_rate_hk: float = 0.003,
        include_excursions: bool = True,
        to_trade_day: ToSymbolTradeDay | None = None,
    ) -> RoundTripReplayResult:
        """Pair recorded fills into closed entry<->exit round trips.

        Read-only FIFO lot ledger that generalizes ``calculate`` across all days
        and symbols. A closing fill is emitted only when the ledger can match its
        entire executed quantity. Unmatched and partially matched exits are
        returned as structured issues instead of partial performance records.

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

        resolve_day: ToSymbolTradeDay = to_trade_day or _symbol_trade_day
        lots: dict[str, dict[str, list[_Lot]]] = {}
        trades: list[ClosedRoundTrip] = []
        issues: list[PnlReplayIssue] = []
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
                closed_trades, issue = self._close_lots(
                    book["long"],
                    fill,
                    "long",
                    fee_rate_us,
                    fee_rate_hk,
                    resolve_day,
                )
                trades.extend(closed_trades)
                if issue is not None:
                    issues.append(issue)
            elif fill.side == "BUY_TO_COVER":
                closed_trades, issue = self._close_lots(
                    book["short"],
                    fill,
                    "short",
                    fee_rate_us,
                    fee_rate_hk,
                    resolve_day,
                )
                trades.extend(closed_trades)
                if issue is not None:
                    issues.append(issue)

        filtered = [
            t for t in trades
            if (from_dt is None or t.exit_at >= from_dt)
            and (to_dt is None or t.exit_at <= to_dt)
        ]
        filtered_issues = [
            issue for issue in issues
            if (from_dt is None or issue.filled_at >= from_dt)
            and (to_dt is None or issue.filled_at <= to_dt)
        ]
        if not include_excursions:
            return RoundTripReplayResult(filtered, filtered_issues)
        try:
            enriched = self._attach_excursions(filtered)
        except (AttributeError, TypeError):
            # Lightweight read-model fakes and legacy integrations may expose
            # orders without the snapshot query surface. PnL remains usable;
            # only optional excursion enrichment is omitted.
            enriched = filtered
        return RoundTripReplayResult(enriched, filtered_issues)

    @staticmethod
    def _close_lots(
        lot_queue: list[_Lot],
        exit_fill: _Fill,
        side: str,
        fee_rate_us: float,
        fee_rate_hk: float,
        to_trade_day: ToSymbolTradeDay,
    ) -> tuple[list[ClosedRoundTrip], PnlReplayIssue | None]:
        from app.core.fees import one_side_fee_rate

        authoritative_outcome = DailyPnlService._authoritative_outcome(exit_fill)
        authoritative = authoritative_outcome is not None
        replay_cost_basis = (
            DailyPnlService._fully_covered_lot_cost_basis(lot_queue, exit_fill)
            if authoritative
            else None
        )
        cost_basis_conflict = (
            replay_cost_basis is not None
            and DailyPnlService._cost_basis_conflicts(
                exit_fill,
                replay_cost_basis,
            )
        )
        if authoritative and not cost_basis_conflict:
            basis_price = exit_fill.cost_basis_price or _ZERO
            position_quantity = exit_fill.position_quantity_before or _ZERO
            opened_at = exit_fill.cost_basis_opened_at or exit_fill.filled_at
            fee_rate = exit_fill.pnl_fee_rate or _ZERO
            lot_queue[:] = [
                _Lot(
                    order_id=0,
                    quantity=position_quantity,
                    price=basis_price,
                    filled_at=opened_at,
                    fee_remaining=basis_price * position_quantity * fee_rate,
                    fee_source="ESTIMATED",
                )
            ]

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
            return [], DailyPnlService._replay_issue(
                exit_fill,
                trade_day=to_trade_day(exit_fill.symbol, exit_fill.filled_at),
                matched_quantity=matched_quantity,
            )

        if matched_quantity <= 0 or first_entry_at is None:
            return [], None

        if cost_basis_conflict:
            logger.error(
                "round-trip replay found conflicting tracked cost basis for %s "
                "order %s: declared=%s replayed=%s position=%s; refusing "
                "suspect closed trade",
                exit_fill.symbol,
                exit_fill.broker_order_id or exit_fill.id,
                exit_fill.cost_basis_price,
                replay_cost_basis,
                exit_fill.position_quantity_before,
            )
            return [], DailyPnlService._replay_issue(
                exit_fill,
                trade_day=to_trade_day(exit_fill.symbol, exit_fill.filled_at),
                matched_quantity=matched_quantity,
                issue_code=PnlReplayIssueCode.COST_BASIS_CONFLICT,
            )

        if authoritative:
            basis_price = exit_fill.cost_basis_price or _ZERO
            pnl_fee, pnl_fee_source = DailyPnlService._effective_authoritative_fee(
                exit_fill
            )
            assert authoritative_outcome is not None
            gross_pnl, net_pnl = authoritative_outcome
            holding_seconds = (exit_fill.filled_at - first_entry_at).total_seconds()
            return [ClosedRoundTrip(
                symbol=exit_fill.symbol,
                side=side,
                entry_order_id=entry_order_id,
                exit_order_id=exit_fill.id,
                entry_at=first_entry_at,
                exit_at=exit_fill.filled_at,
                entry_price=float(basis_price),
                exit_price=float(exit_fill.price),
                quantity=float(exit_fill.cost_basis_quantity or exit_fill.quantity),
                gross_pnl=float(gross_pnl),
                est_fees=float(pnl_fee),
                net_pnl=float(net_pnl),
                holding_seconds=holding_seconds,
                exit_broker_order_id=exit_fill.broker_order_id,
                fee_source=pnl_fee_source,
                actual_fees=(
                    float(pnl_fee)
                    if pnl_fee_source == "ACTUAL"
                    else None
                ),
                slippage_amount=exit_fill.slippage_amount,
                slippage_bps=exit_fill.slippage_bps,
                ack_latency_ms=exit_fill.ack_latency_ms,
                fill_latency_ms=exit_fill.fill_latency_ms,
                exit_cause=exit_fill.exit_cause,
                exit_reason=exit_fill.exit_reason,
            )], None

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
        )], None

    @staticmethod
    def _replay_issue(
        fill: _Fill,
        *,
        trade_day: date,
        matched_quantity: Decimal,
        issue_code: PnlReplayIssueCode | None = None,
    ) -> PnlReplayIssue:
        unmatched_quantity = max(_ZERO, fill.quantity - matched_quantity)
        resolved_code = issue_code or (
            PnlReplayIssueCode.FULL_UNMATCHED_EXIT
            if matched_quantity <= 0
            else PnlReplayIssueCode.PARTIAL_OVERCLOSE
        )
        return PnlReplayIssue(
            issue_code=resolved_code,
            symbol=fill.symbol,
            side=fill.side,
            trade_day=trade_day,
            filled_at=fill.filled_at,
            exit_order_id=fill.id,
            exit_broker_order_id=fill.broker_order_id,
            filled_quantity=float(fill.quantity),
            matched_quantity=float(matched_quantity),
            unmatched_quantity=float(unmatched_quantity),
        )

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
        if quantity <= 0:
            return None

        symbol = str(getattr(order, "symbol", "") or "").upper()
        side = str(getattr(order, "side", "") or "").upper()
        if not symbol or side not in {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}:
            return None
        price = self._executed_price(order)
        if price <= 0:
            return None

        status = str(getattr(order, "status", "") or "").upper()
        filled_at_raw = getattr(order, "filled_at", None)
        if status == _PARTIAL_FILLED_STATUS and not filled_at_raw:
            return None
        filled_at = self._coerce_datetime(filled_at_raw or getattr(order, "created_at", None))
        if filled_at is None:
            return None

        actual_fee = self._non_negative_optional_decimal(
            getattr(order, "actual_fee", None)
        )
        estimated_fee = self._non_negative_optional_decimal(
            getattr(order, "estimated_fee", None)
        )
        submitted_quantity = self._non_negative_optional_decimal(
            getattr(order, "quantity", None)
        )
        if (
            estimated_fee is not None
            and submitted_quantity is not None
            and submitted_quantity > quantity
        ):
            estimated_fee *= quantity / submitted_quantity
        reported_fee_source = str(
            getattr(order, "fee_source", "UNKNOWN") or "UNKNOWN"
        ).upper()
        fee, fee_source = self._select_fill_fee(
            actual_fee,
            estimated_fee,
            reported_fee_source,
        )
        return _Fill(
            id=int(getattr(order, "id", 0) or 0),
            broker_order_id=str(getattr(order, "broker_order_id", "") or ""),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
            fee=fee,
            fee_source=fee_source,
            actual_fee=actual_fee,
            estimated_fee=estimated_fee,
            reported_fee_source=reported_fee_source,
            slippage_amount=getattr(order, "slippage_amount", None),
            slippage_bps=getattr(order, "slippage_bps", None),
            ack_latency_ms=getattr(order, "ack_latency_ms", None),
            fill_latency_ms=getattr(order, "fill_latency_ms", None),
            exit_cause=str(getattr(order, "exit_cause", "") or ""),
            exit_reason=str(getattr(order, "exit_reason", "") or ""),
            pnl_source=str(getattr(order, "pnl_source", "UNKNOWN") or "UNKNOWN").upper(),
            cost_basis_price=self._optional_decimal(
                getattr(order, "cost_basis_price", None)
            ),
            cost_basis_quantity=self._optional_decimal(
                getattr(order, "cost_basis_quantity", None)
            ),
            cost_basis_opened_at=self._coerce_datetime(
                getattr(order, "cost_basis_opened_at", None)
            ),
            position_quantity_before=self._optional_decimal(
                getattr(order, "position_quantity_before", None)
            ),
            gross_pnl=self._optional_decimal(getattr(order, "gross_pnl", None)),
            net_pnl=self._optional_decimal(getattr(order, "net_pnl", None)),
            pnl_fee=self._optional_decimal(getattr(order, "pnl_fee", None)),
            pnl_fee_source=str(
                getattr(order, "pnl_fee_source", "UNKNOWN") or "UNKNOWN"
            ).upper(),
            pnl_fee_rate=self._optional_decimal(
                getattr(order, "pnl_fee_rate", None)
            ),
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
            executed_quantity = self._executed_quantity(order)
            if (
                executed_quantity <= 0
                or self._decimal(trade.quantity) != executed_quantity
            ):
                logger.error(
                    "refusing partial ledger replay outcome for order %s: "
                    "matched=%s executed=%s",
                    trade.exit_broker_order_id or trade.exit_order_id,
                    trade.quantity,
                    executed_quantity,
                )
                continue
            if str(getattr(order, "pnl_source", "") or "").upper() in {
                "TRACKED_ENTRY",
                "BROKER_POSITION",
            }:
                if self._repair_ambiguous_zero_actual_fee(order, trade):
                    updated += 1
                continue
            order.gross_pnl = trade.gross_pnl
            order.net_pnl = trade.net_pnl
            order.pnl_source = "LEDGER_REPLAY"
            order.cost_basis_price = trade.entry_price
            order.cost_basis_quantity = trade.quantity
            order.cost_basis_opened_at = trade.entry_at
            order.position_quantity_before = trade.quantity
            order.pnl_fee = trade.est_fees
            order.pnl_fee_source = trade.fee_source
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
        if status == _FILLED_STATUS:
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
    def _is_authoritative_exit(fill: _Fill) -> bool:
        return DailyPnlService._authoritative_outcome(fill) is not None

    @staticmethod
    def _authoritative_outcome(
        fill: _Fill,
    ) -> tuple[Decimal, Decimal] | None:
        structurally_valid = (
            fill.side in {"SELL", "BUY_TO_COVER"}
            and fill.pnl_source in {"TRACKED_ENTRY", "BROKER_POSITION"}
            and fill.cost_basis_price is not None
            and fill.cost_basis_price > 0
            and fill.cost_basis_quantity is not None
            and fill.cost_basis_quantity == fill.quantity
            and fill.position_quantity_before is not None
            and fill.position_quantity_before >= fill.cost_basis_quantity
            and fill.gross_pnl is not None
            and fill.net_pnl is not None
            and fill.pnl_fee is not None
            and fill.pnl_fee >= 0
        )
        if not structurally_valid:
            return None
        assert fill.cost_basis_price is not None
        assert fill.gross_pnl is not None
        assert fill.net_pnl is not None
        assert fill.pnl_fee is not None
        if fill.side == "SELL":
            expected_gross = (
                fill.price - fill.cost_basis_price
            ) * fill.quantity
        else:
            expected_gross = (
                fill.cost_basis_price - fill.price
            ) * fill.quantity
        persisted_expected_net = expected_gross - fill.pnl_fee
        if not (
            DailyPnlService._same_decimal_sign(fill.gross_pnl, expected_gross)
            and DailyPnlService._same_decimal_sign(
                fill.net_pnl,
                persisted_expected_net,
            )
            and DailyPnlService._decimal_values_close(fill.gross_pnl, expected_gross)
            and DailyPnlService._decimal_values_close(
                fill.net_pnl,
                persisted_expected_net,
            )
        ):
            return None
        effective_fee, _ = DailyPnlService._effective_authoritative_fee(fill)
        expected_net = expected_gross - effective_fee
        return expected_gross, expected_net

    @staticmethod
    def _effective_authoritative_fee(fill: _Fill) -> tuple[Decimal, str]:
        """Return a conservative total fee for a persisted authoritative exit.

        Current tracked-entry accounting stores the estimated entry fee in
        ``pnl_fee`` and marks the result ``MIXED`` when the broker supplies an
        exit fee. Paper accounts can report that exit fee as zero. In that
        precise case the persisted total is missing only the exit-side cost, so
        add the frozen order estimate (or its stored fee-rate fallback) once
        and downgrade the effective source to ``ESTIMATED``.
        """
        pnl_fee = fill.pnl_fee or _ZERO
        if (
            fill.pnl_fee_source == "MIXED"
            and fill.actual_fee == _ZERO
            and fill.reported_fee_source == "ACTUAL"
        ):
            exit_fee = (
                fill.estimated_fee
                if fill.estimated_fee is not None
                and fill.estimated_fee > _ZERO
                else fill.price * fill.quantity * (fill.pnl_fee_rate or _ZERO)
            )
            if exit_fee > _ZERO:
                return pnl_fee + exit_fee, "ESTIMATED"
        return pnl_fee, fill.pnl_fee_source

    @staticmethod
    def _repair_ambiguous_zero_actual_fee(
        order: Any,
        trade: ClosedRoundTrip,
    ) -> bool:
        """Persist the read-time fallback without charging it again later."""
        actual_fee = DailyPnlService._non_negative_optional_decimal(
            getattr(order, "actual_fee", None)
        )
        pnl_fee = DailyPnlService._non_negative_optional_decimal(
            getattr(order, "pnl_fee", None)
        )
        if not (
            actual_fee == _ZERO
            and pnl_fee is not None
            and str(getattr(order, "fee_source", "") or "").upper() == "ACTUAL"
            and str(getattr(order, "pnl_fee_source", "") or "").upper() == "MIXED"
            and trade.fee_source == "ESTIMATED"
            and Decimal(str(trade.est_fees)) > pnl_fee
        ):
            return False
        order.pnl_fee = trade.est_fees
        order.pnl_fee_source = "ESTIMATED"
        order.net_pnl = trade.net_pnl
        return True

    @staticmethod
    def _same_decimal_sign(left: Decimal, right: Decimal) -> bool:
        return (left > 0) == (right > 0) and (left < 0) == (right < 0)

    @staticmethod
    def _decimal_values_close(left: Decimal, right: Decimal) -> bool:
        tolerance = max(
            _COST_BASIS_ABS_TOLERANCE,
            max(abs(left), abs(right)) * _COST_BASIS_REL_TOLERANCE,
        )
        return abs(left - right) <= tolerance

    @staticmethod
    def _fully_covered_replay_cost_basis(
        position: _LedgerPosition,
        fill: _Fill,
    ) -> Decimal | None:
        """Return replayed average cost only when the whole position is known.

        Quantity equality is intentional. A smaller ledger position is only
        partially represented, while a larger one signals stale inventory or
        a position reset. In either case the tracked entry can legitimately
        have originated outside this order ledger and remains authoritative.
        """
        if fill.pnl_source != "TRACKED_ENTRY":
            return None
        position_quantity = fill.position_quantity_before
        if position_quantity is None or position_quantity <= 0:
            return None
        if fill.side == "SELL":
            if position.long_quantity != position_quantity:
                return None
            return position.long_cost / position.long_quantity
        if fill.side == "BUY_TO_COVER":
            if position.short_quantity != position_quantity:
                return None
            return position.short_proceeds / position.short_quantity
        return None

    @staticmethod
    def _fully_covered_lot_cost_basis(
        lot_queue: list[_Lot],
        fill: _Fill,
    ) -> Decimal | None:
        if fill.pnl_source != "TRACKED_ENTRY":
            return None
        position_quantity = fill.position_quantity_before
        if position_quantity is None or position_quantity <= 0:
            return None
        represented_lots = [lot for lot in lot_queue if lot.quantity > 0]
        represented_quantity = sum(
            (lot.quantity for lot in represented_lots),
            start=_ZERO,
        )
        if represented_quantity != position_quantity:
            return None
        represented_cost = sum(
            (lot.quantity * lot.price for lot in represented_lots),
            start=_ZERO,
        )
        return represented_cost / represented_quantity

    @staticmethod
    def _cost_basis_conflicts(fill: _Fill, replay_cost_basis: Decimal) -> bool:
        declared_cost_basis = fill.cost_basis_price
        if declared_cost_basis is None:
            return False
        tolerance = max(
            _COST_BASIS_ABS_TOLERANCE,
            max(abs(declared_cost_basis), abs(replay_cost_basis))
            * _COST_BASIS_REL_TOLERANCE,
        )
        return abs(declared_cost_basis - replay_cost_basis) > tolerance

    @staticmethod
    def _rebase_position_from_authoritative_exit(
        position: _LedgerPosition,
        fill: _Fill,
    ) -> None:
        basis_price = fill.cost_basis_price or _ZERO
        position_quantity = fill.position_quantity_before or _ZERO
        fee_rate = fill.pnl_fee_rate or _ZERO
        if fill.side == "SELL":
            position.long_quantity = position_quantity
            position.long_cost = basis_price * position_quantity
            position.long_fees = basis_price * position_quantity * fee_rate
        else:
            position.short_quantity = position_quantity
            position.short_proceeds = basis_price * position_quantity
            position.short_fees = basis_price * position_quantity * fee_rate

    @staticmethod
    def _apply_fill_net(
        position: _LedgerPosition,
        fill: _Fill,
        *,
        fee_rate_us: float,
        fee_rate_hk: float,
    ) -> tuple[Decimal, Decimal]:
        from app.core.fees import one_side_fee_rate

        market = "HK" if fill.symbol.endswith(".HK") else "US"
        fee_rate = one_side_fee_rate(
            market,
            Decimal(str(fee_rate_us)),
            Decimal(str(fee_rate_hk)),
        )

        def fill_fee(quantity: Decimal) -> Decimal:
            if quantity <= 0:
                return _ZERO
            if fill.fee is not None and fill.quantity > 0:
                return fill.fee * quantity / fill.quantity
            return fill.price * quantity * fee_rate

        if fill.side == "BUY":
            DailyPnlService._open_long(position, fill.quantity, fill.price)
            position.long_fees += fill_fee(fill.quantity)
            return _ZERO, _ZERO
        if fill.side == "SELL_SHORT":
            DailyPnlService._open_short(position, fill.quantity, fill.price)
            position.short_fees += fill_fee(fill.quantity)
            return _ZERO, _ZERO
        if fill.side == "SELL":
            quantity_before = position.long_quantity
            fees_before = position.long_fees
            unclosed, matched_quantity, gross_pnl = DailyPnlService._close_long(
                position,
                fill.quantity,
                fill.price,
            )
            if unclosed > _ZERO:
                DailyPnlService._warn_unclosed_remainder_once(fill, unclosed)
            entry_fee = (
                fees_before * matched_quantity / quantity_before
                if quantity_before > 0
                else _ZERO
            )
            position.long_fees = max(_ZERO, fees_before - entry_fee)
            if position.long_quantity <= 0:
                position.long_fees = _ZERO
            return matched_quantity, gross_pnl - entry_fee - fill_fee(matched_quantity)
        if fill.side == "BUY_TO_COVER":
            quantity_before = position.short_quantity
            fees_before = position.short_fees
            unclosed, matched_quantity, gross_pnl = DailyPnlService._close_short(
                position,
                fill.quantity,
                fill.price,
            )
            if unclosed > _ZERO:
                DailyPnlService._warn_unclosed_remainder_once(fill, unclosed)
            entry_fee = (
                fees_before * matched_quantity / quantity_before
                if quantity_before > 0
                else _ZERO
            )
            position.short_fees = max(_ZERO, fees_before - entry_fee)
            if position.short_quantity <= 0:
                position.short_fees = _ZERO
            return matched_quantity, gross_pnl - entry_fee - fill_fee(matched_quantity)
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
    def _optional_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            candidate = Decimal(str(value))
        except Exception:
            return None
        return candidate if candidate.is_finite() else None

    @staticmethod
    def _non_negative_optional_decimal(value: Any) -> Decimal | None:
        candidate = DailyPnlService._optional_decimal(value)
        if candidate is None or candidate < _ZERO:
            return None
        return candidate

    @staticmethod
    def _select_fill_fee(
        actual_fee: Decimal | None,
        estimated_fee: Decimal | None,
        reported_fee_source: str,
    ) -> tuple[Decimal | None, str]:
        """Choose one persisted fee without letting an ambiguous zero hide cost."""
        if actual_fee is not None and actual_fee > _ZERO:
            return actual_fee, "ACTUAL"
        if estimated_fee is not None and estimated_fee > _ZERO:
            return estimated_fee, "ESTIMATED"
        if actual_fee == _ZERO and reported_fee_source == "ACTUAL":
            # Broker-synced paper fills commonly expose a placeholder zero.
            # Returning no persisted fee lets the caller use its configured
            # market fee schedule instead of silently treating that zero as
            # free execution.
            return None, "UNKNOWN"
        if actual_fee is not None:
            return actual_fee, "ACTUAL"
        if estimated_fee is not None:
            return estimated_fee, "ESTIMATED"
        return None, "UNKNOWN"

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
