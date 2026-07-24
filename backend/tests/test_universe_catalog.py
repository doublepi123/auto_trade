from __future__ import annotations

from app.domain.universe_selection.catalog import (
    CATALOG_SOURCE_VERSION,
    INDEX_CANDIDATE_CATALOG,
)


def test_catalog_tracks_current_verified_index_snapshot() -> None:
    by_symbol = {
        candidate.symbol: candidate
        for candidate in INDEX_CANDIDATE_CATALOG
    }

    assert len(by_symbol) == len(INDEX_CANDIDATE_CATALOG)
    assert CATALOG_SOURCE_VERSION == (
        "nasdaq-100-2026-07-24_djia-2026-06-29_v2"
    )
    assert {
        "SPCX.US",
        "HONA.US",
        "AMAT.US",
        "KLAC.US",
        "MRVL.US",
        "TXN.US",
        "PANW.US",
        "CRWD.US",
        "APP.US",
        "COST.US",
        "AMGN.US",
        "ISRG.US",
        "CEG.US",
    } <= by_symbol.keys()
    assert "NASDAQ_100" in by_symbol["SPCX.US"].memberships
    assert "NASDAQ_100" in by_symbol["HONA.US"].memberships
    assert "DJIA" in by_symbol["GOOGL.US"].memberships
    assert "VZ.US" not in by_symbol
