"""Frozen identities that keep Strategy v2 forward collection running.

The disproof-assessment route and its evaluator were retired. These two
constants are not assessment logic: ``StrategyV2ShadowService`` uses them to
decide whether a matured forward registration still belongs to the frozen
collection cohort and must keep writing ``strategy_v2_forward_evidence``.
Changing either value would silently stop that collection, so they stay here
as data, not as a scoring pipeline.
"""
from __future__ import annotations

from datetime import date


FROZEN_EVALUATOR_DIGEST = (
    "e5ae9ea3e68dcc47d5131c21d8ba223824aecabf59da1f4b592df72cb9aa0294"
)
FROZEN_QUEUE_AS_OF_DATE = date(2026, 7, 31)
FROZEN_QUEUE_NAME = "nasdaq-djia-disproof-2026-07-31"
# (symbol, role, reason, source_config_version). Role and reason are retained
# so the frozen identity stays auditable; the shadow writer matches only
# symbol + source_config_version.
FROZEN_QUEUE_ENTRIES = (
    (
        "AAPL.US",
        "EXPLORATION",
        (
            "Highest usable quant edge-to-cost and positive small-sample "
            "shadow evidence require prospective disproof."
        ),
        "11583728616a7d5f17328d38f024fa10644c29e8b5691106ea961ba75d160a32",
    ),
    (
        "AVGO.US",
        "EXPLORATION",
        (
            "One-session challenger improvement conflicts with near-flat "
            "historical shadow evidence and negative quant edge."
        ),
        "f2e0b1fcfb832c1887a093abda8a146fc03741b2deb47bb29c3132542a4393e2",
    ),
    (
        "GOOGL.US",
        "SELECTED",
        (
            "Latest universe and observation priority rank one has "
            "insufficient forward trade evidence."
        ),
        "5afa45fb95bda68262818c43bbc4f01f62cb81a5e6457d68b5c64dbd11be5bdc",
    ),
    (
        "META.US",
        "EXPLORATION",
        (
            "Small positive shadow evidence conflicts with strongly "
            "negative cost-adjusted quant edge."
        ),
        "cf97ba201b97e8040e0ff113ebb4197268f7709ef6c801979f27eea0a6e9fcd3",
    ),
    (
        "NVDA.US",
        "CONTROL",
        "Deployed symbol is the mandatory same-window control.",
        "9afed570d67d2394f01d40d6706ad7b5eefea5627c7813b4ae762d46a4eeddd9",
    ),
    (
        "TER.US",
        "WATCHLIST",
        (
            "Positive small-sample shadow evidence conflicts with AVOID "
            "quant evidence and negative backfilled rotation performance."
        ),
        "e9f094c87d6342ea6a8663b04584c0aae16455060e0ad3a903a18620c5411435",
    ),
)
