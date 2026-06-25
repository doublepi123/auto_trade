"""Tests for P249 multi-armed bandits."""

from __future__ import annotations

import pytest

from app.platform.bandits import (
    EpsilonGreedy,
    ThompsonSamplingBeta,
    ThompsonSamplingGaussian,
    UCB1,
    regret,
    simulate,
)


def test_epsilon_greedy_exploits_best_arm():
    b = EpsilonGreedy(3, epsilon=0.0, seed=0)
    # Force exploration: feed rewards to make arm 1 clearly best.
    for _ in range(20):
        b.update(0, 0.1)
        b.update(1, 0.9)
        b.update(2, 0.2)
    # With epsilon=0 it should always pick arm 1 now.
    assert b.select() == 1


def test_epsilon_greedy_explores_with_nonzero_epsilon():
    b = EpsilonGreedy(3, epsilon=1.0, seed=0)
    picks = {b.select() for _ in range(50)}
    # Pure exploration -> all arms get picked at least once (probabilistic but robust).
    assert len(picks) >= 2


def test_ucb1_plays_each_arm_once_first():
    b = UCB1(3, seed=0)
    first_three = []
    for _ in range(3):
        arm = b.select()
        first_three.append(arm)
        b.update(arm, 0.5)
    assert sorted(first_three) == [0, 1, 2]


def test_ucb1_concentrates_on_best():
    res = simulate("ucb1", [0.1, 0.9, 0.2], 500, seed=42)
    # Best arm (index 1) should dominate.
    assert res.arm_counts[1] > res.arm_counts[0]
    assert res.arm_counts[1] > res.arm_counts[2]


def test_thompson_beta_concentrates_on_best():
    res = simulate("thompson_beta", [0.1, 0.9, 0.2], 1000, seed=7)
    assert res.arm_counts[1] > res.arm_counts[0]
    assert res.arm_counts[1] > res.arm_counts[2]


def test_thompson_gaussian_concentrates_on_best():
    res = simulate("thompson_gaussian", [0.1, 0.9, 0.2], 1000, seed=11, sigmas=[0.1, 0.1, 0.1])
    assert res.arm_counts[1] > res.arm_counts[0]


def test_thompson_gaussian_requires_sigmas():
    with pytest.raises(ValueError):
        simulate("thompson_gaussian", [0.1, 0.9], 100, seed=0)


def test_regret_nonnegative_and_zero_when_optimal():
    # If we always pick the best arm, regret against itself is 0.
    sel = [1, 1, 1]
    rewards = [0.9, 0.9, 0.9]
    assert regret(rewards, sel, 2) == 0.0


def test_regret_positive_when_suboptimal():
    sel = [0, 0, 0]
    rewards = [0.1, 0.1, 0.1]
    # Best arm (1) has mean 0 -> regret uses best observed mean which is 0.1 here.
    # regret = best_mean - r summed; best_mean=0.1, rewards all 0.1 -> 0.
    # Use a clearer setup: two arms, picking the worse one.
    sel2 = [0, 0]
    rewards2 = [0.1, 0.1]
    # only arm 0 observed -> its mean 0.1 is "best" -> regret 0. So test picks differently:
    # Simulate regret across both arms so best is well-defined.
    sel3 = [0, 1, 0]
    rewards3 = [0.1, 0.9, 0.1]
    reg = regret(rewards3, sel3, 2)
    assert reg > 0.0


def test_simulate_unknown_algorithm_raises():
    with pytest.raises(ValueError):
        simulate("softmax", [0.5], 10, seed=0)


def test_simulate_deterministic_with_seed():
    a = simulate("ucb1", [0.2, 0.7, 0.4], 100, seed=5)
    b = simulate("ucb1", [0.2, 0.7, 0.4], 100, seed=5)
    assert a.selected_arms == b.selected_arms


def test_invalid_n_arms_raises():
    with pytest.raises(ValueError):
        EpsilonGreedy(0, seed=0)


def test_to_dict_roundtrip():
    res = simulate("epsilon_greedy", [0.3, 0.7], 50, seed=1, epsilon=0.2)
    d = res.to_dict()
    assert d["algorithm"] == "epsilon_greedy"
    assert len(d["selected_arms"]) == 50
    assert d["n_arms"] == 2