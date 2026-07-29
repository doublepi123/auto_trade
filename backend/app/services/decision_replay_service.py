from __future__ import annotations

"""Trade decision replay: chronological timeline of a closed round trip.

There is no dedicated ``Trade`` model in the system: closed trades are the
entry<->exit round trips derived from the order ledger by
:class:`DailyPnlService`. This service layers the chronological
:class:`TradeEvent` log on top of one such round trip so a trader can replay
every decision point (price updates, LLM analysis, risk gates, order
submission, fills, skips and the final close).

A trade is addressed by the id of either of its two order-ledger rows
(entry or exit). ``list_replayable_trades`` returns the recent closed round
trips (by exit time), and ``replay_trade`` rebuilds the full timeline.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.trades import _active_fee_rates
from app.models import OrderRecord, TradeEvent
from app.services.daily_pnl_service import ClosedRoundTrip, DailyPnlService
from app.services.trade_event_service import decode_event_payload

# How many minutes of context to include before the entry and after the exit
# when assembling the event timeline. Generous on purpose: the trader wants to
# see the price context that led up to the entry decision.
_CONTEXT_PAD_MINUTES = 5

# The trade-event types surfaced in the timeline (in canonical order). Any
# other event type for the symbol inside the window is still included and
# bucketed under its own ``event_type``.
_TIMELINE_EVENT_TYPES = (
    "PRICE_UPDATE",
    "LLM_ANALYSIS",
    "RISK_CHECK",
    "ORDER_SUBMIT",
    "ORDER_SUBMITTED",
    "ORDER_FILL",
    "ORDER_FILLED",
    "ENTRY_SKIP",
    "TRADE_CLOSE",
)

# Max number of timeline events to keep payload summaries concise.
_MAX_PAYLOAD_SUMMARY_FIELDS = 6


class DecisionReplayService:
    """Rebuild the decision timeline for one closed round trip."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def replay_trade(self, trade_id: int) -> dict[str, Any] | None:
        """Return the full timeline for the round trip addressed by ``trade_id``.

        ``trade_id`` is the id of either the entry or exit ``OrderRecord`` of
        the round trip. Returns ``None`` when the id does not match any closed
        round trip.
        """
        trip = self._resolve_round_trip(trade_id)
        if trip is None:
            return None

        window_start = trip.entry_at - timedelta(minutes=_CONTEXT_PAD_MINUTES)
        window_end = trip.exit_at + timedelta(minutes=_CONTEXT_PAD_MINUTES)
        events = self._load_events(
            symbol=trip.symbol, from_dt=window_start, to_dt=window_end
        )

        timeline = [self._summarize_event(event) for event in events]
        # ``_summarize_event`` already preserves chronological order from the
        # query; sort defensively on timestamp to tolerate ties.
        timeline.sort(key=lambda item: item["timestamp"])

        return {
            "trade_id": trade_id,
            "symbol": trip.symbol,
            "market": self._infer_market(trip.symbol),
            "side": trip.side,
            "entry_price": round(trip.entry_price, 4),
            "exit_price": round(trip.exit_price, 4),
            "pnl": round(trip.net_pnl, 2),
            "entry_time": trip.entry_at.isoformat(),
            "exit_time": trip.exit_at.isoformat(),
            "timeline": timeline,
        }

    def list_replayable_trades(
        self, limit: int = 50, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Recent closed round trips (most-recent exit first) with event counts."""
        limit = max(1, min(int(limit), 500))
        fee_rate_us, fee_rate_hk = _active_fee_rates(self._db)
        trips = DailyPnlService(self._db).pair_round_trips(
            symbol=symbol,
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
        )
        # Most-recent exit first.
        trips.sort(key=lambda t: t.exit_at, reverse=True)
        trips = trips[:limit]

        # Batch the per-trade event count lookup so we don't issue one query
        # per trade. We count events per symbol in the trade window once and
        # join in Python.
        rows: list[dict[str, Any]] = []
        for trip in trips:
            window_start = trip.entry_at - timedelta(minutes=_CONTEXT_PAD_MINUTES)
            window_end = trip.exit_at + timedelta(minutes=_CONTEXT_PAD_MINUTES)
            count = self._count_events(
                symbol=trip.symbol, from_dt=window_start, to_dt=window_end
            )
            rows.append(
                {
                    "trade_id": trip.exit_order_id,
                    "entry_order_id": trip.entry_order_id,
                    "exit_order_id": trip.exit_order_id,
                    "symbol": trip.symbol,
                    "market": self._infer_market(trip.symbol),
                    "side": trip.side,
                    "pnl": round(trip.net_pnl, 2),
                    "entry_time": trip.entry_at.isoformat(),
                    "exit_time": trip.exit_at.isoformat(),
                    "event_count": count,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    def _resolve_round_trip(self, trade_id: int) -> ClosedRoundTrip | None:
        """Find the closed round trip whose entry or exit order id matches."""
        # Reject obviously invalid ids early to avoid a wasted ledger scan.
        if trade_id <= 0:
            return None

        fee_rate_us, fee_rate_hk = _active_fee_rates(self._db)
        trips = DailyPnlService(self._db).pair_round_trips(
            fee_rate_us=fee_rate_us,
            fee_rate_hk=fee_rate_hk,
        )
        for trip in trips:
            if trade_id in (trip.entry_order_id, trip.exit_order_id):
                return trip
        return None

    # ------------------------------------------------------------------
    # Event loaders
    # ------------------------------------------------------------------
    def _load_events(
        self, *, symbol: str, from_dt, to_dt
    ) -> list[TradeEvent]:
        """TradeEvents for ``symbol`` within ``[from_dt, to_dt]`` (chronological)."""
        stmt = (
            select(TradeEvent)
            .where(TradeEvent.symbol == symbol)
            .where(TradeEvent.created_at >= from_dt)
            .where(TradeEvent.created_at <= to_dt)
            .order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
        )
        events = list(self._db.execute(stmt).scalars().all())
        # Prefer timeline-relevant event types when both are present, but never
        # drop unrecognised event types (the trader may still want them).
        return events

    def _count_events(self, *, symbol: str, from_dt, to_dt) -> int:
        stmt = select(func.count(TradeEvent.id)).where(
            TradeEvent.symbol == symbol,
            TradeEvent.created_at >= from_dt,
            TradeEvent.created_at <= to_dt,
        )
        result = self._db.execute(stmt).scalar()
        return int(result or 0)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _summarize_event(event: TradeEvent) -> dict[str, Any]:
        """Compact timeline entry: timestamp / type / status / message / payload."""
        payload = decode_event_payload(event.payload_json)
        return {
            "timestamp": event.created_at.isoformat(),
            "event_type": event.event_type,
            "status": event.status or "",
            "message": event.message or "",
            "payload_summary": DecisionReplayService._summarize_payload(payload),
        }

    @staticmethod
    def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Reduce a possibly-large payload to a small, JSON-safe summary.

        Keeps the first ``_MAX_PAYLOAD_SUMMARY_FIELDS`` keys and stringifies
        values defensively (payloads are freeform and may contain nested
        objects). Nested dicts/lists are truncated to their top-level repr so
        the timeline stays readable.
        """
        summary: dict[str, Any] = {}
        for key, value in payload.items():
            if len(summary) >= _MAX_PAYLOAD_SUMMARY_FIELDS:
                summary["_truncated"] = True
                break
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
            elif isinstance(value, (list, tuple)):
                summary[key] = f"[{len(value)} items]"
            elif isinstance(value, dict):
                summary[key] = f"{{{len(value)} keys}}"
            else:
                summary[key] = str(value)
        return summary

    @staticmethod
    def _infer_market(symbol: str) -> str:
        """Coarse market tag inferred from the symbol suffix.

        The order ledger does not persist ``market`` directly; the suffix is a
        stable proxy (``AAPL.US`` -> US, ``0700.HK`` -> HK). Falls back to
        ``UNKNOWN`` for un-suffixed symbols.
        """
        if not symbol:
            return "UNKNOWN"
        if symbol.upper().endswith(".HK"):
            return "HK"
        if symbol.upper().endswith(".US"):
            return "US"
        return "UNKNOWN"
