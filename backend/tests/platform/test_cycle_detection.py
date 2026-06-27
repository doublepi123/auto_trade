"""Tests for P260 cycle detection (autocorrelation, Ljung-Box, seasonal strength)."""

from __future__ import annotations

import math

import pytest

from app.platform.cycle_detection import (
    CycleCandidate,
    CycleDetectionResult,
    autocorrelation,
    detect_cycles,
    ljung_box_stat,
)


def _periodic_series(period: int, cycles: int, amp: float = 1.0) -> list[float]:
    """Deterministic sinusoidal series with a known integer period."""
    n = period * cycles
    return [amp * math.sin(2.0 * math.pi * i / period) for i in range(n)]


def _square_wave(period: int, cycles: int, amp: float = 1.0) -> list[float]:
    """Deterministic square wave repeating every ``period`` samples."""
    base = [amp if i % 2 == 0 else -amp for i in range(period)]
    return base * cycles


# ---------------------------------------------------------------------------
# autocorrelation
# ---------------------------------------------------------------------------


def test_autocorrelation_length_and_lag_zero():
    series = _periodic_series(period=5, cycles=6)
    acf = autocorrelation(series, max_lag=10)
    assert len(acf) == 11  # lags 0..10
    # lag-0 autocorrelation of a finite series is 1.0 (perfect self-correlation).
    assert acf[0] == pytest.approx(1.0, abs=1e-9)


def test_autocorrelation_period_5_peaks_at_lag_5():
    """A clean period-5 sinusoid has its autocorrelation peak at lag 5 (and 10)."""
    series = _periodic_series(period=5, cycles=8)
    acf = autocorrelation(series, max_lag=12)
    # Among lags 1..12 the largest peak should sit at 5 (and 10) — at minimum the
    # autocorrelation at lag 5 must exceed that at any non-multiple lag.
    candidates = {5, 10}
    best_lag = max(range(1, 13), key=lambda lag: acf[lag])
    assert best_lag in candidates


def test_autocorrelation_invalid_max_lag_raises():
    with pytest.raises(ValueError):
        autocorrelation([1.0, 2.0, 3.0], max_lag=0)
    with pytest.raises(ValueError):
        autocorrelation([1.0, 2.0, 3.0], max_lag=-1)


def test_autocorrelation_max_lag_too_large_raises():
    # series length 3 ⇒ only lag 0,1,2 are valid; max_lag must be < len.
    with pytest.raises(ValueError):
        autocorrelation([1.0, 2.0, 3.0], max_lag=3)


def test_autocorrelation_short_series_raises():
    with pytest.raises(ValueError):
        autocorrelation([1.0], max_lag=1)


def test_autocorrelation_bool_max_lag_rejected_as_value_error():
    """``max_lag=True`` must be rejected — bool is not a valid lag, and the
    public surface raises ``ValueError`` (not ``TypeError``) for invalid
    arguments so callers (and the platform endpoint) map uniformly to 422."""
    series = _periodic_series(period=5, cycles=6)
    # ``True`` is a subclass of ``int`` in Python and equals ``1`` — without an
    # explicit bool guard it would be silently accepted as lag 1.
    with pytest.raises(ValueError):
        autocorrelation(series, max_lag=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        autocorrelation(series, max_lag=False)  # type: ignore[arg-type]


def test_autocorrelation_non_int_max_lag_raises_value_error():
    """Non-int / non-bool ``max_lag`` surfaces as ``ValueError`` (P260 audit)."""
    series = _periodic_series(period=5, cycles=6)
    with pytest.raises(ValueError):
        autocorrelation(series, max_lag=2.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        autocorrelation(series, max_lag="3")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ljung_box_stat
# ---------------------------------------------------------------------------


def test_ljung_box_non_negative():
    series = _periodic_series(period=5, cycles=8)
    acf = autocorrelation(series, max_lag=8)
    stat = ljung_box_stat(acf, n=len(series))
    assert stat >= 0.0


def test_ljung_box_pure_white_noise_small():
    """Random-ish flat values ⇒ low Ljung-Box statistic."""
    # A constant series has zero variance, acf is undefined → we instead use an
    # alternating tiny-amplitude noise-like series.
    acf = [1.0, 0.01, -0.01, 0.0, 0.02]
    stat = ljung_box_stat(acf, n=100)
    assert stat >= 0.0
    assert stat < 5.0  # tiny autocorrelations ⇒ tiny statistic


def test_ljung_box_periodic_series_large():
    series = _periodic_series(period=5, cycles=20)
    acf = autocorrelation(series, max_lag=8)
    stat = ljung_box_stat(acf, n=len(series))
    # Strong autocorrelation across many lags ⇒ very large statistic.
    assert stat > 50.0


def test_ljung_box_empty_acf_raises_value_error():
    with pytest.raises(ValueError):
        ljung_box_stat([], n=10)


def test_ljung_box_lag0_only_acf_raises_value_error():
    """An ACF with only lag 0 (no non-zero lags) is malformed."""
    with pytest.raises(ValueError):
        ljung_box_stat([1.0], n=10)


def test_ljung_box_non_sequence_acf_raises_value_error():
    with pytest.raises(ValueError):
        ljung_box_stat(42, n=10)  # type: ignore[arg-type]


def test_ljung_box_non_numeric_acf_entry_raises_value_error():
    """A non-numeric entry inside ``acf`` is invalid (``ValueError``)."""
    with pytest.raises(ValueError):
        ljung_box_stat([1.0, "x", 0.5], n=10)  # type: ignore[list-item]


def test_ljung_box_bool_n_raises_value_error():
    """Boolean ``n`` must be rejected as ``ValueError`` (not silently treated
    as ``n=1``/``n=0``)."""
    acf = [1.0, 0.5, 0.25]
    with pytest.raises(ValueError):
        ljung_box_stat(acf, n=True)  # type: ignore[arg-type]


def test_ljung_box_non_int_n_raises_value_error():
    acf = [1.0, 0.5, 0.25]
    with pytest.raises(ValueError):
        ljung_box_stat(acf, n=10.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# detect_cycles
# ---------------------------------------------------------------------------


def test_detect_cycles_period_5_ranks_high():
    series = _periodic_series(period=5, cycles=10)
    result = detect_cycles(series, min_period=2, max_period=10)
    assert isinstance(result, CycleDetectionResult)
    # The strongest candidate should be a multiple of the true period 5.
    top = result.candidates[0]
    assert top.period == 5
    assert top.period == 5
    assert top.autocorrelation > 0.0
    assert top.score > 0.0


def test_detect_cycles_seasonal_strength_in_unit_interval():
    series = _periodic_series(period=5, cycles=10)
    result = detect_cycles(series, min_period=2, max_period=10)
    assert 0.0 <= result.seasonal_strength <= 1.0


def test_detect_cycles_seasonal_strength_periodic_gt_trend():
    """A clearly periodic series must score higher seasonal strength than a pure
    monotonic trend (which carries no seasonality)."""
    periodic = _square_wave(period=5, cycles=10)
    trend = [float(i) for i in range(len(periodic))]
    res_p = detect_cycles(periodic, min_period=2, max_period=10)
    res_t = detect_cycles(trend, min_period=2, max_period=10)
    assert res_p.seasonal_strength > res_t.seasonal_strength


def test_detect_cycles_too_short_raises():
    with pytest.raises(ValueError):
        detect_cycles([1.0, 2.0], min_period=2)


def test_detect_cycles_invalid_min_period_raises():
    with pytest.raises(ValueError):
        detect_cycles([1.0, 2.0, 3.0, 4.0], min_period=1)
    with pytest.raises(ValueError):
        detect_cycles([1.0, 2.0, 3.0, 4.0], min_period=0)


def test_detect_cycles_bool_period_raises_value_error():
    """Boolean ``min_period`` / ``max_period`` must be rejected as
    ``ValueError`` (not silently accepted as 1/0)."""
    series = _periodic_series(period=5, cycles=6)
    with pytest.raises(ValueError):
        detect_cycles(series, min_period=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        detect_cycles(series, min_period=2, max_period=False)  # type: ignore[arg-type]


def test_detect_cycles_non_int_period_raises_value_error():
    """Non-integer ``min_period`` / ``max_period`` surface as ``ValueError``."""
    series = _periodic_series(period=5, cycles=6)
    with pytest.raises(ValueError):
        detect_cycles(series, min_period=2.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        detect_cycles(series, min_period=2, max_period=4.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        detect_cycles(series, min_period="2")  # type: ignore[arg-type]


def test_detect_cycles_invalid_max_period_raises():
    # max_period smaller than min_period
    with pytest.raises(ValueError):
        detect_cycles([1.0] * 20, min_period=5, max_period=3)


def test_detect_cycles_invalid_max_period_exceeds_length_raises():
    # max_period must be < len(series) — at least max_period+1 samples needed.
    with pytest.raises(ValueError):
        detect_cycles([1.0, 2.0, 3.0, 4.0], min_period=2, max_period=4)


def test_detect_cycles_to_dict_roundtrip():
    series = _periodic_series(period=5, cycles=8)
    result = detect_cycles(series, min_period=2, max_period=10)
    body = result.to_dict()
    assert set(body.keys()) >= {
        "candidates",
        "seasonal_strength",
        "ljung_box_stat",
        "n",
        "max_period",
    }
    assert isinstance(body["candidates"], list)
    for cand in body["candidates"]:
        assert set(cand.keys()) == {"period", "autocorrelation", "score"}


def test_detect_cycles_frozen_result_is_immutable():
    series = _periodic_series(period=5, cycles=8)
    result = detect_cycles(series, min_period=2, max_period=10)
    with pytest.raises(Exception):
        result.seasonal_strength = 0.5  # type: ignore[misc]


def test_cycle_candidate_is_frozen():
    cand = CycleCandidate(period=5, autocorrelation=0.8, score=0.9)
    with pytest.raises(Exception):
        cand.period = 6  # type: ignore[misc]
    assert cand.period == 5


def test_cycle_candidate_to_dict_keys():
    """``CycleCandidate.to_dict()`` exposes ``period``/``autocorrelation``/
    ``score`` so it can be reused by the result aggregator."""
    cand = CycleCandidate(period=7, autocorrelation=0.42, score=0.31)
    body = cand.to_dict()
    assert set(body.keys()) == {"period", "autocorrelation", "score"}
    assert body["period"] == 7
    assert body["autocorrelation"] == pytest.approx(0.42)
    assert body["score"] == pytest.approx(0.31)


def test_cycle_detection_result_to_dict_reuses_candidate_to_dict():
    """``CycleDetectionResult.to_dict()`` reuses ``CycleCandidate.to_dict()``
    so the candidate shape stays a single source of truth."""
    series = _periodic_series(period=5, cycles=8)
    result = detect_cycles(series, min_period=2, max_period=10)
    assert result.candidates, "expected at least one candidate for a clean sinusoid"
    body = result.to_dict()
    first = body["candidates"][0]
    assert set(first.keys()) == {"period", "autocorrelation", "score"}
    # The candidate dict must equal ``CycleCandidate.to_dict()`` of the same row.
    assert first == result.candidates[0].to_dict()
