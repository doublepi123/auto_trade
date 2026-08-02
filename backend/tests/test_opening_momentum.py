from __future__ import annotations

import pytest

from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
    evaluate_opening_momentum_path_eligible,
    evaluate_opening_range_breakout,
    evaluate_stocks_in_play_opening_range_breakout,
    evaluate_opening_reversal,
    opening_path_efficiency,
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


def _range_high_for_breakout_depth(
    observation: OpeningMomentumObservation,
    breakout_depth_bps: float,
) -> float:
    return observation.signal_close / (1 + breakout_depth_bps / 10_000)


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


def test_opening_range_breakout_selects_strongest_confirmed_break() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 80, 100),
        )
    ]
    range_highs = {
        item.symbol: (
            100.5
            if item.symbol == "S6.US"
            else 100.9
            if item.symbol == "S7.US"
            else item.signal_close + 0.1
        )
        for item in observations
    }

    decision = evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
    )

    assert decision.action == "ENTER_LONG"
    assert decision.reason == "FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
    assert decision.candidate_symbol == "S6.US"
    assert decision.candidate_return_bps == pytest.approx(80.0)
    assert decision.market_return_bps == pytest.approx(12.5)
    assert decision.excess_return_bps == pytest.approx(67.5)
    assert decision.entry_price == 100.0
    assert decision.ranking[0].symbol == "S7.US"


def test_opening_range_breakout_requires_strict_close_confirmation() -> None:
    observations = [
        _observation(f"S{index}.US", 10 + index)
        for index in range(8)
    ]

    decision = evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol={
            item.symbol: item.signal_close for item in observations
        },
    )

    assert decision.action == "SKIP"
    assert decision.reason == "OPENING_RANGE_BREAKOUT_MISSING"
    assert decision.candidate_symbol is None
    assert decision.entry_price is None


def test_opening_range_breakout_missing_entry_fails_closed() -> None:
    observations = [
        _observation(
            f"S{index}.US",
            100 if index == 7 else index,
            entry_open=None if index == 7 else 100.0,
        )
        for index in range(8)
    ]

    decision = evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol={
            item.symbol: (
                item.signal_close - 0.1
                if item.symbol == "S7.US"
                else item.signal_close + 0.1
            )
            for item in observations
        },
    )

    assert decision.action == "SKIP"
    assert decision.reason == "ENTRY_BAR_MISSING"
    assert decision.candidate_symbol == "S7.US"
    assert decision.entry_price is None


@pytest.mark.parametrize(
    "range_highs",
    [
        {"S0.US": float("nan")},
        {"S0.US": 0.0},
        {"s0.us": 100.0, "S0.US": 101.0},
    ],
)
def test_opening_range_breakout_rejects_invalid_highs(
    range_highs: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        evaluate_opening_range_breakout(
            [_observation("S0.US", 10)],
            opening_range_high_by_symbol=range_highs,
        )


@pytest.mark.parametrize(
    "minimum_depth_bps",
    [-1.0, float("nan"), float("inf"), float("-inf")],
)
def test_opening_range_breakout_rejects_invalid_minimum_depth(
    minimum_depth_bps: float,
) -> None:
    observations = [_observation(f"S{index}.US", index) for index in range(8)]
    range_highs = {
        item.symbol: item.signal_close - 0.1 for item in observations
    }

    with pytest.raises(ValueError, match="minimum_breakout_depth_bps"):
        evaluate_opening_range_breakout(
            observations,
            opening_range_high_by_symbol=range_highs,
            minimum_breakout_depth_bps=minimum_depth_bps,
        )
    with pytest.raises(ValueError, match="minimum_breakout_depth_bps"):
        evaluate_stocks_in_play_opening_range_breakout(
            observations,
            opening_range_high_by_symbol=range_highs,
            opening_activity_ratio_by_symbol={
                item.symbol: 1.0 for item in observations
            },
            minimum_breakout_depth_bps=minimum_depth_bps,
        )


def test_opening_range_breakout_none_minimum_depth_preserves_results() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate((-10, 0, 5, 10, 15, 20, 80, 100))
    ]
    range_highs = {
        item.symbol: (
            item.signal_close - 0.1
            if item.symbol in {"S6.US", "S7.US"}
            else item.signal_close + 0.1
        )
        for item in observations
    }
    activity = {item.symbol: 1.0 for item in observations}

    assert evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
    ) == evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        minimum_breakout_depth_bps=None,
    )
    assert evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol=activity,
    ) == evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol=activity,
        minimum_breakout_depth_bps=None,
    )


@pytest.mark.parametrize(
    ("breakout_depth_bps", "expected_action"),
    [(9.99, "SKIP"), (10.0, "ENTER_LONG"), (10.01, "ENTER_LONG")],
)
def test_opening_range_breakout_minimum_depth_boundary(
    breakout_depth_bps: float,
    expected_action: str,
) -> None:
    observations = [
        _observation(f"S{index}.US", 10 + index) for index in range(8)
    ]
    target = observations[-1]
    range_highs = {
        item.symbol: item.signal_close + 0.1 for item in observations
    }
    range_highs[target.symbol] = _range_high_for_breakout_depth(
        target,
        breakout_depth_bps,
    )

    decision = evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        minimum_breakout_depth_bps=10.0,
    )

    assert decision.action == expected_action
    assert decision.reason == (
        "MINIMUM_BREAKOUT_DEPTH_FILTER"
        if expected_action == "SKIP"
        else "FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
    )
    assert decision.candidate_symbol == (
        None if expected_action == "SKIP" else target.symbol
    )


def test_stocks_in_play_orb_selects_next_ranked_eligible_breakout() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate((-10, 0, 5, 10, 15, 20, 80, 100))
    ]
    shallow_leader = observations[-1]
    eligible_runner_up = observations[-2]
    range_highs = {
        item.symbol: item.signal_close + 0.1 for item in observations
    }
    range_highs[shallow_leader.symbol] = _range_high_for_breakout_depth(
        shallow_leader,
        9.99,
    )
    range_highs[eligible_runner_up.symbol] = _range_high_for_breakout_depth(
        eligible_runner_up,
        10.01,
    )

    decision = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol={
            item.symbol: 1.0 for item in observations
        },
        maximum_stocks_in_play=8,
        candidate_ranking="OPENING_RETURN",
        minimum_breakout_depth_bps=10.0,
    )

    assert decision.action == "ENTER_LONG"
    assert decision.candidate_symbol == eligible_runner_up.symbol
    assert decision.candidate_return_bps == pytest.approx(80.0)


def test_opening_range_breakout_reports_all_confirmed_depths_filtered() -> None:
    observations = [
        _observation(f"S{index}.US", 10 + index) for index in range(8)
    ]
    range_highs = {
        item.symbol: item.signal_close + 0.1 for item in observations
    }
    for item, depth_bps in zip(observations[-2:], (8.0, 9.99), strict=True):
        range_highs[item.symbol] = _range_high_for_breakout_depth(
            item,
            depth_bps,
        )

    decision = evaluate_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        minimum_breakout_depth_bps=10.0,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "MINIMUM_BREAKOUT_DEPTH_FILTER"
    assert decision.candidate_symbol is None
    assert decision.entry_price is None


def test_stocks_in_play_orb_restricts_breakout_to_activity_leaders() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 80, 120),
        )
    ]
    range_highs = {
        item.symbol: (
            item.signal_close - 0.2
            if item.symbol in {"S6.US", "S7.US"}
            else item.signal_close + 0.2
        )
        for item in observations
    }

    decision = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol={
            item.symbol: (
                2.0
                if item.symbol == "S6.US"
                else 1.0
                if item.symbol == "S5.US"
                else 0.1
            )
            for item in observations
        },
        maximum_stocks_in_play=2,
    )

    assert decision.action == "ENTER_LONG"
    assert decision.reason == (
        "STOCKS_IN_PLAY_FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
    )
    assert decision.candidate_symbol == "S6.US"
    assert decision.candidate_return_bps == pytest.approx(80.0)
    assert decision.market_return_bps == pytest.approx(12.5)
    assert decision.universe_size == 8


def test_stocks_in_play_orb_can_rerank_breakouts_by_opening_return() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 80, 100),
        )
    ]
    range_highs = {
        item.symbol: (
            100.1
            if item.symbol == "S6.US"
            else 100.6
            if item.symbol == "S7.US"
            else item.signal_close + 0.1
        )
        for item in observations
    }
    activity = {item.symbol: 1.0 for item in observations}

    breakout_depth = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol=activity,
        maximum_stocks_in_play=8,
    )
    opening_return = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol=range_highs,
        opening_activity_ratio_by_symbol=activity,
        maximum_stocks_in_play=8,
        candidate_ranking="OPENING_RETURN",
    )

    assert breakout_depth.candidate_symbol == "S6.US"
    assert opening_return.action == "ENTER_LONG"
    assert opening_return.reason == (
        "OPENING_RETURN_RERANKED_STOCKS_IN_PLAY_"
        "FIVE_MINUTE_OPENING_RANGE_BREAKOUT"
    )
    assert opening_return.candidate_symbol == "S7.US"
    assert opening_return.candidate_return_bps == pytest.approx(100.0)


def test_stocks_in_play_orb_fails_closed_with_missing_activity() -> None:
    observations = [
        _observation(f"S{index}.US", 10 + index)
        for index in range(8)
    ]

    decision = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol={
            item.symbol: item.signal_close - 0.1
            for item in observations
        },
        opening_activity_ratio_by_symbol={
            item.symbol: 1.0 for item in observations[:-1]
        },
    )

    assert decision.action == "SKIP"
    assert decision.reason == "OPENING_ACTIVITY_DATA_INCOMPLETE"
    assert decision.candidate_symbol is None
    assert decision.universe_size == 8


def test_stocks_in_play_orb_applies_minimum_activity_before_top_n() -> None:
    observations = [
        _observation(f"S{index}.US", 10 + index * 10)
        for index in range(8)
    ]
    decision = evaluate_stocks_in_play_opening_range_breakout(
        observations,
        opening_range_high_by_symbol={
            item.symbol: item.signal_close - 0.1
            for item in observations
        },
        opening_activity_ratio_by_symbol={
            item.symbol: (
                0.9
                if item.symbol == "S7.US"
                else 1.2
                if item.symbol == "S6.US"
                else 0.5
            )
            for item in observations
        },
        maximum_stocks_in_play=5,
        minimum_opening_activity_ratio=1.0,
    )

    assert decision.action == "ENTER_LONG"
    assert decision.candidate_symbol == "S6.US"


@pytest.mark.parametrize(
    ("activity", "limit"),
    [
        ({"S0.US": float("nan")}, 20),
        ({"S0.US": 0.0}, 20),
        ({"s0.us": 0.1, "S0.US": 0.2}, 20),
        ({"S0.US": 0.1}, 0),
    ],
)
def test_stocks_in_play_orb_rejects_invalid_activity(
    activity: dict[str, float],
    limit: int,
) -> None:
    with pytest.raises(ValueError):
        evaluate_stocks_in_play_opening_range_breakout(
            [_observation("S0.US", 10)],
            opening_range_high_by_symbol={"S0.US": 100.0},
            opening_activity_ratio_by_symbol=activity,
            maximum_stocks_in_play=limit,
        )


@pytest.mark.parametrize("minimum_ratio", [0.0, float("nan")])
def test_stocks_in_play_orb_rejects_invalid_minimum_activity(
    minimum_ratio: float,
) -> None:
    with pytest.raises(ValueError):
        evaluate_stocks_in_play_opening_range_breakout(
            [_observation("S0.US", 10)],
            opening_range_high_by_symbol={"S0.US": 100.0},
            opening_activity_ratio_by_symbol={"S0.US": 1.0},
            minimum_opening_activity_ratio=minimum_ratio,
        )


def test_path_eligible_rerank_selects_strongest_eligible_candidate() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 80, 100),
        )
    ]

    decision = evaluate_opening_momentum_path_eligible(
        observations,
        path_efficiency_by_symbol={
            f"S{index}.US": 0.80 if index == 6 else 0.40
            for index in range(8)
        },
        minimum_path_efficiency=0.70,
    )

    assert decision.action == "ENTER_LONG"
    assert decision.reason == "PATH_ELIGIBLE_OPENING_LEADER"
    assert decision.candidate_symbol == "S6.US"
    assert decision.candidate_return_bps == pytest.approx(80.0)
    assert decision.market_return_bps == pytest.approx(12.5)
    assert decision.excess_return_bps == pytest.approx(67.5)
    assert decision.ranking[0].symbol == "S7.US"


def test_path_eligible_rerank_fails_closed_without_eligible_name() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-10, 0, 5, 10, 15, 20, 80, 100),
        )
    ]

    decision = evaluate_opening_momentum_path_eligible(
        observations,
        path_efficiency_by_symbol={
            item.symbol: 0.69 for item in observations
        },
        minimum_path_efficiency=0.70,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "PATH_ELIGIBLE_CANDIDATE_MISSING"
    assert decision.candidate_symbol is None
    assert decision.market_return_bps == pytest.approx(12.5)
    assert decision.ranking[0].symbol == "S7.US"


@pytest.mark.parametrize(
    ("efficiencies", "minimum"),
    [
        ({"S0.US": float("nan")}, 0.70),
        ({"S0.US": 1.01}, 0.70),
        ({"S0.US": 0.80}, -0.01),
        ({"s0.us": 0.80, "S0.US": 0.90}, 0.70),
    ],
)
def test_path_eligible_rerank_rejects_invalid_configuration(
    efficiencies: dict[str, float],
    minimum: float,
) -> None:
    with pytest.raises(ValueError):
        evaluate_opening_momentum_path_eligible(
            [_observation("S0.US", 100)],
            path_efficiency_by_symbol=efficiencies,
            minimum_path_efficiency=minimum,
        )


def test_selects_deterministic_opening_laggard_for_reversal() -> None:
    observations = [
        _observation(f"S{index}.US", value)
        for index, value in enumerate(
            (-80, -25, -20, -10, 0, 10, 20, 30),
        )
    ]

    decision = evaluate_opening_reversal(observations)

    assert decision.action == "ENTER_LONG"
    assert decision.reason == "OPENING_LAGGARD_REVERSAL"
    assert decision.candidate_symbol == "S0.US"
    assert decision.market_return_bps == pytest.approx(-5.0)
    assert decision.candidate_return_bps == pytest.approx(-80.0)
    assert decision.excess_return_bps == pytest.approx(-75.0)
    assert decision.entry_price == 100.0
    assert decision.ranking[0].symbol == "S0.US"


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        (
            [_observation(f"S{index}.US", -index) for index in range(7)],
            "INSUFFICIENT_UNIVERSE",
        ),
        (
            [
                _observation(f"S{index}.US", value)
                for index, value in enumerate(
                    (-100, -90, -80, -70, -60, -50, -40, -30),
                )
            ],
            "MARKET_FILTER",
        ),
        (
            [
                _observation(f"S{index}.US", 10 + index)
                for index in range(8)
            ],
            "CANDIDATE_NOT_NEGATIVE",
        ),
        (
            [
                _observation(f"S{index}.US", -10 + index)
                for index in range(8)
            ],
            "RELATIVE_LOSS_FILTER",
        ),
    ],
)
def test_reversal_entry_gates_fail_closed(
    observations: list[OpeningMomentumObservation],
    reason: str,
) -> None:
    decision = evaluate_opening_reversal(observations)

    assert decision.action == "SKIP"
    assert decision.reason == reason
    assert decision.entry_price is None


def test_reversal_missing_entry_does_not_substitute_runner_up() -> None:
    observations = [
        _observation(
            f"S{index}.US",
            -100 if index == 0 else index,
            entry_open=None if index == 0 else 100.0,
        )
        for index in range(8)
    ]

    decision = evaluate_opening_reversal(observations)

    assert decision.action == "SKIP"
    assert decision.reason == "ENTRY_BAR_MISSING"
    assert decision.candidate_symbol == "S0.US"


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


def test_stop_loss_is_versioned_and_validated() -> None:
    fixed_hold = OpeningMomentumConfig(stop_loss_pct=None)
    stop_first = OpeningMomentumConfig(stop_loss_pct=1.0)

    assert fixed_hold.version_hash() == (
        "04870d0a9d7b9dd823321182bca02120c0ad42ff0675e4758540d6058b35c86e"
    )
    assert fixed_hold.version_hash() != stop_first.version_hash()
    with pytest.raises(ValueError, match="stop_loss_pct"):
        OpeningMomentumConfig(stop_loss_pct=0)


def test_duplicate_symbols_are_rejected() -> None:
    item = _observation("AAPL.US", 50)

    with pytest.raises(ValueError, match="duplicate"):
        evaluate_opening_momentum([item, item])
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_opening_momentum_path_eligible(
            [item, item],
            path_efficiency_by_symbol={"AAPL.US": 1.0},
            minimum_path_efficiency=0.70,
        )
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_opening_range_breakout(
            [item, item],
            opening_range_high_by_symbol={"AAPL.US": 100.0},
        )
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_opening_reversal([item, item])


def test_opening_path_efficiency_uses_completed_close_path() -> None:
    assert opening_path_efficiency(
        opening_price=100.0,
        closing_prices=(102.0, 99.0, 101.0),
    ) == pytest.approx(1 / 7)
    assert opening_path_efficiency(
        opening_price=100.0,
        closing_prices=(101.0, 102.0, 103.0),
    ) == 1.0
    assert opening_path_efficiency(
        opening_price=100.0,
        closing_prices=(100.0, 100.0),
    ) == 0.0


@pytest.mark.parametrize(
    ("opening_price", "closing_prices"),
    (
        (0.0, (100.0,)),
        (100.0, ()),
        (100.0, (float("nan"),)),
    ),
)
def test_opening_path_efficiency_rejects_invalid_prices(
    opening_price: float,
    closing_prices: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        opening_path_efficiency(
            opening_price=opening_price,
            closing_prices=closing_prices,
        )
