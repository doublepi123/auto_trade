"""Bayesian online changepoint detection with a conjugate NIG model.

Implements Adams & MacKay (2007), arXiv:0710.3742, in log space for
numerical stability. Each run-length hypothesis uses a Normal-Inverse-Gamma
posterior whose marginal predictive distribution is Student-t. The module is
pure Python and performs no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

__all__ = ["BocpdReport", "bocpd"]


class _BocpdReportDict(TypedDict):
    changepoint_probs: list[float]
    map_run_lengths: list[int]
    detected_changepoints: list[int]
    n_observations: int
    threshold: float


@dataclass(frozen=True, slots=True)
class _NigHypothesis:
    mu: float
    kappa: float
    alpha: float
    beta: float

    def predictive_log_probability(self, value: float) -> float:
        """Return the Student-t predictive log-density for one observation."""
        degrees_of_freedom = 2.0 * self.alpha
        scale_squared = max(
            self.beta * (self.kappa + 1.0) / (self.alpha * self.kappa),
            1e-12,
        )
        difference = value - self.mu
        half_degrees = degrees_of_freedom / 2.0
        return (
            math.lgamma(half_degrees + 0.5)
            - math.lgamma(half_degrees)
            - 0.5 * math.log(degrees_of_freedom * math.pi * scale_squared)
            - (half_degrees + 0.5)
            * math.log1p(
                difference * difference / (degrees_of_freedom * scale_squared)
            )
        )

    def updated(self, value: float) -> _NigHypothesis:
        """Return the conjugate posterior after observing ``value``."""
        next_kappa = self.kappa + 1.0
        difference = value - self.mu
        return _NigHypothesis(
            mu=(self.kappa * self.mu + value) / next_kappa,
            kappa=next_kappa,
            alpha=self.alpha + 0.5,
            beta=self.beta
            + self.kappa * difference * difference / (2.0 * next_kappa),
        )


@dataclass(frozen=True, slots=True)
class BocpdReport:
    """One-shot BOCPD posterior summary."""

    changepoint_probs: list[float]
    map_run_lengths: list[int]
    detected_changepoints: list[int]
    n_observations: int
    threshold: float

    def to_dict(self) -> _BocpdReportDict:
        return {
            "changepoint_probs": self.changepoint_probs.copy(),
            "map_run_lengths": self.map_run_lengths.copy(),
            "detected_changepoints": self.detected_changepoints.copy(),
            "n_observations": self.n_observations,
            "threshold": self.threshold,
        }


def _logsumexp(values: Sequence[float]) -> float:
    maximum = max(values)
    if maximum == -math.inf:
        return -math.inf
    return maximum + math.log(sum(math.exp(value - maximum) for value in values))


def bocpd(
    values: Sequence[float],
    *,
    mu0: float = 0.0,
    kappa0: float = 1.0,
    alpha0: float = 1.0,
    beta0: float = 1.0,
    lam: float = 100.0,
    threshold: float = 0.5,
) -> BocpdReport:
    """Run Bayesian online changepoint detection over ``values`` once."""
    observations = list(values)
    if not observations:
        message = "values must not be empty"
        raise ValueError(message)
    if any(not math.isfinite(value) for value in observations):
        message = "values must contain only finite numbers"
        raise ValueError(message)
    if not math.isfinite(mu0):
        message = "mu0 must be finite"
        raise ValueError(message)
    if not math.isfinite(kappa0) or kappa0 <= 0.0:
        message = "kappa0 must be finite and > 0"
        raise ValueError(message)
    if not math.isfinite(alpha0) or alpha0 <= 0.0:
        message = "alpha0 must be finite and > 0"
        raise ValueError(message)
    if not math.isfinite(beta0) or beta0 <= 0.0:
        message = "beta0 must be finite and > 0"
        raise ValueError(message)
    if not math.isfinite(lam) or lam <= 1.0:
        message = "lam must be finite and > 1"
        raise ValueError(message)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        message = "threshold must be finite and in [0, 1]"
        raise ValueError(message)

    prior = _NigHypothesis(mu=mu0, kappa=kappa0, alpha=alpha0, beta=beta0)
    hypotheses = [prior]
    message = [0.0]
    log_hazard = math.log(1.0 / lam)
    log_survival = math.log(1.0 - 1.0 / lam)
    changepoint_probs: list[float] = []
    map_run_lengths: list[int] = []

    for observation in observations:
        log_predictives = [
            hypothesis.predictive_log_probability(observation)
            for hypothesis in hypotheses
        ]
        log_growth = [
            predictive + previous + log_survival
            for predictive, previous in zip(log_predictives, message, strict=True)
        ]
        log_changepoint = _logsumexp(
            [
                predictive + previous + log_hazard
                for predictive, previous in zip(
                    log_predictives, message, strict=True
                )
            ]
        )
        new_log_joint = [log_changepoint, *log_growth]
        normalizer = _logsumexp(new_log_joint)
        log_run_length = [value - normalizer for value in new_log_joint]

        changepoint_probs.append(math.exp(log_run_length[0]))
        map_run_lengths.append(
            max(range(len(log_run_length)), key=log_run_length.__getitem__)
        )
        message = new_log_joint
        hypotheses = [prior, *[hypothesis.updated(observation) for hypothesis in hypotheses]]

    detected_changepoints = [
        index
        for index, probability in enumerate(changepoint_probs)
        if probability > threshold
    ]
    return BocpdReport(
        changepoint_probs=changepoint_probs,
        map_run_lengths=map_run_lengths,
        detected_changepoints=detected_changepoints,
        n_observations=len(observations),
        threshold=threshold,
    )
