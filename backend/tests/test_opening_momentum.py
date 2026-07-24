from __future__ import annotations

import pytest

from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
    shadow_round_trip_return_bps,
)


def _observation(
    symbol: str,
    opening_return_bps: float,
    *,
    entry_open: float | None = 100.0,
) -> OpeningMomentumObservation:
    return OpeningMomentumObservation(
        symbol=symbol,
        session_open=100.0,
        signal_close=100.0 * (1 + opening_return_bps / 10_000),
        entry_open=entry_open,
    )


def test_selects_deterministic_opening_leader_after_all_gates() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 25, 80),
        )
    ]

    decision = evaluate_opening_momentum(observations)

    assert decision.action == "ENTER_LONG"
    assert decision.reason == "OPENING_LEADER"
    assert decision.candidate_symbol == "S7.US"
    assert decision.market_return_bps == pytest.approx(12.5)
    assert decision.excess_return_bps == pytest.approx(67.5)
    assert decision.entry_price == 100.0
    assert decision.ranking[0].symbol == "S7.US"


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        (
            [_observation(f"S{index}.US", index) for index in range(7)],
            "INSUFFICIENT_UNIVERSE",
        ),
        (
            [
                _observation(f"S{index}.US", value)
                for index, value in enumerate(
                    (-80, -70, -60, -50, -40, -30, -20, 20),
                )
            ],
            "MARKET_FILTER",
        ),
        (
            [
                _observation(f"S{index}.US", -10 - index)
                for index in range(8)
            ],
            "CANDIDATE_NOT_POSITIVE",
        ),
        (
            [
                _observation(f"S{index}.US", 10 + index)
                for index in range(8)
            ],
            "EXCESS_RETURN_FILTER",
        ),
    ],
)
def test_entry_gates_fail_closed(
    observations: list[OpeningMomentumObservation],
    reason: str,
) -> None:
    decision = evaluate_opening_momentum(observations)

    assert decision.action == "SKIP"
    assert decision.reason == reason
    assert decision.entry_price is None


def test_missing_next_bar_does_not_fall_through_to_second_rank() -> None:
    observations = [
        _observation(
            f"S{index}.US",
            100 if index == 7 else index,
            entry_open=None if index == 7 else 100.0,
        )
        for index in range(8)
    ]

    decision = evaluate_opening_momentum(observations)

    assert decision.action == "SKIP"
    assert decision.reason == "ENTRY_BAR_MISSING"
    assert decision.candidate_symbol == "S7.US"


def test_round_trip_cost_is_applied_after_raw_return() -> None:
    config = OpeningMomentumConfig(
        one_side_fee_rate=0.0005,
        one_side_slippage_bps=2.0,
    )

    gross, net = shadow_round_trip_return_bps(
        entry_price=100.0,
        exit_price=101.0,
        config=config,
    )

    assert config.round_trip_cost_bps == 14.0
    assert gross == pytest.approx(100.0)
    assert net == pytest.approx(86.0)


def test_execution_delay_cannot_reintroduce_same_bar_lookahead() -> None:
    with pytest.raises(
        ValueError,
        match="execution_delay_minutes",
    ):
        OpeningMomentumConfig(execution_delay_minutes=0)

    causal = OpeningMomentumConfig(execution_delay_minutes=1)
    slower = OpeningMomentumConfig(execution_delay_minutes=2)

    assert causal.version_hash() != slower.version_hash()


def test_duplicate_symbols_are_rejected() -> None:
    item = _observation("AAPL.US", 50)

    with pytest.raises(ValueError, match="duplicate"):
        evaluate_opening_momentum([item, item])
