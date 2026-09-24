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
6. ``sized_quantity_positive`` — entry or exit sizing returned a quantity > 0,
   recorded once in the execution service before later fee/risk vetoes,
   only for the runner's current primary symbol
7. ``submit_attempts`` — an order submission was attempted against the broker
8. ``broker_acks`` — the broker acknowledged the order
9. ``persisted`` — the order was persisted locally (new order row committed)

``pre_submit_risk_check_invocations`` counts mandatory boundary invocations
in the execution service (unlike stage 6, it is not primary-symbol gated).

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
- triggers fire but ``sized_quantity_positive == 0`` → execution stopped before
  positive sizing: inspect pre-sizing skips; a zero quantity is POSITION.
- sizing positive but ``submit_attempts == 0`` → inspect post-sizing FEE/RISK
  or final precheck skips; sizing/capital already passed.
- submit attempts occur but ``broker_acks == 0`` → the broker path.
- acks occur but ``persisted == 0`` → the known persistence defect.

Counters reset per trading session (exchange-local trading day, via the
injected ``trade_day_provider`` — the runner passes its existing
``_market_trade_day``). On rollover the completed session's snapshot is
queued for the runner to persist; mutators never perform I/O.

The runner also moves the live session's counts to the database about once a
minute, from a background writer, as deltas added with
:func:`add_session_counts`. A mid-session restart therefore continues the
session's row instead of erasing everything counted before it.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.models import DecisionFunnelSessionSummary

logger = logging.getLogger("auto_trade.decision_funnel")

SKIP_CATEGORIES: tuple[str, ...] = (
    "FEE",
    "REPRICING",
    "COOLDOWN",
    "REGIME",
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

    def collect_sessions(
        self,
    ) -> tuple[list[DecisionFunnelSnapshot], DecisionFunnelSnapshot]:
        """Atomically take the closed sessions and snapshot the live one.

        One lock acquisition, so a day rollover cannot fall between the two
        and leave counts attributed to neither session, or to both.
        """
        with self._lock:
            self._maybe_rollover_locked()
            closed = list(self._closed_sessions)
            self._closed_sessions.clear()
            return closed, self._snapshot_locked()

    def take_all_sessions(self) -> list[DecisionFunnelSnapshot]:
        """Atomically take every unpersisted session and restart at zero.

        Returns the closed sessions still queued followed by the current one.
        Used when the primary symbol changes mid-session, so counts gathered
        for one symbol are never attributed to another.
        """
        with self._lock:
            self._maybe_rollover_locked()
            taken = list(self._closed_sessions)
            self._closed_sessions.clear()
            taken.append(self._snapshot_locked())
            self._reset_counters_locked()
            return taken

    def discard_all(self) -> None:
        """Drop every count. Last resort when ownership cannot be proven."""
        with self._lock:
            self._closed_sessions.clear()
            self._reset_counters_locked()


def combine_snapshots(
    base: DecisionFunnelSnapshot,
    other: DecisionFunnelSnapshot,
    *,
    sign: int,
) -> DecisionFunnelSnapshot:
    """Counter-wise ``base + sign * other`` for one session, floored at zero."""
    combined: dict[str, object] = {"session_date": base.session_date}
    for item in fields(DecisionFunnelSnapshot):
        if item.name == "session_date":
            continue
        left = getattr(base, item.name)
        right = getattr(other, item.name)
        if isinstance(left, dict):
            combined[item.name] = {
                key: max(0, int(left.get(key, 0) or 0) + sign * int(right.get(key, 0) or 0))
                for key in set(left) | set(right)
            }
        else:
            combined[item.name] = max(0, int(left or 0) + sign * int(right or 0))
    return DecisionFunnelSnapshot(**combined)  # pyright: ignore[reportArgumentType]


def is_empty_snapshot(snapshot: DecisionFunnelSnapshot) -> bool:
    """True when a snapshot carries no counts at all."""
    for item in fields(DecisionFunnelSnapshot):
        if item.name == "session_date":
            continue
        value = getattr(snapshot, item.name)
        if isinstance(value, dict):
            if any(int(count or 0) for count in value.values()):
                return False
        elif int(value or 0):
            return False
    return True


def _json_counts(raw: str | None) -> dict[str, int]:
    try:
        decoded = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): int(value or 0) for key, value in decoded.items()}


_SCALAR_COUNTERS: tuple[str, ...] = (
    "primary_quotes_seen",
    "quality_rejections",
    "entry_crossing_blocks",
    "fresh_primary_quote",
    "evaluations",
    "threshold_crossings",
    "triggers",
    "sized_quantity_positive",
    "submit_attempts",
    "broker_acks",
    "persisted",
    "pre_submit_risk_check_invocations",
)


def add_session_counts(
    db: Session,
    delta: DecisionFunnelSnapshot,
    *,
    symbol: str,
    market: str,
) -> None:
    """Add counts gathered since the last write to the (session, symbol) row.

    The runner hands each count to this function exactly once, so a process
    restart continues the row instead of overwriting it with its own tail.
    The caller commits.
    """
    session_day = date.fromisoformat(delta.session_date)
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
            **{name: 0 for name in _SCALAR_COUNTERS},
            skips_json="{}",
            quality_rejections_json="{}",
        )
        db.add(row)
    row.market = market
    for name in _SCALAR_COUNTERS:
        setattr(row, name, int(getattr(row, name) or 0) + int(getattr(delta, name) or 0))

    def added(current: str | None, extra: dict[str, int]) -> str:
        merged = _json_counts(current)
        for key, value in extra.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
        return json.dumps(merged, sort_keys=True)

    row.skips_json = added(row.skips_json, delta.skips_by_category)
    row.quality_rejections_json = added(
        row.quality_rejections_json, delta.quality_rejections_by_reason
    )
    row.updated_at = datetime.now(timezone.utc)


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
