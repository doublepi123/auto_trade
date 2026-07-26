from __future__ import annotations

from datetime import date

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
    assert coverage.authoritative_symbols == 121
    assert coverage.snapshot_only_symbols == (
        "HONA.US",
        "SPCX.US",
    )
    assert coverage.missing_symbols == ()
    assert 0.98 < coverage.authoritative_ratio < 0.99
    assert coverage.historical_symbol_count == 169
    assert coverage.historical_symbols_present == 121
    assert len(coverage.historical_symbols_missing) == 48


def test_research_catalog_covers_all_historical_membership_symbols() -> None:
    coverage = INDEX_MEMBERSHIP_HISTORY.coverage(
        ROTATION_RESEARCH_CANDIDATE_CATALOG
    )

    assert coverage.catalog_size == 171
    assert coverage.authoritative_symbols == 169
    assert coverage.snapshot_only_symbols == (
        "HONA.US",
        "SPCX.US",
    )
    assert coverage.missing_symbols == ()
    assert coverage.historical_symbols_present == 169
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


def test_snapshot_only_membership_fails_closed_before_snapshot() -> None:
    hona = _candidate("HONA.US")

    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        hona,
        date(2026, 7, 23),
    ) is False
    assert INDEX_MEMBERSHIP_HISTORY.is_active(
        hona,
        date(2026, 7, 24),
    ) is True
