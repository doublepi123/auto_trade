"""P257: Principal Component Analysis via the cyclic Jacobi eigenvalue method.

Pure-Python, dependency-free PCA: given a data matrix ``X`` (n_samples ×
n_features, as ``list[list[float]]``), compute the principal axes via the
cyclic **Jacobi rotation** eigen-decomposition of the sample covariance matrix,
then return eigenvalues, eigenvectors, explained-variance ratios and the
projection of the data onto the principal components.

The Jacobi method (1846) iteratively zero-sweeps off-diagonal elements of the
symmetric covariance via Givens rotations, converging quadratically to the
eigendecomposition ``Σ = V Λ Vᵀ``. No numpy/scipy. Deterministic.

Reference: Golub & Van Loan "Matrix Computations" §8.4; Jacobi (1846).
Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = ["PcaResult", "pca"]

Matrix = list[list[float]]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _covariance(X: Matrix) -> Matrix:
    """Sample covariance matrix (ddof=1) of an n×p data matrix (rows = samples)."""
    n = len(X)
    if n == 0:
        raise ValueError("data must be non-empty")
    p = len(X[0])
    for row in X:
        if len(row) != p:
            raise ValueError("all rows must have equal length")
    means = [_mean([X[i][j] for i in range(n)]) for j in range(p)]
    cov: Matrix = [[0.0] * p for _ in range(p)]
    ddof = n - 1 if n > 1 else 1
    for i in range(p):
        for j in range(i, p):
            s = 0.0
            for r in range(n):
                s += (X[r][i] - means[i]) * (X[r][j] - means[j])
            s /= ddof
            cov[i][j] = s
            cov[j][i] = s
    return cov


def _jacobi_eigen(a: Matrix, *, max_sweeps: int = 100, tol: float = 1e-12) -> tuple[list[float], Matrix]:
    """Eigen-decomposition of a symmetric matrix via cyclic Jacobi rotations.

    Returns ``(eigenvalues (descending), eigenvectors as columns)``.
    """
    n = len(a)
    A = [row[:] for row in a]
    V = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(max_sweeps):
        off = 0.0
        for p in range(n):
            for q in range(p + 1, n):
                off += A[p][q] * A[p][q]
        if off < tol:
            break
        for p in range(n):
            for q in range(p + 1, n):
                apq = A[p][q]
                if abs(apq) < 1e-300:
                    continue
                app = A[p][p]
                aqq = A[q][q]
                # Rotation angle.
                if abs(app - aqq) < 1e-300:
                    theta = math.pi / 4.0
                else:
                    theta = 0.5 * math.atan2(2.0 * apq, app - aqq)
                c = math.cos(theta)
                s = math.sin(theta)
                # Apply rotation to A (both rows/cols p and q).
                for i in range(n):
                    aip = A[i][p]
                    aiq = A[i][q]
                    A[i][p] = c * aip + s * aiq
                    A[i][q] = -s * aip + c * aiq
                for i in range(n):
                    api = A[p][i]
                    aqi = A[q][i]
                    A[p][i] = c * api + s * aqi
                    A[q][i] = -s * api + c * aqi
                # Update eigenvector matrix.
                for i in range(n):
                    vip = V[i][p]
                    viq = V[i][q]
                    V[i][p] = c * vip + s * viq
                    V[i][q] = -s * vip + c * viq
    eigvals = [A[i][i] for i in range(n)]
    # Sort descending.
    order = sorted(range(n), key=lambda i: -eigvals[i])
    eigvals_sorted = [eigvals[i] for i in order]
    V_sorted = [[V[i][order[j]] for j in range(n)] for i in range(n)]
    return eigvals_sorted, V_sorted


@dataclass(frozen=True)
class PcaResult:
    eigenvalues: list[float]
    eigenvectors: list[list[float]]  # columns are the principal axes
    explained_variance_ratio: list[float]
    cumulative_variance_ratio: list[float]
    n_components: int
    n_samples: int
    projection: list[list[float]]

    def to_dict(self) -> dict:
        return {
            "eigenvalues": self.eigenvalues,
            "eigenvectors": self.eigenvectors,
            "explained_variance_ratio": self.explained_variance_ratio,
            "cumulative_variance_ratio": self.cumulative_variance_ratio,
            "n_components": self.n_components,
            "n_samples": self.n_samples,
            "projection": self.projection,
        }


def _mat_vec(a: Matrix, v: list[float]) -> list[float]:
    n, m = len(a), len(a[0])
    return [sum(a[i][j] * v[j] for j in range(m)) for i in range(n)]


def pca(X: Sequence[Sequence[float]], *, n_components: int | None = None) -> PcaResult:
    """Principal component analysis of an n×p data matrix (rows = samples).

    Returns :class:`PcaResult` with eigenvalues (descending), eigenvectors
    (principal axes as columns), explained-variance ratios, cumulative ratios,
    and the projection ``X_centered @ V``. Raises ``ValueError`` on ragged /
    empty input or ``n_components`` out of range.
    """
    n = len(X)
    if n < 2:
        raise ValueError("need at least 2 samples")
    p = len(X[0])
    if p < 1:
        raise ValueError("need at least 1 feature")
    if n_components is not None and (n_components < 1 or n_components > p):
        raise ValueError("n_components must be in [1, n_features]")

    data = [[float(v) for v in row] for row in X]
    cov = _covariance(data)
    eigvals, eigvecs = _jacobi_eigen(cov)

    total_var = sum(eigvals)
    if total_var <= 0.0:
        # Constant features -> all variance zero; ratios degenerate.
        evr = [0.0] * p
    else:
        evr = [e / total_var for e in eigvals]
    cum = []
    running = 0.0
    for r in evr:
        running += r
        cum.append(running)

    # Center the data and project onto the principal axes.
    means = [_mean([data[i][j] for i in range(n)]) for j in range(p)]
    centered = [[data[i][j] - means[j] for j in range(p)] for i in range(n)]
    projection = [_mat_vec(centered, [eigvecs[i][k] for i in range(p)]) for k in range(p)]
    # Transpose so projection[k] = scores on component k → return as rows-by-sample.
    projection_t = [[projection[k][i] for k in range(p)] for i in range(n)]

    k = n_components if n_components is not None else p
    return PcaResult(
        eigenvalues=eigvals[:k],
        eigenvectors=[[eigvecs[i][j] for j in range(k)] for i in range(p)],
        explained_variance_ratio=evr[:k],
        cumulative_variance_ratio=cum[:k],
        n_components=k,
        n_samples=n,
        projection=[row[:k] for row in projection_t],
    )