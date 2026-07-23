"""Wald sequential probability ratio tests for trading-strategy evidence.

The binary test compares two Bernoulli win probabilities. The normal test
compares two normal means with known variance. Both accumulate the log of the
likelihood ratio ``L(H1) / L(H0)`` and apply Wald's approximate error-rate
boundaries.

After a boundary is crossed, later observations are not incorporated. The
remaining entries in ``llr_path`` repeat the terminating value so the path
stays aligned with the supplied sequence. ``terminated_at`` is zero-based.

Reference: Wald (1945), "Sequential Tests of Statistical Hypotheses".
Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence, TypedDict

__all__ = ["SprtReport", "binary_sprt", "normal_sprt"]

SprtDecision = Literal["accept_h1", "accept_h0", "continue"]


class _SprtReportDict(TypedDict):
    decision: SprtDecision
    log_likelihood_ratio: float
    llr_path: list[float]
    upper_boundary: float
    lower_boundary: float
    n_observations: int
    terminated_at: int | None


@dataclass(frozen=True, slots=True)
class SprtReport:
    """Result of a sequential probability ratio test."""

    decision: SprtDecision
    log_likelihood_ratio: float
    llr_path: list[float]
    upper_boundary: float
    lower_boundary: float
    n_observations: int
    terminated_at: int | None

    def to_dict(self) -> _SprtReportDict:
        """Return a plain dictionary suitable for serialization."""
        return {
            "decision": self.decision,
            "log_likelihood_ratio": self.log_likelihood_ratio,
            "llr_path": list(self.llr_path),
            "upper_boundary": self.upper_boundary,
            "lower_boundary": self.lower_boundary,
            "n_observations": self.n_observations,
            "terminated_at": self.terminated_at,
        }


@dataclass(frozen=True, slots=True)
class _InvalidSprtInput(ValueError):
    parameter: str
    requirement: str

    def __str__(self) -> str:
        return f"{self.parameter} {self.requirement}"


def _validate_error_rates(alpha: float, beta: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise _InvalidSprtInput("alpha", "must be in (0, 1)")
    if not 0.0 < beta < 1.0:
        raise _InvalidSprtInput("beta", "must be in (0, 1)")


def _wald_boundaries(alpha: float, beta: float) -> tuple[float, float]:
    upper = math.log((1.0 - beta) / alpha)
    lower = math.log(beta / (1.0 - alpha))
    return upper, lower


def _run_sprt(increments: Sequence[float], alpha: float, beta: float) -> SprtReport:
    upper, lower = _wald_boundaries(alpha, beta)
    llr = 0.0
    llr_path: list[float] = []
    terminated_at: int | None = None

    for index, increment in enumerate(increments):
        if terminated_at is None:
            llr += increment
            if llr >= upper or llr <= lower:
                terminated_at = index
        llr_path.append(llr)

    if llr >= upper:
        decision: SprtDecision = "accept_h1"
    elif llr <= lower:
        decision = "accept_h0"
    else:
        decision = "continue"

    return SprtReport(
        decision=decision,
        log_likelihood_ratio=llr,
        llr_path=llr_path,
        upper_boundary=upper,
        lower_boundary=lower,
        n_observations=len(increments),
        terminated_at=terminated_at,
    )


def binary_sprt(
    outcomes: Sequence[int | bool],
    p0: float,
    p1: float,
    alpha: float,
    beta: float,
) -> SprtReport:
    """Test Bernoulli win probability ``p0`` against alternative ``p1``."""
    if not outcomes:
        raise _InvalidSprtInput("outcomes", "must not be empty")
    if not 0.0 < p0 < 1.0:
        raise _InvalidSprtInput("p0", "must be in (0, 1)")
    if not 0.0 < p1 < 1.0:
        raise _InvalidSprtInput("p1", "must be in (0, 1)")
    if p0 == p1:
        raise _InvalidSprtInput("p0 and p1", "must differ")
    _validate_error_rates(alpha, beta)

    win_increment = math.log(p1 / p0)
    loss_increment = math.log((1.0 - p1) / (1.0 - p0))
    increments: list[float] = []
    for outcome in outcomes:
        if outcome not in (0, 1):
            raise _InvalidSprtInput(
                "outcomes", "must contain only 0, 1, False, or True"
            )
        x = int(outcome)
        increments.append(x * win_increment + (1 - x) * loss_increment)

    return _run_sprt(increments, alpha, beta)


def normal_sprt(
    values: Sequence[float],
    mu0: float,
    mu1: float,
    sigma: float,
    alpha: float,
    beta: float,
) -> SprtReport:
    """Test normal mean ``mu0`` against ``mu1`` with known ``sigma``."""
    if not values:
        raise _InvalidSprtInput("values", "must not be empty")
    if sigma <= 0.0:
        raise _InvalidSprtInput("sigma", "must be > 0")
    _validate_error_rates(alpha, beta)

    scale = (mu1 - mu0) / (sigma * sigma)
    midpoint = (mu0 + mu1) / 2.0
    increments = [scale * (value - midpoint) for value in values]
    return _run_sprt(increments, alpha, beta)
