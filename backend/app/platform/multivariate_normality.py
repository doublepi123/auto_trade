"""P374: Mardia multivariate normality test.

Pure-Python implementation of Mardia's (1970) multivariate skewness and
kurtosis tests. Assesses whether a panel of asset returns is consistent
with a joint multivariate normal distribution.

Reference: Mardia, K. V. (1970). "Measures of Multivariate Skewness
and Kurtosis with Applications".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MultivariateNormalityResult:
    """Frozen carrier for Mardia multivariate normality test results."""

    mardia_skewness: float
    mardia_kurtosis: float
    skewness_p_value: float
    kurtosis_p_value: float
    is_multivariate_normal: bool
    n_observations: int
    n_assets: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mardia_skewness": self.mardia_skewness,
            "mardia_kurtosis": self.mardia_kurtosis,
            "skewness_p_value": self.skewness_p_value,
            "kurtosis_p_value": self.kurtosis_p_value,
            "is_multivariate_normal": self.is_multivariate_normal,
            "n_observations": self.n_observations,
            "n_assets": self.n_assets,
        }


def _validate_returns_panel(
    returns_panel: dict[str, list[float]],
) -> tuple[list[str], list[list[float]], int, int]:
    """Validate the returns panel, returning sorted asset names, matrix, n, p."""
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")

    p = len(returns_panel)
    n: int | None = None
    assets: list[str] = []
    matrix: list[list[float]] = []

    for name, series in returns_panel.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"returns_panel['{name}'] must be a list of finite numbers")
        if not series:
            raise ValueError(f"returns_panel['{name}'] must be non-empty")

        validated: list[float] = []
        for v in series:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"returns_panel['{name}'] entries must be finite numbers")
            fv = float(v)
            if not math.isfinite(fv):
                raise ValueError(f"returns_panel['{name}'] entries must be finite numbers")
            validated.append(fv)

        if n is None:
            n = len(validated)
        elif len(validated) != n:
            raise ValueError("returns_panel series must have equal length")
        assets.append(str(name))
        matrix.append(validated)

    if n is None or n < 3:
        raise ValueError("returns_panel must contain at least 3 observations")

    return assets, matrix, n, p


def _chol_inv_2x2_or_3x3(cov: list[list[float]]) -> list[list[float]] | None:
    """Compute the inverse of a p×p covariance matrix via Cholesky for p<=3."""
    p = len(cov)
    # Build Cholesky L
    L: list[list[float]] = [[0.0] * p for _ in range(p)]
    for i in range(p):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                diag = cov[i][i] - s
                if diag <= 0:
                    return None
                L[i][j] = math.sqrt(diag)
            else:
                if L[j][j] == 0:
                    return None
                L[i][j] = (cov[i][j] - s) / L[j][j]
    # Invert lower-triangular L
    Linv: list[list[float]] = [[0.0] * p for _ in range(p)]
    for i in range(p):
        if L[i][i] == 0:
            return None
        Linv[i][i] = 1.0 / L[i][i]
        for j in range(i):
            s = sum(L[i][k] * Linv[k][j] for k in range(j, i))
            Linv[i][j] = -s / L[i][i]
    # cov_inv = L^{-T} * L^{-1}
    cov_inv: list[list[float]] = [[0.0] * p for _ in range(p)]
    for i in range(p):
        for j in range(i + 1):
            s = 0.0
            for k in range(max(i, j), p):
                s += Linv[k][i] * Linv[k][j]
            cov_inv[i][j] = s
            cov_inv[j][i] = s
    return cov_inv


def _sample_cov(matrix: list[list[float]]) -> list[list[float]]:
    """Compute p×p sample covariance matrix from T×p data stored as list of p rows, each of length T."""
    p = len(matrix)
    T = len(matrix[0])
    means = [sum(row) / T for row in matrix]
    cov: list[list[float]] = [[0.0] * p for _ in range(p)]
    for i in range(p):
        for j in range(i, p):
            s = 0.0
            for t in range(T):
                s += (matrix[i][t] - means[i]) * (matrix[j][t] - means[j])
            cov[i][j] = s / (T - 1)
            cov[j][i] = cov[i][j]
    return cov


def _mardia_skewness(matrix: list[list[float]], cov_inv: list[list[float]]) -> float:
    """Compute Mardia's multivariate skewness b1p."""
    p = len(matrix)
    T = len(matrix[0])
    means = [sum(row) / T for row in matrix]

    b1p = 0.0
    for s in range(T):
        for t in range(T):
            # Mahalanobis distance: (x_s - mu)^T S^{-1} (x_t - mu)
            z_st = 0.0
            for i in range(p):
                for j in range(p):
                    z_st += (matrix[i][s] - means[i]) * cov_inv[i][j] * (matrix[j][t] - means[j])
            b1p += z_st ** 3
    b1p = b1p / (T * T)
    return b1p


def _mardia_kurtosis(matrix: list[list[float]], cov_inv: list[list[float]]) -> float:
    """Compute Mardia's multivariate kurtosis b2p."""
    p = len(matrix)
    T = len(matrix[0])
    means = [sum(row) / T for row in matrix]

    b2p = 0.0
    for t in range(T):
        md2 = 0.0
        for i in range(p):
            for j in range(p):
                md2 += (matrix[i][t] - means[i]) * cov_inv[i][j] * (matrix[j][t] - means[j])
        b2p += md2 * md2
    b2p = b2p / T
    return b2p


def _chi2_cdf(x: float, df: float) -> float:
    """Approximate chi-square CDF using normal approximation for large df, or Wilson-Hilferty."""
    if x <= 0:
        return 0.0
    if df < 1:
        return 0.0

    # Wilson-Hilferty transformation: (x/df)^{1/3} approx N(1 - 2/(9df), 2/(9df))
    cbrt = (x / df) ** (1.0 / 3.0)
    mean = 1.0 - 2.0 / (9.0 * df)
    std = math.sqrt(2.0 / (9.0 * df))
    if std <= 0:
        return 0.5
    z = (cbrt - mean) / std
    return _std_norm_cdf_approx(z)


def _std_norm_cdf_approx(x: float) -> float:
    """Approximation of the standard normal CDF."""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    # Abramowitz & Stegun 7.1.26
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429
    phi = 1.0 / math.sqrt(2.0 * math.pi) * math.exp(-x * x / 2.0)
    cdf = 1.0 - phi * (b1 * t + b2 * t * t + b3 * t * t * t + b4 * t * t * t * t + b5 * t * t * t * t * t)
    if x < 0:
        return 1.0 - cdf
    return cdf


def multivariate_normality_report(
    returns_panel: dict[str, list[float]],
) -> MultivariateNormalityResult:
    """Mardia's multivariate normality test for a panel of asset returns.

    Parameters
    ----------
    returns_panel:
        Dict mapping asset name to list of period returns.
        All series must be equal-length, non-empty, non-constant.
        Maximum 50 assets.

    Returns
    -------
    MultivariateNormalityResult with Mardia statistics and p-values.
    """
    assets, matrix, n, p = _validate_returns_panel(returns_panel)

    # Compute sample covariance
    cov = _sample_cov(matrix)

    # Compute Cholesky inverse of covariance
    # For general p > 3, fall back to a simpler approach
    cov_inv = _chol_inv_2x2_or_3x3(cov)
    if cov_inv is None:
        # Covariance matrix is singular — cannot assess normality
        return MultivariateNormalityResult(
            mardia_skewness=float("inf"),
            mardia_kurtosis=float("inf"),
            skewness_p_value=0.0,
            kurtosis_p_value=0.0,
            is_multivariate_normal=False,
            n_observations=n,
            n_assets=p,
        )

    b1p = _mardia_skewness(matrix, cov_inv)
    b2p = _mardia_kurtosis(matrix, cov_inv)

    # Skewness test: T * b1p / 6 ~ chi2 with df = p(p+1)(p+2)/6
    df_skew = p * (p + 1) * (p + 2) / 6.0
    skew_stat = n * b1p / 6.0
    skew_p = 1.0 - _chi2_cdf(max(skew_stat, 0.0), df_skew)

    # Kurtosis test: (b2p - p(p+2)) / sqrt(8p(p+2)/T) ~ N(0,1)
    expected_kurt = p * (p + 2)
    std_kurt = math.sqrt(8.0 * p * (p + 2) / n)
    if std_kurt <= 0:
        kurt_p = 0.5
    else:
        kurt_z = (b2p - expected_kurt) / std_kurt
        kurt_p = 2.0 * (1.0 - _std_norm_cdf_approx(abs(kurt_z)))

    is_normal = (skew_p > 0.05) and (kurt_p > 0.05)

    return MultivariateNormalityResult(
        mardia_skewness=b1p,
        mardia_kurtosis=b2p,
        skewness_p_value=skew_p,
        kurtosis_p_value=kurt_p,
        is_multivariate_normal=is_normal,
        n_observations=n,
        n_assets=p,
    )
