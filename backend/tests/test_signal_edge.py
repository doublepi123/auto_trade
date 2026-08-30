"""Signal edge gate — first-passage and cluster-robust significance."""
from __future__ import annotations

import math
from datetime import date

from app.domain.strategy_v2.signal_edge import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_PASS,
    assess_first_passage,
    assess_signal_edge,
    binomial_p_upper,
    clustered_t_test,
    first_passage_baseline,
)


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
