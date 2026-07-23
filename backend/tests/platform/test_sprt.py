"""Tests for Wald sequential probability ratio tests."""

from __future__ import annotations

import math

import pytest

from app.platform.sprt import binary_sprt, normal_sprt


def test_binary_sprt_accepts_h1_for_clear_winning_series():
    outcomes = [True] * 20

    report = binary_sprt(outcomes, p0=0.5, p1=0.7, alpha=0.05, beta=0.05)

    assert report.decision == "accept_h1"
    assert report.terminated_at is not None
    assert report.log_likelihood_ratio >= report.upper_boundary


def test_binary_sprt_accepts_h0_for_clear_losing_series():
    outcomes = [False] * 20

    report = binary_sprt(outcomes, p0=0.5, p1=0.7, alpha=0.05, beta=0.05)

    assert report.decision == "accept_h0"
    assert report.terminated_at is not None
    assert report.log_likelihood_ratio <= report.lower_boundary


def test_binary_sprt_continues_for_ambiguous_short_series():
    report = binary_sprt([1, 0], p0=0.5, p1=0.7, alpha=0.05, beta=0.05)

    assert report.decision == "continue"
    assert report.terminated_at is None


def test_binary_sprt_uses_wald_boundaries():
    alpha = 0.05
    beta = 0.1

    report = binary_sprt([1], p0=0.5, p1=0.6, alpha=alpha, beta=beta)

    assert report.upper_boundary == pytest.approx(math.log((1.0 - beta) / alpha))
    assert report.lower_boundary == pytest.approx(math.log(beta / (1.0 - alpha)))


def test_binary_sprt_keeps_path_flat_after_termination():
    outcomes = [True] * 20

    report = binary_sprt(outcomes, p0=0.5, p1=0.7, alpha=0.05, beta=0.05)

    assert report.terminated_at is not None
    assert report.n_observations == len(outcomes)
    assert len(report.llr_path) == len(outcomes)
    assert report.llr_path[report.terminated_at :] == pytest.approx(
        [report.log_likelihood_ratio] * (len(outcomes) - report.terminated_at)
    )


def test_normal_sprt_accepts_h1_for_positive_mean():
    values = [0.02] * 10

    report = normal_sprt(
        values,
        mu0=0.0,
        mu1=0.01,
        sigma=0.01,
        alpha=0.05,
        beta=0.05,
    )

    assert report.decision == "accept_h1"
    assert report.terminated_at is not None
    assert report.log_likelihood_ratio >= report.upper_boundary


def test_sprt_report_to_dict_includes_all_fields():
    report = binary_sprt([1, 0], p0=0.5, p1=0.7, alpha=0.05, beta=0.05)

    assert report.to_dict() == {
        "decision": report.decision,
        "log_likelihood_ratio": report.log_likelihood_ratio,
        "llr_path": report.llr_path,
        "upper_boundary": report.upper_boundary,
        "lower_boundary": report.lower_boundary,
        "n_observations": report.n_observations,
        "terminated_at": report.terminated_at,
    }


@pytest.mark.parametrize(
    ("p0", "p1"),
    [(0.0, 0.6), (1.0, 0.6), (0.5, 0.0), (0.5, 1.0), (0.5, 0.5)],
)
def test_binary_sprt_rejects_invalid_probabilities(p0: float, p1: float):
    with pytest.raises(ValueError):
        binary_sprt([1], p0=p0, p1=p1, alpha=0.05, beta=0.05)


@pytest.mark.parametrize(
    ("alpha", "beta"),
    [(0.0, 0.05), (1.0, 0.05), (0.05, 0.0), (0.05, 1.0)],
)
def test_binary_sprt_rejects_invalid_error_rates(alpha: float, beta: float):
    with pytest.raises(ValueError):
        binary_sprt([1], p0=0.5, p1=0.6, alpha=alpha, beta=beta)


def test_binary_sprt_rejects_empty_or_non_binary_outcomes():
    with pytest.raises(ValueError):
        binary_sprt([], p0=0.5, p1=0.6, alpha=0.05, beta=0.05)
    with pytest.raises(ValueError):
        binary_sprt([1, 2], p0=0.5, p1=0.6, alpha=0.05, beta=0.05)


def test_normal_sprt_rejects_empty_values_or_non_positive_sigma():
    with pytest.raises(ValueError):
        normal_sprt([], mu0=0.0, mu1=0.01, sigma=0.01, alpha=0.05, beta=0.05)
    with pytest.raises(ValueError):
        normal_sprt([0.01], mu0=0.0, mu1=0.01, sigma=0.0, alpha=0.05, beta=0.05)
