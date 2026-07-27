from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.opening_momentum import OpeningMomentumConfig
from app.domain.opening_momentum_policy import (
    OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION,
    OPENING_POLICY_DIAGNOSTIC_VERSION,
    OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION,
    PRODUCTION_POLICY_NAME,
    OpeningPolicyCohortReport,
    OpeningPolicyCohortSlice,
    OpeningPolicyHorizonReport,
    OpeningPolicyHorizonSlice,
    OpeningPolicyResult,
    OpeningPolicySession,
    OpeningPolicySlice,
    OpeningPolicySpec,
    evaluate_opening_policy_cohort,
    evaluate_opening_policy_grid,
    evaluate_opening_policy_horizons,
    opening_execution_config,
)


def _slice(
    result: OpeningPolicyResult,
    name: str,
) -> OpeningPolicySlice:
    return next(value for value in result.slices if value.name == name)


def _cohort_slice(
    report: OpeningPolicyCohortReport,
    name: str,
) -> OpeningPolicyCohortSlice:
    return next(value for value in report.slices if value.name == name)


def _horizon_slice(
    report: OpeningPolicyHorizonReport,
    holding_minutes: int,
    name: str,
) -> OpeningPolicyHorizonSlice:
    result = next(
        value
        for value in report.results
        if value.holding_minutes == holding_minutes
    )
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


def test_cohort_report_pairs_dates_and_exposes_tail_and_displacements() -> None:
    first = date(2026, 1, 2)
    baseline = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=value + 14.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol=f"BASE{index}.US",
            stop_triggered=index == 4,
        )
        for index, value in enumerate((100.0, -50.0, 25.0, 200.0, -100.0))
    )
    cohort_values = (150.0, -60.0, 25.0, 300.0)
    cohort = tuple(
        OpeningPolicySession(
            session_date=first + timedelta(days=index),
            baseline_signal=True,
            gross_return_bps=value + 14.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol=(
                "RKLB.US" if index == 3 else f"COHORT{index}.US"
            ),
        )
        for index, value in enumerate(cohort_values)
    ) + (
        OpeningPolicySession(
            session_date=first + timedelta(days=4),
            baseline_signal=False,
            gross_return_bps=0.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="PANW.US",
        ),
        OpeningPolicySession(
            session_date=first + timedelta(days=10),
            baseline_signal=True,
            gross_return_bps=114.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="QCOM.US",
        ),
    )
    policy = OpeningPolicySpec(
        PRODUCTION_POLICY_NAME,
        minimum_path_efficiency=0.7,
        maximum_market_return_bps=0.0,
    )

    report = evaluate_opening_policy_cohort(
        baseline,
        cohort,
        policy=policy,
        cohort_symbols=("QCOM.US", "PANW.US", "RKLB.US"),
        round_trip_cost_bps=14.0,
    )

    holdout = _cohort_slice(report, "HOLDOUT")
    assert report.algorithm_version == (
        OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION
    )
    assert report.baseline_source_sessions == 5
    assert report.cohort_source_sessions == 6
    assert report.paired_sessions == 5
    assert report.discovery_sessions == 3
    assert report.holdout_sessions == 2
    assert holdout.baseline.entries == 2
    assert holdout.baseline.cumulative_return_bps == pytest.approx(100.0)
    assert holdout.cohort.entries == 1
    assert holdout.cohort.cumulative_return_bps == pytest.approx(300.0)
    assert holdout.comparison.cumulative_delta_bps == pytest.approx(200.0)
    assert holdout.candidate_displacement_sessions == 2
    assert holdout.execution_displacement_sessions == 2
    assert holdout.baseline_only_entry_sessions == 1
    assert holdout.cohort_only_entry_sessions == 0
    assert holdout.cohort_symbol_entry_sessions == 1
    assert len(holdout.displacements) == 2
    assert holdout.displacements[0].session_date == first + timedelta(days=3)
    assert holdout.displacements[0].delta_bps == pytest.approx(100.0)
    assert holdout.displacements[1].session_date == first + timedelta(days=4)
    assert holdout.displacements[1].delta_bps == pytest.approx(100.0)
    assert holdout.tail_robustness_available is False
    assert holdout.tail_robustness_passed is False
    assert report.diagnostic_only is True
    assert report.automatic_promotion_allowed is False
    assert report.to_dict()["cohort_symbols"] == [
        "QCOM.US",
        "PANW.US",
        "RKLB.US",
    ]


def test_cohort_report_requires_unique_paired_sessions_and_symbols() -> None:
    policy = OpeningPolicySpec(PRODUCTION_POLICY_NAME)
    duplicate = (_sessions()[0], _sessions()[0], _sessions()[1])

    with pytest.raises(ValueError, match="dates must be unique"):
        evaluate_opening_policy_cohort(
            duplicate,
            _sessions()[:2],
            policy=policy,
            cohort_symbols=("QCOM.US",),
            round_trip_cost_bps=14.0,
        )
    with pytest.raises(ValueError, match="non-empty symbols"):
        evaluate_opening_policy_cohort(
            _sessions()[:2],
            _sessions()[:2],
            policy=policy,
            cohort_symbols=(),
            round_trip_cost_bps=14.0,
        )


def test_horizon_report_pairs_identical_decisions_and_exit_returns() -> None:
    first = date(2026, 1, 2)
    baseline_values = (100.0, -50.0, 40.0, 80.0, -20.0, 30.0)
    challenger_values = (120.0, -40.0, 20.0, 100.0, -100.0, 60.0)

    def build(
        values: tuple[float, ...],
        *,
        stop_indexes: set[int],
    ) -> tuple[OpeningPolicySession, ...]:
        return tuple(
            OpeningPolicySession(
                session_date=first + timedelta(days=index),
                baseline_signal=True,
                gross_return_bps=value + 14.0,
                market_return_bps=-1.0,
                candidate_path_efficiency=0.8,
                candidate_symbol=f"S{index}.US",
                stop_triggered=index in stop_indexes,
            )
            for index, value in enumerate(values)
        )

    baseline = build(baseline_values, stop_indexes={4})
    challenger = build(challenger_values, stop_indexes={1, 4}) + (
        OpeningPolicySession(
            session_date=first + timedelta(days=10),
            baseline_signal=True,
            gross_return_bps=114.0,
            market_return_bps=-1.0,
            candidate_path_efficiency=0.8,
            candidate_symbol="EXTRA.US",
        ),
    )
    report = evaluate_opening_policy_horizons(
        {60: baseline, 90: challenger},
        baseline_holding_minutes=60,
        policy=OpeningPolicySpec(
            PRODUCTION_POLICY_NAME,
            minimum_path_efficiency=0.7,
            maximum_market_return_bps=0.0,
        ),
        round_trip_cost_bps=14.0,
    )

    discovery = _horizon_slice(report, 90, "DISCOVERY")
    holdout = _horizon_slice(report, 90, "HOLDOUT")
    assert report.algorithm_version == (
        OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION
    )
    sources = [
        (value.holding_minutes, value.source_sessions)
        for value in report.sources
    ]
    assert sources == [
        (60, 6),
        (90, 7),
    ]
    assert report.paired_sessions == 6
    assert report.discovery_sessions == 3
    assert report.holdout_sessions == 3
    assert discovery.baseline.cumulative_return_bps == pytest.approx(90.0)
    assert discovery.challenger.cumulative_return_bps == pytest.approx(100.0)
    assert discovery.comparison.cumulative_delta_bps == pytest.approx(10.0)
    assert holdout.baseline.cumulative_return_bps == pytest.approx(90.0)
    assert holdout.challenger.cumulative_return_bps == pytest.approx(60.0)
    assert holdout.comparison.cumulative_delta_bps == pytest.approx(-30.0)
    assert holdout.baseline.stop_exits == 1
    assert holdout.challenger.stop_exits == 1
    assert holdout.changed_return_sessions == 3
    assert holdout.deltas[1].delta_bps == pytest.approx(-80.0)
    assert holdout.tail_robustness_available is False
    assert holdout.tail_robustness_passed is False
    assert report.diagnostic_only is True
    assert report.automatic_promotion_allowed is False
    assert report.to_dict()["baseline_holding_minutes"] == 60


def test_horizon_report_rejects_changed_signal_decisions() -> None:
    baseline = _sessions()[:2]
    changed = (
        OpeningPolicySession(
            session_date=baseline[0].session_date,
            baseline_signal=True,
            gross_return_bps=baseline[0].gross_return_bps,
            market_return_bps=baseline[0].market_return_bps,
            candidate_path_efficiency=(
                baseline[0].candidate_path_efficiency
            ),
            candidate_symbol="CHANGED.US",
        ),
        baseline[1],
    )

    with pytest.raises(ValueError, match="changed the signal decision"):
        evaluate_opening_policy_horizons(
            {60: baseline, 90: changed},
            baseline_holding_minutes=60,
            policy=OpeningPolicySpec(PRODUCTION_POLICY_NAME),
            round_trip_cost_bps=14.0,
        )
    with pytest.raises(ValueError, match="baseline holding horizon"):
        evaluate_opening_policy_horizons(
            {90: baseline, 120: baseline},
            baseline_holding_minutes=60,
            policy=OpeningPolicySpec(PRODUCTION_POLICY_NAME),
            round_trip_cost_bps=14.0,
        )
