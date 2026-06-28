"""P329: correlation network — returns-panel → correlation → distance → MST."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson, spearman, validate_series


@dataclass(frozen=True)
class CorrelationNetworkResult:
    mst_edges: list[tuple[str, str, float]]
    node_degrees: dict[str, int]
    average_distance: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mst_edges": [list(edge) for edge in self.mst_edges],
            "node_degrees": dict(self.node_degrees),
            "average_distance": self.average_distance,
        }


def correlation_network_report(
    returns_panel: dict[str, list[float]],
    *,
    method: str = "pearson",
) -> CorrelationNetworkResult:
    if method not in ("pearson", "spearman"):
        raise ValueError("method must be 'pearson' or 'spearman'")
    if not isinstance(returns_panel, dict):
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) < 2:
        raise ValueError("returns_panel must contain at least 2 assets")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")

    corr_fn = pearson if method == "pearson" else spearman
    assets = sorted(returns_panel.keys())
    series_map: dict[str, list[float]] = {}
    for asset in assets:
        series_map[asset] = validate_series(returns_panel[asset], name=f"returns_panel['{asset}']", min_len=2)

    # Check equal length
    lengths = {len(s) for s in series_map.values()}
    if len(lengths) != 1:
        raise ValueError("returns_panel series must have equal length")

    n = len(assets)

    # Build correlation matrix → distance matrix
    # distance = sqrt(2 * (1 - rho))
    dist: dict[frozenset[str], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = assets[i], assets[j]
            rho = corr_fn(series_map[a], series_map[b])
            # Clamp correlation to [-1, 1] for numerical safety
            rho = max(-1.0, min(1.0, rho))
            d = math.sqrt(max(0.0, 2.0 * (1.0 - rho)))
            dist[frozenset((a, b))] = d

    # Kruskal MST via union-find
    edges_sorted = sorted(dist.items(), key=lambda item: item[1])
    parent: dict[str, str] = {a: a for a in assets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> bool:
        rx, ry = find(x), find(y)
        if rx == ry:
            return False
        parent[rx] = ry
        return True

    mst: list[tuple[str, str, float]] = []
    for pair, d in edges_sorted:
        nodes = list(pair)
        if union(nodes[0], nodes[1]):
            mst.append((nodes[0], nodes[1], d))
        if len(mst) == n - 1:
            break

    # Node degrees
    degrees: dict[str, int] = {a: 0 for a in assets}
    for a, b, _ in mst:
        degrees[a] += 1
        degrees[b] += 1

    avg_dist = sum(d for _, _, d in mst) / len(mst) if mst else 0.0

    return CorrelationNetworkResult(
        mst_edges=mst,
        node_degrees=degrees,
        average_distance=avg_dist,
    )
