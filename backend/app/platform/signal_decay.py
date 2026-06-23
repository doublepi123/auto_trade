"""P198: rolling online signal decay model.

Applies an exponential half-life decay to per-signal (or per-strategy)
contributions so that more recent performance weighs more heavily when
rebalancing a strategy combinator or re-fitting a sizer. This is the online
learning control analog of zipline's rolling re-fit and the decay used in
vectorbt signal reweighting — but kept as a deterministic pure-Python primitive
(no ML dependency).

Two modes:

* :class:`ExponentialDecay` — weight = ``base ** age``, where ``age`` is the
  position of the observation in the window (0 = most recent). ``base < 1``
  produces a half-life decay; ``base == 1`` is uniform.
* :class:`RollingWindowDecay` — keeps the last ``window`` observations and
  assigns each a weight drawn from an injected decay function (defaults to
  exponential). Observations older than ``window`` get weight 0.

Both are reproducible given the same inputs (no time/random dependence), so
backtests are deterministic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "ExponentialDecay",
    "RollingWindowDecay",
    "half_life_base",
    "weighted_average",
]


def half_life_base(half_life: int) -> float:
    """The per-age-step multiplier giving the requested ``half_life``.

    ``base ** half_life == 0.5``. ``half_life`` must be >= 1.
    """
    if half_life < 1:
        raise ValueError("half_life must be >= 1")
    return 0.5 ** (1.0 / half_life)


def weighted_average(values: list[float], weights: list[float]) -> float:
    """Mean of ``values`` weighted by ``weights`` (0 if total weight is 0)."""
    pairs = [(v, w) for v, w in zip(values, weights) if w > 0]
    if not pairs:
        return 0.0
    total_w = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total_w


@dataclass
class ExponentialDecay:
    """Pure functional exponential decay: weight = ``base ** age``.

    Most-recent observation has weight ``base**0 = 1``; older observations
    shrink by ``base`` per step. With ``base = half_life_base(5)`` the weight
    halves every 5 steps.
    """

    base: float = 0.9

    def weight(self, age: int) -> float:
        if age < 0:
            raise ValueError("age must be non-negative")
        return self.base ** age

    def weights(self, n: int) -> list[float]:
        """Weights for a window of ``n`` observations, oldest-first."""
        if n <= 0:
            return []
        # oldest (index 0) has age n-1, newest (index n-1) has age 0
        return [self.weight(n - 1 - i) for i in range(n)]

    def normalized(self, n: int) -> list[float]:
        """Weights for ``n`` observations summing to 1.0 (oldest-first)."""
        raw = self.weights(n)
        total = sum(raw)
        if total <= 0:
            return [0.0] * n
        return [w / total for w in raw]


@dataclass
class RollingWindowDecay:
    """Online rolling window that decays the weight of stale observations.

    Push per-bar signal/return observations (oldest first via :meth:`push`);
    query :meth:`reweight` for a dict of per-key weights reflecting how much
    each key's recent observations contributed, discounted by age.

    The window is FIFO: pushing beyond ``window`` drops the oldest observation.
    """

    window: int = 20
    decay: ExponentialDecay = field(default_factory=ExponentialDecay)

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("window must be >= 1")
        self._observations: deque[tuple[str, float]] = deque(maxlen=self.window)

    def push(self, key: str, value: float) -> None:
        """Record the most recent observation for ``key``.

        ``value`` is typically a per-bar signal magnitude or realized return;
        the decay model only cares about recency + magnitude.
        """
        self._observations.append((key, value))

    def observations(self) -> list[tuple[str, float]]:
        return list(self._observations)

    def reweight(self) -> dict[str, float]:
        """Return normalized weights per key, newest observations heaviest.

        Weights sum to 1.0 across all keys present in the window. Keys not
        seen in the window are absent from the result (weight 0).
        """
        obs = list(self._observations)
        n = len(obs)
        if n == 0:
            return {}
        # normalized(n) returns weights oldest-first (index 0 = smallest weight,
        # index n-1 = largest weight). Our deque is also oldest-first, so they
        # align directly — the newest observation gets the largest weight.
        weights = self.decay.normalized(n)
        per_key: dict[str, float] = {}
        for (key, _value), w in zip(obs, weights):
            per_key[key] = per_key.get(key, 0.0) + w
        total = sum(per_key.values())
        if total <= 0:
            return {}
        return {k: v / total for k, v in per_key.items()}

    def clear(self) -> None:
        self._observations.clear()


def reweight_combinator_weights(
    static_weights: dict[str, float],
    decay: RollingWindowDecay,
    fallback: Callable[[dict[str, float], dict[str, float]], dict[str, float]] | None = None,
) -> dict[str, float]:
    """Blend static combinator weights with online decay weights.

    ``static_weights`` is the base allocation per strategy id; ``decay`` carries
    the rolling observation history. The result is the static weight multiplied
    by the decay's per-key weight, renormalized to sum to 1.0. Keys absent from
    the decay window keep their static share via ``fallback`` (defaults to
    returning the static weights unchanged when the decay window is empty).
    """
    online = decay.reweight()
    if not online:
        return fallback(static_weights, online) if fallback else dict(static_weights)
    blended: dict[str, float] = {}
    for key, static_w in static_weights.items():
        online_w = online.get(key, 0.0)
        blended[key] = static_w * (1.0 + online_w)
    total = sum(blended.values())
    if total <= 0:
        return dict(static_weights)
    return {k: v / total for k, v in blended.items()}
