"""Tests for P263 rolling feature module (rolling stats / EWMA / beta).

Pure-Python rolling statistical features: mean, population std, z-score
(current point vs. trailing window), skew, kurtosis, EWMA, and rolling
beta vs. a benchmark series.

The contract is:

* Output lists have the same length as the input series.
* The first ``window - 1`` rolling entries are ``None`` (insufficient data).
  ``ewma`` is the **only** exception — see ``test_ewma_has_no_warmup``.
* ``std`` is the *population* standard deviation (denominator = ``window``).
* ``zscore[i]`` compares the *current* point ``series[i]`` to the trailing
  window ``series[i - window + 1 .. i]``.
* ``skew`` / ``kurtosis`` use population central moments; when ``std == 0``
  they return ``0.0`` rather than ``NaN``.
* ``beta`` (vs. a benchmark) is ``None`` for the first ``window - 1`` entries;
  if no benchmark is supplied, the ``beta`` field of the report is ``None``.

Error-handling contract (P263 review): the public surface raises
``ValueError`` **uniformly** for any invalid argument — including a
non-integer ``window`` (and ``bool``), a non-numeric / non-finite / ``bool``
series entry, and a non-numeric / ``bool`` / out-of-range ``alpha``. The
callers (the platform HTTP layer) translate this single exception family into
HTTP 422 without needing to special-case ``TypeError``.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.platform.rolling_features import (
    RollingFeatureResult,
    ewma,
    rolling_beta,
    rolling_feature_report,
    rolling_kurtosis,
    rolling_mean,
    rolling_skew,
    rolling_std,
    rolling_zscore,
)


# ---------------------------------------------------------------------------
# rolling_mean
# ---------------------------------------------------------------------------


def test_rolling_mean_basic():
    # [1,2,3,4] window=2 → [None, 1.5, 2.5, 3.5]
    result = rolling_mean([1.0, 2.0, 3.0, 4.0], 2)
    assert len(result) == 4
    assert result[0] is None
    assert result[1] == pytest.approx(1.5)
    assert result[2] == pytest.approx(2.5)
    assert result[3] == pytest.approx(3.5)


def test_rolling_mean_full_window():
    # window == len(series): exactly one non-None entry (the last).
    result = rolling_mean([1.0, 2.0, 3.0, 4.0], 4)
    assert result == [None, None, None, pytest.approx(2.5)]


# ---------------------------------------------------------------------------
# rolling_std (population)
# ---------------------------------------------------------------------------


def test_rolling_std_population_basic():
    # window=2 of [1,2] has population std 0.5.
    result = rolling_std([1.0, 2.0, 3.0, 4.0], 2)
    assert len(result) == 4
    assert result[0] is None
    # [1,2] → var = ((1-1.5)^2 + (2-1.5)^2)/2 = 0.25 → std 0.5
    assert result[1] == pytest.approx(0.5)
    assert result[2] == pytest.approx(0.5)
    assert result[3] == pytest.approx(0.5)


def test_rolling_std_constant_window_is_zero():
    result = rolling_std([5.0, 5.0, 5.0], 2)
    assert result == [None, pytest.approx(0.0), pytest.approx(0.0)]


# ---------------------------------------------------------------------------
# rolling_zscore (current point vs. trailing window)
# ---------------------------------------------------------------------------


def test_rolling_zscore_basic():
    # window=2, series=[1,2,3,4]:
    # at i=1 window=[1,2]: mean=1.5 std=0.5; current=2 → (2-1.5)/0.5 = 1.0
    result = rolling_zscore([1.0, 2.0, 3.0, 4.0], 2)
    assert result[0] is None
    assert result[1] == pytest.approx(1.0)
    assert result[2] == pytest.approx(1.0)
    assert result[3] == pytest.approx(1.0)


def test_rolling_zscore_zero_std_returns_zero():
    # When the window std is zero the z-score must be 0 (not NaN/inf).
    result = rolling_zscore([5.0, 5.0, 5.0], 2)
    assert result == [None, 0.0, 0.0]


# ---------------------------------------------------------------------------
# ewma
# ---------------------------------------------------------------------------


def test_ewma_recursive_formula():
    # alpha=0.5 recursive: y[i] = alpha * x[i] + (1-alpha) * y[i-1], y[0]=x[0].
    series = [10.0, 20.0, 30.0]
    result = ewma(series, 0.5)
    assert len(result) == 3
    assert result[0] == pytest.approx(10.0)
    assert result[1] == pytest.approx(0.5 * 20.0 + 0.5 * 10.0)  # 15.0
    assert result[2] == pytest.approx(0.5 * 30.0 + 0.5 * 15.0)  # 22.5


def test_ewma_alpha_one_is_identity():
    result = ewma([1.0, 2.0, 3.0], 1.0)
    assert result == [1.0, 2.0, 3.0]


def test_ewma_has_no_warmup_and_starts_at_first_observation():
    # EWMA does NOT depend on a fixed rolling window, so — unlike the other
    # rolling stats — it has no warm-up: the very first output equals the
    # first observation (y[0] = x[0]) and every subsequent index is defined.
    series = [3.0, 6.0, 9.0]
    result = ewma(series, 0.5)
    assert len(result) == len(series)
    # No leading None warm-up entries.
    assert all(v is not None for v in result)
    # Starts at the first observation.
    assert result[0] == pytest.approx(series[0])


def test_rolling_skew_and_kurtosis_length_and_constant_zero():
    skew = rolling_skew([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    kurt = rolling_kurtosis([1.0, 2.0, 3.0, 4.0, 5.0], 3)
    # First window-1 entries are None.
    assert len(skew) == 5
    assert len(kurt) == 5
    assert skew[0] is None and skew[1] is None
    assert kurt[0] is None and kurt[1] is None
    # Symmetric arithmetic sequence over a window of 3 has skew 0.
    assert skew[2] == pytest.approx(0.0, abs=1e-12)
    assert skew[3] == pytest.approx(0.0, abs=1e-12)
    assert skew[4] == pytest.approx(0.0, abs=1e-12)


def test_rolling_skew_constant_window_is_zero():
    skew = rolling_skew([7.0, 7.0, 7.0, 7.0], 3)
    assert skew == [None, None, 0.0, 0.0]


def test_rolling_kurtosis_constant_window_is_zero():
    kurt = rolling_kurtosis([7.0, 7.0, 7.0, 7.0], 3)
    assert kurt == [None, None, 0.0, 0.0]


# ---------------------------------------------------------------------------
# rolling_beta
# ---------------------------------------------------------------------------


def test_rolling_beta_identical_series_is_one():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    beta = rolling_beta(series, series, 3)
    # First window-1 entries are None; remaining beta ≈ 1.0 since cov == var.
    assert beta[0] is None
    assert beta[1] is None
    for value in beta[2:]:
        assert value == pytest.approx(1.0, rel=1e-9, abs=1e-9)


def test_rolling_beta_zero_variance_returns_zero():
    # When benchmark variance is 0 (constant benchmark), beta is undefined → 0.
    series = [1.0, 2.0, 3.0, 4.0]
    bench = [5.0, 5.0, 5.0, 5.0]
    beta = rolling_beta(series, bench, 2)
    assert beta[0] is None
    for value in beta[1:]:
        assert value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rolling_mean_rejects_empty_series():
    with pytest.raises(ValueError):
        rolling_mean([], 2)


def test_rolling_mean_rejects_bool_entry():
    # bool is a subclass of int but must be rejected as an invalid value.
    with pytest.raises(ValueError):
        rolling_mean([1.0, True, 3.0], 2)  # type: ignore[list-item]


def test_rolling_mean_rejects_non_finite_entry():
    with pytest.raises(ValueError):
        rolling_mean([1.0, float("nan")], 2)


def test_rolling_mean_rejects_non_number_entry():
    # A non-numeric series entry is an invalid value, not a type error.
    with pytest.raises(ValueError):
        rolling_mean([1.0, "x", 3.0], 2)  # type: ignore[list-item]


def test_rolling_mean_rejects_window_below_two():
    with pytest.raises(ValueError):
        rolling_mean([1.0, 2.0], 1)


def test_rolling_mean_rejects_window_larger_than_series():
    with pytest.raises(ValueError):
        rolling_mean([1.0, 2.0], 5)


def test_rolling_mean_rejects_bool_window():
    # bool is a subclass of int but must be rejected as an invalid value.
    with pytest.raises(ValueError):
        rolling_mean([1.0, 2.0, 3.0], True)  # type: ignore[arg-type]


def test_rolling_mean_rejects_non_int_window():
    # A non-integer window is an invalid value, not a type error.
    with pytest.raises(ValueError):
        rolling_mean([1.0, 2.0, 3.0], 2.0)  # type: ignore[arg-type]


def test_ewma_rejects_alpha_zero():
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], 0.0)


def test_ewma_rejects_alpha_above_one():
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], 1.5)


def test_ewma_rejects_bool_alpha():
    # bool is a subclass of int but must be rejected as an invalid value.
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], True)  # type: ignore[arg-type]


def test_ewma_rejects_non_number_alpha():
    # A non-numeric alpha is an invalid value, not a type error.
    with pytest.raises(ValueError):
        ewma([1.0, 2.0], "0.5")  # type: ignore[arg-type]


def test_rolling_beta_rejects_length_mismatch():
    with pytest.raises(ValueError):
        rolling_beta([1.0, 2.0, 3.0], [1.0, 2.0], 2)


def test_rolling_beta_rejects_non_finite_benchmark():
    with pytest.raises(ValueError):
        rolling_beta([1.0, 2.0, 3.0], [1.0, float("inf"), 3.0], 2)


def test_rolling_beta_rejects_bool_entry_benchmark():
    # bool is a subclass of int but must be rejected as an invalid value.
    with pytest.raises(ValueError):
        rolling_beta([1.0, 2.0, 3.0], [1.0, True, 3.0], 2)  # type: ignore[list-item]


def test_rolling_mean_rejects_none_series():
    # ``None`` is not a sequence — must surface as ValueError (HTTP 422),
    # never a raw TypeError leaking to the caller.
    with pytest.raises(ValueError):
        rolling_mean(None, 2)  # type: ignore[arg-type]


def test_rolling_mean_rejects_scalar_series():
    # A bare scalar (non-iterable) is an invalid argument, not a type error.
    with pytest.raises(ValueError):
        rolling_mean(123, 2)  # type: ignore[arg-type]


def test_rolling_beta_rejects_none_benchmark():
    with pytest.raises(ValueError):
        rolling_beta([1, 2, 3], None, 2)  # type: ignore[arg-type]


def test_rolling_beta_rejects_scalar_benchmark():
    with pytest.raises(ValueError):
        rolling_beta([1, 2, 3], 123, 2)  # type: ignore[arg-type]


def test_rolling_mean_rejects_string_series():
    # A ``str`` is iterable (yields characters) which would silently produce
    # nonsense; it must be rejected as an invalid sequence argument.
    with pytest.raises(ValueError):
        rolling_mean("abc", 2)  # type: ignore[arg-type]


def test_rolling_mean_rejects_dict_series():
    # A ``dict`` / ``Mapping`` is iterable (yielding its keys) — without an
    # explicit guard ``list({1.0: 'a', 2.0: 'b'})`` would silently produce
    # ``[1.0, 2.0]`` and pass validation. A mapping is semantically not a
    # numeric sequence, so it must be rejected as an invalid argument.
    with pytest.raises(ValueError):
        rolling_mean({1.0: "a", 2.0: "b"}, 2)  # type: ignore[arg-type]


def test_rolling_beta_rejects_dict_benchmark():
    # Same dict-as-benchmark trap: ``list(dict)`` yields keys and would
    # bypass validation. Reject it uniformly.
    with pytest.raises(ValueError):
        rolling_beta([1, 2, 3], {1.0: "a", 2.0: "b", 3.0: "c"}, 2)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RollingFeatureResult dataclass
# ---------------------------------------------------------------------------


def test_rolling_feature_result_to_dict_contains_all_fields():
    result = RollingFeatureResult(
        mean=[None, 1.5],
        std=[None, 0.5],
        zscore=[None, 1.0],
        skew=[None, 0.0],
        kurtosis=[None, 0.0],
        ewma=[1.0, 1.5],
        beta=[None, 1.0],
    )
    body = result.to_dict()
    assert set(body.keys()) == {
        "mean", "std", "zscore", "skew", "kurtosis", "ewma", "beta",
    }
    assert body["mean"] == [None, 1.5]
    assert body["beta"] == [None, 1.0]


def test_rolling_feature_result_is_frozen():
    result = RollingFeatureResult(
        mean=[None], std=[None], zscore=[None], skew=[None],
        kurtosis=[None], ewma=[1.0], beta=None,
    )
    with pytest.raises(FrozenInstanceError):
        result.mean = [1.0]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# rolling_feature_report integration
# ---------------------------------------------------------------------------


def test_rolling_feature_report_returns_aligned_lengths():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    report = rolling_feature_report(series, window=3, alpha=0.3)
    n = len(series)
    for field in ("mean", "std", "zscore", "skew", "kurtosis", "ewma"):
        values = getattr(report, field)
        assert len(values) == n, f"{field} length mismatch"
        # First window-1 entries must be None for the rolling stats.
        # ``ewma`` is the exception: it has no warm-up (it does not depend
        # on a fixed rolling window), so it is defined at every index.
        if field != "ewma":
            assert all(v is None for v in values[:2]), f"{field} warmup not None"
    # ewma has no warm-up — first entry equals the first observation.
    assert report.ewma[0] == pytest.approx(series[0])
    # No benchmark ⇒ beta is None.
    assert report.beta is None


def test_rolling_feature_report_with_benchmark_returns_beta():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    report = rolling_feature_report(series, window=3, benchmark=series)
    assert report.beta is not None
    assert len(report.beta) == len(series)
    assert report.beta[0] is None
    assert report.beta[1] is None
    assert report.beta[2] == pytest.approx(1.0, rel=1e-9, abs=1e-9)


def test_rolling_feature_report_default_window_is_five():
    series = list(range(1, 11))  # 1..10
    report = rolling_feature_report(series)
    # default window=5 → first 4 entries of mean are None.
    assert all(v is None for v in report.mean[:4])
    assert report.mean[4] is not None


def test_rolling_feature_report_validates_benchmark_length():
    series = [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(ValueError):
        rolling_feature_report(series, window=2, benchmark=[1.0, 2.0])


def test_rolling_feature_report_alpha_validation():
    series = [1.0, 2.0, 3.0, 4.0]
    with pytest.raises(ValueError):
        rolling_feature_report(series, window=2, alpha=0.0)
