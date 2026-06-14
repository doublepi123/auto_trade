"""Static exchange holiday calendars (NYSE + HKEX) for years 2024-2027.

This module is a deliberately compact, dependency-free holiday calendar
covering the dates we care about for risk accounting and RTH checks. It
intentionally avoids pulling in a large third-party library (e.g.
``exchange_calendars``) for the handful of fixed dates per year.

Sources of truth:
  * NYSE: https://www.nyse.com/markets/hours-calendars
  * HKEX: https://www.hkex.com.hk/-/media/HKEX-Market/Services/Trading-Calendar

Half-day sessions (e.g. Christmas Eve, NYE on HKEX) are recorded separately
so the calendar can still mark the day as "open" while flagging reduced
hours — the trading engine does not currently adjust for half-day closes,
so this metadata is exported but not enforced.

The data is intentionally a flat tuple of dates for fast O(1) lookup. We
do not attempt to handle ad-hoc closures (weather, technical outages) — the
runner already detects quote silence and auto-pauses.

Coverage: 2024-01-01 through 2027-12-31 (inclusive). Beyond that range
``is_market_closed`` returns ``False`` (i.e. *not* closed) — callers that
need a hard "no data" signal should consult :data:`COVERAGE_END_YEAR` or
check ``closure_label(...)`` which returns ``None`` for out-of-range days.
"""

from __future__ import annotations

from datetime import date

# Full-day closures. Each tuple: (date, market, label).
_FULL_DAY_CLOSURES: tuple[tuple[date, str, str], ...] = (
    # ---- NYSE 2024 ----
    (date(2024, 1, 1), "US", "New Year's Day"),
    (date(2024, 1, 15), "US", "Martin Luther King Jr. Day"),
    (date(2024, 2, 19), "US", "Presidents' Day"),
    (date(2024, 3, 29), "US", "Good Friday"),
    (date(2024, 5, 27), "US", "Memorial Day"),
    (date(2024, 6, 19), "US", "Juneteenth"),
    (date(2024, 7, 4), "US", "Independence Day"),
    (date(2024, 9, 2), "US", "Labor Day"),
    (date(2024, 11, 28), "US", "Thanksgiving Day"),
    (date(2024, 12, 25), "US", "Christmas Day"),
    # ---- NYSE 2025 ----
    (date(2025, 1, 1), "US", "New Year's Day"),
    (date(2025, 1, 20), "US", "Martin Luther King Jr. Day"),
    (date(2025, 2, 17), "US", "Presidents' Day"),
    (date(2025, 4, 18), "US", "Good Friday"),
    (date(2025, 5, 26), "US", "Memorial Day"),
    (date(2025, 6, 19), "US", "Juneteenth"),
    (date(2025, 7, 4), "US", "Independence Day"),
    (date(2025, 9, 1), "US", "Labor Day"),
    (date(2025, 11, 27), "US", "Thanksgiving Day"),
    (date(2025, 12, 25), "US", "Christmas Day"),
    # ---- NYSE 2026 ----
    (date(2026, 1, 1), "US", "New Year's Day"),
    (date(2026, 1, 19), "US", "Martin Luther King Jr. Day"),
    (date(2026, 2, 16), "US", "Presidents' Day"),
    (date(2026, 4, 3), "US", "Good Friday"),
    (date(2026, 5, 25), "US", "Memorial Day"),
    (date(2026, 6, 19), "US", "Juneteenth"),
    (date(2026, 7, 3), "US", "Independence Day (observed)"),
    (date(2026, 9, 7), "US", "Labor Day"),
    (date(2026, 11, 26), "US", "Thanksgiving Day"),
    (date(2026, 12, 25), "US", "Christmas Day"),
    # ---- HKEX 2024 ----
    (date(2024, 1, 1), "HK", "New Year's Day"),
    (date(2024, 2, 9), "HK", "Lunar New Year"),
    (date(2024, 2, 12), "HK", "Lunar New Year"),
    (date(2024, 2, 13), "HK", "Lunar New Year"),
    (date(2024, 3, 29), "HK", "Good Friday"),
    (date(2024, 4, 1), "HK", "Easter Monday"),
    (date(2024, 4, 4), "HK", "Ching Ming Festival"),
    (date(2024, 5, 1), "HK", "Labour Day"),
    (date(2024, 5, 15), "HK", "Birthday of the Buddha"),
    (date(2024, 6, 10), "HK", "Tuen Ng Festival"),
    (date(2024, 7, 1), "HK", "HKSAR Establishment Day"),
    (date(2024, 9, 17), "HK", "Day after Mid-Autumn Festival"),
    (date(2024, 10, 1), "HK", "National Day"),
    (date(2024, 10, 11), "HK", "Chung Yeung Festival"),
    (date(2024, 12, 25), "HK", "Christmas Day"),
    (date(2024, 12, 26), "HK", "Boxing Day"),
    # ---- HKEX 2025 ----
    (date(2025, 1, 1), "HK", "New Year's Day"),
    (date(2025, 1, 29), "HK", "Lunar New Year"),
    (date(2025, 1, 30), "HK", "Lunar New Year"),
    (date(2025, 1, 31), "HK", "Lunar New Year"),
    (date(2025, 4, 4), "HK", "Ching Ming Festival"),
    (date(2025, 4, 18), "HK", "Good Friday"),
    (date(2025, 4, 21), "HK", "Easter Monday"),
    (date(2025, 5, 1), "HK", "Labour Day"),
    (date(2025, 5, 5), "HK", "Birthday of the Buddha"),
    (date(2025, 5, 31), "HK", "Tuen Ng Festival"),
    (date(2025, 7, 1), "HK", "HKSAR Establishment Day"),
    (date(2025, 10, 1), "HK", "National Day"),
    (date(2025, 10, 7), "HK", "Day after Mid-Autumn Festival"),
    (date(2025, 10, 29), "HK", "Chung Yeung Festival"),
    (date(2025, 12, 25), "HK", "Christmas Day"),
    (date(2025, 12, 26), "HK", "Boxing Day"),
    # ---- HKEX 2026 ----
    (date(2026, 1, 1), "HK", "New Year's Day"),
    (date(2026, 2, 17), "HK", "Lunar New Year"),
    (date(2026, 2, 18), "HK", "Lunar New Year"),
    (date(2026, 2, 19), "HK", "Lunar New Year"),
    (date(2026, 4, 3), "HK", "Good Friday"),
    (date(2026, 4, 6), "HK", "Easter Monday"),
    (date(2026, 4, 7), "HK", "Day after Ching Ming Festival"),
    (date(2026, 5, 1), "HK", "Labour Day"),
    (date(2026, 5, 25), "HK", "Birthday of the Buddha"),
    (date(2026, 6, 19), "HK", "Tuen Ng Festival"),
    (date(2026, 7, 1), "HK", "HKSAR Establishment Day"),
    (date(2026, 9, 26), "HK", "Day after Mid-Autumn Festival"),
    (date(2026, 10, 1), "HK", "National Day"),
    (date(2026, 10, 19), "HK", "Chung Yeung Festival"),
    (date(2026, 12, 25), "HK", "Christmas Day"),
    (date(2026, 12, 26), "HK", "Boxing Day"),
    # ---- NYSE 2027 ----
    (date(2027, 1, 1), "US", "New Year's Day"),
    (date(2027, 1, 18), "US", "Martin Luther King Jr. Day"),
    (date(2027, 2, 15), "US", "Presidents' Day"),
    (date(2027, 3, 26), "US", "Good Friday"),
    (date(2027, 5, 31), "US", "Memorial Day"),
    (date(2027, 6, 18), "US", "Juneteenth (observed)"),
    (date(2027, 7, 5), "US", "Independence Day (observed)"),
    (date(2027, 9, 6), "US", "Labor Day"),
    (date(2027, 11, 25), "US", "Thanksgiving Day"),
    (date(2027, 12, 24), "US", "Christmas Day (observed)"),
    # ---- HKEX 2027 ----
    # Lunar New Year 2027 falls on Feb 6 (Sat) — HKEX observes Feb 8-10 (Mon-Wed).
    (date(2027, 1, 1), "HK", "New Year's Day"),
    (date(2027, 2, 8), "HK", "Lunar New Year"),
    (date(2027, 2, 9), "HK", "Lunar New Year"),
    (date(2027, 2, 10), "HK", "Lunar New Year"),
    (date(2027, 3, 26), "HK", "Good Friday"),
    (date(2027, 3, 29), "HK", "Easter Monday"),
    (date(2027, 4, 5), "HK", "Ching Ming Festival"),
    (date(2027, 5, 1), "HK", "Labour Day"),
    (date(2027, 5, 13), "HK", "Birthday of the Buddha"),
    (date(2027, 6, 9), "HK", "Tuen Ng Festival"),
    (date(2027, 7, 1), "HK", "HKSAR Establishment Day"),
    (date(2027, 9, 16), "HK", "Day after Mid-Autumn Festival"),
    (date(2027, 10, 1), "HK", "National Day"),
    (date(2027, 10, 7), "HK", "Chung Yeung Festival"),
    (date(2027, 12, 24), "HK", "Christmas Eve (observed)"),
    (date(2027, 12, 27), "HK", "Christmas Day (observed)"),
)

#: Earliest year covered by the static closure data above. The API and
#: ``is_market_closed`` return safe fallbacks for dates outside this range.
COVERAGE_START_YEAR: int = 2024
#: Latest year covered by the static closure data above. The API and
#: ``is_market_closed`` return safe fallbacks for dates outside this range.
COVERAGE_END_YEAR: int = 2027

# Half-day sessions: market closes at lunch on these days. The trading
# engine does not currently adjust execution, so this set is informational
# (exposed via /api/calendar) and can be used by a future iteration to
# restrict RTH windows.
_HALF_DAY_SESSIONS: tuple[tuple[date, str, str], ...] = (
    (date(2024, 12, 24), "HK", "Christmas Eve"),
    (date(2024, 12, 31), "HK", "New Year's Eve"),
    (date(2025, 12, 24), "HK", "Christmas Eve"),
    (date(2025, 12, 31), "HK", "New Year's Eve"),
    (date(2026, 12, 24), "HK", "Christmas Eve"),
    (date(2026, 12, 31), "HK", "New Year's Eve"),
    # NYSE: day before Independence Day, Thanksgiving, Christmas (varies)
    (date(2024, 7, 3), "US", "Day before Independence Day"),
    (date(2024, 11, 29), "US", "Black Friday (early close 13:00)"),
    (date(2024, 12, 24), "US", "Christmas Eve (early close 13:00)"),
    (date(2025, 7, 3), "US", "Day before Independence Day"),
    (date(2025, 11, 28), "US", "Black Friday (early close 13:00)"),
    (date(2025, 12, 24), "US", "Christmas Eve (early close 13:00)"),
    (date(2026, 11, 27), "US", "Black Friday (early close 13:00)"),
    (date(2026, 12, 24), "US", "Christmas Eve (early close 13:00)"),
)


# Lookup indices built lazily on first access.
_CLOSURE_INDEX: dict[tuple[date, str], str] | None = None
_HALF_DAY_INDEX: dict[tuple[date, str], str] | None = None


def _build_indices() -> None:
    global _CLOSURE_INDEX, _HALF_DAY_INDEX
    if _CLOSURE_INDEX is None:
        _CLOSURE_INDEX = {(d, m): label for d, m, label in _FULL_DAY_CLOSURES}
    if _HALF_DAY_INDEX is None:
        _HALF_DAY_INDEX = {(d, m): label for d, m, label in _HALF_DAY_SESSIONS}


def is_market_closed(market: str, day: date) -> bool:
    """True if the given exchange is fully closed on ``day`` (full-day closure)."""
    _build_indices()
    assert _CLOSURE_INDEX is not None
    return (day, (market or "US").upper()) in _CLOSURE_INDEX


def is_half_day(market: str, day: date) -> bool:
    """True if the given exchange has a half-day session on ``day``."""
    _build_indices()
    assert _HALF_DAY_INDEX is not None
    return (day, (market or "US").upper()) in _HALF_DAY_INDEX


def closure_label(market: str, day: date) -> str | None:
    """Return the human-readable closure reason, or None if not a closure."""
    _build_indices()
    assert _CLOSURE_INDEX is not None
    return _CLOSURE_INDEX.get((day, (market or "US").upper()))


def list_closures(market: str, year: int) -> list[dict[str, str]]:
    """Return all closures for ``market`` in ``year``, sorted by date."""
    m = (market or "US").upper()
    out: list[dict[str, str]] = []
    for d, mk, label in _FULL_DAY_CLOSURES:
        if mk == m and d.year == year:
            out.append({"date": d.isoformat(), "market": mk, "label": label})
    out.sort(key=lambda e: e["date"])
    return out
