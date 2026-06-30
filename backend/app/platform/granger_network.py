"""P341: Granger causality network over a returns panel.

For each ordered pair (A, B) of assets, an F-test for Granger causality
(A → B at lag=max_lag) is performed. Results form a directed adjacency
matrix. PageRank (simplified power iteration) and strongly connected
components (Kosaraju) are computed over the significant-edge graph.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.platform.factor_utils import mean, validate_series


@dataclass(frozen=True)
class GrangerNetworkResult:
    adjacency_matrix: dict[str, dict[str, float]]
    significant_edges: list[dict[str, Any]]
    pagerank: dict[str, float]
    strongly_connected_components: list[list[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adjacency_matrix": {k: dict(v) for k, v in self.adjacency_matrix.items()},
            "significant_edges": list(self.significant_edges),
            "pagerank": dict(self.pagerank),
            "strongly_connected_components": [list(comp) for comp in self.strongly_connected_components],
        }


def _ols_solve(Y: list[float], X: list[list[float]]) -> list[float] | None:
    """Solve OLS: X is n_obs × n_features (already includes intercept column if needed).

    Returns coefficients (length n_features) or None on failure.
    """
    n_rows = len(Y)
    n_cols = len(X[0])

    # X^T X
    XtX = [[0.0] * n_cols for _ in range(n_cols)]
    for i in range(n_rows):
        for j in range(n_cols):
            for k in range(n_cols):
                XtX[j][k] += X[i][j] * X[i][k]

    # X^T Y
    XtY = [0.0] * n_cols
    for i in range(n_rows):
        for j in range(n_cols):
            XtY[j] += X[i][j] * Y[i]

    # Gaussian elimination with partial pivot
    aug = [XtX[i][:] + [XtY[i]] for i in range(n_cols)]
    for col in range(n_cols):
        max_row = col
        max_val = abs(aug[col][col])
        for row in range(col + 1, n_cols):
            if abs(aug[row][col]) > max_val:
                max_val = abs(aug[row][col])
                max_row = row
        if max_val < 1e-12:
            return None
        aug[col], aug[max_row] = aug[max_row], aug[col]

        pivot = aug[col][col]
        for j in range(col, n_cols + 1):
            aug[col][j] /= pivot

        for row in range(n_cols):
            if row != col:
                factor = aug[row][col]
                for j in range(col, n_cols + 1):
                    aug[row][j] -= factor * aug[col][j]

    return [aug[i][n_cols] for i in range(n_cols)]


def _ols_coefficients(y: list[float], x: list[list[float]]) -> list[float] | None:
    """Simple OLS: solve X^T X β = X^T y. Returns coefficients or None on failure.

    x is a list of columns (each x[j] is a list of values for feature j).
    Adds an intercept column automatically.
    """
    n = len(y)
    if n == 0:
        return None
    p = len(x)
    if p == 0:
        return None

    # Build X matrix (n × (p+1)) with intercept
    X: list[list[float]] = []
    for i in range(n):
        row = [1.0]
        for j in range(p):
            if i >= len(x[j]):
                return None
            row.append(x[j][i])
        X.append(row)

    return _ols_solve(y, X)


def _f_test_p_value(f_stat: float, df1: int, df2: int) -> float:
    """Approximate upper-tail p-value of the F-distribution.

    Returns P(F > f_stat) via the Wilson-Hilferty normal approximation; small
    values indicate Granger-causality significance.
    """
    # Wilson-Hilferty approximation for chi-square → normal
    # F(d1,d2) → approximation
    if f_stat <= 0:
        return 0.0

    # Use the relationship: if F ~ F(d1,d2), then x = (d1*F)/(d2 + d1*F) ~ Beta(d1/2, d2/2)
    # We'll use a simple normal approximation for large df
    x = (df1 * f_stat) / (df2 + df1 * f_stat)
    # Normal approximation via Cornish-Fisher
    # For simplicity, use the fact that (F^(1/3)*(1-2/(9*d2)) - (1-2/(9*d1))) / sqrt(2/(9*d1) + 2/(9*d2)*F^(2/3)) ~ N(0,1)
    if df1 <= 0 or df2 <= 0:
        return 0.0

    f_cuberoot = f_stat ** (1.0 / 3.0)
    numerator = f_cuberoot * (1.0 - 2.0 / (9.0 * df2)) - (1.0 - 2.0 / (9.0 * df1))
    denominator = math.sqrt(2.0 / (9.0 * df1) + 2.0 / (9.0 * df2) * f_stat ** (2.0 / 3.0))
    if denominator == 0:
        return 0.0
    z = numerator / denominator

    # Normal CDF approximation (Abramowitz & Stegun 26.2.17)
    def norm_cdf(z_val: float) -> float:
        if z_val < -8.0:
            return 0.0
        if z_val > 8.0:
            return 1.0
        t = 1.0 / (1.0 + 0.2316419 * abs(z_val))
        d = 0.3989423 * math.exp(-z_val * z_val / 2.0)
        poly = t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
        prob = 1.0 - d * poly
        return 1.0 - prob if z_val < 0 else prob

    return 1.0 - norm_cdf(z)  # p-value


def _granger_f_test(x: list[float], y: list[float], max_lag: int) -> float:
    """F-statistic for Granger causality: does x Granger-cause y at lag=max_lag?

    Unrestricted: y_t = α + Σ_{i=1}^{max_lag} β_i * y_{t-i} + Σ_{i=1}^{max_lag} γ_i * x_{t-i} + ε_t
    Restricted:   y_t = α + Σ_{i=1}^{max_lag} β_i * y_{t-i} + ε_t
    """
    n = len(y)
    if n <= 2 * max_lag + 1:
        return 0.0

    # Build lagged matrices
    # Start from index max_lag to n-1
    t_start = max_lag
    n_obs = n - t_start
    if n_obs <= max_lag + 1:
        return 0.0

    # Unrestricted: regressors = [1, y_{t-1}, ..., y_{t-max_lag}, x_{t-1}, ..., x_{t-max_lag}]
    Y_vals: list[float] = []
    X_unrestricted: list[list[float]] = []
    for t in range(t_start, n):
        Y_vals.append(y[t])
        row = [1.0]
        for lag in range(1, max_lag + 1):
            row.append(y[t - lag])
        for lag in range(1, max_lag + 1):
            row.append(x[t - lag])
        X_unrestricted.append(row)

    # Restricted: regressors = [1, y_{t-1}, ..., y_{t-max_lag}]
    X_restricted: list[list[float]] = []
    for t in range(t_start, n):
        row = [1.0]
        for lag in range(1, max_lag + 1):
            row.append(y[t - lag])
        X_restricted.append(row)

    # OLS for unrestricted
    n_rows = len(Y_vals)
    p_unrestricted = len(X_unrestricted[0])  # 1 + max_lag + max_lag
    p_restricted = len(X_restricted[0])  # 1 + max_lag

    # Compute RSS for unrestricted via OLS (X already in row format with intercept)
    beta_unrestricted = _ols_solve(Y_vals, X_unrestricted)
    if beta_unrestricted is None:
        return 0.0
    rss_unrestricted = 0.0
    for i in range(n_rows):
        fitted = 0.0
        for j in range(p_unrestricted):
            fitted += beta_unrestricted[j] * X_unrestricted[i][j]
        rss_unrestricted += (Y_vals[i] - fitted) ** 2

    beta_restricted = _ols_solve(Y_vals, X_restricted)
    if beta_restricted is None:
        return 0.0
    rss_restricted = 0.0
    for i in range(n_rows):
        fitted = 0.0
        for j in range(p_restricted):
            fitted += beta_restricted[j] * X_restricted[i][j]
        rss_restricted += (Y_vals[i] - fitted) ** 2

    if rss_unrestricted < 1e-15:
        return 0.0

    num_restrictions = p_unrestricted - p_restricted  # max_lag
    df_denom = n_rows - p_unrestricted

    if df_denom <= 0:
        return 0.0

    f_stat = ((rss_restricted - rss_unrestricted) / num_restrictions) / (rss_unrestricted / df_denom)
    return max(0.0, f_stat)


def _pagerank(adj: dict[str, dict[str, float]], damping: float = 0.85, iterations: int = 50) -> dict[str, float]:
    """Simple PageRank via power iteration."""
    nodes = sorted(adj.keys())
    if not nodes:
        return {}
    n = len(nodes)
    pr = {node: 1.0 / n for node in nodes}

    # Build outgoing link sums
    out_sum: dict[str, float] = {}
    for a in nodes:
        out_sum[a] = sum(adj[a].get(b, 0.0) for b in nodes)
    for a in nodes:
        if out_sum[a] < 1e-12:
            out_sum[a] = 0.0  # dangling node

    for _ in range(iterations):
        new_pr: dict[str, float] = {}
        for a in nodes:
            rank = (1.0 - damping) / n
            for b in nodes:
                if out_sum[b] > 1e-12 and adj[b].get(a, 0.0) > 0:
                    rank += damping * pr[b] * adj[b][a] / out_sum[b]
                elif out_sum[b] < 1e-12:
                    rank += damping * pr[b] / n
            new_pr[a] = rank
        pr = new_pr

    return pr


def _kosaraju_scc(adj: dict[str, dict[str, float]]) -> list[list[str]]:
    """Find strongly connected components via Kosaraju's algorithm on non-zero edges."""
    nodes = sorted(adj.keys())
    if not nodes:
        return []

    # Build edge lists
    forward: dict[str, list[str]] = {a: [] for a in nodes}
    reverse: dict[str, list[str]] = {a: [] for a in nodes}
    for a in nodes:
        for b in nodes:
            if adj[a].get(b, 0.0) > 0:
                forward[a].append(b)
                reverse[b].append(a)

    visited: set[str] = set()
    order: list[str] = []

    def dfs1(v: str) -> None:
        visited.add(v)
        for w in forward.get(v, []):
            if w not in visited:
                dfs1(w)
        order.append(v)

    for v in nodes:
        if v not in visited:
            dfs1(v)

    visited.clear()
    components: list[list[str]] = []

    def dfs2(v: str, comp: list[str]) -> None:
        visited.add(v)
        comp.append(v)
        for w in reverse.get(v, []):
            if w not in visited:
                dfs2(w, comp)

    for v in reversed(order):
        if v not in visited:
            comp: list[str] = []
            dfs2(v, comp)
            components.append(sorted(comp))

    return components


def granger_network_report(
    returns_panel: dict[str, list[float]],
    *,
    max_lag: int = 3,
    significance: float = 0.1,
) -> GrangerNetworkResult:
    """Build a Granger causality network from a returns panel.

    For every ordered pair (A→B), computes the F-statistic for Granger causality.
    """
    if not isinstance(returns_panel, dict):
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) < 2:
        raise ValueError("returns_panel must contain at least 2 assets")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")

    assets = sorted(returns_panel.keys())
    series_map: dict[str, list[float]] = {}
    for asset in assets:
        series_map[asset] = validate_series(
            returns_panel[asset], name=f"returns_panel['{asset}']", min_len=2 * max_lag + 2
        )

    # Check equal length
    lengths = {len(s) for s in series_map.values()}
    if len(lengths) != 1:
        raise ValueError("returns_panel series must have equal length")

    # Build adjacency matrix (F-statistics)
    adj: dict[str, dict[str, float]] = {}
    for a in assets:
        adj[a] = {}
        for b in assets:
            if a == b:
                adj[a][b] = 0.0
            else:
                f_stat = _granger_f_test(series_map[a], series_map[b], max_lag)
                adj[a][b] = f_stat

    # Significant edges
    significant_edges: list[dict[str, Any]] = []
    for a in assets:
        for b in assets:
            f_val = adj[a][b]
            p_value = _f_test_p_value(f_val, max_lag, len(series_map[a]) - 2 * max_lag - 1)
            if f_val > 0 and p_value < significance:
                significant_edges.append({"from": a, "to": b, "f_stat": f_val})

    # PageRank on significant edges graph
    pr = _pagerank(adj)

    # Strongly connected components
    scc = _kosaraju_scc(adj)

    return GrangerNetworkResult(
        adjacency_matrix=adj,
        significant_edges=significant_edges,
        pagerank=pr,
        strongly_connected_components=scc,
    )
