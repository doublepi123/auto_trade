"""P370: Distance Correlation — Székely et al. non-linear dependence measure.

Computes distance correlation and distance covariance between two random vectors.
Unlike Pearson correlation, distance correlation detects non-linear dependencies
and equals zero iff the variables are independent.

Pure Python, no scipy/numpy.

Reference: Székely, Rizzo & Bakirov (2007) "Measuring and testing dependence
by correlation of distances".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["DistanceCorrelationResult", "distance_correlation_report"]


@dataclass(frozen=True)
class DistanceCorrelationResult:
    """Frozen aggregate result of :func:`distance_correlation_report`.

    * ``distance_correlation`` — dCor ∈ [0, 1]; 1 = perfect non-linear dependence.
    * ``distance_covariance`` — dCov².
    * ``distance_variance_x`` / ``distance_variance_y`` — dVar for each variable.
    """

    distance_correlation: float
    distance_covariance: float
    distance_variance_x: float
    distance_variance_y: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "distance_correlation": self.distance_correlation,
            "distance_covariance": self.distance_covariance,
            "distance_variance_x": self.distance_variance_x,
            "distance_variance_y": self.distance_variance_y,
        }


def _validate_xy(x: list[float], y: list[float]) -> tuple[list[float], list[float]]:
    """Validate x and y series, return cleaned copies."""
    if not isinstance(x, list) or not isinstance(y, list):
        raise ValueError("x and y must be lists")

    cleaned_x: list[float] = []
    cleaned_y: list[float] = []

    for i, v in enumerate(x):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"x[{i}] must be a number")
        if not math.isfinite(float(v)):
            raise ValueError(f"x[{i}] must be a finite number")
        cleaned_x.append(float(v))

    for i, v in enumerate(y):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"y[{i}] must be a number")
        if not math.isfinite(float(v)):
            raise ValueError(f"y[{i}] must be a finite number")
        cleaned_y.append(float(v))

    if len(cleaned_x) != len(cleaned_y):
        raise ValueError("x and y must have equal length")
    if len(cleaned_x) < 3:
        raise ValueError("need at least 3 observations")

    return cleaned_x, cleaned_y


def _distance_matrix(values: list[float]) -> list[list[float]]:
    """Compute pairwise Euclidean distance matrix.

    Returns n×n matrix where A[i][j] = |values[i] - values[j]|.
    """
    n = len(values)
    # Create n×n matrix
    dist: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            dist[i][j] = abs(values[i] - values[j])
    return dist


def _double_center(dist: list[list[float]]) -> list[list[float]]:
    """Double-center a distance matrix: remove row and column means, add grand mean.

    A_ij = a_ij - ā_i. - ā_.j + ā_..
    """
    n = len(dist)

    # Row means
    row_means = [sum(row) / n for row in dist]
    # Column means
    col_means = [0.0] * n
    for j in range(n):
        col_means[j] = sum(dist[i][j] for i in range(n)) / n
    # Grand mean
    grand_mean = sum(row_means) / n

    centered: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            centered[i][j] = dist[i][j] - row_means[i] - col_means[j] + grand_mean

    return centered


def distance_correlation_report(
    x: list[float], y: list[float]
) -> DistanceCorrelationResult:
    """Compute Székely distance correlation between x and y.

    Parameters
    ----------
    x, y : list[float]
        Two numeric series of equal length.

    Returns
    -------
    DistanceCorrelationResult
    """
    x_clean, y_clean = _validate_xy(x, y)
    n = len(x_clean)

    # Compute distance matrices
    a_dist = _distance_matrix(x_clean)
    b_dist = _distance_matrix(y_clean)

    # Double-center both matrices
    a_centered = _double_center(a_dist)
    b_centered = _double_center(b_dist)

    # dCov² = (1/n²) * Σᵢⱼ A_ij * B_ij
    d_cov_sq = 0.0
    d_var_x_sq = 0.0
    d_var_y_sq = 0.0
    for i in range(n):
        for j in range(n):
            d_cov_sq += a_centered[i][j] * b_centered[i][j]
            d_var_x_sq += a_centered[i][j] * a_centered[i][j]
            d_var_y_sq += b_centered[i][j] * b_centered[i][j]

    n_sq = float(n * n)
    d_cov_sq /= n_sq
    d_var_x_sq /= n_sq
    d_var_y_sq /= n_sq

    # dCor = sqrt(dCov² / (dVar_x * dVar_y))
    # Clamp to avoid tiny negatives from floating point
    d_var_x = max(0.0, d_var_x_sq)
    d_var_y = max(0.0, d_var_y_sq)
    distance_variance_x = math.sqrt(d_var_x)
    distance_variance_y = math.sqrt(d_var_y)
    distance_covariance = math.sqrt(max(0.0, d_cov_sq))

    prod = distance_variance_x * distance_variance_y
    if prod < 1e-15:
        distance_correlation = 0.0
    else:
        denom = math.sqrt(prod)
        distance_correlation = distance_covariance / denom
        # Clamp to [0, 1]
        distance_correlation = max(0.0, min(1.0, distance_correlation))

    return DistanceCorrelationResult(
        distance_correlation=distance_correlation,
        distance_covariance=distance_covariance,
        distance_variance_x=distance_variance_x,
        distance_variance_y=distance_variance_y,
    )
