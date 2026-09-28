"""Sealed NYSE-rule trading-calendar derivation, 2010-2021 (PURE).

Decision 14.7 item 5: month-ends and next opens must be validated
against an INDEPENDENT sealed calendar so that dates missing on BOTH
SPY and QQQ are caught (the SPY n QQQ intersection cannot see those).
No offline exchange-calendar package exists in this environment, so
option (a) is registered: a holiday list written from the PUBLIC NYSE
rules, as data plus a derivation, pinned by file hash in the
preregistration contract.

Rules implemented (NYSE published rules, verifiable at
https://www.nyse.com/markets/hours-calendars):

- Saturdays and Sundays are closed.
- New Year's Day (Jan 1), MLK Day (3rd Monday of January), Washington's
  Birthday (3rd Monday of February), Good Friday, Memorial Day (last
  Monday of May), Juneteenth (June 19, OBSERVED from 2022 — the first
  NYSE observance was 2022-06-20), Independence Day (Jul 4), Labor Day
  (1st Monday of September), Thanksgiving (4th Thursday of November),
  Christmas (Dec 25).
- When a fixed-date holiday falls on a Saturday it is observed on the
  preceding Friday; on a Sunday, the following Monday.  (Jul 4 2010
  Sunday -> Monday Jul 5; Dec 25 2010 Saturday -> Friday Dec 24; Jan 1
  2011 Saturday -> Friday Dec 31 2010; Jul 4 2015 Saturday -> Friday
  Jul 3; Dec 25 2021 Saturday -> Friday Dec 24 2021.)
- Good Friday: the Friday before Easter (Gauss/Meeus anonymous
  Gregorian algorithm, pure integer arithmetic).

Known deviations REGISTERED (the NYSE declared these one-off closures /
special days; a pure rules engine cannot derive them, so they are data):

- 2012-10-29/30: Hurricane Sandy closures (two sessions).
- 2018-12-05: closed for President George H. W. Bush's funeral.

Half days are NOT modelled: the replay needs only session DATES (the
open price exists on early-close days), and the contract states half
days do not change this monthly rule.

This module computes; the sealed EXPECTED session list is derived and
can be cross-checked against the sealed SPY/QQQ bars at seal time.
"""

from __future__ import annotations

from datetime import date, timedelta

#: One-off NYSE closures not derivable from the standing rules.
#: Complete list of full-market one-off closures 2010-2026, verified
#: against NYSE press releases (https://www.nyse.com/markets/hours-calendars)
#: and cross-checked with the sealed ORB provider calendar (real bars):
#:
#: - 2012-10-29/30: Hurricane Sandy
#:   (https://www.nyse.com/markets/hours-calendars, historically
#:   nyse.com/press-release/1186248936104.html).
#: - 2018-12-05: national day of mourning for President George H. W.
#:   Bush (nyse.com article 2018-12-01; markets closed).
#: - 2025-01-09: national day of mourning for President Jimmy Carter
#:   (nyse.com/press-release/... 2025-01; confirmed by the sealed ORB
#:   provider calendar: no bars that day).
#:
#: No other full-market one-off closure exists in 2010-2026 (9/11 was
#: 2001; special/limited trading days like 2004-06-11 Reagan or
#: 2007-01-02 Ford fell outside this range).
ONE_OFF_CLOSURES: frozenset[date] = frozenset(
    {
        date(2012, 10, 29),  # Hurricane Sandy
        date(2012, 10, 30),  # Hurricane Sandy
        date(2018, 12, 5),  # G.H.W. Bush national day of mourning
        date(2025, 1, 9),  # Carter national day of mourning
    }
)

#: Juneteenth became a federal/NYSE holiday in 2022 (first observed
#: 2022-06-20, the Monday after the Sunday 19th).  Source:
#: https://www.nyse.com/markets/hours-calendars
JUNETEENTH_FIRST_YEAR = 2022


def easter(year: int) -> date:
    """Easter Sunday (Gregorian), Meeus anonymous algorithm."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """Saturday -> preceding Friday; Sunday -> following Monday."""

    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(
    year: int, month: int, weekday: int, n: int
) -> date:
    """The n-th ``weekday`` (0=Mon) of the month; n=-1 means last."""

    if n > 0:
        day = date(year, month, 1)
        offset = (weekday - day.weekday()) % 7
        return day + timedelta(days=offset + 7 * (n - 1))
    day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    offset = (day.weekday() - weekday) % 7
    return day - timedelta(days=offset)


def nyse_rule_holidays(year: int) -> set[date]:
    """Standing-rule NYSE holidays for ``year`` (observed dates).

    Juneteenth (June 19) is included from 2022 onward (first NYSE
    observance 2022-06-20, the Monday after the Sunday 19th; Sat->Fri,
    Sun->Mon observation thereafter).  Before 2022 it was not an NYSE
    holiday and must NOT be excluded.
    """

    holidays = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK: 3rd Monday of January
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),  # Memorial Day: last Monday of May
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day: 1st Monday
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving: 4th Thursday
        _observed(date(year, 12, 25)),  # Christmas
    }
    if year >= JUNETEENTH_FIRST_YEAR:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def expected_nyse_sessions(
    start: date, end: date
) -> list[date]:
    """The sealed expected NYSE session list for [start, end]."""

    sessions: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            holidays = nyse_rule_holidays(cursor.year)
            if (
                cursor not in holidays
                and cursor not in ONE_OFF_CLOSURES
            ):
                sessions.append(cursor)
        cursor += timedelta(days=1)
    return sessions
