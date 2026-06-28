"""P357: Pair Screening — pairwise dependence metrics over a returns panel.

Given a panel {asset: [returns]}, compute dependence scores for all asset pairs
using either mutual information (binned) or distance correlation, and return
the top-N most dependent pairs.

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "PairScreeningResult",
    "pair_screening_report",
]

_MAX_ASSETS = 50
"""Maximum number of assets allowed in the panel."""


def _validate_panel(
    panel: dict[str, list[float]],
) -> tuple[list[str], list[list[float]], int]:
    """Validate and coerce a returns panel.

    Returns (asset_names, validated_series, n_observations).
    Raises ValueError/TypeError on invalid input.
    """
    if not isinstance(panel, dict) or not panel:
        raise ValueError("panel must be a non-empty dict")
    if len(panel) < 2:
        raise ValueError("panel must contain at least 2 assets")
    if len(panel) > _MAX_ASSETS:
        raise ValueError(f"panel must contain at most {_MAX_ASSETS} assets")

    names: list[str] = []
    series_list: list[list[float]] = []
    n_obs: int | None = None

    for name, values in panel.items():
        name_str = str(name)
        if not isinstance(values, list):
            raise ValueError(f"panel['{name_str}'] must be a list")
        if not values:
            raise ValueError(f"panel['{name_str}'] must be non-empty")
        validated: list[float] = []
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise TypeError(f"panel['{name_str}'] entries must be finite numbers")
            number = float(v)
            if not math.isfinite(number):
                raise ValueError(f"panel['{name_str}'] entries must be finite numbers")
            validated.append(number)
        if n_obs is None:
            n_obs = len(validated)
        elif len(validated) != n_obs:
            raise ValueError(
                f"panel['{name_str}'] has {len(validated)} observations, "
                f"expected {n_obs}"
            )
        names.append(name_str)
        series_list.append(validated)

    return names, series_list, n_obs  # type: ignore[return-value]


def _mutual_information(x: list[float], y: list[float], bins: int = 10) -> float:
    """Estimate mutual information via equal-width binning.

    I(X; Y) = Σ_{x,y} p(x,y) · log(p(x,y) / (p(x) · p(y))).
    """
    n = len(x)
    if n < 2:
        return 0.0

    # Determine bin edges.
    x_min, x_max = min(x), max(x)
    y_min, y_max = min(y), max(y)
    x_range = x_max - x_min
    y_range = y_max - y_min

    if x_range == 0 and y_range == 0:
        return 0.0

    x_bin_width = x_range / bins if x_range > 0 else 1.0
    y_bin_width = y_range / bins if y_range > 0 else 1.0

    # 2D histogram and marginal histograms.
    joint = [[0.0] * bins for _ in range(bins)]
    x_marginal = [0.0] * bins
    y_marginal = [0.0] * bins

    for xi, yi in zip(x, y):
        bx = int((xi - x_min) / x_bin_width) if x_range > 0 else 0
        by = int((yi - y_min) / y_bin_width) if y_range > 0 else 0
        if bx >= bins:
            bx = bins - 1
        if bx < 0:
            bx = 0
        if by >= bins:
            by = bins - 1
        if by < 0:
            by = 0
        joint[bx][by] += 1.0
        x_marginal[bx] += 1.0
        y_marginal[by] += 1.0

    # Normalise to probabilities.
    for bx in range(bins):
        for by in range(bins):
            joint[bx][by] /= n
    for bx in range(bins):
        x_marginal[bx] /= n
    for by in range(bins):
        y_marginal[by] /= n

    # Compute MI.
    mi = 0.0
    for bx in range(bins):
        for by in range(bins):
            p_joint = joint[bx][by]
            if p_joint <= 0:
                continue
            p_x = x_marginal[bx]
            p_y = y_marginal[by]
            if p_x <= 0 or p_y <= 0:
                continue
            mi += p_joint * math.log(p_joint / (p_x * p_y))

    return mi


def _distance_matrix(series: list[float]) -> list[list[float]]:
    """Compute pairwise absolute distance matrix for a 1D series."""
    n = len(series)
    d = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            d[i][j] = abs(series[i] - series[j])
    return d


def _double_center(matrix: list[list[float]]) -> list[list[float]]:
    """Double-center a distance matrix: A_ij = d_ij - mean_row_i - mean_col_j + grand_mean."""
    n = len(matrix)
    row_means = [sum(row) / n for row in matrix]
    col_means = [sum(matrix[i][j] for i in range(n)) / n for j in range(n)]
    grand_mean = sum(row_means) / n
    centered = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            centered[i][j] = matrix[i][j] - row_means[i] - col_means[j] + grand_mean
    return centered


def _distance_correlation(x: list[float], y: list[float]) -> float:
    """Estimate distance correlation (dCor) between two 1D series.

    Based on Székely-Rizzo (2007). Pure Python approximation.
    """
    n = len(x)
    if n < 2:
        return 0.0

    dx = _distance_matrix(x)
    dy = _distance_matrix(y)
    ax = _double_center(dx)
    ay = _double_center(dy)

    # dCov² = (1/n²) Σ_{i,j} A_ij · B_ij.
    dcov2 = sum(ax[i][j] * ay[i][j] for i in range(n) for j in range(n)) / (n * n)
    if dcov2 <= 0:
        return 0.0

    # dVar²(X) = (1/n²) Σ_{i,j} A_ij².
    dvar_x = sum(ax[i][j] * ax[i][j] for i in range(n) for j in range(n)) / (n * n)
    dvar_y = sum(ay[i][j] * ay[i][j] for i in range(n) for j in range(n)) / (n * n)

    if dvar_x <= 0 or dvar_y <= 0:
        return 0.0

    dcor = math.sqrt(dcov2 / math.sqrt(dvar_x * dvar_y))
    return dcor


@dataclass(frozen=True)
class PairScreeningResult:
    """Frozen carrier for pair screening results.

    Attributes
    ----------
    pairs: List of {asset_a, asset_b, score}, sorted by score descending.
    method: "mutual_info" or "distance_corr".
    total_pairs_screened: Total number of pairs evaluated.
    """

    pairs: list[dict[str, Any]]
    method: str
    total_pairs_screened: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "method": self.method,
            "total_pairs_screened": self.total_pairs_screened,
        }


def pair_screening_report(
    panel: dict[str, list[float]],
    *,
    top_n: int = 10,
    method: str = "mutual_info",
) -> PairScreeningResult:
    """Screen all asset pairs for dependence and return the top-N.

    Parameters
    ----------
    panel: {asset: [returns]} dict.
    top_n: Number of top pairs to return.
    method: "mutual_info" or "distance_corr".

    Returns a frozen result with ranked pairs.

    Raises ValueError/TypeError on invalid input.
    """
    if method not in ("mutual_info", "distance_corr"):
        raise ValueError("method must be 'mutual_info' or 'distance_corr'")
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        raise ValueError("top_n must be an int")
    if top_n < 1:
        raise ValueError("top_n must be >= 1")

    names, series_list, n_obs = _validate_panel(panel)
    num_assets = len(names)

    # Compute all pairwise scores.
    pair_scores: list[dict[str, Any]] = []
    total_pairs = 0
    for i in range(num_assets):
        for j in range(i + 1, num_assets):
            total_pairs += 1
            if method == "mutual_info":
                score = _mutual_information(series_list[i], series_list[j])
            else:
                score = _distance_correlation(series_list[i], series_list[j])
            pair_scores.append({
                "asset_a": names[i],
                "asset_b": names[j],
                "score": score,
            })

    # Sort descending by score.
    pair_scores.sort(key=lambda p: p["score"], reverse=True)

    return PairScreeningResult(
        pairs=pair_scores[:top_n],
        method=method,
        total_pairs_screened=total_pairs,
    )
