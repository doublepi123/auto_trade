from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.opening_momentum import OpeningMomentumConfig
from app.domain.opening_momentum_policy import (
    OPENING_POLICY_DIAGNOSTIC_VERSION,
    PRODUCTION_POLICY_NAME,
    OpeningPolicyResult,
    OpeningPolicySession,
    OpeningPolicySlice,
    OpeningPolicySpec,
    evaluate_opening_policy_grid,
    opening_execution_config,
)


def _slice(
    result: OpeningPolicyResult,
    name: str,
) -> OpeningPolicySlice:
    return next(value for value in result.slices if value.name == name)


def _sessions() -> tuple[OpeningPolicySession, ...]:
    net_returns = (
        -100.0,
        100.0,
        200.0,
        -50.0,
        -100.0,
        50.0,
        -100.0,
        120.0,
        -80.0,
        50.0,
    )
    paths = (0.5, 0.8, 0.8, 0.8, 0.6, 0.9, 0.5, 0.8, 0.8, 0.8)
    markets = (-5.0, -5.0, 5.0, -1.0, -1.0, -1.0, -1.0, -1.0, 10.0, 10.0)
    return tuple(
        OpeningPolicySession(
            session_date=date(2026, 1, 2) + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=net_return + 14.0,
            market_return_bps=markets[index],
            candidate_path_efficiency=paths[index],
            candidate_symbol=f"S{index}.US",
            stop_triggered=index in {3, 6, 8},
        )
        for index, net_return in enumerate(net_returns)
    )


def test_policy_grid_uses_chronological_holdout_and_paired_skips() -> None:
    report = evaluate_opening_policy_grid(
        _sessions(),
        policies=(
            OpeningPolicySpec("BROAD"),
            OpeningPolicySpec(
                PRODUCTION_POLICY_NAME,
                minimum_path_efficiency=0.70,
                maximum_market_return_bps=0.0,
            ),
        ),
        round_trip_cost_bps=14.0,
    )

    production = report.policies[1]
    discovery = _slice(production, "DISCOVERY")
    holdout = _slice(production, "HOLDOUT")
    assert report.algorithm_version == OPENING_POLICY_DIAGNOSTIC_VERSION
    assert report.discovery_sessions == 6
    assert report.holdout_sessions == 4
    assert discovery.start_date == _sessions()[0].session_date
    assert discovery.end_date == _sessions()[5].session_date
    assert holdout.metrics.entries == 1
    assert holdout.metrics.wins == 1
    assert holdout.metrics.cumulative_return_bps == pytest.approx(120.0)
    assert holdout.comparison_to_baseline.cumulative_delta_bps == (
        pytest.approx(130.0)
    )
    assert holdout.displacement.displaced_signal_sessions == 3
    assert holdout.displacement.avoided_losing_signals == 2
    assert holdout.displacement.avoided_winning_signals == 1
    assert holdout.displacement.cumulative_delta_bps == pytest.approx(130.0)
    assert holdout.displacement.outperformance_rate == pytest.approx(2 / 3)
    assert report.automatic_promotion_allowed is False
    assert report.to_dict()["automatic_promotion_allowed"] is False


def test_baseline_policy_preserves_every_resolved_signal() -> None:
    report = evaluate_opening_policy_grid(
        _sessions(),
        policies=(
            OpeningPolicySpec("BROAD"),
            OpeningPolicySpec(PRODUCTION_POLICY_NAME),
        ),
        round_trip_cost_bps=14.0,
    )

    baseline = _slice(report.policies[0], "ALL")
    production = _slice(report.policies[1], "ALL")
    assert baseline.metrics.entries == 10
    assert baseline.metrics.cumulative_return_bps == pytest.approx(90.0)
    assert baseline.displacement.displaced_signal_sessions == 0
    assert production.metrics == baseline.metrics


def test_execution_config_is_shared_and_preserves_cost_inputs() -> None:
    config = opening_execution_config(OpeningMomentumConfig(
        minimum_universe_size=12,
        one_side_fee_rate=0.0007,
        one_side_slippage_bps=3.0,
    ))

    assert config.signal_minutes == 3
    assert config.execution_delay_minutes == 1
    assert config.holding_minutes == 60
    assert config.minimum_universe_size == 12
    assert config.minimum_market_return_bps == -50.0
    assert config.minimum_candidate_return_bps == 50.0
    assert config.minimum_excess_return_bps == 25.0
    assert config.stop_loss_pct == 1.0
    assert config.round_trip_cost_bps == 20.0


def test_policy_grid_requires_explicit_ungated_baseline_and_production() -> None:
    with pytest.raises(ValueError, match="baseline policy must not"):
        evaluate_opening_policy_grid(
            _sessions(),
            policies=(
                OpeningPolicySpec("BROAD", minimum_path_efficiency=0.5),
                OpeningPolicySpec(PRODUCTION_POLICY_NAME),
            ),
            round_trip_cost_bps=14.0,
        )
    with pytest.raises(ValueError, match="production policy is missing"):
        evaluate_opening_policy_grid(
            _sessions(),
            policies=(OpeningPolicySpec("BROAD"),),
            round_trip_cost_bps=14.0,
        )
