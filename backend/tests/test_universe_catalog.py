from __future__ import annotations

from app.domain.universe_selection.catalog import (
    CATALOG_SOURCE_VERSION,
    HISTORICAL_INDEX_CANDIDATE_CATALOG,
    INDEX_CANDIDATE_CATALOG,
    ROTATION_RESEARCH_CANDIDATE_CATALOG,
)


def test_catalog_tracks_current_verified_index_snapshot() -> None:
    by_symbol = {
        candidate.symbol: candidate
        for candidate in INDEX_CANDIDATE_CATALOG
    }

    assert len(by_symbol) == len(INDEX_CANDIDATE_CATALOG)
    assert len(by_symbol) == 123
    assert CATALOG_SOURCE_VERSION == (
        "nasdaq-100-2026-07-24_djia-2026-06-29_historical-pit-v9"
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
        "ASML.US",
        "STX.US",
        "IBM.US",
        "UNH.US",
        "ADSK.US",
        "CCEP.US",
        "CSX.US",
        "EA.US",
        "GEHC.US",
        "MCHP.US",
        "MNST.US",
        "MPWR.US",
        "NXPI.US",
        "PYPL.US",
        "REGN.US",
        "ROST.US",
        "SBUX.US",
        "XEL.US",
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
    } <= by_symbol.keys()
    assert sum(
        "NASDAQ_100" in candidate.memberships
        for candidate in by_symbol.values()
    ) == 102
    assert sum(
        "DJIA" in candidate.memberships
        for candidate in by_symbol.values()
    ) == 30
    assert "NASDAQ_100" in by_symbol["SPCX.US"].memberships
    assert "NASDAQ_100" in by_symbol["HONA.US"].memberships
    assert "DJIA" in by_symbol["GOOGL.US"].memberships
    assert {"NASDAQ_100", "DJIA"} <= set(
        by_symbol["AMGN.US"].memberships
    )
    assert {"NASDAQ_100", "DJIA"} <= set(
        by_symbol["CSCO.US"].memberships
    )
    assert {"NASDAQ_100", "DJIA"} <= set(
        by_symbol["HON.US"].memberships
    )
    assert {"NASDAQ_100", "DJIA"} <= set(
        by_symbol["WMT.US"].memberships
    )
    assert "VZ.US" not in by_symbol
    assert "GOOG.US" not in by_symbol


def test_rotation_research_catalog_adds_only_former_constituents() -> None:
    live_symbols = {
        candidate.symbol for candidate in INDEX_CANDIDATE_CATALOG
    }
    historical_symbols = {
        candidate.symbol
        for candidate in HISTORICAL_INDEX_CANDIDATE_CATALOG
    }
    research_symbols = {
        candidate.symbol
        for candidate in ROTATION_RESEARCH_CANDIDATE_CATALOG
    }

    assert len(historical_symbols) == 48
    assert not live_symbols & historical_symbols
    assert research_symbols == live_symbols | historical_symbols
    assert len(research_symbols) == 171


def test_catalog_collapses_technology_industries_into_one_risk_group() -> None:
    by_symbol = {
        candidate.symbol: candidate
        for candidate in INDEX_CANDIDATE_CATALOG
    }

    assert {
        by_symbol[symbol].risk_group
        for symbol in (
            "NVDA.US",
            "MSFT.US",
            "IBM.US",
            "CRWV.US",
            "MPWR.US",
            "MSTR.US",
            "SNPS.US",
            "WDAY.US",
        )
    } == {"Information Technology"}
    assert by_symbol["META.US"].risk_group == "Communication Services"
    assert by_symbol["MNST.US"].risk_group == "Consumer Staples"
