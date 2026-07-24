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
    residual_1m_bps: float | None = None,
    residual_5m_bps: float | None = None,
    round_trip_cost_bps: float | None = None,
    observed_round_trip_cost_bps: float | None = None,
    stop_distance_bps: float | None = None,
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
        residual_1m_bps=residual_1m_bps,
        residual_5m_bps=residual_5m_bps,
        round_trip_cost_bps=round_trip_cost_bps,
        observed_round_trip_cost_bps=observed_round_trip_cost_bps,
        stop_distance_bps=stop_distance_bps,
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


def test_selected_vwap_edge_requires_cost_to_stop_discount_band() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                selected=True,
                rank=2,
                residual_1m_bps=-25,
                residual_5m_bps=-40,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
            _candidate(
                "MSFT.US",
                2,
                selected=True,
                rank=1,
                residual_1m_bps=5,
                residual_5m_bps=-30,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
            _candidate(
                "AMD.US",
                3,
                selected=True,
                rank=3,
                residual_1m_bps=-80,
                residual_5m_bps=-40,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
            _candidate(
                "NVDA.US",
                4,
                selected=False,
                residual_1m_bps=-30,
                residual_5m_bps=-30,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
        ],
        policy="SELECTED_VWAP_EDGE",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == ["AAPL.US"]
    assert ranked[0].vwap_edge_eligible is True
    assert ranked[0].vwap_edge_score_bps == 11


def test_vwap_edge_pool_ranks_guaranteed_discount_after_cost() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                selected=True,
                rank=1,
                residual_1m_bps=-25,
                residual_5m_bps=-20,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
            _candidate(
                "TER.US",
                2,
                residual_1m_bps=-35,
                residual_5m_bps=-55,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
            _candidate(
                "MRVL.US",
                3,
                residual_1m_bps=-25,
                residual_5m_bps=-65,
                round_trip_cost_bps=14,
                stop_distance_bps=75,
            ),
        ],
        policy="VWAP_EDGE_POOL",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == [
        "TER.US",
        "MRVL.US",
        "AAPL.US",
    ]


def test_vwap_edge_supports_valid_zero_cost_configuration() -> None:
    candidate = _candidate(
        "AAPL.US",
        1,
        residual_1m_bps=-5,
        residual_5m_bps=-10,
        round_trip_cost_bps=0,
        stop_distance_bps=75,
    )

    assert candidate.vwap_edge_eligible is True
    assert candidate.vwap_edge_score_bps == 5


def test_observed_cost_vwap_edge_fails_closed_and_never_weakens_cost() -> None:
    missing = _candidate(
        "AAPL.US",
        1,
        residual_1m_bps=-25,
        residual_5m_bps=-30,
        round_trip_cost_bps=14,
        stop_distance_bps=75,
    )
    spread_heavy = _candidate(
        "MSFT.US",
        2,
        residual_1m_bps=-20,
        residual_5m_bps=-30,
        round_trip_cost_bps=14,
        observed_round_trip_cost_bps=24,
        stop_distance_bps=75,
    )
    low_observation = _candidate(
        "NVDA.US",
        3,
        residual_1m_bps=-10,
        residual_5m_bps=-20,
        round_trip_cost_bps=14,
        observed_round_trip_cost_bps=5,
        stop_distance_bps=75,
    )

    assert missing.vwap_edge_eligible is True
    assert missing.observed_cost_vwap_edge_eligible is False
    assert spread_heavy.vwap_edge_eligible is True
    assert spread_heavy.observed_cost_vwap_edge_eligible is False
    assert low_observation.effective_observed_cost_bps == 14
    assert low_observation.observed_cost_vwap_edge_eligible is False


def test_observed_cost_vwap_edge_pool_ranks_remaining_net_discount() -> None:
    ranked = rank_portfolio_candidates(
        [
            _candidate(
                "AAPL.US",
                1,
                selected=True,
                rank=1,
                residual_1m_bps=-30,
                residual_5m_bps=-40,
                round_trip_cost_bps=14,
                observed_round_trip_cost_bps=24,
                stop_distance_bps=75,
            ),
            _candidate(
                "TER.US",
                2,
                residual_1m_bps=-35,
                residual_5m_bps=-55,
                round_trip_cost_bps=14,
                observed_round_trip_cost_bps=31,
                stop_distance_bps=75,
            ),
            _candidate(
                "MRVL.US",
                3,
                residual_1m_bps=-30,
                residual_5m_bps=-65,
                round_trip_cost_bps=14,
                observed_round_trip_cost_bps=20,
                stop_distance_bps=75,
            ),
        ],
        policy="VWAP_EDGE_OBSERVED_COST_POOL",
        primary_symbol="NVDA.US",
    )

    assert [item.symbol for item in ranked] == [
        "MRVL.US",
        "AAPL.US",
        "TER.US",
    ]
    assert [
        item.observed_cost_vwap_edge_score_bps
        for item in ranked
    ] == [10, 6, 4]


@pytest.mark.parametrize(
    "observed_cost",
    [-1.0, float("inf"), float("nan")],
)
def test_observed_cost_vwap_edge_rejects_invalid_cost(
    observed_cost: float,
) -> None:
    with pytest.raises(ValueError):
        _candidate(
            "AAPL.US",
            1,
            observed_round_trip_cost_bps=observed_cost,
        )


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
