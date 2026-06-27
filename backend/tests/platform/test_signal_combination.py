"""P266: Signal combination utilities — pure unit tests (no fastapi/app import).

Covers the standardisation / ranking / weighting / combination primitives in
``app.platform.signal_combination`` as well as the ``SignalCombinationResult``
dataclass contract. Mirrors the validation/error-handling contract of the
other platform modules (uniform ``ValueError`` for every invalid argument).
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from app.platform.signal_combination import (
    SignalCombinationResult,
    combine_signals,
    normalize_weights,
    rank_signal,
    risk_budget_weights,
    standardize_signal,
)


# ---------------------------------------------------------------------------
# standardize_signal (zscore)
# ---------------------------------------------------------------------------


def test_standardize_signal_mean_zero_and_unit_scale():
    signal = [1.0, 2.0, 3.0, 4.0, 5.0]
    z = standardize_signal(signal)
    assert len(z) == len(signal)
    mean = sum(z) / len(z)
    assert abs(mean) < 1e-12
    # sample std ≈ 1.4142 → z values must be [-1.414, -0.707, 0, 0.707, 1.414]
    assert z[0] < z[1] < z[2] < z[3] < z[4]


def test_standardize_signal_constant_returns_all_zero():
    z = standardize_signal([7.0, 7.0, 7.0, 7.0])
    assert z == [0.0, 0.0, 0.0, 0.0]


def test_standardize_signal_rejects_invalid():
    with pytest.raises(ValueError):
        standardize_signal([])
    with pytest.raises(ValueError):
        standardize_signal([1.0, "x"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        standardize_signal([True, False])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        standardize_signal(float("nan"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        standardize_signal([1.0, float("inf")])


# ---------------------------------------------------------------------------
# rank_signal
# ---------------------------------------------------------------------------


def test_rank_signal_preserves_order_and_handles_ties():
    signal = [3.0, 1.0, 2.0, 1.0]
    ranks = rank_signal(signal)
    assert len(ranks) == len(signal)
    # smallest entries (index 1 and 3, both value 1.0) tie at the lowest rank
    assert ranks[1] == ranks[3]
    # monotonic in value
    assert ranks[1] < ranks[2] < ranks[0]


def test_rank_signal_output_length_matches():
    ranks = rank_signal([10.0, -2.0, 0.0, 4.5, 4.5])
    assert len(ranks) == 5
    # the two 4.5 entries must share the same rank
    assert ranks[3] == ranks[4]


def test_rank_signal_rejects_invalid():
    with pytest.raises(ValueError):
        rank_signal([])
    with pytest.raises(ValueError):
        rank_signal([1.0, True])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        rank_signal([1.0, float("nan")])


# ---------------------------------------------------------------------------
# normalize_weights
# ---------------------------------------------------------------------------


def test_normalize_weights_abs_sum_one_and_signs_preserved():
    w = normalize_weights([2.0, -1.0, 1.0])
    assert len(w) == 3
    assert abs(sum(abs(x) for x in w) - 1.0) < 1e-12
    # signs preserved
    assert w[0] > 0
    assert w[1] < 0
    assert w[2] > 0


def test_normalize_weights_single_entry():
    w = normalize_weights([-5.0])
    assert len(w) == 1
    assert abs(abs(w[0]) - 1.0) < 1e-12
    assert w[0] < 0


def test_normalize_weights_rejects_invalid():
    with pytest.raises(ValueError):
        normalize_weights([])
    with pytest.raises(ValueError):
        normalize_weights([0.0, 0.0])
    with pytest.raises(ValueError):
        normalize_weights([1.0, float("inf")])
    with pytest.raises(ValueError):
        normalize_weights([1.0, True])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# risk_budget_weights
# ---------------------------------------------------------------------------


def test_risk_budget_weights_high_vol_lower_than_low_vol():
    signals = {
        "low_vol": [0.01, -0.01, 0.01, -0.01, 0.01, -0.01],
        "high_vol": [0.5, -0.5, 0.5, -0.5, 0.5, -0.5],
    }
    w = risk_budget_weights(signals)
    assert len(w) == 2
    assert abs(sum(abs(v) for v in w.values()) - 1.0) < 1e-12
    # high-volatility signal gets a smaller absolute weight
    assert abs(w["high_vol"]) < abs(w["low_vol"])


def test_risk_budget_weights_rejects_invalid():
    with pytest.raises(ValueError):
        risk_budget_weights({})
    with pytest.raises(ValueError):
        risk_budget_weights({"a": [1.0, 2.0], "b": [1.0]})  # length mismatch
    with pytest.raises(ValueError):
        risk_budget_weights({"a": [1.0, True]})  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# combine_signals
# ---------------------------------------------------------------------------


def test_combine_signals_zscore_equal_weight():
    signals = {
        "a": [1.0, 2.0, 3.0, 4.0, 5.0],
        "b": [5.0, 4.0, 3.0, 2.0, 1.0],
    }
    result = combine_signals(signals, method="zscore")
    assert isinstance(result, SignalCombinationResult)
    assert result.method == "zscore"
    assert result.n_signals == 2
    assert len(result.combined) == 5
    assert len(result.weights) == 2
    # equal weights: |w| sums to 1
    assert abs(sum(abs(v) for v in result.weights.values()) - 1.0) < 1e-12
    # standardised series present
    assert set(result.standardized.keys()) == {"a", "b"}
    for vec in result.standardized.values():
        assert len(vec) == 5
    # symmetric opposite signals → combined around zero in the middle
    assert abs(result.combined[2]) < 1e-9


def test_combine_signals_rank_method():
    signals = {
        "a": [1.0, 2.0, 3.0, 4.0],
        "b": [4.0, 3.0, 2.0, 1.0],
    }
    result = combine_signals(signals, method="rank")
    assert result.method == "rank"
    assert len(result.combined) == 4
    # equal-and-opposite ranks → combined value constant
    assert max(result.combined) - min(result.combined) < 1e-9


def test_combine_signals_raw_method():
    signals = {"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]}
    result = combine_signals(signals, method="raw")
    assert result.method == "raw"
    assert len(result.combined) == 3
    # equal weights abs-sum = 1 → raw combined = (a+b)/2 each point
    for idx, val in enumerate(result.combined):
        assert abs(val - (signals["a"][idx] + signals["b"][idx]) / 2.0) < 1e-12


def test_combine_signals_explicit_weights_normalized():
    signals = {"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]}
    result = combine_signals(signals, weights={"a": 2.0, "b": -2.0}, method="raw")
    # 2 / 4 = 0.5, -2 / 4 = -0.5
    assert abs(result.weights["a"] - 0.5) < 1e-12
    assert abs(result.weights["b"] + 0.5) < 1e-12


def test_combine_signals_invalid_inputs():
    # empty
    with pytest.raises(ValueError):
        combine_signals({})
    # length mismatch
    with pytest.raises(ValueError):
        combine_signals({"a": [1.0, 2.0], "b": [1.0]})
    # non-sequence value
    with pytest.raises(ValueError):
        combine_signals({"a": 5})  # type: ignore[dict-item]
    # dict value
    with pytest.raises(ValueError):
        combine_signals({"a": {"x": 1}})  # type: ignore[dict-item]
    # bool entry
    with pytest.raises(ValueError):
        combine_signals({"a": [True, False]})  # type: ignore[list-item]
    # invalid method
    with pytest.raises(ValueError):
        combine_signals({"a": [1.0, 2.0]}, method="bogus")
    # weights mismatch (missing key)
    with pytest.raises(ValueError):
        combine_signals({"a": [1.0, 2.0], "b": [3.0, 4.0]}, weights={"a": 1.0})
    # zero weights
    with pytest.raises(ValueError):
        combine_signals({"a": [1.0, 2.0], "b": [3.0, 4.0]}, weights={"a": 0.0, "b": 0.0})


# ---------------------------------------------------------------------------
# SignalCombinationResult dataclass contract
# ---------------------------------------------------------------------------


def test_signal_combination_result_to_dict_and_frozen():
    result = combine_signals({"a": [1.0, 2.0, 3.0]}, method="zscore")
    d = result.to_dict()
    assert set(d.keys()) == {"combined", "weights", "standardized", "method", "n_signals"}
    assert d["method"] == "zscore"
    assert d["n_signals"] == 1
    assert isinstance(d["combined"], list)
    assert isinstance(d["weights"], dict)
    assert isinstance(d["standardized"], dict)
    # frozen
    with pytest.raises(FrozenInstanceError):
        result.combined = [0.0, 0.0, 0.0]  # type: ignore[misc]
