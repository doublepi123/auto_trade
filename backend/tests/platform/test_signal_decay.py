"""Tests for the P198 rolling online signal decay model."""

import pytest

from app.platform.signal_decay import (
    ExponentialDecay,
    RollingWindowDecay,
    half_life_base,
    reweight_combinator_weights,
    weighted_average,
)


def test_half_life_base_doubles_weight_over_half_life():
    base = half_life_base(5)
    # base ** 5 should be ~0.5
    assert abs(base ** 5 - 0.5) < 1e-9
    assert base < 1.0


def test_half_life_base_rejects_invalid():
    with pytest.raises(ValueError):
        half_life_base(0)


def test_exponential_decay_weight_decreases_with_age():
    decay = ExponentialDecay(base=0.5)
    assert decay.weight(0) == 1.0
    assert decay.weight(1) == 0.5
    assert decay.weight(2) == 0.25


def test_exponential_decay_base_one_is_uniform():
    decay = ExponentialDecay(base=1.0)
    weights = decay.normalized(5)
    assert all(abs(w - 0.2) < 1e-9 for w in weights)
    assert abs(sum(weights) - 1.0) < 1e-9


def test_exponential_decay_normalized_sums_to_one():
    decay = ExponentialDecay(base=0.9)
    weights = decay.normalized(10)
    assert abs(sum(weights) - 1.0) < 1e-9
    # newest (last) should be largest
    assert weights[-1] == max(weights)


def test_exponential_decay_empty_window():
    assert ExponentialDecay().weights(0) == []
    assert ExponentialDecay().normalized(0) == []


def test_weighted_average():
    assert weighted_average([1.0, 2.0, 3.0], [1.0, 1.0, 1.0]) == 2.0
    assert weighted_average([1.0, 2.0], [3.0, 1.0]) == 1.25
    assert weighted_average([1.0, 2.0], [0.0, 0.0]) == 0.0


def test_rolling_window_decay_weights_newest_most():
    rw = RollingWindowDecay(window=3, decay=ExponentialDecay(base=0.5))
    rw.push("a", 1.0)
    rw.push("a", 1.0)
    rw.push("b", 1.0)

    weights = rw.reweight()
    # Most recent obs is 'b', so b should outweigh a.
    assert weights["b"] > weights["a"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_rolling_window_decay_drops_old_observations():
    rw = RollingWindowDecay(window=2)
    rw.push("a", 1.0)
    rw.push("b", 1.0)
    rw.push("c", 1.0)  # 'a' evicted

    weights = rw.reweight()
    assert "a" not in weights
    assert set(weights.keys()) == {"b", "c"}


def test_rolling_window_decay_aggregates_per_key():
    rw = RollingWindowDecay(window=4, decay=ExponentialDecay(base=0.9))
    # a appears twice (older + newer); b once (newest, but gentle decay).
    rw.push("a", 1.0)
    rw.push("a", 1.0)
    rw.push("b", 1.0)

    weights = rw.reweight()
    # With a gentle 0.9 decay, the two 'a' observations combined outweigh
    # the single newest 'b'.
    assert weights["a"] > weights["b"]
    assert set(weights.keys()) == {"a", "b"}


def test_rolling_window_empty_returns_empty():
    rw = RollingWindowDecay(window=5)
    assert rw.reweight() == {}


def test_reweight_combinator_uses_decay_when_window_populated():
    static = {"alpha": 0.5, "beta": 0.5}
    rw = RollingWindowDecay(window=5, decay=ExponentialDecay(base=0.5))
    # alpha had a recent strong observation; beta had an old one.
    rw.push("beta", 1.0)
    rw.push("alpha", 1.0)

    blended = reweight_combinator_weights(static, rw)
    assert abs(sum(blended.values()) - 1.0) < 1e-9
    # alpha's recency boosts its share above the static 0.5.
    assert blended["alpha"] > 0.5
    assert blended["beta"] < 0.5


def test_reweight_combinator_falls_back_to_static_when_empty():
    static = {"alpha": 0.6, "beta": 0.4}
    rw = RollingWindowDecay(window=5)
    blended = reweight_combinator_weights(static, rw)
    assert blended == static


def test_reweight_combinator_preserves_keys_absent_from_window():
    static = {"alpha": 0.5, "beta": 0.5}
    rw = RollingWindowDecay(window=5, decay=ExponentialDecay(base=0.5))
    rw.push("alpha", 1.0)  # beta never observed

    blended = reweight_combinator_weights(static, rw)
    assert set(blended.keys()) == {"alpha", "beta"}
    assert abs(sum(blended.values()) - 1.0) < 1e-9
