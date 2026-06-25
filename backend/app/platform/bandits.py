"""P249: Multi-armed bandit strategy selection.

Pure-Python, dependency-free implementations of the canonical bandit
algorithms (mirroring SMPyBandits' abstraction shape) for choosing among K
"arms" — e.g. candidate strategies or signals — under the explore/exploit
trade-off. All variants take an injected ``random.Random(seed)`` so they are
fully deterministic.

* **EpsilonGreedy** — explore a random arm with probability ε, exploit the
  best empirical mean otherwise.
* **UCB1** — Auer-Cesa-Bianchi-Fischer (2002) upper-confidence-bound:
  ``argmax_i mean_i + sqrt(2 ln t / n_i)``.
* **ThompsonSamplingBeta** — Bernoulli rewards: maintain a Beta(α,β)
  posterior per arm, sample, pick the max.
* **ThompsonSamplingGaussian** — Gaussian rewards with known σ: maintain a
  Gaussian posterior on each arm's mean, sample, pick the max.

A ``simulate`` helper runs a bandit over a sequence of true arm reward
probabilities and returns the per-step arm choices, cumulative reward, and
the realised regret vs the best arm's expected reward.

Reference: Auer et al. (2002) "Finite-time Analysis of the Multiarmed Bandit
Problem"; Thompson (1933); SMPyBanduits. Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "BanditResult",
    "EpsilonGreedy",
    "UCB1",
    "ThompsonSamplingBeta",
    "ThompsonSamplingGaussian",
    "simulate",
    "regret",
]


def _beta_sample(rng: random.Random, a: float, b: float) -> float:
    """Sample from Beta(a, b) via two Gamma draws (no numpy/scipy)."""
    # Gamma(shape, scale=1) via Marsaglia-Tsang for shape >= 1; for shape < 1
    # use the boosting trick. Returns a single sample in (0, 1).
    def gamma(shape: float) -> float:
        if shape < 1.0:
            # Boost: gamma(shape) = gamma(shape+1) * U^{1/shape}
            u = rng.random()
            if u < 1e-12:
                u = 1e-12
            return gamma(shape + 1.0) * (u ** (1.0 / shape))
        d = shape - 1.0 / 3.0
        c = 1.0 / math.sqrt(9.0 * d)
        while True:
            x = _normal(rng)
            v = 1.0 + c * x
            if v <= 0.0:
                continue
            v = v * v * v
            u = rng.random()
            if u < 1.0 - 0.0331 * x * x * x * x:
                return d * v
            if math.log(u) < 0.5 * x * x + d * (1.0 - math.log(v) + v - 1.0):
                return d * v

    x = gamma(a)
    y = gamma(b)
    s = x + y
    if s == 0.0:
        return 0.5
    return x / s


def _normal(rng: random.Random) -> float:
    u1 = rng.random()
    u2 = rng.random()
    if u1 < 1e-12:
        u1 = 1e-12
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _gaussian_sample(rng: random.Random, mean: float, std: float) -> float:
    return mean + std * _normal(rng)


class _BanditBase:
    """Common book-keeping for the bandit algorithms."""

    def __init__(self, n_arms: int, seed: int = 0) -> None:
        if n_arms < 1:
            raise ValueError("n_arms must be >= 1")
        self.n_arms = n_arms
        self.rng = random.Random(seed)
        self.seed = seed
        self.counts = [0] * n_arms
        self.rewards = [0.0] * n_arms
        self.t = 0

    def _empirical_mean(self, arm: int) -> float:
        c = self.counts[arm]
        return self.rewards[arm] / c if c > 0 else 0.0

    def update(self, arm: int, reward: float) -> None:
        self.counts[arm] += 1
        self.rewards[arm] += float(reward)
        self.t += 1


class EpsilonGreedy(_BanditBase):
    def __init__(self, n_arms: int, epsilon: float = 0.1, seed: int = 0) -> None:
        super().__init__(n_arms, seed)
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError("epsilon must be in [0, 1]")
        self.epsilon = epsilon

    def select(self) -> int:
        if self.rng.random() < self.epsilon:
            return self.rng.randrange(self.n_arms)
        # exploit: best empirical mean; tie-break randomly among unexplored.
        best = 0.0
        best_arm = 0
        for a in range(self.n_arms):
            m = self._empirical_mean(a)
            if m > best:
                best = m
                best_arm = a
        # If no arm has been tried yet, pick uniformly.
        if all(c == 0 for c in self.counts):
            return self.rng.randrange(self.n_arms)
        return best_arm


class UCB1(_BanditBase):
    def select(self) -> int:
        # Play each arm once first.
        for a in range(self.n_arms):
            if self.counts[a] == 0:
                return a
        # UCB1: mean + sqrt(2 ln t / n_i)
        log_t = math.log(max(self.t, 1))
        best_val = -float("inf")
        best_arm = 0
        for a in range(self.n_arms):
            mean = self._empirical_mean(a)
            bonus = math.sqrt(2.0 * log_t / self.counts[a])
            val = mean + bonus
            if val > best_val:
                best_val = val
                best_arm = a
        return best_arm


class ThompsonSamplingBeta(_BanditBase):
    """Beta(1,1) prior; Bernoulli rewards in [0,1]."""

    def __init__(self, n_arms: int, seed: int = 0, alpha0: float = 1.0, beta0: float = 1.0) -> None:
        super().__init__(n_arms, seed)
        self.alpha = [alpha0] * n_arms
        self.beta = [beta0] * n_arms

    def select(self) -> int:
        best = -float("inf")
        best_arm = 0
        for a in range(self.n_arms):
            s = _beta_sample(self.rng, self.alpha[a], self.beta[a])
            if s > best:
                best = s
                best_arm = a
        return best_arm

    def update(self, arm: int, reward: float) -> None:
        super().update(arm, reward)
        r = float(reward)
        if r >= 1.0:
            self.alpha[arm] += 1.0
        elif r <= 0.0:
            self.beta[arm] += 1.0
        else:
            # fractional reward: split mass.
            self.alpha[arm] += r
            self.beta[arm] += 1.0 - r


class ThompsonSamplingGaussian(_BanditBase):
    """Gaussian rewards with known per-arm std; conjugate Normal posterior."""

    def __init__(self, n_arms: int, sigmas: Sequence[float], seed: int = 0,
                 prior_mean: float = 0.0, prior_var: float = 1e6) -> None:
        super().__init__(n_arms, seed)
        if len(sigmas) != n_arms:
            raise ValueError("sigmas length must match n_arms")
        if any(s <= 0.0 for s in sigmas):
            raise ValueError("sigmas must be positive")
        self.sigmas = list(sigmas)
        self.post_mean = [prior_mean] * n_arms
        self.post_var = [prior_var] * n_arms

    def select(self) -> int:
        best = -float("inf")
        best_arm = 0
        for a in range(self.n_arms):
            s = _gaussian_sample(self.rng, self.post_mean[a], math.sqrt(self.post_var[a]))
            if s > best:
                best = s
                best_arm = a
        return best_arm

    def update(self, arm: int, reward: float) -> None:
        super().update(arm, reward)
        r = float(reward)
        sigma2 = self.sigmas[arm] ** 2
        v0 = self.post_var[arm]
        m0 = self.post_mean[arm]
        # Posterior precision: 1/v = 1/v0 + n/sigma^2 (using cumulative count).
        n = self.counts[arm]
        prec = 1.0 / v0 + n / sigma2
        v_new = 1.0 / prec
        # Running sum already in rewards[arm]; posterior mean update.
        m_new = v_new * (m0 / v0 + self.rewards[arm] / sigma2)
        self.post_mean[arm] = m_new
        self.post_var[arm] = v_new


@dataclass(frozen=True)
class BanditResult:
    algorithm: str
    n_arms: int
    n_steps: int
    arm_counts: list[int]
    cumulative_reward: float
    cumulative_regret: float
    selected_arms: list[int]

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "n_arms": self.n_arms,
            "n_steps": self.n_steps,
            "arm_counts": self.arm_counts,
            "cumulative_reward": self.cumulative_reward,
            "cumulative_regret": self.cumulative_regret,
            "selected_arms": self.selected_arms,
        }


def regret(true_rewards: Sequence[float], selected_arms: list[int], n_arms: int) -> float:
    """Cumulative regret vs the best arm's mean reward."""
    counts = [0] * n_arms
    sums = [0.0] * n_arms
    best_means = [0.0] * len(selected_arms)
    # Compute running best-arm mean up to each step (clairvoyant uses true best).
    overall_means = [0.0] * n_arms
    for i, r in enumerate(true_rewards):
        a = selected_arms[i]
        counts[a] += 1
        sums[a] += r
        overall_means[a] = sums[a] / counts[a]
    best_mean = max(overall_means) if overall_means else 0.0
    return sum(best_mean - true_rewards[i] for i in range(len(selected_arms)))


def simulate(
    algorithm: str,
    true_means: Sequence[float],
    n_steps: int,
    seed: int = 0,
    *,
    epsilon: float = 0.1,
    sigmas: Sequence[float] | None = None,
) -> BanditResult:
    """Run a bandit simulation over Bernoulli (or Gaussian) arms.

    For ``epsilon_greedy`` / ``ucb1`` / ``thompson_beta`` rewards are drawn
    as Bernoulli(true_mean). For ``thompson_gaussian`` rewards are
    ``N(true_mean, sigma²)`` and ``sigmas`` must be supplied.
    """
    n_arms = len(true_means)
    if n_arms == 0:
        raise ValueError("true_means must be non-empty")
    if algorithm == "epsilon_greedy":
        bandit: _BanditBase = EpsilonGreedy(n_arms, epsilon=epsilon, seed=seed)
    elif algorithm == "ucb1":
        bandit = UCB1(n_arms, seed=seed)
    elif algorithm == "thompson_beta":
        bandit = ThompsonSamplingBeta(n_arms, seed=seed)
    elif algorithm == "thompson_gaussian":
        if sigmas is None:
            raise ValueError("thompson_gaussian requires sigmas")
        bandit = ThompsonSamplingGaussian(n_arms, sigmas, seed=seed)
    else:
        raise ValueError(f"unknown algorithm: {algorithm}")

    reward_rng = random.Random(seed + 1)
    selected: list[int] = []
    rewards_obs: list[float] = []
    for _ in range(n_steps):
        arm = bandit.select()
        selected.append(arm)
        if algorithm == "thompson_gaussian":
            r = true_means[arm] + sigmas[arm] * _normal(reward_rng)  # type: ignore[index]
        else:
            r = 1.0 if reward_rng.random() < true_means[arm] else 0.0
        rewards_obs.append(r)
        bandit.update(arm, r)
    cum_reward = sum(rewards_obs)
    cum_regret = regret(rewards_obs, selected, n_arms)
    return BanditResult(
        algorithm=algorithm,
        n_arms=n_arms,
        n_steps=n_steps,
        arm_counts=list(bandit.counts),
        cumulative_reward=cum_reward,
        cumulative_regret=cum_regret,
        selected_arms=selected,
    )