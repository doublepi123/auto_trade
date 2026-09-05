from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.domain.strategy_v2 import clustered_returns
from app.domain.strategy_v2.futility import FUTILITY_BOUND_CRITICAL_VALUE, assess_futility
from app.domain.strategy_v2.signal_edge import assess_first_passage, assess_promotion


def _observations(days: int) -> list[tuple[date, float]]:
    # Balanced unit shocks give SE = 1/sqrt(D-1), hence t = 2.025.
    mean = 2.025 / math.sqrt(days - 1)
    return [
        (date(2026, 1, 1) + timedelta(days=i), mean + (-1.0 if i % 2 else 1.0))
        for i in range(days)
    ]


def test_borderline_28_day_cohort_is_not_significant() -> None:
    # Given: t lies above 2.0 but below the df=27 two-sided 95% bar.
    observations = _observations(28)
    # When: the caller omits the critical-value override.
    result = clustered_returns.clustered_t_test(observations)
    # Then: the old fixed bar must not certify this cohort.
    assert result.clustered_t is not None and 2.0 < result.clustered_t < 2.0518
    assert result.significant is False
    assert result.ci_lower is not None and result.ci_lower < 0
    assert result.t_critical == pytest.approx(2.051830516480, abs=5e-12)
    assert result.degrees_of_freedom == 27
    assert result.ci_upper == pytest.approx(
        2.025 / math.sqrt(27) + 2.051830516480 / math.sqrt(27), abs=5e-12,
    )


@pytest.mark.parametrize("critical", [2.0, 1.5, 3.0])
def test_explicit_override_is_used_exactly(critical: float) -> None:
    # Given / When: the same borderline cohort with an explicit numeric override.
    result = clustered_returns.clustered_t_test(_observations(28), t_critical=critical)
    # Then: both bounds and the diagnostic significance use that exact number.
    assert result.significant is (2.025 > critical)
    assert result.ci_lower == pytest.approx((2.025 - critical) / math.sqrt(27))
    assert result.ci_upper == pytest.approx((2.025 + critical) / math.sqrt(27))
    assert result.t_critical == critical
    assert result.degrees_of_freedom == 27


def test_default_rejects_cohort_beyond_fixed_table_even_with_zero_variance() -> None:
    observations = [(day, 1.0) for day, _ in _observations(122)]
    with pytest.raises(ValueError, match="day cluster count exceeds the fixed t table"):
        clustered_returns.clustered_t_test(observations)


@pytest.mark.parametrize("days", [0, 1, 122])
def test_lookup_rejects_unsupported_cluster_counts(days: int) -> None:
    with pytest.raises(ValueError, match="day cluster count.*fixed t table"):
        clustered_returns.day_cluster_t_critical(days)


def test_explicit_override_does_not_need_table_coverage() -> None:
    result = clustered_returns.clustered_t_test(_observations(122), t_critical=2.0)
    assert result.significant is True
    assert result.t_critical == 2.0
    assert result.degrees_of_freedom == 121


@pytest.mark.parametrize("days", [0, 1])
def test_thin_cohort_retains_unavailable_inference(days: int) -> None:
    result = clustered_returns.clustered_t_test([(date(2026, 1, 1), 1.0)] * days)
    assert result.significant is False
    assert result.ci_lower is None
    assert result.t_critical is None
    assert result.degrees_of_freedom is None


@pytest.mark.parametrize("critical", [0.1, 2.0, 10.0])
def test_promotion_uses_preregistered_df_bar_not_query_override(critical: float) -> None:
    # Given: a strong first-passage result and borderline net t.
    first_passage = assess_first_passage(
        target_hits=150, stop_hits=30, stop_pct=1.0, target_pct=1.0,
    )
    net = clustered_returns.clustered_t_test(_observations(28), t_critical=critical)
    # When: promotion is assessed independently of analysis thresholds.
    result = assess_promotion(first_passage, net)
    # Then: AND #1 is false even if the analysis query lowered its threshold.
    assert result.net_significant is False
    assert result.eligible is False
    assert (result.required_distinct_days, result.required_resolved_brackets) == (60, 180)


@pytest.mark.parametrize("days", [28, 60, 120])
@pytest.mark.parametrize("critical", [None, 0.1, 10.0])
def test_futility_bound_and_powered_verdict_stay_fixed(
    days: int, critical: float | None,
) -> None:
    # Given: fixed mean/SE with ample power at every tested D; only CI policy varies.
    evidence = clustered_returns.clustered_t_test(
        _observations(days) * 2, t_critical=critical,
    )
    gross = replace(evidence, naive_mean=-0.2, clustered_standard_error=0.01)
    net = replace(evidence, naive_mean=-0.3, clustered_standard_error=0.01)
    # When: futility recomputes its own bound instead of using the input CI.
    result = assess_futility(gross=gross, net=net, resolved_brackets=2 * days)
    # Then: D still affects MDE, but never the bound multiplier or this powered verdict.
    assert clustered_returns.DEFAULT_T_CRITICAL == FUTILITY_BOUND_CRITICAL_VALUE == 2.0
    assert result.bound_critical_value == 2.0
    assert result.evidence_floor_met is True
    assert result.gross_upper_bound_bps == pytest.approx(-18.0)
    assert result.status == "FUTILE"
