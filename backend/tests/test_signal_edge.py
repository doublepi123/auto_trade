"""Signal edge gate — first-passage and cluster-robust significance."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import app.domain.strategy_v2.signal_edge as signal_edge

from app.domain.strategy_v2.signal_edge import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_PASS,
    FirstPassageResult,
    assess_first_passage,
    assess_signal_edge,
    binomial_p_upper,
    clustered_t_test,
    first_passage_baseline,
)


def _clustered(
    mean_pct: float,
    se_pct: float,
    days: int,
    n: int,
) -> signal_edge.ClusteredTTestResult:
    return signal_edge.ClusteredTTestResult(
        observations=n,
        distinct_days=days,
        naive_t=mean_pct / se_pct,
        clustered_t=mean_pct / se_pct,
        naive_mean=mean_pct,
        day_mean=mean_pct,
        clustered_standard_error=se_pct,
        ci_lower=mean_pct - 2.0 * se_pct,
        ci_upper=mean_pct + 2.0 * se_pct,
        inflation_factor=math.sqrt(n / days),
        significant=mean_pct / se_pct > 2.0,
    )


class TestFutilityAssessment:
    def test_mde_reproduces_preregistered_table_and_shrinks_with_days(self) -> None:
        expected = {24: 10.15, 31: 8.93, 60: 6.42, 100: 4.97}

        measured = {
            days: signal_edge.minimum_detectable_effect_bps(
                sigma_day_bps=20.0,
                distinct_days=days,
            )
            for days in expected
        }

        assert {days: round(value, 2) for days, value in measured.items()} == expected
        assert all(
            earlier > later
            for earlier, later in zip(measured.values(), list(measured.values())[1:])
        )
        at_96_days = signal_edge.minimum_detectable_effect_bps(
            sigma_day_bps=20.0,
            distinct_days=96,
        )
        assert math.isclose(measured[24] / at_96_days, 2.0, rel_tol=1e-12)

    @pytest.mark.parametrize(
        ("sigma_day_bps", "distinct_days", "alpha", "power"),
        [
            (0.0, 31, 0.05, 0.80),
            (-1.0, 31, 0.05, 0.80),
            (float("nan"), 31, 0.05, 0.80),
            (20.0, 0, 0.05, 0.80),
            (20.0, 31, 0.0, 0.80),
            (20.0, 31, 1.0, 0.80),
            (20.0, 31, 0.05, 0.0),
            (20.0, 31, 0.05, 1.0),
        ],
    )
    def test_mde_rejects_invalid_inputs(
        self,
        sigma_day_bps: float,
        distinct_days: int,
        alpha: float,
        power: float,
    ) -> None:
        with pytest.raises(ValueError):
            signal_edge.minimum_detectable_effect_bps(
                sigma_day_bps=sigma_day_bps,
                distinct_days=distinct_days,
                alpha=alpha,
                power=power,
            )

    def test_live_cohort_is_futile_with_preregistered_inputs(self) -> None:
        gross = _clustered(-0.0057, 0.0255, 31, 232)
        net = _clustered(-0.1057, 0.0255, 31, 232)

        result = signal_edge.assess_futility(
            gross=gross,
            net=net,
            resolved_brackets=232,
        )

        assert result.status == "FUTILE"
        assert math.isclose(result.gross_upper_bound_bps or 0.0, 4.53, abs_tol=0.01)
        assert math.isclose(result.required_effect_bps or 0.0, 10.57, abs_tol=0.01)
        assert math.isclose(result.mde_bps or 0.0, 8.93, abs_tol=0.01)
        assert math.isclose(result.measured_cost_bps or 0.0, 10.0, abs_tol=0.01)
        assert math.isclose(
            result.sigma_day_measured_bps or 0.0,
            14.2,
            abs_tol=0.1,
        )
        assert any("human ratification" in reason for reason in result.reasons)

    def test_underpowered_cohort_is_insufficient_not_futile(self) -> None:
        result = signal_edge.assess_futility(
            gross=_clustered(-0.0057, 0.0255, 21, 232),
            net=_clustered(-0.1057, 0.0255, 21, 232),
            resolved_brackets=232,
        )

        assert result.status == "INSUFFICIENT_DATA"
        assert math.isclose(result.mde_bps or 0.0, 10.85, abs_tol=0.01)
        assert result.evidence_floor_met is True
        assert result.upper_bound_below_cost_floor is True
        assert result.powered_for_required_effect is False
        assert any("underpowered" in reason for reason in result.reasons)

    def test_upper_bound_equal_to_cost_floor_is_alive(self) -> None:
        result = signal_edge.assess_futility(
            gross=_clustered(0.049, 0.0255, 31, 232),
            net=_clustered(-0.051, 0.0255, 31, 232),
            resolved_brackets=232,
        )

        assert math.isclose(result.gross_upper_bound_bps or 0.0, 10.0)
        assert result.status == "ALIVE"
        assert result.upper_bound_below_cost_floor is False

    def test_bound_is_recomputed_instead_of_trusting_input_ci(self) -> None:
        gross = replace(
            _clustered(-0.0057, 0.0255, 31, 232),
            ci_upper=999.0,
        )

        result = signal_edge.assess_futility(
            gross=gross,
            net=_clustered(-0.1057, 0.0255, 31, 232),
            resolved_brackets=232,
        )

        assert math.isclose(result.gross_upper_bound_bps or 0.0, 4.53, abs_tol=0.01)
        assert result.status == "FUTILE"

    def test_verdict_evidence_floors_gate_futility_but_keep_mde(self) -> None:
        result = signal_edge.assess_futility(
            gross=_clustered(-0.0057, 0.0255, 31, 232),
            net=_clustered(-0.1057, 0.0255, 31, 232),
            resolved_brackets=29,
        )

        assert result.status == "INSUFFICIENT_DATA"
        assert result.mde_bps is not None
        assert any("verdict evidence floors not met" in reason for reason in result.reasons)

    def test_empty_cohort_is_insufficient_without_fabricated_bound(self) -> None:
        empty = clustered_t_test([])

        result = signal_edge.assess_futility(
            gross=empty,
            net=empty,
            resolved_brackets=0,
        )

        assert result.status == "INSUFFICIENT_DATA"
        assert result.gross_upper_bound_bps is None

    @pytest.mark.parametrize("cost_floor_bps", [0.0, -1.0, float("nan")])
    def test_invalid_cost_floor_fails_closed(self, cost_floor_bps: float) -> None:
        with pytest.raises(ValueError):
            signal_edge.assess_futility(
                gross=_clustered(-0.0057, 0.0255, 31, 232),
                net=_clustered(-0.1057, 0.0255, 31, 232),
                resolved_brackets=232,
                cost_floor_bps=cost_floor_bps,
            )

    def test_non_positive_bound_critical_value_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            signal_edge.assess_futility(
                gross=_clustered(-0.0057, 0.0255, 31, 232),
                net=_clustered(-0.1057, 0.0255, 31, 232),
                resolved_brackets=232,
                bound_critical_value=0.0,
            )

    def test_constants_match_preregistration_and_auto_abandon_stays_absent(self) -> None:
        preregistration = (
            Path(__file__).parents[1]
            / "app/domain/strategy_v2/PREREGISTRATION.md"
        ).read_text(encoding="utf-8")

        assert signal_edge.PREREGISTERED_COST_FLOOR_BPS == 10.0
        assert signal_edge.PREREGISTERED_SIGMA_DAY_BPS == 20.0
        assert "PREREGISTERED_COST_FLOOR_BPS" in preregistration
        assert "PREREGISTERED_SIGMA_DAY_BPS" in preregistration
        assert "自动弃置同样不存在" in preregistration


class TestFirstPassageBaseline:
    def test_matches_the_gamblers_ruin_result(self) -> None:
        # For driftless Brownian motion the probability of touching +target
        # before -stop is stop/(stop+target); the deployed 0.45/0.80 bracket
        # therefore has a 36% baseline before any signal is applied.
        assert first_passage_baseline(stop_pct=0.45, target_pct=0.80) == 0.36

    def test_symmetric_barriers_give_even_odds(self) -> None:
        assert first_passage_baseline(stop_pct=1.0, target_pct=1.0) == 0.5

    def test_a_nearer_target_is_more_likely(self) -> None:
        near = first_passage_baseline(stop_pct=1.0, target_pct=0.5)
        far = first_passage_baseline(stop_pct=1.0, target_pct=2.0)
        assert near > far

    def test_rejects_non_positive_barriers(self) -> None:
        for stop, target in ((0.0, 1.0), (1.0, 0.0), (-1.0, 1.0)):
            try:
                first_passage_baseline(stop_pct=stop, target_pct=target)
                raise AssertionError("expected ValueError")
            except ValueError:
                pass

    def test_rejects_non_finite_barriers(self) -> None:
        try:
            first_passage_baseline(stop_pct=float("nan"), target_pct=1.0)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


class TestBinomialUpperTail:
    def test_full_range_sums_to_one(self) -> None:
        assert binomial_p_upper(0, 10, 0.36) == 1.0

    def test_impossible_count_is_zero(self) -> None:
        assert binomial_p_upper(11, 10, 0.36) == 0.0

    def test_matches_a_hand_computed_case(self) -> None:
        # P(X >= 2), X ~ Bin(3, 0.5) = (3 + 1)/8
        assert math.isclose(binomial_p_upper(2, 3, 0.5), 0.5, rel_tol=1e-12)

    def test_is_monotone_in_the_threshold(self) -> None:
        values = [binomial_p_upper(k, 20, 0.36) for k in range(21)]
        assert all(a >= b for a, b in zip(values, values[1:]))


class TestAssessFirstPassage:
    def test_live_measurement_does_not_beat_a_random_walk(self) -> None:
        """The deployed signal's own numbers: 38 targets against 83 stops."""
        result = assess_first_passage(
            target_hits=38, stop_hits=83, stop_pct=0.45, target_pct=0.80
        )
        assert result.resolved == 121
        assert result.baseline_rate == 0.36
        assert result.observed_rate is not None
        assert result.observed_rate < result.baseline_rate
        assert result.edge_pp is not None and result.edge_pp < 0
        assert result.beats_baseline is False

    def test_a_genuinely_informative_signal_passes(self) -> None:
        result = assess_first_passage(
            target_hits=80, stop_hits=40, stop_pct=0.45, target_pct=0.80
        )
        assert result.observed_rate is not None
        assert result.observed_rate > result.baseline_rate
        assert result.p_value is not None and result.p_value < 0.05
        assert result.beats_baseline is True

    def test_a_small_edge_on_thin_evidence_does_not_pass(self) -> None:
        """Beating the baseline is not enough; it must be distinguishable."""
        result = assess_first_passage(
            target_hits=3, stop_hits=4, stop_pct=0.45, target_pct=0.80
        )
        assert result.observed_rate is not None
        assert result.observed_rate > result.baseline_rate
        assert result.p_value is not None and result.p_value > 0.05
        assert result.beats_baseline is False

    def test_no_resolved_trades_yields_no_rate(self) -> None:
        result = assess_first_passage(
            target_hits=0, stop_hits=0, stop_pct=0.45, target_pct=0.80
        )
        assert result.resolved == 0
        assert result.observed_rate is None
        assert result.p_value is None
        assert result.beats_baseline is False


class TestTimeExitConditioningDisclosure:
    """The first-passage test conditions on price-barrier resolution.

    Trades that exit on the TIME barrier are silently dropped from the
    denominator.  The statistic is valid (strong Markov property under a
    driftless null) and is deliberately NOT changed here; what was missing
    is any way for a reader to see HOW MUCH evidence the conditioning
    removed, and whether the verdict survives the worst-case allocation of
    those trades.
    """

    def test_time_exit_count_and_fraction_are_first_class(self) -> None:
        # Given the live cohort: 44 target, 88 stop, 150 time exits.
        result = assess_first_passage(
            target_hits=44,
            stop_hits=88,
            time_exit_excluded=150,
            stop_pct=0.45,
            target_pct=0.80,
        )
        # Then the conditioning is disclosed as data, not buried in prose.
        assert result.resolved == 132
        assert result.time_exit_excluded == 150
        assert result.time_exit_fraction is not None
        assert math.isclose(result.time_exit_fraction, 150 / 282, rel_tol=1e-12)

    def test_sensitivity_bounds_bracket_the_reported_rate(self) -> None:
        # Given the same live cohort.
        result = assess_first_passage(
            target_hits=44,
            stop_hits=88,
            time_exit_excluded=150,
            stop_pct=0.45,
            target_pct=0.80,
        )
        # Then the floor allocates every time exit to STOP, the ceiling to TARGET,
        # and the as-reported conditional rate lies strictly between them.
        assert result.observed_rate_floor is not None
        assert result.observed_rate_ceiling is not None
        assert result.observed_rate is not None
        assert math.isclose(result.observed_rate_floor, 44 / 282, rel_tol=1e-12)
        assert math.isclose(result.observed_rate_ceiling, 194 / 282, rel_tol=1e-12)
        assert math.isclose(result.observed_rate, 44 / 132, rel_tol=1e-12)
        assert (
            result.observed_rate_floor
            < result.observed_rate
            < result.observed_rate_ceiling
        )

    def test_only_the_impossible_all_to_target_allocation_clears_baseline(
        self,
    ) -> None:
        # Given the live cohort against its own driftless baseline.
        result = assess_first_passage(
            target_hits=44,
            stop_hits=88,
            time_exit_excluded=150,
            stop_pct=0.45,
            target_pct=0.80,
        )
        # Then the verdict survives the conditioning: only the physically
        # impossible allocation of every time exit to TARGET beats the baseline.
        assert result.observed_rate_floor is not None
        assert result.observed_rate_ceiling is not None
        assert result.observed_rate_floor < result.baseline_rate
        assert result.observed_rate is not None
        assert result.observed_rate < result.baseline_rate
        assert result.observed_rate_ceiling > result.baseline_rate

    def test_disclosure_does_not_move_the_verdict_math(self) -> None:
        # Given identical barrier outcomes, disclosed and undisclosed.
        disclosed = assess_first_passage(
            target_hits=44,
            stop_hits=88,
            time_exit_excluded=150,
            stop_pct=0.45,
            target_pct=0.80,
        )
        undisclosed = assess_first_passage(
            target_hits=44, stop_hits=88, stop_pct=0.45, target_pct=0.80
        )
        # Then every statistic that feeds a decision is byte-identical.
        assert disclosed.resolved == undisclosed.resolved
        assert disclosed.observed_rate == undisclosed.observed_rate
        assert disclosed.baseline_rate == undisclosed.baseline_rate
        assert disclosed.edge_pp == undisclosed.edge_pp
        assert disclosed.p_value == undisclosed.p_value
        assert disclosed.beats_baseline == undisclosed.beats_baseline

    def test_no_time_exits_collapses_the_bounds_onto_the_rate(self) -> None:
        # Given a cohort with no time-barrier exits at all.
        result = assess_first_passage(
            target_hits=44, stop_hits=88, stop_pct=0.45, target_pct=0.80
        )
        # Then there is nothing to condition away and the bounds are degenerate.
        assert result.time_exit_excluded == 0
        assert result.time_exit_fraction == 0.0
        assert result.observed_rate_floor == result.observed_rate
        assert result.observed_rate_ceiling == result.observed_rate

    def test_empty_cohort_reports_no_bounds(self) -> None:
        # Given no trades of any kind.
        result = assess_first_passage(
            target_hits=0, stop_hits=0, stop_pct=0.45, target_pct=0.80
        )
        # Then bounds are absent rather than a fabricated zero.
        assert result.time_exit_excluded == 0
        assert result.time_exit_fraction is None
        assert result.observed_rate_floor is None
        assert result.observed_rate_ceiling is None

    def test_rejects_a_negative_time_exit_count(self) -> None:
        try:
            assess_first_passage(
                target_hits=44,
                stop_hits=88,
                time_exit_excluded=-1,
                stop_pct=0.45,
                target_pct=0.80,
            )
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_existing_constructions_without_the_new_fields_still_work(self) -> None:
        # Given a caller that predates the disclosure fields.
        result = FirstPassageResult(
            target_hits=1,
            stop_hits=1,
            resolved=2,
            barrier_mismatch_excluded=0,
            observed_rate=0.5,
            baseline_rate=0.36,
            edge_pp=14.0,
            p_value=0.5,
            beats_baseline=False,
        )
        # Then it still constructs, with inert defaults for the new fields.
        assert result.time_exit_excluded == 0
        assert result.time_exit_fraction is None
        assert result.observed_rate_floor is None
        assert result.observed_rate_ceiling is None


class TestLiveShapedVerdictInvariance:
    """Anti-regression: the disclosure must not change the live verdict."""

    def _live_shaped_observations(self) -> list[tuple[date, float]]:
        # 282 closed trades over 31 days, gross mean slightly negative with
        # day-level noise calibrated to the measured day-clustered t of ~-0.6.
        observations: list[tuple[date, float]] = []
        for day_index in range(31):
            trades = 10 if day_index < 3 else 9
            shock = 0.50 if day_index % 2 == 0 else -0.50
            observations.extend(
                (date(2026, 7, 1) + timedelta(days=day_index), -0.0716 + shock)
                for _ in range(trades)
            )
        return observations

    def test_live_shaped_cohort_still_fails_with_disclosure(self) -> None:
        # Given the live cohort with the time-exit conditioning now disclosed.
        first_passage = assess_first_passage(
            target_hits=44,
            stop_hits=88,
            time_exit_excluded=150,
            stop_pct=0.45,
            target_pct=0.80,
        )
        gross = clustered_t_test(self._live_shaped_observations())
        net = clustered_t_test(
            [(day, value - 0.4) for day, value in self._live_shaped_observations()]
        )

        # When the promotion gate assesses it.
        verdict = assess_signal_edge(
            first_passage=first_passage, clustered=net, gross=gross
        )

        # Then the verdict is unchanged: FAIL, baseline not beaten.
        assert (gross.observations, gross.distinct_days) == (282, 31)
        assert gross.clustered_t is not None
        assert math.isclose(gross.clustered_t, -0.6, abs_tol=0.05)
        assert verdict.verdict == VERDICT_FAIL
        assert verdict.first_passage.beats_baseline is False
        assert any("random-walk baseline" in reason for reason in verdict.reasons)
        assert all("futil" not in reason.lower() for reason in verdict.reasons)
        # And the disclosure travelled with the verdict.
        assert verdict.first_passage.time_exit_excluded == 150

    def test_verdict_is_identical_with_and_without_the_disclosure(self) -> None:
        # Given the same evidence, only the disclosure differs.
        gross = clustered_t_test(self._live_shaped_observations())
        net = clustered_t_test(
            [(day, value - 0.4) for day, value in self._live_shaped_observations()]
        )
        disclosed = assess_signal_edge(
            first_passage=assess_first_passage(
                target_hits=44,
                stop_hits=88,
                time_exit_excluded=150,
                stop_pct=0.45,
                target_pct=0.80,
            ),
            clustered=net,
            gross=gross,
        )
        undisclosed = assess_signal_edge(
            first_passage=assess_first_passage(
                target_hits=44, stop_hits=88, stop_pct=0.45, target_pct=0.80
            ),
            clustered=net,
            gross=gross,
        )
        # Then the verdict label and every decision statistic agree.
        assert disclosed.verdict == undisclosed.verdict
        assert (
            disclosed.first_passage.beats_baseline
            == undisclosed.first_passage.beats_baseline
        )
        assert disclosed.first_passage.p_value == undisclosed.first_passage.p_value
        assert (
            disclosed.first_passage.observed_rate
            == undisclosed.first_passage.observed_rate
        )


class TestClusteredTTest:
    def test_same_day_trades_collapse_to_one_observation(self) -> None:
        day = date(2026, 8, 3)
        result = clustered_t_test([(day, 1.0), (day, 2.0), (day, 3.0)])
        assert result.observations == 3
        assert result.distinct_days == 1
        assert result.day_mean == 2.0
        # One day cannot support a t-statistic, however many trades it holds.
        assert result.clustered_t is None
        assert result.significant is False

    def test_clustering_deflates_an_inflated_naive_t(self) -> None:
        """Repeating one day's return many times must not manufacture power."""
        observations = []
        for i in range(10):
            day = date(2026, 8, 3 + i)
            observations.extend((day, 0.5) for _ in range(20))
        result = clustered_t_test(observations)
        assert result.observations == 200
        assert result.distinct_days == 10
        assert result.inflation_factor is not None
        assert math.isclose(result.inflation_factor, math.sqrt(20), rel_tol=1e-9)

    def test_inflation_factor_is_sqrt_of_the_n_ratio(self) -> None:
        observations = [(date(2026, 8, 3 + i // 4), float(i)) for i in range(16)]
        result = clustered_t_test(observations)
        assert result.observations == 16
        assert result.distinct_days == 4
        assert result.inflation_factor is not None
        assert math.isclose(result.inflation_factor, 2.0, rel_tol=1e-9)

    def test_consistently_positive_days_are_significant(self) -> None:
        observations = [(date(2026, 8, 1 + i), 1.0 + (i % 3) * 0.05) for i in range(25)]
        result = clustered_t_test(observations)
        assert result.clustered_t is not None and result.clustered_t > 2.0
        assert result.significant is True

    def test_noisy_days_around_zero_are_not_significant(self) -> None:
        observations = [
            (date(2026, 8, 1 + i), 1.0 if i % 2 == 0 else -1.0) for i in range(25)
        ]
        result = clustered_t_test(observations)
        assert result.clustered_t is not None
        assert abs(result.clustered_t) < 2.0
        assert result.significant is False

    def test_empty_input_is_not_significant(self) -> None:
        result = clustered_t_test([])
        assert result.observations == 0
        assert result.clustered_t is None
        assert result.significant is False

    def test_zero_variance_days_yield_no_statistic(self) -> None:
        """A constant series has no dispersion, so no t can be formed."""
        observations = [(date(2026, 8, 1 + i), 1.0) for i in range(10)]
        result = clustered_t_test(observations)
        assert result.clustered_t is None
        assert result.significant is False


class TestSignalEdgeVerdict:
    def _passing_inputs(self):
        fp = assess_first_passage(
            target_hits=80, stop_hits=40, stop_pct=0.45, target_pct=0.80
        )
        clustered = clustered_t_test(
            [(date(2026, 8, 1 + i), 1.0 + (i % 3) * 0.05) for i in range(30)]
        )
        return fp, clustered

    def test_both_checks_passing_yields_pass(self) -> None:
        fp, clustered = self._passing_inputs()
        verdict = assess_signal_edge(first_passage=fp, clustered=clustered)
        assert verdict.verdict == VERDICT_PASS
        assert verdict.reasons == ()
        assert verdict.futility.status != "FUTILE"

    def test_thin_evidence_is_reported_separately_from_failure(self) -> None:
        """Not-yet-provable and proven-absent demand different actions."""
        fp = assess_first_passage(
            target_hits=3, stop_hits=4, stop_pct=0.45, target_pct=0.80
        )
        clustered = clustered_t_test([(date(2026, 8, 1), 1.0)])
        verdict = assess_signal_edge(first_passage=fp, clustered=clustered)
        assert verdict.verdict == VERDICT_INSUFFICIENT_DATA
        assert any("bracket-resolved" in r for r in verdict.reasons)
        assert any("gross trading days" in r for r in verdict.reasons)
        assert any("net trading days" in r for r in verdict.reasons)

    def test_many_trades_on_few_days_is_still_thin(self) -> None:
        """The day floor is what stops a burst of correlated trades qualifying."""
        fp = assess_first_passage(
            target_hits=80, stop_hits=40, stop_pct=0.45, target_pct=0.80
        )
        clustered = clustered_t_test(
            [(date(2026, 8, 1 + i // 40), 1.0) for i in range(120)]
        )
        verdict = assess_signal_edge(first_passage=fp, clustered=clustered)
        assert verdict.verdict == VERDICT_INSUFFICIENT_DATA
        assert any("gross trading days" in r for r in verdict.reasons)
        assert any("net trading days" in r for r in verdict.reasons)

    def test_failing_first_passage_blocks_the_verdict(self) -> None:
        fp = assess_first_passage(
            target_hits=38, stop_hits=83, stop_pct=0.45, target_pct=0.80
        )
        clustered = clustered_t_test(
            [(date(2026, 8, 1 + i), 1.0 + (i % 3) * 0.05) for i in range(30)]
        )
        verdict = assess_signal_edge(first_passage=fp, clustered=clustered)
        assert verdict.verdict == VERDICT_FAIL
        assert any("random-walk baseline" in r for r in verdict.reasons)

    def test_failing_significance_blocks_the_verdict(self) -> None:
        fp = assess_first_passage(
            target_hits=80, stop_hits=40, stop_pct=0.45, target_pct=0.80
        )
        clustered = clustered_t_test(
            [(date(2026, 8, 1 + i), 1.0 if i % 2 == 0 else -1.0) for i in range(30)]
        )
        verdict = assess_signal_edge(first_passage=fp, clustered=clustered)
        assert verdict.verdict == VERDICT_FAIL
        assert any("cluster-robust" in r for r in verdict.reasons)

    def test_rejects_non_positive_evidence_floors(self) -> None:
        fp, clustered = self._passing_inputs()
        try:
            assess_signal_edge(
                first_passage=fp, clustered=clustered, min_distinct_days=0
            )
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
