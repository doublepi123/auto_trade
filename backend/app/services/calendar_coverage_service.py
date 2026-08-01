"""Read-only market calendar coverage status.

Reports whether the static holiday data in ``app/core/holiday_calendar.py`` is
authoritative for a given "as-of" date. This is **not** a market open/closed
signal — it tells operators whether the calendar's holiday data can be trusted
for the date they care about, so they know when to update the static tables
before a missed closure silently becomes an open day.

Semantic boundary (made explicit in the response):

* ``COVERED``  — the as-of date is within the static coverage window and far
  enough from its end that the data is authoritative.
* ``WARNING`` — the as-of date is within the coverage window but inside the
  warning window (the final ``warning_window_days`` before coverage end). The
  data is still authoritative, but operators should plan a calendar update.
* ``EXPIRED`` — the as-of date is after the coverage end date. The static data
  is no longer authoritative; ``is_market_closed`` silently returns ``False``
  for out-of-range dates, so closures will be missed.

The service is pure and deterministic: it takes an injected ``as_of`` date and
the coverage constants, performs no I/O, and never alters calendar closure
logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final, Literal

from app.core.holiday_calendar import COVERAGE_END_YEAR, COVERAGE_START_YEAR

#: Number of days before the coverage end date during which the status is
#: ``WARNING`` (still authoritative, but an update is due soon).
DEFAULT_WARNING_WINDOW_DAYS: Final[int] = 60


@dataclass(frozen=True)
class CalendarCoverageStatus:
    """Pure coverage status result (no I/O, no secrets)."""

    status: Literal["COVERED", "WARNING", "EXPIRED"]
    authoritative: bool
    coverage_start_year: int
    coverage_end_year: int
    coverage_years: list[int]
    as_of: date
    coverage_end_date: date
    days_until_coverage_end: int
    warning_window_days: int
    message: str


class CalendarCoverageService:
    """Pure read-only calendar coverage projection.

    Constructed with the static coverage constants and a warning window. The
    ``status`` method takes an injected ``as_of`` date so tests are
    deterministic; production callers pass ``date.today()`` or a UTC date.
    """

    def __init__(
        self,
        *,
        coverage_start_year: int = COVERAGE_START_YEAR,
        coverage_end_year: int = COVERAGE_END_YEAR,
        warning_window_days: int = DEFAULT_WARNING_WINDOW_DAYS,
    ) -> None:
        self._start_year = int(coverage_start_year)
        self._end_year = int(coverage_end_year)
        self._warning_window_days = max(0, int(warning_window_days))

    def status(self, as_of: date | None = None) -> CalendarCoverageStatus:
        """Return the coverage status for ``as_of`` (default: today UTC)."""
        if as_of is None:
            as_of = datetime.now(timezone.utc).date()
        coverage_end_date = date(self._end_year, 12, 31)
        coverage_start_date = date(self._start_year, 1, 1)
        days_until_end = (coverage_end_date - as_of).days
        coverage_years = list(range(self._start_year, self._end_year + 1))

        if as_of > coverage_end_date:
            status: Literal["COVERED", "WARNING", "EXPIRED"] = "EXPIRED"
            authoritative = False
            message = (
                f"Static holiday data expired on {coverage_end_date.isoformat()}; "
                "closures after this date are not tracked and will be missed."
            )
        elif as_of < coverage_start_date:
            # Before the coverage window: data is not authoritative for this date.
            status = "EXPIRED"
            authoritative = False
            message = (
                f"As-of date {as_of.isoformat()} is before the coverage window "
                f"starting {coverage_start_date.isoformat()}."
            )
        elif days_until_end <= self._warning_window_days:
            status = "WARNING"
            authoritative = True
            message = (
                f"Holiday data is authoritative but coverage ends in "
                f"{days_until_end} day(s) on {coverage_end_date.isoformat()}; "
                "update the static calendar soon."
            )
        else:
            status = "COVERED"
            authoritative = True
            message = (
                f"Holiday data is authoritative through {coverage_end_date.isoformat()}."
            )

        return CalendarCoverageStatus(
            status=status,
            authoritative=authoritative,
            coverage_start_year=self._start_year,
            coverage_end_year=self._end_year,
            coverage_years=coverage_years,
            as_of=as_of,
            coverage_end_date=coverage_end_date,
            days_until_coverage_end=days_until_end,
            warning_window_days=self._warning_window_days,
            message=message,
        )


def default_coverage_status(as_of: date | None = None) -> CalendarCoverageStatus:
    """Convenience: build a status using the current static constants."""
    return CalendarCoverageService().status(as_of)
