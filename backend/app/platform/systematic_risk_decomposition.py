"""P361: systematic risk decomposition.

Decompose the total risk of a multi-asset return panel into systematic and
idiosyncratic components via eigenvalue analysis of the covariance matrix.
Uses the cyclic Jacobi rotation method for eigen-decomposition (pure Python,
self-contained, no external dependencies).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["SystematicRiskDecompositionResult", "systematic_risk_decomposition_report"]

_MAX_ASSETS = 50


@dataclass(frozen=True)
class SystematicRiskDecompositionResult:
    systematic_ratio: float
    eigenvalue_spectrum: list[float]
    concentration_hhi: float
    explained_variance_ratio: list[float]
    suggested_k: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "systematic_ratio": self.systematic_ratio,
            "eigenvalue_spectrum": self.eigenvalue_spectrum,
            "concentration_hhi": self.concentration_hhi,
            "explained_variance_ratio": self.explained_variance_ratio,
            "suggested_k": self.suggested_k,
        }


def systematic_risk_decomposition_report(
    returns_panel: dict[str, list[float]],
    *,
    n_components: int | None = None,
) -> SystematicRiskDecompositionResult:
    """Decompose systematic vs idiosyncratic risk from a returns panel.

    Args:
        returns_panel: Dict mapping asset names to return series (all equal length,
            2 ≤ n_assets ≤ 50, each series ≥ 2 observations).
        n_components: Optional number of principal components to retain. Clamped
            to the number of assets.

    Returns:
        SystematicRiskDecompositionResult with systematic_ratio (fraction of
        total variance explained by top-k eigenvalues), eigenvalue_spectrum
        (descending), concentration_hhi (Herfindahl of eigenvalue shares),
        explained_variance_ratio (cumulative), and suggested_k (knee point).
    """
    # Validate panel.
    panel = _validate_panel(returns_panel)
    names = list(panel)
    p = len(names)
    n_obs = len(panel[names[0]])

    if n_components is not None:
        n_components = max(1, min(n_components, p))
    else:
        n_components = p

    # Build covariance matrix (sample covariance, ddof=1).
    cov = _build_covariance(panel, names, n_obs)

    # Jacobi eigen-decomposition.
    eigvals, _ = _jacobi_eigen(cov)

    total_var = sum(eigvals)
    if total_var <= 0.0:
        # Degenerate: all variance zero.
        evr_cum = [0.0] * p
        return SystematicRiskDecompositionResult(
            systematic_ratio=0.0,
            eigenvalue_spectrum=eigvals,
            concentration_hhi=0.0,
            explained_variance_ratio=evr_cum,
            suggested_k=1,
        )

    # Systematic ratio: top-k eigenvalue sum / total sum.
    k = n_components
    systematic = sum(eigvals[:k]) / total_var

    # HHI: Σ(λ_i/Σλ)².
    shares = [e / total_var for e in eigvals]
    hhi = sum(s * s for s in shares)

    # Cumulative explained variance ratio (all components).
    evr_cum: list[float] = []
    running = 0.0
    for e in eigvals:
        running += e / total_var
        evr_cum.append(running)

    # Suggested k: knee point — first component where individual explained
    # variance ratio < 10%, then k = that index + 1.
    individual_shares = [e / total_var for e in eigvals]
    suggested_k = p
    for i, share in enumerate(individual_shares):
        if share < 0.10:
            suggested_k = i
            break
    # suggested_k is the number of components before the first < 10% one.
    # At minimum 1.
    suggested_k = max(1, suggested_k)

    return SystematicRiskDecompositionResult(
        systematic_ratio=systematic,
        eigenvalue_spectrum=eigvals,
        concentration_hhi=hhi,
        explained_variance_ratio=evr_cum,
        suggested_k=suggested_k,
    )


def _validate_panel(panel: dict[str, list[float]]) -> dict[str, list[float]]:
    if not isinstance(panel, dict) or len(panel) < 2:
        raise ValueError("returns_panel must contain at least two assets")
    if len(panel) > _MAX_ASSETS:
        raise ValueError(f"returns_panel must contain at most {_MAX_ASSETS} assets")
    out = {
        str(name): validate_series(series, name=str(name), min_len=2)
        for name, series in panel.items()
    }
    lengths = {len(series) for series in out.values()}
    if len(lengths) != 1:
        raise ValueError("return series must have equal length")
    return out


def _build_covariance(
    panel: dict[str, list[float]],
    names: list[str],
    n_obs: int,
) -> list[list[float]]:
    """Build sample covariance matrix (ddof=1)."""
    p = len(names)
    # Compute means.
    means = [sum(panel[name]) / n_obs for name in names]

    cov: list[list[float]] = [[0.0] * p for _ in range(p)]
    ddof = n_obs - 1 if n_obs > 1 else 1
    for i in range(p):
        for j in range(i, p):
            s = 0.0
            for t in range(n_obs):
                s += (panel[names[i]][t] - means[i]) * (panel[names[j]][t] - means[j])
            s /= ddof
            cov[i][j] = s
            cov[j][i] = s
    return cov


def _jacobi_eigen(
    a: list[list[float]], *, max_sweeps: int = 100, tol: float = 1e-12
) -> tuple[list[float], list[list[float]]]:
    """Eigen-decomposition of a symmetric matrix via cyclic Jacobi rotations.

    Returns (eigenvalues descending, eigenvectors as columns).
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
                if abs(app - aqq) < 1e-300:
                    theta = math.pi / 4.0
                else:
                    theta = 0.5 * math.atan2(2.0 * apq, app - aqq)
                c = math.cos(theta)
                s = math.sin(theta)

                # Apply rotation to A.
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

                # Update eigenvectors.
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
