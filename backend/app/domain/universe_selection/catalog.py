from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CATALOG_SOURCE_VERSION = (
    "nasdaq-100-2026-07-24_djia-2026-06-29_historical-pit-v9"
)

_RISK_GROUP_BY_SECTOR = {
    "Semiconductors": "Information Technology",
    "Software": "Information Technology",
    "Technology Hardware": "Information Technology",
    "Technology Infrastructure": "Information Technology",
}


def risk_group_for_sector(sector: str) -> str:
    """Collapse industry labels into broad portfolio-risk groups."""
    normalized = sector.strip()
    return _RISK_GROUP_BY_SECTOR.get(normalized, normalized)


@dataclass(frozen=True)
class IndexCandidate:
    symbol: str
    alias: str
    sector: str
    memberships: tuple[str, ...]
    market: Literal["US", "HK"] = "US"

    @property
    def risk_group(self) -> str:
        return risk_group_for_sector(self.sector)


# This is intentionally a liquid, diversified screening seed rather than a
# hard-coded portfolio. Daily market-data gates decide which names enter the
# observed pool. Membership was verified against Nasdaq's NDX weighting page
# dated 2026-07-24 and S&P DJI's 2026-06-23 announcement for the DJIA changes
# effective 2026-06-29. The DJIA portion is complete. The Nasdaq-100 portion
# includes one security per current constituent company so daily market-data
# gates, rather than a static liquidity snapshot, decide which names remain
# observable. Alphabet is represented by GOOGL only to prevent the two share
# classes from consuming separate sector or observation capacity. The live
# catalog remains company-level; former constituents are isolated below for
# point-in-time research. Daily liquidity, volatility, and cost-opportunity
# gates still fail closed.
INDEX_CANDIDATE_CATALOG: tuple[IndexCandidate, ...] = (
    IndexCandidate("NVDA.US", "NVIDIA", "Semiconductors", ("NASDAQ_100", "DJIA")),
    IndexCandidate("AAPL.US", "Apple", "Technology Hardware", ("NASDAQ_100", "DJIA")),
    IndexCandidate("MSFT.US", "Microsoft", "Software", ("NASDAQ_100", "DJIA")),
    IndexCandidate("AMZN.US", "Amazon", "Consumer Discretionary", ("NASDAQ_100", "DJIA")),
    IndexCandidate("GOOGL.US", "Alphabet Class A", "Communication Services", ("NASDAQ_100", "DJIA")),
    IndexCandidate("META.US", "Meta Platforms", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("TSLA.US", "Tesla", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("AMD.US", "Advanced Micro Devices", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("AVGO.US", "Broadcom", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("MU.US", "Micron Technology", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("PLTR.US", "Palantir Technologies", "Software", ("NASDAQ_100",)),
    IndexCandidate("INTC.US", "Intel", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("NFLX.US", "Netflix", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("QCOM.US", "Qualcomm", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("LRCX.US", "Lam Research", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("AMAT.US", "Applied Materials", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("KLAC.US", "KLA", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("MRVL.US", "Marvell Technology", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("TXN.US", "Texas Instruments", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("PANW.US", "Palo Alto Networks", "Software", ("NASDAQ_100",)),
    IndexCandidate("CRWD.US", "CrowdStrike", "Software", ("NASDAQ_100",)),
    IndexCandidate("APP.US", "AppLovin", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("COST.US", "Costco", "Consumer Staples", ("NASDAQ_100",)),
    IndexCandidate("AMGN.US", "Amgen", "Healthcare", ("NASDAQ_100", "DJIA")),
    IndexCandidate("ISRG.US", "Intuitive Surgical", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("CEG.US", "Constellation Energy", "Utilities", ("NASDAQ_100",)),
    IndexCandidate("SPCX.US", "SpaceX", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("HONA.US", "Honeywell Aerospace", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("ALAB.US", "Astera Labs", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("CRWV.US", "CoreWeave", "Technology Infrastructure", ("NASDAQ_100",)),
    IndexCandidate("NBIS.US", "Nebius Group", "Technology Infrastructure", ("NASDAQ_100",)),
    IndexCandidate("RKLB.US", "Rocket Lab", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("TER.US", "Teradyne", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("JPM.US", "JPMorgan Chase", "Financials", ("DJIA",)),
    IndexCandidate("GS.US", "Goldman Sachs", "Financials", ("DJIA",)),
    IndexCandidate("V.US", "Visa", "Financials", ("DJIA",)),
    IndexCandidate("CAT.US", "Caterpillar", "Industrials", ("DJIA",)),
    IndexCandidate(
        "HON.US",
        "Honeywell Technologies",
        "Industrials",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate("BA.US", "Boeing", "Industrials", ("DJIA",)),
    IndexCandidate("CRM.US", "Salesforce", "Software", ("DJIA",)),
    IndexCandidate(
        "CSCO.US",
        "Cisco Systems",
        "Technology Hardware",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate("DIS.US", "Walt Disney", "Communication Services", ("DJIA",)),
    IndexCandidate(
        "WMT.US",
        "Walmart",
        "Consumer Staples",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate("ADBE.US", "Adobe", "Software", ("NASDAQ_100",)),
    IndexCandidate("ADSK.US", "Autodesk", "Software", ("NASDAQ_100",)),
    IndexCandidate(
        "ABNB.US",
        "Airbnb",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "AEP.US",
        "American Electric Power",
        "Utilities",
        ("NASDAQ_100",),
    ),
    IndexCandidate("ADI.US", "Analog Devices", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("ARM.US", "Arm Holdings", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("ASML.US", "ASML Holding", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate(
        "ADP.US",
        "Automatic Data Processing",
        "Industrials",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "AXON.US",
        "Axon Enterprise",
        "Industrials",
        ("NASDAQ_100",),
    ),
    IndexCandidate("BKR.US", "Baker Hughes", "Energy", ("NASDAQ_100",)),
    IndexCandidate(
        "BKNG.US",
        "Booking Holdings",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "CDNS.US",
        "Cadence Design Systems",
        "Software",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "CCEP.US",
        "Coca-Cola Europacific Partners",
        "Consumer Staples",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "CMCSA.US",
        "Comcast",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate("CSX.US", "CSX", "Industrials", ("NASDAQ_100",)),
    IndexCandidate(
        "DASH.US",
        "DoorDash",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate("DDOG.US", "Datadog", "Software", ("NASDAQ_100",)),
    IndexCandidate(
        "EA.US",
        "Electronic Arts",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate("FANG.US", "Diamondback Energy", "Energy", ("NASDAQ_100",)),
    IndexCandidate("EXC.US", "Exelon", "Utilities", ("NASDAQ_100",)),
    IndexCandidate("FTNT.US", "Fortinet", "Software", ("NASDAQ_100",)),
    IndexCandidate(
        "GEHC.US",
        "GE HealthCare Technologies",
        "Healthcare",
        ("NASDAQ_100",),
    ),
    IndexCandidate("GILD.US", "Gilead Sciences", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("INTU.US", "Intuit", "Software", ("NASDAQ_100",)),
    IndexCandidate("LIN.US", "Linde", "Materials", ("NASDAQ_100",)),
    IndexCandidate(
        "LITE.US",
        "Lumentum Holdings",
        "Technology Hardware",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MDLZ.US",
        "Mondelez International",
        "Consumer Staples",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MCHP.US",
        "Microchip Technology",
        "Semiconductors",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MELI.US",
        "MercadoLibre",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MAR.US",
        "Marriott International",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MNST.US",
        "Monster Beverage",
        "Consumer Staples",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MSTR.US",
        "Strategy",
        "Software",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "MPWR.US",
        "Monolithic Power Systems",
        "Semiconductors",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "NXPI.US",
        "NXP Semiconductors",
        "Semiconductors",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "ORLY.US",
        "O'Reilly Automotive",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "PDD.US",
        "PDD Holdings",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate("PEP.US", "PepsiCo", "Consumer Staples", ("NASDAQ_100",)),
    IndexCandidate("PYPL.US", "PayPal Holdings", "Financials", ("NASDAQ_100",)),
    IndexCandidate(
        "REGN.US",
        "Regeneron Pharmaceuticals",
        "Healthcare",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "ROST.US",
        "Ross Stores",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "SBUX.US",
        "Starbucks",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "SHOP.US",
        "Shopify",
        "Consumer Discretionary",
        ("NASDAQ_100",),
    ),
    IndexCandidate("SNPS.US", "Synopsys", "Software", ("NASDAQ_100",)),
    IndexCandidate(
        "SNDK.US",
        "Sandisk",
        "Technology Hardware",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "STX.US",
        "Seagate Technology",
        "Technology Hardware",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "TMUS.US",
        "T-Mobile US",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "TTWO.US",
        "Take-Two Interactive",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "VRTX.US",
        "Vertex Pharmaceuticals",
        "Healthcare",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "WBD.US",
        "Warner Bros. Discovery",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "WDC.US",
        "Western Digital",
        "Technology Hardware",
        ("NASDAQ_100",),
    ),
    IndexCandidate("WDAY.US", "Workday", "Software", ("NASDAQ_100",)),
    IndexCandidate("XEL.US", "Xcel Energy", "Utilities", ("NASDAQ_100",)),
    IndexCandidate(
        "ALNY.US",
        "Alnylam Pharmaceuticals",
        "Healthcare",
        ("NASDAQ_100",),
    ),
    IndexCandidate("CPRT.US", "Copart", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("CTAS.US", "Cintas", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("DXCM.US", "DexCom", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("FAST.US", "Fastenal", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("FER.US", "Ferrovial", "Industrials", ("NASDAQ_100",)),
    IndexCandidate(
        "IDXX.US",
        "IDEXX Laboratories",
        "Healthcare",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "KDP.US",
        "Keurig Dr Pepper",
        "Consumer Staples",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "KHC.US",
        "Kraft Heinz",
        "Consumer Staples",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "ODFL.US",
        "Old Dominion Freight Line",
        "Industrials",
        ("NASDAQ_100",),
    ),
    IndexCandidate("PAYX.US", "Paychex", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("PCAR.US", "PACCAR", "Industrials", ("NASDAQ_100",)),
    IndexCandidate(
        "ROP.US",
        "Roper Technologies",
        "Industrials",
        ("NASDAQ_100",),
    ),
    IndexCandidate(
        "TRI.US",
        "Thomson Reuters",
        "Industrials",
        ("NASDAQ_100",),
    ),
    IndexCandidate("AXP.US", "American Express", "Financials", ("DJIA",)),
    IndexCandidate("CVX.US", "Chevron", "Energy", ("DJIA",)),
    IndexCandidate(
        "HD.US",
        "Home Depot",
        "Consumer Discretionary",
        ("DJIA",),
    ),
    IndexCandidate("IBM.US", "IBM", "Technology Hardware", ("DJIA",)),
    IndexCandidate("JNJ.US", "Johnson & Johnson", "Healthcare", ("DJIA",)),
    IndexCandidate("KO.US", "Coca-Cola", "Consumer Staples", ("DJIA",)),
    IndexCandidate(
        "MCD.US",
        "McDonald's",
        "Consumer Discretionary",
        ("DJIA",),
    ),
    IndexCandidate("MMM.US", "3M", "Industrials", ("DJIA",)),
    IndexCandidate("MRK.US", "Merck", "Healthcare", ("DJIA",)),
    IndexCandidate(
        "NKE.US",
        "Nike",
        "Consumer Discretionary",
        ("DJIA",),
    ),
    IndexCandidate(
        "PG.US",
        "Procter & Gamble",
        "Consumer Staples",
        ("DJIA",),
    ),
    IndexCandidate("SHW.US", "Sherwin-Williams", "Materials", ("DJIA",)),
    IndexCandidate("TRV.US", "Travelers", "Financials", ("DJIA",)),
    IndexCandidate("UNH.US", "UnitedHealth", "Healthcare", ("DJIA",)),
)


# Research-only former constituents represented in the bundled point-in-time
# membership history. They are fetched for walk-forward evaluation, but never
# enter the current live screening or observation pool.
HISTORICAL_INDEX_CANDIDATE_CATALOG: tuple[IndexCandidate, ...] = (
    IndexCandidate("ALGN.US", "Align Technology", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("ANSS.US", "ANSYS", "Software", ("NASDAQ_100",)),
    IndexCandidate("ATVI.US", "Activision Blizzard", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("AZN.US", "AstraZeneca", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("BIDU.US", "Baidu", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("BIIB.US", "Biogen", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("CDW.US", "CDW", "Technology Hardware", ("NASDAQ_100",)),
    IndexCandidate("CHTR.US", "Charter Communications", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("CSGP.US", "CoStar Group", "Real Estate", ("NASDAQ_100",)),
    IndexCandidate("CTSH.US", "Cognizant", "Software", ("NASDAQ_100",)),
    IndexCandidate("DLTR.US", "Dollar Tree", "Consumer Staples", ("NASDAQ_100",)),
    IndexCandidate("DOCU.US", "DocuSign", "Software", ("NASDAQ_100",)),
    IndexCandidate("DOW.US", "Dow", "Materials", ("DJIA",)),
    IndexCandidate("EBAY.US", "eBay", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("ENPH.US", "Enphase Energy", "Technology Hardware", ("NASDAQ_100",)),
    IndexCandidate(
        "FB.US",
        "Meta Platforms (legacy ticker)",
        "Communication Services",
        ("NASDAQ_100",),
    ),
    IndexCandidate("FISV.US", "Fiserv (legacy ticker)", "Financials", ("NASDAQ_100",)),
    IndexCandidate("GFS.US", "GlobalFoundries", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("GOOG.US", "Alphabet Class C", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("ILMN.US", "Illumina", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("INSM.US", "Insmed", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("JD.US", "JD.com", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("LCID.US", "Lucid Group", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("LULU.US", "Lululemon Athletica", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("MDB.US", "MongoDB", "Software", ("NASDAQ_100",)),
    IndexCandidate("MRNA.US", "Moderna", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("MTCH.US", "Match Group", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("NTES.US", "NetEase", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("OKTA.US", "Okta", "Software", ("NASDAQ_100",)),
    IndexCandidate("ON.US", "ON Semiconductor", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("PTON.US", "Peloton", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("RIVN.US", "Rivian Automotive", "Consumer Discretionary", ("NASDAQ_100",)),
    IndexCandidate("SGEN.US", "Seagen", "Healthcare", ("NASDAQ_100",)),
    IndexCandidate("SIRI.US", "Sirius XM", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("SMCI.US", "Super Micro Computer", "Technology Hardware", ("NASDAQ_100",)),
    IndexCandidate("SOLS.US", "Solstice Advanced Materials", "Materials", ("NASDAQ_100",)),
    IndexCandidate("SPLK.US", "Splunk", "Software", ("NASDAQ_100",)),
    IndexCandidate("SWKS.US", "Skyworks Solutions", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("TEAM.US", "Atlassian", "Software", ("NASDAQ_100",)),
    IndexCandidate("TTD.US", "Trade Desk", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("VRSK.US", "Verisk Analytics", "Industrials", ("NASDAQ_100",)),
    IndexCandidate("VRSN.US", "VeriSign", "Technology Infrastructure", ("NASDAQ_100",)),
    IndexCandidate("VSNT.US", "Versant Media", "Communication Services", ("NASDAQ_100",)),
    IndexCandidate("VZ.US", "Verizon", "Communication Services", ("DJIA",)),
    IndexCandidate(
        "WBA.US",
        "Walgreens Boots Alliance",
        "Consumer Staples",
        ("NASDAQ_100", "DJIA"),
    ),
    IndexCandidate("XLNX.US", "Xilinx", "Semiconductors", ("NASDAQ_100",)),
    IndexCandidate("ZM.US", "Zoom Communications", "Software", ("NASDAQ_100",)),
    IndexCandidate("ZS.US", "Zscaler", "Software", ("NASDAQ_100",)),
)


ROTATION_RESEARCH_CANDIDATE_CATALOG: tuple[IndexCandidate, ...] = (
    *INDEX_CANDIDATE_CATALOG,
    *HISTORICAL_INDEX_CANDIDATE_CATALOG,
)
