"""P323: Regime Transitions — transition matrix + expected duration + steady state.

Given a sequence of regime labels, computes:

* **transition_matrix** — count-based transitions between regimes.
* **transition_probabilities** — row-normalised transition probabilities.
* **expected_durations** — ``1 / (1 − p_stay)`` per regime.
* **steady_state** — stationary distribution (eigenvector of transition
  probability matrix).

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


__all__ = ["RegimeTransitionsResult", "regime_transitions_report"]


@dataclass(frozen=True)
class RegimeTransitionsResult:
    """Frozen aggregate result of :func:`regime_transitions_report`.

    * ``transition_matrix`` — ``{from: {to: count}}`` count-based.
    * ``transition_probabilities`` — row-normalised probabilities.
    * ``expected_durations`` — ``{regime: float}``.
    * ``steady_state`` — ``{regime: float}`` stationary probabilities.
    """

    transition_matrix: dict[str, dict[str, int]]
    transition_probabilities: dict[str, dict[str, float]]
    expected_durations: dict[str, float]
    steady_state: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_matrix": self.transition_matrix,
            "transition_probabilities": self.transition_probabilities,
            "expected_durations": self.expected_durations,
            "steady_state": self.steady_state,
        }


def regime_transitions_report(regimes: list[str]) -> RegimeTransitionsResult:
    """Compute regime transition diagnostics from a label sequence.

    Parameters
    ----------
    regimes : list[str]
        A sequence of regime labels.

    Returns
    -------
    RegimeTransitionsResult

    Raises
    ------
    ValueError
        If ``regimes`` has fewer than 2 entries.
    """
    if not isinstance(regimes, list) or len(regimes) < 2:
        raise ValueError("regimes must be a list with at least 2 entries")
    for i, r in enumerate(regimes):
        if not isinstance(r, str):
            raise ValueError(f"regimes[{i}] must be a string")
        if not r:
            raise ValueError(f"regimes[{i}] must be a non-empty string")

    # Build count-based transition matrix
    transition_matrix: dict[str, dict[str, int]] = {}
    for i in range(len(regimes) - 1):
        src = regimes[i]
        dst = regimes[i + 1]
        if src not in transition_matrix:
            transition_matrix[src] = {}
        transition_matrix[src][dst] = transition_matrix[src].get(dst, 0) + 1

    # Also register last regime for durations
    all_regimes = sorted(set(regimes))
    for r in all_regimes:
        if r not in transition_matrix:
            transition_matrix[r] = {}

    # Row-normalised probabilities
    transition_probabilities: dict[str, dict[str, float]] = {}
    for src, dsts in transition_matrix.items():
        total = sum(dsts.values())
        if total > 0:
            transition_probabilities[src] = {k: v / total for k, v in dsts.items()}
        else:
            # No transitions observed → self-loop probability = 1.0
            transition_probabilities[src] = {src: 1.0}

    # Expected duration: 1 / (1 - p_stay)
    expected_durations: dict[str, float] = {}
    for r in all_regimes:
        p_stay = transition_probabilities.get(r, {}).get(r, 0.0)
        if p_stay >= 1.0:
            expected_durations[r] = float("inf")
        elif p_stay <= 0.0:
            expected_durations[r] = 1.0
        else:
            expected_durations[r] = 1.0 / (1.0 - p_stay)

    # Steady-state distribution via iterative power method
    n = len(all_regimes)
    if n == 0:
        steady_state: dict[str, float] = {}
    elif n == 1:
        steady_state = {all_regimes[0]: 1.0}
    else:
        # Build full transition matrix for power iteration
        idx_map = {r: i for i, r in enumerate(all_regimes)}
        P = [[0.0] * n for _ in range(n)]
        for src, dsts in transition_probabilities.items():
            si = idx_map[src]
            for dst, prob in dsts.items():
                di = idx_map.get(dst)
                if di is not None:
                    P[si][di] = prob
        # For regimes with no outgoing transitions, fill self-loop
        for si, r in enumerate(all_regimes):
            if sum(P[si]) < 1e-15:
                P[si][si] = 1.0

        # Power method: iterate until convergence
        vec = [1.0 / n] * n
        for _ in range(500):
            new_vec = [0.0] * n
            for i in range(n):
                for j in range(n):
                    new_vec[j] += vec[i] * P[i][j]
            # Convergence check
            max_diff = max(abs(new_vec[i] - vec[i]) for i in range(n))
            vec = new_vec
            if max_diff < 1e-9:
                break
        steady_state = {all_regimes[i]: vec[i] for i in range(n)}

    return RegimeTransitionsResult(
        transition_matrix=transition_matrix,
        transition_probabilities=transition_probabilities,
        expected_durations=expected_durations,
        steady_state=steady_state,
    )
