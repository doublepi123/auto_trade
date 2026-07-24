from __future__ import annotations

import pytest

from app.domain.strategy_v2 import (
    PortfolioRoutingCandidate,
    rank_portfolio_candidates,
)


def _candidate(
    symbol: str,
    decision_id: int,
    *,
    selected: bool = False,
    rank: int | None = None,
    selection_score: float | None = None,
    quant_source: str = "",
    quant_action: str = "",
    quant_score: float | None = None,
    confidence: float | None = None,
) -> PortfolioRoutingCandidate:
    return PortfolioRoutingCandidate(
        symbol=symbol,
        signal_decision_id=decision_id,
        source_config_version=symbol * 2,
        selection_selected=selected,
        selection_rank=rank,
        selection_score=selection_score,
        quant_source=quant_source,
        quant_action=quant_action,
        quant_score=quant_score,
        quant_confidence=confidence,
    )


def test_fixed_primary_ignores_higher_ranked_secondary() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate("AAPL.US", 1, selected=True, rank=1),
            _candidate("NVDA.US", 2, selected=True, rank=12),
        ],
        policy="FIXED_PRIMARY",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == ["NVDA.US"]


def test_selected_universe_uses_frozen_run_rank() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                selected=True,
                rank=2,
                selection_score=80,
            ),
            _candidate(
                "MSFT.US",
                2,
                selected=True,
                rank=1,
                selection_score=70,
            ),
            _candidate(
                "AMD.US",
                3,
                selected=False,
                selection_score=99,
            ),
        ],
        policy="SELECTED_UNIVERSE",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == [
        "MSFT.US",
        "AAPL.US",
    ]


def test_quant_candidate_excludes_watch_and_wrong_generation() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                quant_source="quant_v5",
                quant_action="CANDIDATE",
                quant_score=55,
                confidence=0.8,
            ),
            _candidate(
                "MSFT.US",
                2,
                quant_source="quant_v5",
                quant_action="WATCH",
                quant_score=70,
                confidence=0.9,
            ),
            _candidate(
                "AMD.US",
                3,
                quant_source="quant_error_v5",
                quant_action="CANDIDATE",
                quant_score=90,
                confidence=1,
            ),
        ],
        policy="QUANT_CANDIDATE",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == ["AAPL.US"]


def test_quant_watch_plus_prioritizes_candidate_then_score() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                quant_source="quant_v5",
                quant_action="WATCH",
                quant_score=80,
                confidence=0.9,
            ),
            _candidate(
                "MSFT.US",
                2,
                quant_source="quant_v5",
                quant_action="CANDIDATE",
                quant_score=51,
                confidence=0.7,
            ),
            _candidate(
                "META.US",
                3,
                quant_source="quant_v5",
                quant_action="WATCH",
                quant_score=60,
                confidence=0.8,
            ),
        ],
        policy="QUANT_WATCH_PLUS",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == [
        "MSFT.US",
        "AAPL.US",
        "META.US",
    ]


def test_duplicate_symbol_is_rejected() -> None:
    candidate = _candidate("AAPL.US", 1)

    with pytest.raises(
        ValueError,
        match="duplicate portfolio routing candidate",
    ):
        rank_portfolio_candidates(
            [candidate, candidate],
            policy="FIXED_PRIMARY",
            primary_symbol="AAPL.US",
        )
