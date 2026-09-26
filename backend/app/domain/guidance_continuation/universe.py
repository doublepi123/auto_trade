"""Universe liquidity filters (PREREGISTRATION §10.2).

The history window is exactly the 20 most recent COMPLETE US trading days
strictly before the target day (§10.2 L448), derived from the static
``market_calendar`` / ``holiday_calendar`` — unique and contiguous, with
full-day closures skipped and HALF DAYS COUNTED as complete trading days.
All comparisons are exact ``Decimal`` so the inclusive price band
endpoints compare without float error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from app.core.holiday_calendar import is_market_closed
from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)

RC_OK: Final[str] = "OK"
RC_MISSING_HISTORY: Final[str] = "MISSING_HISTORY"
RC_INVALID_BAR: Final[str] = "INVALID_BAR"
RC_PRICE_BAND: Final[str] = "PRICE_BAND"
RC_ADV_FLOOR: Final[str] = "ADV_FLOOR"
RC_CORPORATE_ACTION: Final[str] = "CORPORATE_ACTION"


def is_trading_day(day: date) -> bool:
    """A complete US trading day: weekday and not a full-day closure.

    Half days ARE complete trading days (§10.2 L448 二十个完整交易日 —
    a scheduled 13:00 close is still a complete session).
    """
    return day.weekday() < 5 and not is_market_closed("US", day)


def expected_history_days(
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> tuple[date, ...]:
    """The N most recent complete US trading days strictly before ``target_day``.

    Returned newest-first (index 0 = T−1), unique and contiguous per the
    holiday calendar; half days included.  The entry RVOL15 history uses
    the SAME day set.
    """
    days: list[date] = []
    day = target_day - timedelta(days=1)
    while len(days) < config.adv_lookback_days:
        if day.weekday() < 5 and not is_market_closed("US", day):
            days.append(day)
        day -= timedelta(days=1)
    return tuple(days)


@dataclass(frozen=True, slots=True)
class PriorDayBar:
    """One complete prior daily bar.  ``day`` is the US trading day."""

    day: date
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class UniverseVerdict:
    eligible: bool
    reason_code: str
    detail: str = ""


def evaluate_universe(
    *,
    bars: tuple[PriorDayBar, ...],
    has_split_or_unit_change: bool,
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> UniverseVerdict:
    """Evaluate the §10.2 liquidity filters over the exact 20 prior days.

    ``bars`` must match EXACTLY the day set from :func:`expected_history_days`
    for ``target_day``: same count, same unique days, no gaps.  20 copies
    of one day, a holiday, or a dropped day are all MISSING_HISTORY.
    """
    if has_split_or_unit_change:
        return UniverseVerdict(
            False,
            RC_CORPORATE_ACTION,
            "split, reverse split or unreliable price/volume unit change",
        )

    expected = expected_history_days(target_day, config)
    required = config.adv_lookback_days
    got_days = [b.day for b in bars]
    if len(bars) != required or set(got_days) != set(expected):
        return UniverseVerdict(
            False,
            RC_MISSING_HISTORY,
            f"need exactly the {required} complete prior trading days "
            f"{expected[0]}..{expected[-1]}, got {len(bars)} bars over "
            f"{len(set(got_days))} distinct days",
        )

    for bar in bars:
        if (
            not bar.close.is_finite()
            or bar.close <= 0
            or not isinstance(bar.volume, int)
            or isinstance(bar.volume, bool)
            or bar.volume <= 0
        ):
            return UniverseVerdict(
                False,
                RC_INVALID_BAR,
                f"invalid close/volume on {bar.day.isoformat()}",
            )

    by_day = {b.day: b for b in bars}
    tminus1 = by_day[expected[0]].close
    if not (
        config.tminus1_close_min_usd
        <= tminus1
        <= config.tminus1_close_max_usd
    ):
        return UniverseVerdict(
            False,
            RC_PRICE_BAND,
            f"T-1 close {tminus1} outside ["
            f"{config.tminus1_close_min_usd}, {config.tminus1_close_max_usd}]",
        )

    mean_turnover = sum(
        (b.close * Decimal(b.volume) for b in bars), Decimal(0)
    ) / Decimal(len(bars))
    if mean_turnover < config.adv_min_avg_daily_turnover_usd:
        return UniverseVerdict(
            False,
            RC_ADV_FLOOR,
            f"mean daily turnover {mean_turnover} below floor",
        )

    return UniverseVerdict(True, RC_OK)
