"""Decision funnel — compact stage counters for the live trading path.

Answers exactly one question: **at which stage does the pipeline stop?**

The counters form a monotone funnel over the primary symbol's live path:

1. ``fresh_primary_quote`` — a usable, fresh push quote for the primary symbol
2. ``evaluations`` — the runner evaluated a quote against engine thresholds
3. ``threshold_crossings`` — price actually crossed an entry threshold
4. ``skips_by_category`` — a trigger/crossing was suppressed, keyed by the
   existing skip categories (``FEE | REPRICING | COOLDOWN | RISK | PENDING |
   POSITION | SESSION``); unknown categories are ignored, never invented
5. ``triggers`` — an entry/exit trigger fired and survived the pre-trigger
   risk veto
6. ``sized_quantity_positive`` — sizing provably returned a quantity > 0
   (observed runner-side: the order reached broker submission; a precise
   in-service sizing probe is a planned follow-up hook)
7. ``submit_attempts`` — an order submission was attempted against the broker
8. ``broker_acks`` — the broker acknowledged the order
9. ``persisted`` — the order was persisted locally (new order row committed)

``pre_submit_risk_check_invocations`` is reserved: always present, expected
to remain 0 until a later task introduces a mandatory pre-submit risk
boundary and wires this as its live probe.

Interpretation contract (read the counters in order, first zero indicts):
- ``primary_quotes_seen == 0`` → no quote reached the primary path at all.
- quotes arrive but ``evaluations == 0`` and ``quality_rejections == 0`` →
  the loop is not running (stopped or a trigger stuck in flight).
- ``quality_rejections`` dominates → the live quality gate is refusing the
  feed; ``quality_rejections_by_reason`` names the failing predicate.
- evaluations flow but ``threshold_crossings == 0`` → the configured interval
  is stale or genuinely never touched.
- crossings occur but ``entry_crossing_blocks`` matches them → entries were
  withheld for want of fresh crossing evidence, not for want of a signal.
- crossings occur but skips dominate → the indicted stage is the dominant
  skip category.
- triggers fire but ``sized_quantity_positive == 0`` → sizing/capital (see
  the dominant skip category, typically POSITION/FEE).
- sizing positive but ``submit_attempts == 0`` or ``broker_acks == 0`` → the
  broker path.
- acks occur but ``persisted == 0`` → the known persistence defect.

Counters reset per trading session (exchange-local trading day, via the
injected ``trade_day_provider`` — the runner passes its existing
``_market_trade_day``). On rollover the completed session's snapshot is
queued for the run loop to persist via :func:`persist_session_summary`;
mutators never perform I/O.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import DecisionFunnelSessionSummary

logger = logging.getLogger("auto_trade.decision_funnel")

SKIP_CATEGORIES: tuple[str, ...] = (
    "FEE",
    "REPRICING",
    "COOLDOWN",
    "RISK",
    "PENDING",
    "POSITION",
    "SESSION",
)


QUALITY_PREDICATES: tuple[str, ...] = (
    "price_positive",
    "spread_reasonable",
    "last_bbo_consistent",
    "source_timestamp_fresh",
)


@dataclass(frozen=True)
class DecisionFunnelSnapshot:
    """Immutable projection of one session's funnel counters."""

    session_date: str
    primary_quotes_seen: int = 0
    quality_rejections: int = 0
    quality_rejections_by_reason: dict[str, int] = field(default_factory=dict)
    fresh_primary_quote: int = 0
    evaluations: int = 0
    threshold_crossings: int = 0
    entry_crossing_blocks: int = 0
    skips_by_category: dict[str, int] = field(default_factory=dict)
    triggers: int = 0
    sized_quantity_positive: int = 0
    submit_attempts: int = 0
    broker_acks: int = 0
    persisted: int = 0
    pre_submit_risk_check_invocations: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class DecisionFunnelTracker:
    """Process-local, thread-safe decision-funnel counters (observer-only).

    Constructed by the runner and mutated from the existing quote/trigger/
    execution code paths. Mutators are constant-time integer increments under
    a private lock plus the session-rollover check; they swallow ordinary
    ``Exception`` so observer failures never alter control flow.
    """

    def __init__(
        self,
        trade_day_provider: Callable[[], date],
    ) -> None:
        self._trade_day_provider = trade_day_provider
        self._lock = threading.Lock()
        self._session_day: date | None = None
        self._closed_sessions: deque[DecisionFunnelSnapshot] = deque()
        self._counts: dict[str, int] = self._zero_counts()
        self._skips_by_category: dict[str, int] = {
            category: 0 for category in SKIP_CATEGORIES
        }
        self._quality_rejections_by_reason: dict[str, int] = {
            predicate: 0 for predicate in QUALITY_PREDICATES
        }

    @staticmethod
    def _zero_counts() -> dict[str, int]:
        return {
            "primary_quotes_seen": 0,
            "quality_rejections": 0,
            "fresh_primary_quote": 0,
            "evaluations": 0,
            "threshold_crossings": 0,
            "entry_crossing_blocks": 0,
            "triggers": 0,
            "sized_quantity_positive": 0,
            "submit_attempts": 0,
            "broker_acks": 0,
            "persisted": 0,
            "pre_submit_risk_check_invocations": 0,
        }

    # --- hot-path mutators (no I/O, no-throw) -----------------------------

    def _increment(self, stage: str) -> None:
        try:
            with self._lock:
                self._maybe_rollover_locked()
                self._counts[stage] += 1
        except Exception:
            logger.debug("decision-funnel record failed", exc_info=True)

    def record_fresh_primary_quote(self) -> None:
        self._increment("fresh_primary_quote")

    def record_primary_quote_seen(self) -> None:
        self._increment("primary_quotes_seen")

    def record_entry_crossing_block(self) -> None:
        self._increment("entry_crossing_blocks")

    def record_quality_rejection(self, failed_predicates: Sequence[str]) -> None:
        """Count one quote the live quality gate refused, and why.

        A rejected quote never reaches ``evaluations``, so without this the
        gate is indistinguishable from a stopped loop.
        """
        try:
            with self._lock:
                self._maybe_rollover_locked()
                self._counts["quality_rejections"] += 1
                for predicate in failed_predicates:
                    if predicate in self._quality_rejections_by_reason:
                        self._quality_rejections_by_reason[predicate] += 1
        except Exception:
            logger.debug("decision-funnel record failed", exc_info=True)

    def record_evaluation(self) -> None:
        self._increment("evaluations")

    def record_threshold_crossing(self) -> None:
        self._increment("threshold_crossings")

    def record_trigger(self) -> None:
        self._increment("triggers")

    def record_sized_quantity_positive(self) -> None:
        self._increment("sized_quantity_positive")

    def record_submit_attempt(self) -> None:
        self._increment("submit_attempts")

    def record_broker_ack(self) -> None:
        self._increment("broker_acks")

    def record_persisted(self) -> None:
        self._increment("persisted")

    def record_pre_submit_risk_check(self) -> None:
        """Reserved probe for the planned mandatory pre-submit risk boundary.

        Intentionally unwired for now — the counter stays 0 until that task
        lands. Present so diagnostics consumers can rely on the field.
        """
        self._increment("pre_submit_risk_check_invocations")

    def record_skip(self, category: str) -> None:
        normalized = str(category or "").upper()
        if normalized not in SKIP_CATEGORIES:
            return
        try:
            with self._lock:
                self._maybe_rollover_locked()
                self._skips_by_category[normalized] += 1
        except Exception:
            logger.debug("decision-funnel record failed", exc_info=True)

    # --- read projections / session rollover ------------------------------

    def _maybe_rollover_locked(self) -> None:
        """Close the current session when the exchange-local day changes.

        The completed session's snapshot is queued for the run loop to
        persist; counters reset. Caller holds the lock. No I/O.
        """
        day = self._trade_day_provider()
        if self._session_day is None:
            self._session_day = day
            return
        if day != self._session_day:
            self._closed_sessions.append(self._snapshot_locked())
            self._reset_counters_locked()
            self._session_day = day

    def _reset_counters_locked(self) -> None:
        self._counts = self._zero_counts()
        self._skips_by_category = {category: 0 for category in SKIP_CATEGORIES}
        self._quality_rejections_by_reason = {
            predicate: 0 for predicate in QUALITY_PREDICATES
        }

    def _snapshot_locked(self) -> DecisionFunnelSnapshot:
        return DecisionFunnelSnapshot(
            session_date=(
                self._session_day.isoformat() if self._session_day else ""
            ),
            skips_by_category=dict(self._skips_by_category),
            quality_rejections_by_reason=dict(self._quality_rejections_by_reason),
            **self._counts,
        )

    def snapshot(self) -> DecisionFunnelSnapshot:
        """Immutable projection of the current session's counters.

        Also detects a day boundary crossed while no quotes flowed, so the
        closed session reaches the persistence queue promptly.
        """
        try:
            with self._lock:
                self._maybe_rollover_locked()
                return self._snapshot_locked()
        except Exception:
            logger.debug("decision-funnel snapshot failed", exc_info=True)
            return DecisionFunnelSnapshot(
                session_date="",
                skips_by_category={category: 0 for category in SKIP_CATEGORIES},
                quality_rejections_by_reason={
                    predicate: 0 for predicate in QUALITY_PREDICATES
                },
            )

    def drain_closed_sessions(self) -> list[DecisionFunnelSnapshot]:
        """Atomically take the queue of closed sessions awaiting persistence."""
        try:
            with self._lock:
                self._maybe_rollover_locked()
                drained = list(self._closed_sessions)
                self._closed_sessions.clear()
                return drained
        except Exception:
            logger.debug("decision-funnel drain failed", exc_info=True)
            return []

    def requeue_closed_sessions(
        self, snapshots: list[DecisionFunnelSnapshot]
    ) -> None:
        """Put back sessions whose persistence failed (order preserved)."""
        with self._lock:
            self._closed_sessions.extendleft(reversed(list(snapshots)))


def persist_session_summary(
    db: Session,
    snapshot: DecisionFunnelSnapshot,
    *,
    symbol: str,
    market: str,
) -> None:
    """Upsert one session's funnel summary keyed by (session_date, symbol).

    Exactly one durable row exists per session per symbol: re-persisting the
    same session (e.g. after a process restart) updates the row in place.
    """
    session_day = date.fromisoformat(snapshot.session_date)
    row = (
        db.query(DecisionFunnelSessionSummary)
        .filter(
            DecisionFunnelSessionSummary.session_date == session_day,
            DecisionFunnelSessionSummary.symbol == symbol,
        )
        .first()
    )
    if row is None:
        row = DecisionFunnelSessionSummary(
            session_date=session_day,
            symbol=symbol,
            market=market,
        )
        db.add(row)
    row.market = market
    row.primary_quotes_seen = snapshot.primary_quotes_seen
    row.quality_rejections = snapshot.quality_rejections
    row.quality_rejections_json = json.dumps(
        snapshot.quality_rejections_by_reason, sort_keys=True
    )
    row.entry_crossing_blocks = snapshot.entry_crossing_blocks
    row.fresh_primary_quote = snapshot.fresh_primary_quote
    row.evaluations = snapshot.evaluations
    row.threshold_crossings = snapshot.threshold_crossings
    row.triggers = snapshot.triggers
    row.sized_quantity_positive = snapshot.sized_quantity_positive
    row.submit_attempts = snapshot.submit_attempts
    row.broker_acks = snapshot.broker_acks
    row.persisted = snapshot.persisted
    row.pre_submit_risk_check_invocations = (
        snapshot.pre_submit_risk_check_invocations
    )
    row.skips_json = json.dumps(snapshot.skips_by_category, sort_keys=True)
    row.updated_at = datetime.now(timezone.utc)
