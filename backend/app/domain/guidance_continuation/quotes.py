"""Quote qualification and entry-fill simulation (PREREGISTRATION §10.4).

A quote qualifies when it is fresh (0 ≤ age ≤ 1 s, computed on
UTC-converted instants so DST folds cannot manufacture freshness), has
``bid > 0`` and ``ask >= bid``, and its mid spread is ≤ 5 bps.  A
NEGATIVE age (``quote_ts`` after ``received_at``) fails closed as STALE.

``simulate_entry`` derives its attempt window from the config and the
target day — 09:46:00–09:46:05 ET exactly — and additionally requires
all inputs to have been obtained by 09:45:59 ET and the attempt to sit at
least ``entry_cutoff_minutes_before_close`` before the real close (half
days included).  Quantity is always the package's own
``position_quantity``; there is no external ``quantity_fn``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from app.core.holiday_calendar import is_half_day, is_market_closed
from app.core.market_calendar import get_session
from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)
from app.domain.guidance_continuation.sizing import ceil_to_tick, position_quantity

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
_US = "US"

RESULT_FILLED: Final[str] = "FILLED"
RESULT_UNFILLED: Final[str] = "UNFILLED"
RESULT_NO_ATTEMPT: Final[str] = "NO_ATTEMPT"
RESULT_MISSED_WINDOW: Final[str] = "MISSED_WINDOW"

REASON_QUALIFIED: Final[str] = "QUALIFIED"
REASON_NO_QUALIFYING_QUOTE: Final[str] = "NO_QUALIFYING_QUOTE"
REASON_STALE: Final[str] = "STALE"
REASON_CROSSED: Final[str] = "CROSSED"
REASON_ZERO_BID: Final[str] = "ZERO_BID"
REASON_WIDE_SPREAD: Final[str] = "WIDE_SPREAD"
REASON_ASK_ABOVE_LIMIT: Final[str] = "ASK_ABOVE_LIMIT"
REASON_ASK_SIZE_SHORTFALL: Final[str] = "ASK_SIZE_SHORTFALL"
REASON_FILL_WINDOW_ELAPSED: Final[str] = "FILL_WINDOW_ELAPSED"
REASON_INPUTS_LATE: Final[str] = "INPUTS_LATE"
REASON_PAST_ENTRY_CUTOFF: Final[str] = "PAST_ENTRY_CUTOFF"


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    quote_ts: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class QuoteQualification:
    qualifies: bool
    reason_code: str
    age_seconds: Decimal | None = None
    spread_bps: Decimal | None = None

    @property
    def fresh(self) -> bool:
        return self.qualifies


@dataclass(frozen=True, slots=True)
class EntrySimulationResult:
    result: str  # FILLED | UNFILLED | NO_ATTEMPT | MISSED_WINDOW
    limit_price: Decimal | None
    quantity: int | None
    reference_quote_index: int | None
    reference_received_at: datetime | None
    fill_quote_index: int | None
    fill_received_at: datetime | None
    fill_price: Decimal | None
    unfilled_reason: str = ""

    @property
    def filled(self) -> bool:
        return self.result == RESULT_FILLED


def _to_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("quote timestamps must be timezone-aware")
    return instant.astimezone(_UTC)


def attempt_window(
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> tuple[datetime, datetime]:
    """The frozen single-attempt window 09:46:00–09:46:05 ET on ``target_day``."""
    start = datetime.combine(
        target_day,
        time(
            config.entry_attempt_window_start_et.hour,
            config.entry_attempt_window_start_et.minute,
            config.entry_attempt_window_start_et.second,
        ),
        tzinfo=_ET,
    )
    end = datetime.combine(
        target_day,
        time(
            config.entry_attempt_window_end_et.hour,
            config.entry_attempt_window_end_et.minute,
            config.entry_attempt_window_end_et.second,
        ),
        tzinfo=_ET,
    )
    return start, end


def inputs_deadline(
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> datetime:
    """09:45:59 ET on the target day (right-inclusive)."""
    t = config.inputs_deadline_et
    return datetime.combine(
        target_day, time(t.hour, t.minute, t.second), tzinfo=_ET
    )


def entry_cutoff_instant(
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> datetime:
    """Real close − 45 min (13:00 half days included)."""
    session = get_session(_US)
    close_at = datetime.combine(
        target_day, session.close_time(target_day), tzinfo=_ET
    )
    return close_at - timedelta(
        minutes=config.entry_cutoff_minutes_before_close
    )


def evaluate_entry_time_gate(
    *,
    target_day: date,
    inputs_obtained_at: datetime,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> str | None:
    """Pure §10.4 entry time gate; None = pass, else a REASON_* code.

    1. All inputs must have been obtained by 09:45:59 ET inclusive.
    2. The attempt window is exactly 09:46:00–09:46:05 ET on the target
       day (callers cannot choose another).
    3. The attempt must be at least 45 minutes before the real close
       (half-day early closes included).
    """
    if _to_utc(inputs_obtained_at) > inputs_deadline(target_day, config):
        return REASON_INPUTS_LATE
    cutoff = entry_cutoff_instant(target_day, config)
    start, end = attempt_window(target_day, config)
    if _to_utc(end) > cutoff:
        return REASON_PAST_ENTRY_CUTOFF
    _ = start
    return None


def qualify_quote(
    quote: QuoteObservation,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> QuoteQualification:
    """Apply the §10.4 freshness / two-sidedness / spread checks.

    Age is computed AFTER converting both timestamps to UTC, so a DST
    fold in either zone cannot manufacture freshness.  Negative age
    (``quote_ts`` after ``received_at``) fails closed as STALE.
    """
    age_seconds: Decimal | None = None
    if quote.received_at.tzinfo is None or quote.quote_ts.tzinfo is None:
        raise ValueError("quote timestamps must be timezone-aware")

    delta = _to_utc(quote.received_at) - _to_utc(quote.quote_ts)
    age_seconds = Decimal(str(delta.total_seconds()))
    max_age = config.quote_max_age_seconds
    if age_seconds > max_age or age_seconds < 0:
        return QuoteQualification(False, REASON_STALE, age_seconds)
    if quote.bid <= 0:
        return QuoteQualification(False, REASON_ZERO_BID, age_seconds)
    if quote.ask < quote.bid:
        return QuoteQualification(False, REASON_CROSSED, age_seconds)
    mid = (quote.ask + quote.bid) / Decimal(2)
    spread_bps = (quote.ask - quote.bid) / mid * Decimal(10000)
    if spread_bps > config.max_spread_bps:
        return QuoteQualification(False, REASON_WIDE_SPREAD, age_seconds)
    return QuoteQualification(True, REASON_QUALIFIED, age_seconds, spread_bps)


def simulate_entry(
    quotes_in_time_order: tuple[QuoteObservation, ...],
    *,
    target_day: date,
    inputs_obtained_at: datetime,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> EntrySimulationResult:
    """Simulate the single §10.4 virtual entry attempt.

    The attempt window is DERIVED from the config and ``target_day``
    (09:46:00–09:46:05 ET); no arbitrary window can be passed.  All
    inputs must have been obtained by 09:45:59 ET and the window must
    clear the 45-minutes-before-close entry cutoff.

    1. The first qualifying quote received inside the window sets
       ``L = ceil_to_tick(ask)`` and ``q = position_quantity(L)``.
    2. Wait ≥ 1 s after that quote; within the following 5 s the first NEW
       qualifying quote with ``ask <= L`` and ``ask_size >= q`` fills at ``L``.
    3. No qualifying quote in the window → NO_ATTEMPT (quotes present but
       none qualifying) or MISSED_WINDOW (no quote received at all).
    """
    gate = evaluate_entry_time_gate(
        target_day=target_day,
        inputs_obtained_at=inputs_obtained_at,
        config=config,
    )
    window_start, window_end = attempt_window(target_day, config)

    def _no_attempt(reason: str) -> EntrySimulationResult:
        return EntrySimulationResult(
            result=RESULT_NO_ATTEMPT,
            limit_price=None,
            quantity=None,
            reference_quote_index=None,
            reference_received_at=None,
            fill_quote_index=None,
            fill_received_at=None,
            fill_price=None,
            unfilled_reason=reason,
        )

    if gate == REASON_INPUTS_LATE:
        return _no_attempt(REASON_INPUTS_LATE)
    if gate == REASON_PAST_ENTRY_CUTOFF:
        return _no_attempt(REASON_PAST_ENTRY_CUTOFF)

    window_start_utc = _to_utc(window_start)
    window_end_utc = _to_utc(window_end)
    any_in_window = any(
        window_start_utc <= _to_utc(q.received_at) <= window_end_utc
        for q in quotes_in_time_order
    )

    for idx, quote in enumerate(quotes_in_time_order):
        if not (window_start_utc <= _to_utc(quote.received_at) <= window_end_utc):
            continue
        if not qualify_quote(quote, config).qualifies:
            continue

        limit_price = ceil_to_tick(quote.ask, config)
        quantity = position_quantity(limit_price, config)
        reference_received_at = quote.received_at
        wait_deadline = _to_utc(reference_received_at) + timedelta(
            seconds=float(config.entry_wait_seconds)
        )
        fill_deadline = wait_deadline + timedelta(
            seconds=float(config.entry_fill_window_seconds)
        )

        if quantity < 1:
            return EntrySimulationResult(
                result=RESULT_NO_ATTEMPT,
                limit_price=limit_price,
                quantity=quantity,
                reference_quote_index=idx,
                reference_received_at=reference_received_at,
                fill_quote_index=None,
                fill_received_at=None,
                fill_price=None,
                unfilled_reason="sizing produced q < 1",
            )

        for j in range(idx + 1, len(quotes_in_time_order)):
            later = quotes_in_time_order[j]
            at = _to_utc(later.received_at)
            if at < wait_deadline:
                continue
            if at > fill_deadline:
                break
            if not qualify_quote(later, config).qualifies:
                continue
            if later.ask > limit_price:
                continue
            if later.ask_size < quantity:
                continue
            return EntrySimulationResult(
                result=RESULT_FILLED,
                limit_price=limit_price,
                quantity=quantity,
                reference_quote_index=idx,
                reference_received_at=reference_received_at,
                fill_quote_index=j,
                fill_received_at=later.received_at,
                fill_price=limit_price,
                unfilled_reason="",
            )

        return EntrySimulationResult(
            result=RESULT_UNFILLED,
            limit_price=limit_price,
            quantity=quantity,
            reference_quote_index=idx,
            reference_received_at=reference_received_at,
            fill_quote_index=None,
            fill_received_at=None,
            fill_price=None,
            unfilled_reason=REASON_FILL_WINDOW_ELAPSED,
        )

    if not any_in_window:
        return EntrySimulationResult(
            result=RESULT_MISSED_WINDOW,
            limit_price=None,
            quantity=None,
            reference_quote_index=None,
            reference_received_at=None,
            fill_quote_index=None,
            fill_received_at=None,
            fill_price=None,
            unfilled_reason=REASON_NO_QUALIFYING_QUOTE,
        )
    return _no_attempt(REASON_NO_QUALIFYING_QUOTE)
