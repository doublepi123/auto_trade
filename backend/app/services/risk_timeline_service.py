"""Risk check timeline — chronological view of pre-trade risk gate outcomes.

The live path records trade-decision outcomes as ``TradeEvent`` rows
(``ORDER_SKIPPED`` with a ``skip_category``, plus ``ORDER_SUBMITTED`` /
``ORDER_REJECTED`` / ``ORDER_FAILED``). This service re-hydrates those rows
into a per-trade (or per-symbol) sequence of risk-check steps with pass/fail
status, and rolls them up into a time-windowed summary broken down by the
repo's skip-category taxonomy (FEE | REPRICING | COOLDOWN | RISK | PENDING |
POSITION | SESSION).

Read-only: it never mutates state or places orders.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import TradeEvent

logger = logging.getLogger("auto_trade.risk_timeline")

# The repo's skip-category taxonomy (see AGENTS.md §Domain Concepts). Order is
# stable for deterministic summary rendering.
SKIP_CATEGORIES: tuple[str, ...] = (
    "FEE",
    "REPRICING",
    "COOLDOWN",
    "RISK",
    "PENDING",
    "POSITION",
    "SESSION",
)

# Event types that carry risk-gate decisions. The live path emits
# ``ORDER_SKIPPED`` (with ``skip_category``) for blocks and
# ``ORDER_SUBMITTED`` for passes; ``ORDER_REJECTED`` / ``ORDER_FAILED`` are
# broker-side failures that the timeline surfaces for completeness. The spec's
# conceptual names (``RISK_CHECK``, ``RISK_BLOCK``, ``ENTRY_SKIP``,
# ``ORDER_REJECT``) are included as aliases so the service keeps working if
# those event types are introduced later.
_BLOCK_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ORDER_SKIPPED",
        "ORDER_REJECTED",
        "ORDER_FAILED",
        "ORDER_TIMEOUT",
        "RISK_BLOCK",
        "ENTRY_SKIP",
        "ORDER_REJECT",
    }
)
_PASS_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ORDER_SUBMITTED",
        "ORDER_FILLED",
        "ORDER_ACKNOWLEDGED",
        "RISK_CHECK",
    }
)
_RELEVANT_EVENT_TYPES: frozenset[str] = _BLOCK_EVENT_TYPES | _PASS_EVENT_TYPES


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _to_utc(value).isoformat()


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class RiskTimelineService:
    """Reconstruct a pass/fail timeline of pre-trade risk checks."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------
    def get_trade_risk_checks(
        self,
        trade_id: int | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Chronological list of risk-check steps.

        ``trade_id`` here is interpreted as the ``broker_order_id`` column on
        ``TradeEvent`` (the live path tags every decision event for one round
        trip with the same broker order id). When omitted, all relevant events
        are returned, optionally narrowed by ``symbol``.
        """
        if limit <= 0:
            return []

        query = self._db.query(TradeEvent).filter(
            TradeEvent.event_type.in_(sorted(_RELEVANT_EVENT_TYPES))
        )
        if trade_id is not None:
            query = query.filter(TradeEvent.broker_order_id == str(trade_id))
        if symbol:
            query = query.filter(TradeEvent.symbol == symbol)

        rows = (
            query.order_by(TradeEvent.created_at.asc(), TradeEvent.id.asc())
            .limit(limit)
            .all()
        )
        return [self._shape_event(row) for row in rows]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def get_risk_summary(self, hours: int = 24) -> dict[str, Any]:
        """Aggregate risk-gate counts over the trailing ``hours`` window."""
        if hours < 0:
            hours = 0
        window_start = datetime.now(timezone.utc) - timedelta(hours=hours)

        query = self._db.query(TradeEvent).filter(
            TradeEvent.event_type.in_(sorted(_RELEVANT_EVENT_TYPES)),
            TradeEvent.created_at >= window_start,
        )
        rows = query.order_by(
            TradeEvent.created_at.desc(), TradeEvent.id.desc()
        ).all()

        total = len(rows)
        passed = 0
        blocked = 0
        by_category: dict[str, int] = {category: 0 for category in SKIP_CATEGORIES}
        recent_blocks: list[dict[str, Any]] = []

        for row in rows:
            if self._is_pass(row):
                passed += 1
            else:
                blocked += 1
                category = self._category_for(row)
                by_category[category] = by_category.get(category, 0) + 1
                if len(recent_blocks) < 10:
                    recent_blocks.append(self._shape_event(row))

        return {
            "window_hours": hours,
            "total_checks": total,
            "passed": passed,
            "blocked": blocked,
            "by_category": by_category,
            "recent_blocks": recent_blocks,
        }

    # ------------------------------------------------------------------
    # Shaping helpers
    # ------------------------------------------------------------------
    def _shape_event(self, event: TradeEvent) -> dict[str, Any]:
        payload = _parse_json(event.payload_json)
        passed = self._is_pass(event)
        return {
            "id": event.id,
            "event_type": event.event_type,
            "trade_id": event.broker_order_id or None,
            "symbol": event.symbol,
            "side": event.side,
            "status": event.status,
            "passed": passed,
            "check_name": str(payload.get("check_name") or payload.get("source") or ""),
            "reason": str(payload.get("reason") or event.message or ""),
            "skip_category": str(payload.get("skip_category") or ""),
            "threshold": self._maybe_number(payload.get("threshold")),
            "actual_value": self._maybe_number(payload.get("actual_value")),
            "created_at": _to_iso(event.created_at),
        }

    @staticmethod
    def _is_pass(event: TradeEvent) -> bool:
        """A pass is an explicit submit/fill; everything else is a block."""
        return event.event_type in _PASS_EVENT_TYPES

    @staticmethod
    def _category_for(event: TradeEvent) -> str:
        payload = _parse_json(event.payload_json)
        category = str(payload.get("skip_category") or "").upper()
        if category in SKIP_CATEGORIES:
            return category
        # Broker-side rejections (no skip_category) bucket under RISK by
        # convention — they prevented a trade but weren't a quant fee/session
        # gate. ``REGIME`` is a live-entry-policy category that we normalize to
        # RISK for the summary (it's a risk-regime gate).
        if category == "REGIME":
            return "RISK"
        return "RISK"

    @staticmethod
    def _maybe_number(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:  # NaN guard
            return None
        return number
