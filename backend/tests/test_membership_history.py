from __future__ import annotations

from datetime import date

import pytest

from app.domain.universe_selection import (
    INDEX_CANDIDATE_CATALOG,
    INDEX_MEMBERSHIP_HISTORY,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
    IndexCandidate,
)


def _candidate(symbol: str) -> IndexCandidate:
    return next(
        candidate
        for candidate in INDEX_CANDIDATE_CATALOG
        if candidate.symbol == symbol
    )


def test_membership_history_tracks_nasdaq_changes() -> None:
    pltr = _candidate("PLTR.US")

    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        pltr,
        date(2024, 12, 22),
    ) is False
    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        pltr,
        date(2024, 12, 23),
    ) is True


def test_membership_history_tracks_dow_changes() -> None:
    verizon = IndexCandidate(
        symbol="VZ.US",
        alias="Verizon",
        sector="Communication Services",
        memberships=("DJIA",),
    )

    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        verizon,
        date(2026, 6, 28),
    ) is True
    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        verizon,
        date(2026, 6, 29),
    ) is False


def test_membership_history_keeps_yaml_ticker_strings() -> None:
    on_semiconductor = IndexCandidate(
        symbol="ON.US",
        alias="ON Semiconductor",
        sector="Semiconductors",
        memberships=("NASDAQ_100",),
    )

    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        on_semiconductor,
        date(2024, 1, 1),
    ) is True


def test_membership_history_reports_partial_catalog_coverage() -> None:
    coverage = INDEX_MEMBERSHIP_HISTORY.coverage(
        INDEX_CANDIDATE_CATALOG
    )

    assert coverage.catalog_size == 123
    assert coverage.authoritative_symbols == 123
    assert coverage.snapshot_only_symbols == ()
    assert coverage.missing_symbols == ()
    assert coverage.authoritative_ratio == 1.0
    assert coverage.historical_symbol_count == 171
    assert coverage.historical_symbols_present == 123
    assert len(coverage.historical_symbols_missing) == 48


def test_research_catalog_covers_all_historical_membership_symbols() -> None:
    coverage = INDEX_MEMBERSHIP_HISTORY.coverage(
        ROTATION_RESEARCH_CANDIDATE_CATALOG
    )

    assert coverage.catalog_size == 171
    assert coverage.authoritative_symbols == 171
    assert coverage.snapshot_only_symbols == ()
    assert coverage.missing_symbols == ()
    assert coverage.historical_symbols_present == 171
    assert coverage.historical_symbols_missing == ()
    assert coverage.historical_coverage_ratio == 1.0


def test_expanded_candidates_are_active_at_catalog_snapshot() -> None:
    for symbol in (
        "MAR.US",
        "MSTR.US",
        "ORLY.US",
        "PDD.US",
        "SNPS.US",
        "TTWO.US",
        "WBD.US",
        "WDAY.US",
        "LITE.US",
        "SNDK.US",
        "ALNY.US",
        "CPRT.US",
        "CTAS.US",
        "DXCM.US",
        "FAST.US",
        "FER.US",
        "IDXX.US",
        "KDP.US",
        "KHC.US",
        "ODFL.US",
        "PAYX.US",
        "PCAR.US",
        "ROP.US",
        "TRI.US",
    ):
        assert INDEX_MEMBERSHIP_HISTORY.is_active(
            _candidate(symbol),
            date(2026, 7, 24),
        ) is True


@pytest.mark.parametrize("symbol,before,joined", [
    ("HONA.US", date(2026, 6, 28), date(2026, 6, 29)),
    ("SPCX.US", date(2026, 7, 6), date(2026, 7, 7)),
])
def test_membership_history_tracks_real_union_boundaries(
    symbol: str, before: date, joined: date,
) -> None:
    # Real dated unions supersede the former snapshot-only overrides.
    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        _candidate(symbol),
        before,
    ) is False
    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        _candidate(symbol),
        joined,
    ) is True


@pytest.mark.parametrize("symbol,before,removed", [
    ("EA.US", date(2026, 8, 3), date(2026, 8, 4)),
    ("KHC.US", date(2026, 9, 13), date(2026, 9, 14)),
])
def test_membership_history_tracks_2026_removal_boundaries(
    symbol: str, before: date, removed: date,
) -> None:
    assert INDEX_MEMBERSHIP_HISTORY.is_active(_candidate(symbol), before) is True
    assert INDEX_MEMBERSHIP_HISTORY.is_active(_candidate(symbol), removed) is False
