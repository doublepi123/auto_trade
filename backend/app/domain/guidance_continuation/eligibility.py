"""Guidance-raise eligibility (PREREGISTRATION §10.3).

Pure evaluation of one newly published guidance statement against the most
recent prior guidance statement for the same fiscal year — never an older,
lower one.

Wall-clock semantics (all America/New_York):
  * announcement window = (previous US trading-day close, target 09:00 ET]
    — the §10.3 text says "after the previous close, up to 09:00 ET,
    right-inclusive", so a publish instant exactly at the previous close is
    OUT and exactly at 09:00:00 is IN;
  * registration deadline 09:25 ET — for BOTH the new statement and the
    prior, all three evidence timestamps (``first_observed_at`` /
    source obtained, ``transcription_reviewed_at``, ``registered_at``)
    must be STRICTLY before it;
  * the 120-natural-day lookback is counted on the America/New_York LOCAL
    calendar (date difference), boundary-inclusive — never as
    120×24 h UTC arithmetic, which breaks across DST transitions.

Prior selection (§10.3 L487): the prior is the most recent statement for
(symbol, fiscal_year) published before the new one — INCLUDING
incomparable ones (different metric/currency/kind) and ones not yet
visible.  If that statement is not simultaneously visible before the
cutoff, comparable and a valid GUIDANCE, the event is INELIGIBLE
(``PRIOR_HISTORY_INCOMPLETE`` / ``PRIOR_NOT_COMPARABLE`` /
``PRIOR_WITHDRAWN`` / ``INVALID_VALUES``); there is NEVER a fallback to an
older statement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
_US = "US"

GUIDANCE_KIND: Final[str] = "GUIDANCE"
WITHDRAWN_KIND: Final[str] = "WITHDRAWN"
_VALID_KINDS: Final[frozenset[str]] = frozenset({GUIDANCE_KIND, WITHDRAWN_KIND})


@dataclass(frozen=True, slots=True)
class GuidanceStatement:
    """One transcribed guidance statement from a quarterly earnings release.

    ``low``/``high`` are the full-year revenue guidance bounds in USD;
    ``low == high`` is allowed (single-point guidance).  ``kind`` is
    ``GUIDANCE`` or ``WITHDRAWN``.  Three evidence timestamps (all
    timezone-aware instants supplied by the collection service):
    ``first_observed_at`` (source obtained), ``transcription_reviewed_at``
    and ``registered_at`` — all three must be strictly before the target
    day's 09:25 ET deadline for the statement to be usable.
    """

    symbol: str
    fiscal_year: int
    metric: str
    currency: str
    low: Decimal
    high: Decimal
    kind: str
    source_published_at: datetime
    first_observed_at: datetime
    transcription_reviewed_at: datetime
    registered_at: datetime
    source_sha256: str


@dataclass(frozen=True, slots=True)
class GuidanceRaiseVerdict:
    """Outcome of :func:`evaluate_guidance_raise` with a reason code."""

    eligible: bool
    reason_code: str
    detail: str = ""


# Reason codes (frozen vocabulary; do not renumber semantics).
RC_CURRENCY_MISMATCH: Final[str] = "CURRENCY_MISMATCH"
RC_METRIC_MISMATCH: Final[str] = "METRIC_MISMATCH"
RC_FISCAL_YEAR_MISMATCH: Final[str] = "FISCAL_YEAR_MISMATCH"
RC_INVALID_VALUES: Final[str] = "INVALID_VALUES"
RC_INVALID_KIND: Final[str] = "INVALID_KIND"
RC_CONFLICTING_RECORDS: Final[str] = "CONFLICTING_RECORDS"
RC_NO_PRIOR_GUIDANCE: Final[str] = "NO_PRIOR_GUIDANCE"
RC_PRIOR_WITHDRAWN: Final[str] = "PRIOR_WITHDRAWN"
RC_PRIOR_NOT_COMPARABLE: Final[str] = "PRIOR_NOT_COMPARABLE"
RC_PRIOR_HISTORY_INCOMPLETE: Final[str] = "PRIOR_HISTORY_INCOMPLETE"
RC_PRIOR_INVALID_VALUES: Final[str] = "PRIOR_INVALID_VALUES"
RC_LOWER_BOUND: Final[str] = "LOWER_BOUND"
RC_INSUFFICIENT_RAISE: Final[str] = "INSUFFICIENT_RAISE"
RC_OUTSIDE_ANNOUNCEMENT_WINDOW: Final[str] = "OUTSIDE_ANNOUNCEMENT_WINDOW"
RC_LATE_REGISTRATION: Final[str] = "LATE_REGISTRATION"
RC_ELIGIBLE: Final[str] = "ELIGIBLE"


def _to_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None:
        raise ValueError("guidance timestamps must be timezone-aware")
    return instant.astimezone(_UTC)


def _et_local(instant: datetime) -> datetime:
    return _to_utc(instant).astimezone(_ET)


def previous_trading_day_close(target_day: date) -> datetime:
    """Close instant of the last complete US trading day before ``target_day``.

    Uses the static holiday calendar, half days included: a half day's close
    is 13:00 ET per ``market_calendar.MarketSession.close_time``.
    """
    session = get_session(_US)
    day = target_day - timedelta(days=1)
    while day.weekday() >= 5 or is_market_closed(_US, day):
        day -= timedelta(days=1)
    return datetime.combine(day, session.close_time(day), tzinfo=_ET)


def announcement_window(target_day: date) -> tuple[datetime, datetime]:
    """(previous close, 09:00 ET] as an explicit pair.

    ``start`` is EXCLUSIVE, ``end`` is INCLUSIVE (§10.3 right-inclusive).
    """
    end = datetime.combine(
        target_day, time(9, 0), tzinfo=_ET
    )
    return previous_trading_day_close(target_day), end


def registration_deadline(target_day: date) -> datetime:
    """09:25 ET on the target trading day."""
    return datetime.combine(target_day, time(9, 25), tzinfo=_ET)


def _is_finite_positive(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _values_valid(statement: GuidanceStatement) -> bool:
    if statement.kind == WITHDRAWN_KIND:
        return True  # bounds are not comparable for withdrawals
    return (
        _is_finite_positive(statement.low)
        and _is_finite_positive(statement.high)
        and statement.low <= statement.high
    )


def _kind_valid(kind: str) -> bool:
    return kind in _VALID_KINDS


def natural_days_between(earlier: datetime, later: datetime) -> int:
    """Calendar-day difference on the America/New_York LOCAL dates.

    §10.3 L476 counts 自然日 (natural days) on the local calendar, so a
    120-day lookback spans the same wall-clock dates regardless of DST
    transitions.  Both instants are converted to ET local time first; the
    result is inclusive-compatible (``<= lookback`` tests use it directly).
    """
    earlier_date = _et_local(earlier).date()
    later_date = _et_local(later).date()
    return (later_date - earlier_date).days


def _fully_registered(
    statement: GuidanceStatement, target_day: date
) -> bool:
    """All three evidence timestamps strictly before 09:25 ET."""
    deadline = registration_deadline(target_day)
    return all(
        _to_utc(ts) < deadline
        for ts in (
            statement.first_observed_at,
            statement.transcription_reviewed_at,
            statement.registered_at,
        )
    )


def _select_latest_prior(
    new: GuidanceStatement,
    prior_statements: tuple[GuidanceStatement, ...],
) -> GuidanceStatement | None:
    """The most recent statement for (symbol, FY) published before ``new``.

    §10.3 L487: includes incomparable and not-yet-visible statements; the
    selection ONLY looks at ``source_published_at`` (and the symbol/FY
    identity), never at metric/currency/visibility — those are judged
    AFTER selection so an unusable latest prior blocks instead of allowing
    a fallback to an older, lower one.  Conflicts and deduplication are
    resolved by :func:`_prior_conflict` / ``_KEY_FIELDS``.
    """
    published = _to_utc(new.source_published_at)
    candidates = [
        s
        for s in prior_statements
        if s.symbol == new.symbol
        and s.fiscal_year == new.fiscal_year
        and _to_utc(s.source_published_at) < published
    ]
    if not candidates:
        return None
    latest_time = max(_to_utc(s.source_published_at) for s in candidates)
    latest = [s for s in candidates if _to_utc(s.source_published_at) == latest_time]
    if _prior_conflict(new, prior_statements):
        return None  # ambiguous; caller reports CONFLICTING_RECORDS
    return latest[0]


#: Every decision-relevant field of a GuidanceStatement.  Two records
#: tied as "most recent" must agree on ALL of these to count as one
#: record (exact duplicates deduplicate); ANY difference is a conflict.
#: Identical ``source_sha256`` alone is NOT proof of an identical
#: transcription — two transcriptions of the same source document can
#: differ (R1).
_KEY_FIELDS: Final[tuple[str, ...]] = (
    "kind",
    "metric",
    "currency",
    "low",
    "high",
    "fiscal_year",
    "first_observed_at",
    "transcription_reviewed_at",
    "registered_at",
    "source_sha256",
)


def _identity(statement: GuidanceStatement) -> tuple[object, ...]:
    """Order-independent identity over the decision-relevant fields."""
    return tuple(
        _to_utc(getattr(statement, f))
        if f.endswith("_at")
        else getattr(statement, f)
        for f in _KEY_FIELDS
    )


def _prior_conflict(
    new: GuidanceStatement,
    prior_statements: tuple[GuidanceStatement, ...],
) -> bool:
    """Whether the most-recent tie contains two genuinely DIFFERENT records.

    Exact duplicates (identical on every ``_KEY_FIELDS`` value) are
    deduplicated; any remaining difference among the tied statements is a
    conflict.  Selection never depends on input order.
    """
    published = _to_utc(new.source_published_at)
    candidates = [
        s
        for s in prior_statements
        if s.symbol == new.symbol
        and s.fiscal_year == new.fiscal_year
        and _to_utc(s.source_published_at) < published
    ]
    if not candidates:
        return False
    latest_time = max(_to_utc(s.source_published_at) for s in candidates)
    latest = [s for s in candidates if _to_utc(s.source_published_at) == latest_time]
    identities = {_identity(s) for s in latest}
    return len(identities) > 1


def midpoint_raise(new: GuidanceStatement, prior: GuidanceStatement) -> Decimal:
    """((L1+U1)/(L0+U0) − 1), exact Decimal arithmetic."""
    return (new.low + new.high) / (prior.low + prior.high) - Decimal(1)


def evaluate_guidance_raise(
    new: GuidanceStatement,
    prior_statements: tuple[GuidanceStatement, ...],
    *,
    target_day: date,
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> GuidanceRaiseVerdict:
    """Evaluate §10.3 eligibility of ``new`` on ``target_day``.

    Checks run in a fixed order so a single reason code is deterministic.
    """
    # Field compatibility of the NEW statement.
    if new.currency != config.guidance_currency:
        return GuidanceRaiseVerdict(
            False, RC_CURRENCY_MISMATCH, f"currency={new.currency!r}"
        )
    if new.metric != config.guidance_metric:
        return GuidanceRaiseVerdict(
            False, RC_METRIC_MISMATCH, f"metric={new.metric!r}"
        )

    # Registration deadline: all three timestamps strictly before 09:25 ET.
    if not _fully_registered(new, target_day):
        return GuidanceRaiseVerdict(
            False,
            RC_LATE_REGISTRATION,
            "first_observed_at / transcription_reviewed_at / registered_at "
            "not all strictly before 09:25 ET",
        )

    # Announcement window (previous close, 09:00 ET].
    window_start, window_end = announcement_window(target_day)
    published = _to_utc(new.source_published_at)
    if not (window_start < published <= window_end):
        return GuidanceRaiseVerdict(
            False,
            RC_OUTSIDE_ANNOUNCEMENT_WINDOW,
            f"published_at={published.isoformat()} outside "
            f"({window_start.isoformat()}, {window_end.isoformat()}]",
        )

    # The NEW statement must itself be a valid GUIDANCE (B1).
    if not _kind_valid(new.kind):
        return GuidanceRaiseVerdict(
            False,
            RC_INVALID_KIND,
            f"new statement kind={new.kind!r} is not GUIDANCE/WITHDRAWN",
        )
    if new.kind != GUIDANCE_KIND:
        return GuidanceRaiseVerdict(
            False,
            RC_INVALID_KIND,
            "new statement is not a GUIDANCE (e.g. WITHDRAWN)",
        )
    if not _values_valid(new):
        return GuidanceRaiseVerdict(
            False, RC_INVALID_VALUES, "non-finite/non-positive bounds or low > high"
        )

    # Prior selection over ALL statements (visibility NOT filtered).
    if _prior_conflict(new, prior_statements):
        return GuidanceRaiseVerdict(
            False,
            RC_CONFLICTING_RECORDS,
            "conflicting distinct prior records share the latest publish "
            "instant",
        )
    prior = _select_latest_prior(new, prior_statements)
    if prior is None:
        return GuidanceRaiseVerdict(
            False,
            RC_NO_PRIOR_GUIDANCE,
            "no statement for (symbol, fiscal_year) published before the "
            "new one",
        )

    # Local-calendar natural-day lookback (B7).
    elapsed_days = natural_days_between(prior.source_published_at, published)
    if elapsed_days > config.prior_guidance_lookback_days:
        return GuidanceRaiseVerdict(
            False,
            RC_NO_PRIOR_GUIDANCE,
            f"latest prior is {elapsed_days} local calendar days before the "
            f"new statement; lookback is "
            f"{config.prior_guidance_lookback_days}",
        )

    # The latest prior must be fully usable, or the event is ineligible —
    # NEVER fall back to an older statement (B2).
    if not _kind_valid(prior.kind) or prior.kind != GUIDANCE_KIND:
        if not _kind_valid(prior.kind):
            return GuidanceRaiseVerdict(
                False, RC_PRIOR_NOT_COMPARABLE, f"prior kind={prior.kind!r} unknown"
            )
        return GuidanceRaiseVerdict(
            False,
            RC_PRIOR_WITHDRAWN,
            "most recent prior is WITHDRAWN (withdrawn-then-reinstated case)",
        )
    if not _fully_registered(prior, target_day):
        return GuidanceRaiseVerdict(
            False,
            RC_PRIOR_HISTORY_INCOMPLETE,
            "most recent prior was not fully obtained/reviewed/registered "
            "before the 09:25 ET cutoff",
        )
    if (
        prior.metric != config.guidance_metric
        or prior.currency != config.guidance_currency
    ):
        return GuidanceRaiseVerdict(
            False,
            RC_PRIOR_NOT_COMPARABLE,
            "most recent prior differs in metric or currency",
        )
    if not _values_valid(prior):
        return GuidanceRaiseVerdict(
            False,
            RC_PRIOR_INVALID_VALUES,
            "prior bounds are non-finite/non-positive or low > high",
        )

    if new.fiscal_year != prior.fiscal_year:  # pragma: no cover - filtered earlier
        return GuidanceRaiseVerdict(
            False, RC_FISCAL_YEAR_MISMATCH, "prior fiscal year differs"
        )

    if new.low < prior.low or new.high < prior.high:
        return GuidanceRaiseVerdict(
            False,
            RC_LOWER_BOUND,
            "L1 < L0 or U1 < U0",
        )

    raise_pct = midpoint_raise(new, prior)
    if raise_pct < config.min_midpoint_raise:
        return GuidanceRaiseVerdict(
            False,
            RC_INSUFFICIENT_RAISE,
            f"midpoint raise {raise_pct} below {config.min_midpoint_raise}",
        )

    return GuidanceRaiseVerdict(True, RC_ELIGIBLE, "prior midpoint raise >= 2%")
