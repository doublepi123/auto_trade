from __future__ import annotations

"""Execution quality analytics over the trade-event log.

Reads ``TradeEvent`` rows for the ORDER_* lifecycle (submit / fill / reject /
cancel) and derives:

* fill rate, rejection rate, cancellation rate
* average submit-to-fill latency (seconds)
* rejection-reason histogram
* per-symbol order mix

plus a separate slippage analysis that pairs the *decision* (signal) price
recorded on an order with the price it actually filled at. All read-only.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord, TradeEvent
from app.services.trade_event_service import decode_event_payload

logger = logging.getLogger("auto_trade.execution_quality")

# Canonical event types produced by the trade-execution service. We accept the
# trailing-ED spellings (ORDER_REJECTED / ORDER_CANCELLED) as well as the bare
# forms (ORDER_REJECT / ORDER_CANCEL) since both appear in the log.
_SUBMIT_EVENTS = frozenset({"ORDER_SUBMIT", "ORDER_SUBMITTED"})
_FILL_EVENTS = frozenset({"ORDER_FILL", "ORDER_FILLED"})
_REJECT_EVENTS = frozenset({"ORDER_REJECT", "ORDER_REJECTED"})
_CANCEL_EVENTS = frozenset({"ORDER_CANCEL", "ORDER_CANCELLED", "ORDER_CANCEL_ALL"})

# Filled status strings observed on OrderRecord.status / TradeEvent.status.
_FILLED_STATUSES = frozenset({"FILLED", "PARTIAL_FILLED", "FILLED_PARTIAL"})


class ExecutionQualityService:
    """Compute order-fill / rejection / slippage statistics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Order lifecycle summary
    # ------------------------------------------------------------------
    def get_quality_summary(self, days: int = 30) -> dict[str, Any]:
        """Aggregate fill-rate / rejection-rate / fill-time statistics."""
        days = max(1, int(days))
        from_dt = datetime.now(timezone.utc) - timedelta(days=days)
        events = self._load_order_events(from_dt=from_dt)

        if not events:
            return self._empty_summary(days)

        total = len(events)
        filled = rejected = cancelled = 0
        fill_times_seconds: list[float] = []
        rejection_reasons: dict[str, int] = {}
        by_symbol: dict[str, dict[str, int]] = {}
        # Submit events keyed by broker_order_id (or symbol+day) so we can pair
        # the matching fill and compute submit->fill latency.
        submits: dict[str, datetime] = {}

        for event in events:
            etype = (event.event_type or "").upper()
            symbol = (event.symbol or "").strip().upper() or "<unknown>"
            bucket = by_symbol.setdefault(
                symbol, {"orders": 0, "fills": 0, "rejects": 0}
            )
            bucket["orders"] += 1

            payload = decode_event_payload(event.payload_json)
            order_key = self._order_key(event)

            if etype in _SUBMIT_EVENTS:
                ts = self._event_timestamp(event, payload, prefer="submit")
                if order_key is not None and ts is not None:
                    submits[order_key] = ts

            elif etype in _FILL_EVENTS:
                filled += 1
                bucket["fills"] += 1
                fill_ts = self._event_timestamp(event, payload, prefer="fill")
                if order_key is not None and fill_ts is not None and order_key in submits:
                    delta = (fill_ts - submits[order_key]).total_seconds()
                    if delta >= 0:
                        fill_times_seconds.append(delta)

            elif etype in _REJECT_EVENTS:
                rejected += 1
                bucket["rejects"] += 1
                reason = self._extract_rejection_reason(event, payload)
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

            elif etype in _CANCEL_EVENTS:
                cancelled += 1

        fill_rate_pct = round(filled / total * 100.0, 2) if total else 0.0
        rejection_rate_pct = round(rejected / total * 100.0, 2) if total else 0.0
        avg_fill_time = (
            round(sum(fill_times_seconds) / len(fill_times_seconds), 3)
            if fill_times_seconds
            else 0.0
        )

        return {
            "period_days": days,
            "total_orders": total,
            "filled_orders": filled,
            "rejected_orders": rejected,
            "cancelled_orders": cancelled,
            "fill_rate_pct": fill_rate_pct,
            "avg_fill_time_seconds": avg_fill_time,
            "rejection_rate_pct": rejection_rate_pct,
            "rejection_reasons": rejection_reasons,
            "by_symbol": by_symbol,
        }

    # ------------------------------------------------------------------
    # Slippage analysis
    # ------------------------------------------------------------------
    def get_slippage_analysis(self, days: int = 30) -> list[dict[str, Any]]:
        """Per-symbol average / max slippage from decision price to fill price.

        Slippage is read from two sources, whichever is available:

        1. ``OrderRecord.slippage_bps`` (persisted by the fill pipeline) —
           authoritative when present.
        2. A signal price recorded on the order ledger row
           (``decision_bid`` / ``decision_ask`` mid) compared with
           ``executed_price``.
        3. The ``signal_price`` / ``fill_price`` pair embedded in a matching
           fill ``TradeEvent`` payload.

        Positive slippage is favourable (filled better than the signal).
        """
        days = max(1, int(days))
        from_dt = datetime.now(timezone.utc) - timedelta(days=days)

        # Prefer the order ledger — it carries the decision-price snapshot.
        orders = self._load_filled_orders(from_dt=from_dt)
        per_symbol: dict[str, list[float]] = {}

        for order in orders:
            symbol = (order.symbol or "").strip().upper()
            if not symbol:
                continue
            slippage_pct = self._order_slippage_pct(order)
            if slippage_pct is None:
                continue
            per_symbol.setdefault(symbol, []).append(slippage_pct)

        # Fall back / supplement with signal-vs-fill pairs embedded in fill
        # event payloads (covers orders whose ledger row lacks the snapshot).
        if not per_symbol:
            for event in self._load_order_events(from_dt=from_dt, types=_FILL_EVENTS):
                payload = decode_event_payload(event.payload_json)
                symbol = (event.symbol or "").strip().upper()
                signal = payload.get("signal_price") or payload.get("decision_price")
                fill = payload.get("fill_price") or payload.get("executed_price")
                if symbol and signal and fill:
                    try:
                        slip = (float(fill) - float(signal)) / float(signal)
                    except (TypeError, ValueError, ZeroDivisionError):
                        continue
                    per_symbol.setdefault(symbol, []).append(slip)

        rows: list[dict[str, Any]] = []
        for symbol, slips in sorted(per_symbol.items()):
            if not slips:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "avg_slippage_pct": round(sum(slips) / len(slips), 6),
                    "max_slippage_pct": round(max(slips), 6),
                    "trade_count": len(slips),
                    # Positive mean = filled better than signal on average
                    # (favourable). Negative = adverse.
                    "direction_bias": "favorable" if sum(slips) >= 0 else "adverse",
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    def _load_order_events(
        self,
        *,
        from_dt: datetime,
        types: frozenset[str] | None = None,
    ) -> list[TradeEvent]:
        relevant = types if types is not None else (
            _SUBMIT_EVENTS | _FILL_EVENTS | _REJECT_EVENTS | _CANCEL_EVENTS
        )
        stmt = (
            select(TradeEvent)
            .where(TradeEvent.created_at >= from_dt)
            .order_by(TradeEvent.created_at.asc())
        )
        events = list(self._db.execute(stmt).scalars().all())
        # SQLAlchemy has no portable "IN on a set of string literals" filter
        # that also stays case-insensitive across our supported event spellings;
        # filter in Python so we honour every variant uniformly.
        return [e for e in events if (e.event_type or "").upper() in relevant]

    def _load_filled_orders(self, *, from_dt: datetime) -> list[OrderRecord]:
        stmt = (
            select(OrderRecord)
            .where(OrderRecord.filled_at.is_not(None))
            .where(OrderRecord.filled_at >= from_dt)
            .order_by(OrderRecord.filled_at.asc())
        )
        return list(self._db.execute(stmt).scalars().all())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _order_key(event: TradeEvent) -> str | None:
        """Stable key to pair a submit with its fill (broker_order_id preferred)."""
        if event.broker_order_id:
            return f"oid:{event.broker_order_id}"
        if event.symbol:
            return f"sym:{event.symbol}"
        return None

    @staticmethod
    def _event_timestamp(
        event: TradeEvent,
        payload: dict[str, Any],
        *,
        prefer: str,
    ) -> datetime | None:
        """Best-effort timestamp for an event, preferring payload over row time.

        ``prefer`` selects which payload key to consult first ("submit" vs
        "fill"); we always fall back to ``event.created_at`` so a missing
        payload key does not silently zero out the latency.
        """
        keys = (
            ("submit_at", "submitted_at", "ts")
            if prefer == "submit"
            else ("fill_at", "filled_at", "ts")
        )
        for key in keys:
            value = payload.get(key)
            if isinstance(value, datetime):
                return value
            if isinstance(value, str):
                parsed = _parse_iso(value)
                if parsed is not None:
                    return parsed
        return event.created_at

    @staticmethod
    def _extract_rejection_reason(
        event: TradeEvent, payload: dict[str, Any]
    ) -> str:
        """Surface a stable rejection-reason bucket for the histogram."""
        for key in ("reason", "reject_reason", "rejection_reason", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if event.message and event.message.strip():
            return event.message.strip()
        if event.status and event.status.strip():
            return event.status.strip()
        return "UNKNOWN"

    @staticmethod
    def _order_slippage_pct(order: OrderRecord) -> float | None:
        """Slippage as a fraction of the decision (signal) price.

        Order of preference:
          1. Persisted ``slippage_bps`` (already in basis points) -> percent.
          2. decision mid vs executed_price.
          3. configured ``price`` vs executed_price (last-resort fallback).
        """
        # 1. Persisted basis-points figure (authoritative). Convert bps to a
        #    fraction of price: 1 bps = 0.01 % = 0.0001 fraction, so divide by
        #    10_000 to match the (executed - signal) / signal convention below.
        if order.slippage_bps is not None:
            return float(order.slippage_bps) / 10_000.0

        executed = order.executed_price
        if executed is None:
            return None
        executed = float(executed)

        # 2. Decision-time mid (bid/ask snapshot taken at signal time).
        bid = order.decision_bid
        ask = order.decision_ask
        if bid is not None and ask is not None:
            mid = (float(bid) + float(ask)) / 2.0
            if mid > 0:
                return (executed - mid) / mid

        # 3. Legacy fallback: submitted price vs executed price.
        if order.price is not None and float(order.price) > 0:
            return (executed - float(order.price)) / float(order.price)
        return None

    @staticmethod
    def _empty_summary(days: int) -> dict[str, Any]:
        return {
            "period_days": days,
            "total_orders": 0,
            "filled_orders": 0,
            "rejected_orders": 0,
            "cancelled_orders": 0,
            "fill_rate_pct": 0.0,
            "avg_fill_time_seconds": 0.0,
            "rejection_rate_pct": 0.0,
            "rejection_reasons": {},
            "by_symbol": {},
        }


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate a trailing Z and missing tz."""
    try:
        normalised = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
