"""Reporting thresholds cannot manufacture or suppress abandonment evidence."""
from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.domain.strategy_v2 import futility, signal_edge
from app.domain.strategy_v2.signal_edge import (
    assess_first_passage,
    assess_signal_edge,
    clustered_t_test,
)


def _observations(days: int, mean: float, shock: float) -> list[tuple[date, float]]:
    return [
        (date(2026, 8, 1) + timedelta(days=day), mean + (shock if day % 2 == 0 else -shock))
        for day in range(days)
        for _ in range(3 if days == 18 else (10 if day < 16 else 9))
    ]


@pytest.mark.parametrize("floors", [(30, 20), (1, 1), (5, 3), (54, 18)])
def test_thin_cohort_cannot_manufacture_futile(floors: tuple[int, int]) -> None:
    # Given: 54 trades over only 18 days, with enough apparent MDE power.
    observations = _observations(18, -0.30, 0.05)
    first_passage = assess_first_passage(
        target_hits=20, stop_hits=34, stop_pct=0.45, target_pct=0.80,
    )
    # When: the caller changes only reporting floors.
    result = assess_signal_edge(
        first_passage=first_passage,
        gross=clustered_t_test(observations),
        clustered=clustered_t_test([(day, value - 0.10) for day, value in observations]),
        min_resolved_trades=floors[0], min_distinct_days=floors[1],
    )
    # Then: low day counts cannot authorize an abandonment diagnostic.
    assert (result.gross.observations, result.gross.distinct_days) == (54, 18)
    assert result.futility.powered_for_required_effect is True
    assert result.futility.status == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("floors", [(30, 20), (1, 1), (40, 40), (200, 100)])
@pytest.mark.parametrize("mean, expected", [(-0.0057, "FUTILE"), (0.10, "ALIVE")])
def test_honest_cohort_status_cannot_be_suppressed(
    floors: tuple[int, int], mean: float, expected: str,
) -> None:
    # Given: 232 trades over 24 days, genuinely above both analysis floors.
    observations = _observations(24, mean, 0.12205)
    first_passage = assess_first_passage(
        target_hits=100, stop_hits=132, stop_pct=0.45, target_pct=0.80,
    )
    # When: even a raised reporting floor is requested.
    result = assess_signal_edge(
        first_passage=first_passage,
        gross=clustered_t_test(observations),
        clustered=clustered_t_test([(day, value - 0.10) for day, value in observations]),
        min_resolved_trades=floors[0], min_distinct_days=floors[1],
    )
    # Then: the fixed stopping rule remains active in both legitimate branches.
    assert (result.gross.observations, result.gross.distinct_days) == (232, 24)
    assert result.futility.mde_bps == pytest.approx(10.15, abs=0.01)
    assert result.futility.required_effect_bps == pytest.approx(10.0 - mean * 100)
    assert result.futility.gross_upper_bound_bps == pytest.approx(mean * 100 + 5.10, abs=0.02)
    assert result.futility.status == expected


@pytest.mark.parametrize("floors, expected", [((1, 1), "FAIL"), ((30, 20), "INSUFFICIENT_DATA")])
def test_reporting_floors_still_control_verdict(floors: tuple[int, int], expected: str) -> None:
    # Given: the same thin exploit cohort.
    observations = _observations(18, -0.30, 0.05)
    # When: diagnostic reporting is explicitly configured by the caller.
    result = assess_signal_edge(
        first_passage=assess_first_passage(
            target_hits=20, stop_hits=34, stop_pct=0.45, target_pct=0.80,
        ),
        gross=clustered_t_test(observations),
        clustered=clustered_t_test([(day, value - 0.10) for day, value in observations]),
        min_resolved_trades=floors[0], min_distinct_days=floors[1],
    )
    # Then: reporting flexibility is not removed along with the exploit.
    assert result.verdict == expected


def test_preregistered_analysis_constants_and_signatures_are_pinned() -> None:
    # Given / When: inspect the public domain contract, not a particular caller.
    futility_parameters = inspect.signature(futility.assess_futility).parameters
    signal_parameters = inspect.signature(assess_signal_edge).parameters
    # Then: evidence permissions cannot be injected, and there is one numeric floor.
    assert futility.PREREGISTERED_MIN_RESOLVED_BRACKETS == signal_edge.DEFAULT_MIN_RESOLVED_TRADES == 30
    assert futility.PREREGISTERED_MIN_DISTINCT_DAYS == signal_edge.DEFAULT_MIN_DISTINCT_DAYS == 20
    assert futility.FUTILITY_BOUND_CRITICAL_VALUE == 2.0
    assert "evidence_sufficient" not in futility_parameters
    assert "resolved_brackets" in futility_parameters
    assert not {
        "bound_critical_value", "cost_floor_bps", "sigma_day_bps", "evidence_sufficient",
    }.intersection(signal_parameters)


@pytest.mark.parametrize("resolved", [29, 30])
@pytest.mark.parametrize("gross_count", [29, 30])
@pytest.mark.parametrize("net_count", [29, 30])
@pytest.mark.parametrize("gross_days", [19, 20])
@pytest.mark.parametrize("net_days", [19, 20])
def test_each_evidence_floor_matches_default_reporting(
    resolved: int, gross_count: int, net_count: int, gross_days: int, net_days: int,
) -> None:
    # Given: independent boundaries for all five counts; returns are powered and negative.
    evidence = clustered_t_test(_observations(24, -0.30, 0.05))
    gross = replace(evidence, observations=gross_count, distinct_days=gross_days)
    net = replace(evidence, observations=net_count, distinct_days=net_days, naive_mean=-0.40)
    first_passage = assess_first_passage(
        target_hits=0, stop_hits=resolved, stop_pct=0.45, target_pct=0.80,
    )
    expected = (
        resolved >= 30 and gross_count >= 30 and net_count >= 30
        and gross_days >= 20 and net_days >= 20
    )
    # When: assess with DEFAULT reporting settings.
    result = assess_signal_edge(first_passage=first_passage, gross=gross, clustered=net)
    # Then: the derived gate is exactly the previous five-condition default gate.
    assert result.futility.evidence_floor_met is expected
    assert (result.verdict != "INSUFFICIENT_DATA") is expected
    assert result.futility.status == ("FUTILE" if expected else "INSUFFICIENT_DATA")
    assert result.futility.resolved_brackets == resolved
    assert result.futility.required_resolved_brackets == 30
    assert result.futility.required_distinct_days == 20
