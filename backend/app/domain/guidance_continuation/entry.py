"""Precise entry conditions over the 15 opening bars (PREREGISTRATION §10.4).

Inputs are the 15 completed 1-minute RTH bars 09:30:00–09:44 (timestamp =
bar start), the T−1 comparable close ``Pprev``, and the prior 20 trading
days' same-window (date, V15) volume pairs for RVOL15.  All price math is
Decimal.

History completeness: the ``(date, v15)`` pairs must cover EXACTLY the
same 20-day set as the universe window (``expected_history_days``), and
every V15 value must be finite and > 0 (§10.4 L506: the denominator is
the median over the prior twenty COMPLETE trading days; §10.2 L452:
所有交易日均具有有效数据).

Bar validity (§10.4 L496-509): every bar needs
``low <= min(open, close)`` and ``max(open, close) <= high``, all OHLC
finite and positive, and volume > 0 — an impossible OHLC cross is not a
bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median
from typing import Final
from zoneinfo import ZoneInfo


from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)
from app.domain.guidance_continuation.universe import expected_history_days

_ET = ZoneInfo("America/New_York")

RC_ENTRY_OK: Final[str] = "OK"
RC_BAR_COUNT: Final[str] = "BAR_COUNT"
RC_DUPLICATE_BAR: Final[str] = "DUPLICATE_BAR"
RC_BAR_OUT_OF_WINDOW: Final[str] = "BAR_OUT_OF_WINDOW"
RC_BAD_VOLUME: Final[str] = "BAD_VOLUME"
RC_BAD_PRICE: Final[str] = "BAD_PRICE"
RC_HISTORY_INCOMPLETE: Final[str] = "RC_HISTORY_INCOMPLETE"
RC_BAD_HISTORY_VALUES: Final[str] = "BAD_HISTORY_VALUES"
RC_NON_POSITIVE_MEDIAN: Final[str] = "NON_POSITIVE_MEDIAN"
RC_GAP_BELOW_MIN: Final[str] = "GAP_BELOW_MIN"
RC_GAP_ABOVE_MAX: Final[str] = "GAP_ABOVE_MAX"
RC_C15_GAIN: Final[str] = "C15_GAIN"
RC_C15_BELOW_VWAP: Final[str] = "C15_BELOW_VWAP"
RC_RVOL_BELOW_MIN: Final[str] = "RVOL_BELOW_MIN"
RC_NON_POSITIVE_PPREV: Final[str] = "NON_POSITIVE_PPREV"


@dataclass(frozen=True, slots=True)
class EntryBar:
    """One completed 1-minute RTH bar; ``ts`` is the bar START time."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class EntryConditions:
    """The four §10.4 conditions, each individually disclosed."""

    gap_in_band: bool
    c15_gain_ok: bool
    c15_ge_vwap: bool
    rvol_ok: bool


@dataclass(frozen=True, slots=True)
class EntryEvaluation:
    decision: str  # "ENTRY" | "NO_ENTRY"
    reason_code: str
    conditions: EntryConditions | None
    o: Decimal | None = None
    c15: Decimal | None = None
    v15: int | None = None
    vwap15: Decimal | None = None
    rvol15: Decimal | None = None

    @property
    def entry(self) -> bool:
        return self.decision == "ENTRY"


DECISION_ENTRY: Final[str] = "ENTRY"
DECISION_NO_ENTRY: Final[str] = "NO_ENTRY"


def expected_bar_starts(
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> tuple[datetime, ...]:
    """The 15 bar-start timestamps 09:30:00..09:44:00 ET, in order."""
    first = datetime.combine(
        target_day,
        time(
            config.entry_window_first_bar_start_et.hour,
            config.entry_window_first_bar_start_et.minute,
        ),
        tzinfo=_ET,
    )
    return tuple(
        first + timedelta(minutes=i) for i in range(config.entry_bar_count)
    )


def _bar_valid(bar: EntryBar) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close)
    if not all(p.is_finite() and p > 0 for p in prices):
        return False
    # OHLC consistency: the bar must be geometrically possible.
    return (
        bar.low <= min(bar.open, bar.close)
        and max(bar.open, bar.close) <= bar.high
    )


def _history_check(
    history_v15: tuple[tuple[date, int], ...],
    target_day: date,
    config: GuidanceContinuationConfig,
) -> str | None:
    """Validate the (trading day, V15) pairs; None when acceptable."""
    expected = set(expected_history_days(target_day, config))
    got = [d for d, _ in history_v15]
    if len(history_v15) != len(expected) or set(got) != expected:
        return RC_HISTORY_INCOMPLETE
    for _, v15 in history_v15:
        if not isinstance(v15, int) or isinstance(v15, bool) or v15 <= 0:
            return RC_BAD_HISTORY_VALUES
    return None


def evaluate_entry(
    *,
    bars: tuple[EntryBar, ...],
    pprev: Decimal,
    history_v15: tuple[tuple[date, int], ...],
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> EntryEvaluation:
    """Compute O, C15, V15, VWAP15, RVOL15 and the four §10.4 conditions.

    ``history_v15`` is (trading day, same-window volume) pairs and must
    cover exactly the 20-day set of ``expected_history_days(target_day)``
    with every value a positive int.
    """
    expected = expected_bar_starts(target_day, config)

    seen: set[datetime] = set()
    for bar in bars:
        if bar.ts in seen:
            return EntryEvaluation(
                DECISION_NO_ENTRY, RC_DUPLICATE_BAR, None
            )
        seen.add(bar.ts)

    by_ts = {bar.ts: bar for bar in bars}
    ordered: list[EntryBar] = []
    for ts in expected:
        bar = by_ts.get(ts)
        if bar is None:
            return EntryEvaluation(DECISION_NO_ENTRY, RC_BAR_COUNT, None)
        ordered.append(bar)
    if len(bars) != len(expected):
        return EntryEvaluation(DECISION_NO_ENTRY, RC_BAR_COUNT, None)
    for bar in bars:
        if bar.ts not in expected:
            return EntryEvaluation(DECISION_NO_ENTRY, RC_BAR_OUT_OF_WINDOW, None)

    for bar in bars:
        if bar.volume <= 0:
            return EntryEvaluation(
                DECISION_NO_ENTRY,
                RC_BAD_VOLUME,
                None,
            )
        if not _bar_valid(bar):
            return EntryEvaluation(DECISION_NO_ENTRY, RC_BAD_PRICE, None)

    if not pprev.is_finite() or pprev <= 0:
        return EntryEvaluation(DECISION_NO_ENTRY, RC_NON_POSITIVE_PPREV, None)

    history_problem = _history_check(history_v15, target_day, config)
    if history_problem == RC_HISTORY_INCOMPLETE:
        return EntryEvaluation(DECISION_NO_ENTRY, RC_HISTORY_INCOMPLETE, None)
    if history_problem == RC_BAD_HISTORY_VALUES:
        return EntryEvaluation(DECISION_NO_ENTRY, RC_BAD_HISTORY_VALUES, None)

    o = ordered[0].open
    c15 = ordered[-1].close
    v15 = sum(bar.volume for bar in ordered)

    hist_median = Decimal(str(median([v for _, v in history_v15])))
    if hist_median <= 0:
        return EntryEvaluation(
            DECISION_NO_ENTRY, RC_NON_POSITIVE_MEDIAN, None
        )
    rvol15 = Decimal(v15) / hist_median

    total_volume = v15
    if total_volume <= 0:
        return EntryEvaluation(DECISION_NO_ENTRY, RC_BAD_VOLUME, None)
    vwap15 = sum(
        (
            (bar.high + bar.low + bar.close) / Decimal(3) * Decimal(bar.volume)
            for bar in ordered
        ),
        Decimal(0),
    ) / Decimal(total_volume)

    gap = o / pprev - Decimal(1)
    c15_gain = c15 / o - Decimal(1)

    conditions = EntryConditions(
        gap_in_band=config.gap_min <= gap <= config.gap_max,
        c15_gain_ok=c15_gain >= config.min_c15_over_o_gain,
        c15_ge_vwap=c15 >= vwap15,
        rvol_ok=rvol15 >= config.rvol15_min,
    )

    if not conditions.gap_in_band:
        reason = (
            RC_GAP_BELOW_MIN
            if gap < config.gap_min
            else RC_GAP_ABOVE_MAX
        )
    elif not conditions.c15_gain_ok:
        reason = RC_C15_GAIN
    elif not conditions.c15_ge_vwap:
        reason = RC_C15_BELOW_VWAP
    elif not conditions.rvol_ok:
        reason = RC_RVOL_BELOW_MIN
    else:
        reason = RC_ENTRY_OK

    decision = (
        DECISION_ENTRY
        if all(
            (
                conditions.gap_in_band,
                conditions.c15_gain_ok,
                conditions.c15_ge_vwap,
                conditions.rvol_ok,
            )
        )
        else DECISION_NO_ENTRY
    )
    return EntryEvaluation(
        decision=decision,
        reason_code=reason,
        conditions=conditions,
        o=o,
        c15=c15,
        v15=v15,
        vwap15=vwap15,
        rvol15=rvol15,
    )
