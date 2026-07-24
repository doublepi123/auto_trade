from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CATALOG_SOURCE_VERSION = "nasdaq-100-2026-07-24_djia-2026-06-29_v2"


@dataclass(frozen=True)
class IndexCandidate:
    symbol: str
    alias: str
    sector: str
    memberships: tuple[str, ...]
    market: Literal["US", "HK"] = "US"


# This is intentionally a liquid, diversified screening seed rather than a
# hard-coded portfolio. Daily market-data gates decide which names enter the
# observed pool. Membership was verified against Nasdaq's NDX index pages and
# announcements (including SPCX effective 2026-07-07) and S&P DJI's 2026-06-23
# announcement for the DJIA changes effective 2026-06-29.
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
    IndexCandidate("AMGN.US", "Amgen", "Healthcare", ("NASDAQ_100",)),
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
    IndexCandidate("HON.US", "Honeywell Technologies", "Industrials", ("DJIA",)),
    IndexCandidate("BA.US", "Boeing", "Industrials", ("DJIA",)),
    IndexCandidate("CRM.US", "Salesforce", "Software", ("DJIA",)),
    IndexCandidate("CSCO.US", "Cisco Systems", "Technology Hardware", ("DJIA",)),
    IndexCandidate("DIS.US", "Walt Disney", "Communication Services", ("DJIA",)),
    IndexCandidate("WMT.US", "Walmart", "Consumer Staples", ("DJIA",)),
)
