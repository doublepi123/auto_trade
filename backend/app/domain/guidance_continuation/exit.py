"""Fixed-barrier exit state machine (PREREGISTRATION §10.6).

stop = P×(1−0.0045); target = P×(1+0.0080); holding deadline = entry fill
time + 60 min; flatten deadline = close − 15 min (half days honoured).

The exact exit rule implemented here:

1.  Every input quote must pass the full §10.4 qualification
    (``qualify_quote``: fresh — ``received_at − quote_ts <= 1 s``;
    ``bid > 0``; ``ask >= bid``; mid spread ≤ 5 bps).  A stale, crossed
    or wide quote neither triggers nor fills; the number of skipped
    quotes is reported as ``skipped_quote_count`` for evidence.
2.  Let ``D = min(holding_deadline, flatten_deadline)``.  Qualifying
    quotes received strictly before D — or exactly AT D — are the only
    quotes evaluated against the price barriers, grouped by identical
    ``received_at``.  Within one timestamp the priority is fixed:
    stop > flatten > holding > target.  Concretely, for a group at
    instant ``at <= D``: any ``bid <= stop`` → ``PRICE_STOP`` at ``at``;
    else ``at == flatten_deadline`` → ``EOD_FLATTEN`` at ``at``; else
    ``at == holding_deadline`` → ``MAX_HOLD`` at ``at``; else any
    ``bid >= target`` → ``PROFIT_TARGET`` at ``at``.
3.  Time exits trigger AT the deadline instant, never at the observing
    quote's timestamp.  The first qualifying quote strictly after D
    (or a quote stream that ends without a barrier hit) yields the TIME
    exit whose deadline arrived first, with ``trigger_at`` = that
    deadline: ``flatten_deadline <= holding_deadline`` →
    ``EOD_FLATTEN`` at ``flatten_deadline`` (a tie goes to flatten,
    matching the same-instant priority), otherwise ``MAX_HOLD`` at
    ``holding_deadline``.  A price barrier crossed only by that later
    quote does NOT re-label the trigger.
4.  After the trigger we wait ≥ 1 s (``exit_wait_seconds``) and record
    the virtual sell at the first qualifying quote whose
    ``bid_size >= qty``.  The trigger quote itself can never be the
    fill: same-``received_at`` siblings are excluded by the 1 s wait.
    The target fill price is ``min(target, bid)``, never more; every
    other exit fills at that bid.  No fabricated deadline fills: an
    unresolved exit is ``EXIT_GAP``, and the observed trigger-to-fill
    delay is preserved verbatim (§10.6 line 598).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from app.core.market_calendar import get_session
from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)
from app.domain.guidance_continuation.quotes import (
    QuoteObservation,
    qualify_quote,
)

_ET = ZoneInfo("America/New_York")
_US = "US"

TRIGGER_PRICE_STOP: Final[str] = "PRICE_STOP"
TRIGGER_PROFIT_TARGET: Final[str] = "PROFIT_TARGET"
TRIGGER_MAX_HOLD: Final[str] = "MAX_HOLD"
TRIGGER_EOD_FLATTEN: Final[str] = "EOD_FLATTEN"

EXIT_RESOLVED: Final[str] = "RESOLVED"
EXIT_GAP: Final[str] = "EXIT_GAP"

RESULT_FILLED: Final[str] = "FILLED"
RESULT_UNRESOLVED: Final[str] = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ExitBarriers:
    """The four §10.6 barriers for one position."""

    stop_price: Decimal
    target_price: Decimal
    holding_deadline: datetime
    flatten_deadline: datetime

    @classmethod
    def for_position(
        cls,
        *,
        entry_price: Decimal,
        entry_fill_at: datetime,
        target_day: date,
        config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
    ) -> ExitBarriers:
        session = get_session(_US)
        close_at = datetime.combine(
            target_day, session.close_time(target_day), tzinfo=_ET
        )
        return cls(
            stop_price=entry_price * (Decimal(1) - config.stop_loss_pct),
            target_price=entry_price * (Decimal(1) + config.profit_target_pct),
            holding_deadline=entry_fill_at
            + timedelta(minutes=config.max_hold_minutes),
            flatten_deadline=close_at
            - timedelta(minutes=config.flatten_minutes_before_close),
        )


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    trigger: str
    trigger_at: datetime
    fill_at: datetime | None
    fill_price: Decimal | None
    fill_bid_size: int | None
    exit_status: str  # EXIT_RESOLVED | EXIT_GAP
    trigger_to_fill_seconds: Decimal | None
    trigger_quote_index: int | None = None
    fill_quote_index: int | None = None
    #: How many input quotes failed §10.4 qualification and were skipped
    #: entirely (neither trigger nor fill evidence).  Evidence counter.
    skipped_quote_count: int = 0

    @property
    def resolved(self) -> bool:
        return self.exit_status == EXIT_RESOLVED


def _to_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("exit timestamps must be timezone-aware")
    return instant.astimezone(ZoneInfo("UTC"))


def _time_trigger(
    holding: datetime, flatten: datetime
) -> tuple[str, datetime]:
    """The earlier deadline owns the time exit; a tie goes to flatten."""
    if flatten <= holding:
        return TRIGGER_EOD_FLATTEN, flatten
    return TRIGGER_MAX_HOLD, holding


def evaluate_exit(
    *,
    quotes: tuple[QuoteObservation, ...],
    barriers: ExitBarriers,
    quantity: int,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> ExitEvaluation:
    """Replay two-sided quotes against the fixed §10.6 barriers.

    See the module docstring for the exact rule.  Both the trigger and
    the fill must be §10.4-qualifying quotes (``qualify_quote``); the
    sizing check for the fill stays on ``bid_size``.
    """
    qualified: list[tuple[int, QuoteObservation]] = []
    skipped = 0
    for i, quote in enumerate(quotes):
        if qualify_quote(quote, config).qualifies:
            qualified.append((i, quote))
        else:
            skipped += 1

    holding = _to_utc(barriers.holding_deadline)
    flatten = _to_utc(barriers.flatten_deadline)
    earliest_deadline = min(holding, flatten)

    trigger: str | None = None
    trigger_at: datetime | None = None
    trigger_index: int | None = None

    # Group quotes by identical received_at instant: §10.6 fixes the
    # priority WITHIN one timestamp as stop > flatten > holding >
    # target, regardless of arrival order inside that instant.
    i = 0
    n = len(qualified)
    while i < n:
        at = _to_utc(qualified[i][1].received_at)
        group: list[tuple[int, QuoteObservation]] = []
        j = i
        while j < n and _to_utc(qualified[j][1].received_at) == at:
            group.append(qualified[j])
            j += 1

        if at > earliest_deadline:
            # First qualifying quote strictly after the earlier deadline:
            # the TIME exit fired at the deadline instant itself.  Price
            # barriers crossed only by this later quote are not
            # evaluated, and the trigger timestamp is the deadline.
            trigger, trigger_at = _time_trigger(holding, flatten)
            break

        stop_hit = any(q.bid <= barriers.stop_price for _, q in group)
        target_hit = any(q.bid >= barriers.target_price for _, q in group)
        if stop_hit:
            # B6: name a quote that actually SATISFIES the stop barrier.
            stop_index = next(
                i for i, q in group if q.bid <= barriers.stop_price
            )
            trigger, trigger_at, trigger_index = (
                TRIGGER_PRICE_STOP,
                at,
                stop_index,
            )
            break
        if at == flatten:
            trigger, trigger_at, trigger_index = (
                TRIGGER_EOD_FLATTEN,
                at,
                group[0][0],
            )
            break
        if at == holding:
            trigger, trigger_at, trigger_index = (
                TRIGGER_MAX_HOLD,
                at,
                group[0][0],
            )
            break
        if target_hit:
            target_index = next(
                i for i, q in group if q.bid >= barriers.target_price
            )
            trigger, trigger_at, trigger_index = (
                TRIGGER_PROFIT_TARGET,
                at,
                target_index,
            )
            break
        i = j

    if trigger is None or trigger_at is None:
        # No price barrier fired and no quote arrived past the earlier
        # deadline: the deadline itself still triggers the time exit AT
        # the deadline instant.  The fill keeps requiring a later
        # qualifying quote, so a stream that ends early yields EXIT_GAP
        # rather than a fabricated fill.
        trigger, trigger_at = _time_trigger(holding, flatten)

    wait_deadline = trigger_at + timedelta(
        seconds=float(config.exit_wait_seconds)
    )

    for idx, quote in qualified:
        at = _to_utc(quote.received_at)
        if at < wait_deadline:
            continue
        if quote.bid_size < quantity:
            continue
        if trigger == TRIGGER_PROFIT_TARGET:
            price = min(barriers.target_price, quote.bid)
        else:
            price = quote.bid
        delay = Decimal(str((at - trigger_at).total_seconds()))
        return ExitEvaluation(
            trigger=trigger,
            trigger_at=trigger_at,
            fill_at=at,
            fill_price=price,
            fill_bid_size=quote.bid_size,
            exit_status=EXIT_RESOLVED,
            trigger_to_fill_seconds=delay,
            trigger_quote_index=trigger_index,
            fill_quote_index=idx,
            skipped_quote_count=skipped,
        )

    return ExitEvaluation(
        trigger=trigger,
        trigger_at=trigger_at,
        fill_at=None,
        fill_price=None,
        fill_bid_size=None,
        exit_status=EXIT_GAP,
        trigger_to_fill_seconds=None,
        trigger_quote_index=trigger_index,
        fill_quote_index=None,
        skipped_quote_count=skipped,
    )
