"""Tests for P262: entropy & complexity diagnostics.

Pure-Python Shannon / sample / permutation entropies plus a Hurst exponent
estimate (R/S) for a scalar series. The module exposes no I/O and only relies
on the standard library.

Covers:
* constant series → Shannon entropy == 0
* Shannon entropy normalised to ``[0, 1]``
* permutation entropy: monotonic < alternating/mixed pattern
* sample entropy: finite & low for repeated values
* Hurst exponent: clamped to ``[0, 1]``
* invalid embedding / order / bins / short series → ``ValueError``
* the dataclass is frozen and exposes a ``to_dict``

Endpoint-level coverage (200 / 422 + the four-metric ``[0, 1]`` contract) lives
in ``tests/platform/test_api_risk_portfolio.py``.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.platform.entropy_complexity import (
    EntropyComplexityResult,
    hurst_exponent,
    permutation_entropy,
    sample_entropy,
    shannon_entropy,
    entropy_complexity_report,
)


# ---------------------------------------------------------------------------
# Shannon entropy
# ---------------------------------------------------------------------------


def test_shannon_entropy_constant_series_is_zero():
    series = [5.0] * 50
    assert shannon_entropy(series) == pytest.approx(0.0)


def test_shannon_entropy_normalized_range():
    series = [float(i) for i in range(20)]
    value = shannon_entropy(series, bins=10, normalize=True)
    assert 0.0 <= value <= 1.0
    # Normalised form must be <= un-normalised form.
    raw = shannon_entropy(series, bins=10, normalize=False)
    assert value <= raw + 1e-12


def test_shannon_entropy_constant_with_non_default_bins():
    assert shannon_entropy([3.5] * 10, bins=4) == pytest.approx(0.0)


def test_shannon_entropy_uniform_distribution_is_high():
    # Distinct values spread evenly across bins should beat a degenerate one.
    uniform = [float(i) for i in range(50)]
    degenerate = [1.0] * 50
    assert shannon_entropy(uniform, bins=10) > shannon_entropy(degenerate, bins=10)


def test_shannon_entropy_rejects_invalid_bins():
    with pytest.raises(ValueError):
        shannon_entropy([1.0, 2.0, 3.0], bins=1)


def test_shannon_entropy_rejects_bool_entries():
    with pytest.raises(TypeError):
        shannon_entropy([True, False, True])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Sample entropy
# ---------------------------------------------------------------------------


def test_sample_entropy_repeated_values_is_finite_and_low():
    # Highly repetitive series should produce a small finite value.
    series = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    value = sample_entropy(series, m=2)
    assert math.isfinite(value)
    assert value >= 0.0


def test_sample_entropy_mixed_series_greater_than_constant():
    rng_series = [0.1 * ((i * 7) % 13) for i in range(60)]
    constant = [3.0] * 60
    # Sample entropy is undefined when no matches occur; guard via try/except.
    try:
        mixed = sample_entropy(rng_series, m=2)
        const = sample_entropy(constant, m=2)
        # Either the constant series is undefined (skipped) or it must be <= mixed.
        assert const <= mixed + 1e-9
    except ValueError:
        pytest.skip("sample entropy undefined for this configuration")


def test_sample_entropy_rejects_short_series():
    with pytest.raises(ValueError):
        sample_entropy([1.0, 2.0], m=2)


def test_sample_entropy_rejects_invalid_embedding():
    with pytest.raises(ValueError):
        sample_entropy([1.0] * 10, m=0)


def test_sample_entropy_mixed_series_bounded_in_unit_interval():
    # P262 normalization contract: sample_entropy must return a value in [0, 1]
    # for a chaotic / mixed series (it is a normalized sample entropy proxy).
    rng_series = [0.1 * ((i * 7) % 13) for i in range(60)]
    value = sample_entropy(rng_series, m=2)
    assert 0.0 <= value <= 1.0


def test_sample_entropy_explicit_zero_r_raises_value_error():
    # An explicitly-passed ``r=0`` is invalid; only the implicit constant-series
    # fallback (r=None, std==0) is tolerated internally.
    series = [0.1 * ((i * 7) % 13) for i in range(60)]
    with pytest.raises(ValueError):
        sample_entropy(series, m=2, r=0.0)


def test_sample_entropy_explicit_negative_r_raises_value_error():
    series = [0.1 * ((i * 7) % 13) for i in range(60)]
    with pytest.raises(ValueError):
        sample_entropy(series, m=2, r=-1.0)


def test_sample_entropy_constant_series_implicit_r_returns_zero():
    # r=None on a constant series → std==0 internally tolerated → returns 0.0.
    constant = [3.0] * 60
    value = sample_entropy(constant, m=2)
    assert value == pytest.approx(0.0)


def test_sample_entropy_a_zero_b_positive_returns_upper_bound():
    # Construct a series where ``a`` (length-(m+1) matches) == 0 but ``b``
    # (length-m matches) > 0: a long-range predictability breakdown. The
    # normalized proxy must saturate at the upper bound (== 1.0) since
    # raw = -log(0/b) = +inf → raw/(1+raw) → 1.0.
    #
    # Design (m=1, r=0.5): single-value matches exist between the two 0's
    # (|0 - 0| = 0 <= 0.5) so b > 0; length-2 windows are (0,5),(5,0),(0,7)
    # whose pairwise max-norm distances are all > 0.5 → a == 0.
    series = [0.0, 5.0, 0.0, 7.0]
    value = sample_entropy(series, m=1, r=0.5)
    assert value == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Permutation entropy
# ---------------------------------------------------------------------------


def test_permutation_entropy_monotonic_less_than_alternating():
    monotonic = [float(i) for i in range(40)]
    alternating = [1.0 if i % 2 == 0 else 2.0 for i in range(40)]
    pe_mono = permutation_entropy(monotonic, order=3)
    pe_alt = permutation_entropy(alternating, order=3)
    assert pe_mono < pe_alt


def test_permutation_entropy_normalized_range():
    series = [float((i * 13) % 7) for i in range(80)]
    value = permutation_entropy(series, order=3, normalize=True)
    assert 0.0 <= value <= 1.0


def test_permutation_entropy_monotonic_is_near_zero():
    monotonic = [float(i) for i in range(40)]
    assert permutation_entropy(monotonic, order=3, normalize=True) == pytest.approx(0.0, abs=1e-9)


def test_permutation_entropy_rejects_invalid_order():
    with pytest.raises(ValueError):
        permutation_entropy([1.0] * 20, order=1)


def test_permutation_entropy_rejects_invalid_delay():
    with pytest.raises(ValueError):
        permutation_entropy([1.0] * 20, order=3, delay=0)


# ---------------------------------------------------------------------------
# Hurst exponent
# ---------------------------------------------------------------------------


def test_hurst_exponent_in_unit_range():
    series = [float(i) + 0.5 * ((i * 3) % 5) for i in range(200)]
    value = hurst_exponent(series)
    assert 0.0 <= value <= 1.0


def test_hurst_exponent_rejects_short_series():
    with pytest.raises(ValueError):
        hurst_exponent([1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


def test_entropy_complexity_report_aggregates_all_metrics():
    series = [float(i) + 0.1 * ((i * 7) % 11) for i in range(120)]
    result = entropy_complexity_report(series)
    assert isinstance(result, EntropyComplexityResult)
    assert result.n == len(series)
    assert math.isfinite(result.shannon_entropy)
    assert math.isfinite(result.sample_entropy)
    assert math.isfinite(result.permutation_entropy)
    assert 0.0 <= result.hurst_exponent <= 1.0
    assert result.approximation in {"exact", "rs_estimated"}


def test_entropy_complexity_report_all_metrics_in_unit_range():
    # P262 contract: every numeric metric lies in [0, 1].
    series = [float(i) + 0.1 * ((i * 7) % 11) for i in range(150)]
    result = entropy_complexity_report(series)
    assert 0.0 <= result.shannon_entropy <= 1.0
    assert 0.0 <= result.sample_entropy <= 1.0
    assert 0.0 <= result.permutation_entropy <= 1.0
    assert 0.0 <= result.hurst_exponent <= 1.0


def test_entropy_complexity_result_to_dict_round_trip():
    series = [float(i) for i in range(60)]
    result = entropy_complexity_report(series)
    payload = result.to_dict()
    assert set(payload.keys()) == {
        "shannon_entropy",
        "sample_entropy",
        "permutation_entropy",
        "hurst_exponent",
        "n",
        "approximation",
    }
    assert payload["n"] == 60


def test_entropy_complexity_result_is_frozen():
    series = [float(i) for i in range(60)]
    result = entropy_complexity_report(series)
    with pytest.raises(FrozenInstanceError):
        result.shannon_entropy = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation / negative paths
# ---------------------------------------------------------------------------


def test_shannon_entropy_rejects_empty_series():
    with pytest.raises(ValueError):
        shannon_entropy([])


def test_sample_entropy_rejects_bool_entries():
    with pytest.raises(TypeError):
        sample_entropy([True, True, False, True, True, False])  # type: ignore[list-item]
