"""Tests for P257 PCA (cyclic Jacobi eigen-decomposition)."""

from __future__ import annotations

import math

import pytest

from app.platform.pca import pca


def test_pca_diagonal_cov_equal_eigenvalues():
    # Diagonal covariance with equal variance -> equal eigenvalues.
    X = [[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, -1.0]]
    res = pca(X)
    assert res.n_components == 3
    # Each axis has identical sample variance -> equal eigenvalues.
    e0 = res.eigenvalues[0]
    assert all(abs(e - e0) < 1e-6 for e in res.eigenvalues)
    assert all(abs(r - 1.0 / 3) < 1e-6 for r in res.explained_variance_ratio)


def test_pca_dominant_component_direction():
    # Data with variance only along axis [1,1] -> PC1 ≈ [1,1]/sqrt2, eigenvalue dominates.
    X = [[i, i] for i in range(-5, 6)]
    res = pca(X)
    # One eigenvalue large (along [1,1]), one ~0.
    assert res.eigenvalues[0] > res.eigenvalues[1] * 100
    # PC1 direction proportional to [1,1].
    v1 = [res.eigenvectors[0][0], res.eigenvectors[1][0]]
    norm = math.hypot(v1[0], v1[1])
    v1 = [v1[0] / norm, v1[1] / norm]
    assert abs(abs(v1[0]) - 1 / math.sqrt(2)) < 1e-6
    assert abs(abs(v1[1]) - 1 / math.sqrt(2)) < 1e-6


def test_pca_explained_variance_ratios_sum_to_one():
    X = [[1.0, 2.0, 3.0], [4.0, 0.0, 1.0], [2.0, 3.0, 5.0], [0.0, 1.0, 2.0], [5.0, 5.0, 5.0]]
    res = pca(X)
    assert abs(sum(res.explained_variance_ratio) - 1.0) < 1e-6
    assert abs(res.cumulative_variance_ratio[-1] - 1.0) < 1e-6


def test_pca_cumulative_monotone_increasing():
    X = [[float(i + j) for j in range(3)] for i in range(10)]
    res = pca(X)
    for a, b in zip(res.cumulative_variance_ratio, res.cumulative_variance_ratio[1:]):
        assert b >= a - 1e-12


def test_pca_projection_shape():
    X = [[float(i), float(i * i)] for i in range(10)]
    res = pca(X)
    assert len(res.projection) == 10
    assert len(res.projection[0]) == 2


def test_pca_n_components_truncates():
    X = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0], [2.0, 1.0, 0.0]]
    res = pca(X, n_components=2)
    assert res.n_components == 2
    assert len(res.eigenvalues) == 2
    assert len(res.projection[0]) == 2


def test_pca_eigenvectors_orthonormal():
    # Eigenvectors of a symmetric matrix should be orthonormal.
    X = [[float((i * 7 + j * 3) % 11) for j in range(4)] for i in range(20)]
    res = pca(X)
    V = res.eigenvectors
    p = len(V)
    for a in range(p):
        norm = math.sqrt(sum(V[i][a] ** 2 for i in range(p)))
        assert abs(norm - 1.0) < 1e-6
        for b in range(a + 1, p):
            dot = sum(V[i][a] * V[i][b] for i in range(p))
            assert abs(dot) < 1e-6


def test_pca_reconstruction_via_spectral():
    # Sum of eigenvalues == total variance (trace of covariance).
    X = [[float((i * 5 + j * 2) % 13) for j in range(3)] for i in range(15)]
    res = pca(X)
    # total variance = sum of eigenvalues.
    means = [sum(X[i][j] for i in range(15)) / 15 for j in range(3)]
    total_var = sum(sum((X[i][j] - means[j]) ** 2 for i in range(15)) / 14 for j in range(3))
    assert abs(sum(res.eigenvalues) - total_var) < 1e-6


def test_pca_invalid_inputs_raise():
    with pytest.raises(ValueError):
        pca([[1.0]])
    with pytest.raises(ValueError):
        pca([])
    with pytest.raises(ValueError):
        pca([[1.0, 2.0], [3.0]], )  # ragged
    with pytest.raises(ValueError):
        pca([[1.0, 2.0], [3.0, 4.0]], n_components=5)


def test_to_dict_roundtrip():
    X = [[float(i), float(i * 2)] for i in range(8)]
    d = pca(X).to_dict()
    for k in ("eigenvalues", "eigenvectors", "explained_variance_ratio", "projection"):
        assert k in d