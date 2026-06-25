"""Tests for P245 Kalman filter + RTS smoother."""

from __future__ import annotations

import math

import pytest

from app.platform.kalman_filter import kalman_filter, rts_smoother


def test_constant_scalar_recovery():
    # Random-walk-free constant: F=1, H=1, Q=0, R=1; observe noisy constant 5.
    obs = [[5.0 + 0.1 * (i % 3 - 1)] for i in range(20)]
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[1.0]],
        x0=[0.0], P0=[[100.0]],
    )
    # Final filtered estimate should be near 5.
    assert abs(res.filtered_means[-1][0] - 5.0) < 0.5


def test_filtered_covariance_decreases():
    res = kalman_filter(
        [[5.0]] * 10, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[1.0]],
        x0=[0.0], P0=[[100.0]],
    )
    assert res.filtered_covs[-1][0][0] < res.filtered_covs[0][0][0]


def test_random_walk_tracks_signal():
    # F=1, H=1, Q=0.1, R=1; signal drifts linearly.
    obs = [[0.1 * i + 0.05 * ((i * 7) % 5 - 2)] for i in range(30)]
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[0.1]], R=[[1.0]],
        x0=[0.0], P0=[[1.0]],
    )
    # Last estimate near true 0.1*29 = 2.9 within tolerance.
    assert abs(res.filtered_means[-1][0] - 2.9) < 1.0


def test_rts_smoother_reduces_variance():
    obs = [[5.0 + 0.1 * ((i * 3) % 5 - 2)] for i in range(30)]
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[0.05]], R=[[1.0]],
        x0=[0.0], P0=[[10.0]],
    )
    sm = rts_smoother(res, F=[[1.0]])
    assert len(sm.smoothed_means) == len(res.filtered_means)
    # Smoothed variance never exceeds filtered variance.
    for fs, ss in zip(res.filtered_covs, sm.smoothed_covs):
        assert ss[0][0] <= fs[0][0] + 1e-9


def test_rts_smoother_constant_signal_boundary_matches():
    obs = [[5.0]] * 15
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[0.5]],
        x0=[0.0], P0=[[100.0]],
    )
    sm = rts_smoother(res, F=[[1.0]])
    # Boundary: last smoothed step equals last filtered step.
    assert abs(sm.smoothed_means[-1][0] - res.filtered_means[-1][0]) < 1e-9
    # Early smoothed estimate is pulled toward the converged value (more info).
    assert abs(sm.smoothed_means[0][0] - 5.0) <= abs(res.filtered_means[0][0] - 5.0) + 1e-9


def test_velocity_state_two_dim():
    # State [pos, vel], observe pos only; constant velocity.
    dt = 1.0
    F = [[1.0, dt], [0.0, 1.0]]
    H = [[1.0, 0.0]]
    Q = [[1e-4, 0.0], [0.0, 1e-4]]
    R = [[1.0]]
    true_vel = 0.7
    obs = [[0.7 * i + 0.1 * ((i * 5) % 7 - 3)] for i in range(40)]
    res = kalman_filter(
        obs, F=F, H=H, Q=Q, R=R, x0=[0.0, 0.0], P0=[[1000.0, 0.0], [0.0, 1000.0]],
    )
    # Velocity estimate converges near 0.7.
    assert abs(res.filtered_means[-1][1] - true_vel) < 0.3


def test_innovations_have_correct_length():
    obs = [[float(i)] for i in range(10)]
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[1.0]], R=[[1.0]],
        x0=[0.0], P0=[[1.0]],
    )
    assert len(res.innovations) == 10


def test_singular_innovation_cov_raises():
    # P_pred = 0 (P0=0, Q=0) and R = 0 -> S = 0 singular.
    obs = [[1.0]]
    with pytest.raises(ValueError):
        kalman_filter(
            obs, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[0.0]],
            x0=[0.0], P0=[[0.0]],
        )


def test_empty_observations_raises():
    with pytest.raises(ValueError):
        kalman_filter([], F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[1.0]], x0=[0.0], P0=[[1.0]])


def test_control_input_applied():
    # F=1, B=1, u constant 0.5 -> state should grow ~0.5/t minus observation pull.
    obs = [[0.0]] * 5
    res = kalman_filter(
        obs, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[100.0]],
        x0=[0.0], P0=[[1.0]],
        B=[[1.0]], u=[[0.5]] * 5,
    )
    # With huge R the filter trusts the model: x ~ 0.5 * 5 = 2.5
    assert res.filtered_means[-1][0] > 1.0


def test_to_dict_roundtrip():
    res = kalman_filter(
        [[1.0]] * 3, F=[[1.0]], H=[[1.0]], Q=[[0.0]], R=[[1.0]],
        x0=[0.0], P0=[[1.0]],
    )
    d = res.to_dict()
    assert "filtered_means" in d and "smoothed_means" in d
    assert len(d["filtered_means"]) == 3