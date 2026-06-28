"""P347: Network centrality measures.

Compute degree, betweenness, eigenvector centrality and PageRank for a
directed weighted graph represented as an adjacency matrix (dict of dict).

Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["NetworkCentralityResult", "network_centrality_report"]


@dataclass(frozen=True)
class NetworkCentralityResult:
    degree_centrality: dict[str, float] = field(default_factory=dict)
    betweenness_centrality: dict[str, float] = field(default_factory=dict)
    eigenvector_centrality: dict[str, float] = field(default_factory=dict)
    pagerank: dict[str, float] = field(default_factory=dict)
    most_central_node: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "degree_centrality": self.degree_centrality,
            "betweenness_centrality": self.betweenness_centrality,
            "eigenvector_centrality": self.eigenvector_centrality,
            "pagerank": self.pagerank,
            "most_central_node": self.most_central_node,
        }


def _validate_adjacency(adj: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, float]]]:
    """Validate adjacency matrix and return (nodes, validated_adj)."""
    if not adj or not isinstance(adj, dict):
        raise ValueError("adjacency_matrix must be a non-empty dict")
    nodes = sorted(adj.keys())
    validated: dict[str, dict[str, float]] = {}
    for src, targets in adj.items():
        src_s = str(src)
        if not isinstance(targets, dict):
            raise ValueError(f"adjacency_matrix['{src}'] must be a dict")
        validated[src_s] = {}
        for tgt, weight in targets.items():
            tgt_s = str(tgt)
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise ValueError(f"edge weight {src}->{tgt} must be a finite number")
            w = float(weight)
            if not math.isfinite(w):
                raise ValueError(f"edge weight {src}->{tgt} must be finite")
            validated[src_s][tgt_s] = w
            # ensure target node is in nodes list
            if tgt_s not in nodes:
                nodes.append(tgt_s)
    # Re-sort after potentially adding new nodes
    nodes = sorted(set(nodes))
    # Ensure all nodes have an entry (even if no outgoing edges)
    for n in nodes:
        if n not in validated:
            validated[n] = {}
    return nodes, validated


def _degree_centrality(nodes: list[str], adj: dict[str, dict[str, float]]) -> dict[str, float]:
    """Compute normalized degree centrality (in-degree + out-degree) / (2*(n-1))."""
    n = len(nodes)
    if n <= 1:
        return {nodes[0]: 1.0} if nodes else {}
    in_deg: dict[str, float] = {node: 0.0 for node in nodes}
    out_deg: dict[str, float] = {node: 0.0 for node in nodes}
    for src, targets in adj.items():
        for tgt, w in targets.items():
            out_deg[src] += w
            in_deg[tgt] += w
    norm = 2.0 * (n - 1)
    return {node: (in_deg[node] + out_deg[node]) / norm for node in nodes}


def _betweenness_centrality(nodes: list[str], adj: dict[str, dict[str, float]]) -> dict[str, float]:
    """Compute betweenness centrality using all-pairs shortest paths.

    For each pair (s, t) where s != t, count how many shortest paths pass
    through each node v (v != s, v != t). Betweenness is the fraction of
    shortest s→t paths that pass through v, summed over all (s, t) pairs.
    Normalized by (n-1)*(n-2) for directed graphs.
    """
    n = len(nodes)
    bc: dict[str, float] = {node: 0.0 for node in nodes}
    if n <= 2:
        return bc

    # Build index map
    idx = {node: i for i, node in enumerate(nodes)}

    for s in nodes:
        # Dijkstra from s
        dist: dict[str, float] = {node: float("inf") for node in nodes}
        sigma: dict[str, float] = {node: 0.0 for node in nodes}
        pred: dict[str, list[str]] = {node: [] for node in nodes}
        dist[s] = 0.0
        sigma[s] = 1.0

        # Priority queue via list (small graphs, pure Python)
        visited: set[str] = set()
        queue = [(0.0, s)]

        while queue:
            # Find min dist node
            min_idx = 0
            for i in range(1, len(queue)):
                if queue[i][0] < queue[min_idx][0]:
                    min_idx = i
            d, v = queue.pop(min_idx)
            if v in visited:
                continue
            visited.add(v)

            for w, weight in adj.get(v, {}).items():
                new_d = d + weight
                if new_d < dist[w]:
                    dist[w] = new_d
                    sigma[w] = sigma[v]
                    pred[w] = [v]
                    queue.append((new_d, w))
                elif abs(new_d - dist[w]) < 1e-12:
                    sigma[w] += sigma[v]
                    pred[w].append(v)

        # Accumulate dependencies
        delta: dict[str, float] = {node: 0.0 for node in nodes}
        # Process nodes in reverse order of distance
        sorted_nodes = sorted(visited, key=lambda x: dist[x], reverse=True)
        for w in sorted_nodes:
            for v in pred[w]:
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                bc[w] += delta[w]

    # Normalize for directed graph: (n-1)*(n-2)
    norm = (n - 1) * (n - 2) if n > 2 else 1.0
    if norm > 0:
        for node in nodes:
            bc[node] /= norm

    return bc


def _eigenvector_centrality(
    nodes: list[str], adj: dict[str, dict[str, float]], max_iter: int = 100, tol: float = 1e-9
) -> dict[str, float]:
    """Compute eigenvector centrality via power iteration."""
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]: 1.0}

    # Build adjacency matrix as nested list (only out-edges)
    # Eigenvector centrality uses the adjacency matrix A: v = λ A^T v
    # Power iteration: v_{k+1} = A^T v_k / ||A^T v_k||
    vec = {node: 1.0 / math.sqrt(n) for node in nodes}

    for _ in range(max_iter):
        new_vec: dict[str, float] = {node: 0.0 for node in nodes}
        # A^T * vec: for each node i, sum over j of A[j][i] * vec[j]
        for src, targets in adj.items():
            v_src = vec.get(src, 0.0)
            for tgt, w in targets.items():
                new_vec[tgt] = new_vec.get(tgt, 0.0) + w * v_src
        # Normalize
        norm_val = math.sqrt(sum(v * v for v in new_vec.values()))
        if norm_val < 1e-15:
            break
        for node in new_vec:
            new_vec[node] /= norm_val
        # Check convergence
        diff = max(abs(new_vec[node] - vec[node]) for node in nodes)
        vec = new_vec
        if diff < tol:
            break

    return vec


def _pagerank(
    nodes: list[str],
    adj: dict[str, dict[str, float]],
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> dict[str, float]:
    """Compute PageRank via power iteration."""
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]: 1.0}

    # Build out-degree (sum of outgoing edge weights)
    out_sum: dict[str, float] = {}
    for src, targets in adj.items():
        total = sum(targets.values())
        out_sum[src] = total if total > 0 else 0.0

    # Initialize uniform
    pr = {node: 1.0 / n for node in nodes}
    uniform = 1.0 / n

    for _ in range(max_iter):
        new_pr: dict[str, float] = {}
        for node in nodes:
            # Sum of (PR[p] * w_p→node / out_sum[p]) for predecessors p
            rank_sum = 0.0
            for p in nodes:
                if p in adj and node in adj[p]:
                    w = adj[p][node]
                    total_out = out_sum.get(p, 0.0)
                    if total_out > 0:
                        rank_sum += pr[p] * w / total_out
                    else:
                        # Dangling node: distribute uniformly
                        rank_sum += pr[p] * uniform
            # Handle dangling nodes (those with no outgoing edges)
            # already handled above
            new_pr[node] = (1.0 - damping) * uniform + damping * rank_sum

        # Check convergence
        diff = max(abs(new_pr[node] - pr[node]) for node in nodes)
        pr = new_pr
        if diff < tol:
            break

    # Normalize to sum to 1
    total = sum(pr.values())
    if total > 0:
        pr = {k: v / total for k, v in pr.items()}

    return pr


def network_centrality_report(
    adjacency_matrix: dict[str, dict[str, float]],
) -> NetworkCentralityResult:
    """Compute all four centrality measures for a directed weighted graph.

    Args:
        adjacency_matrix: Dict of dict mapping source node to target node
            with float edge weights.

    Returns:
        NetworkCentralityResult with degree, betweenness, eigenvector,
        pagerank, and most_central_node.

    Raises:
        ValueError: On invalid adjacency matrix, non-finite weights,
            or empty graph.
    """
    nodes, adj = _validate_adjacency(adjacency_matrix)

    degree = _degree_centrality(nodes, adj)
    betweenness = _betweenness_centrality(nodes, adj)
    eigenvector = _eigenvector_centrality(nodes, adj)
    pagerank = _pagerank(nodes, adj)

    # most_central_node: highest composite score (degree + pagerank)
    all_nodes = set(degree) | set(pagerank)
    composite = {n: degree.get(n, 0.0) + pagerank.get(n, 0.0) for n in all_nodes}
    most_central = max(composite, key=lambda k: composite[k]) if composite else ""

    return NetworkCentralityResult(
        degree_centrality=degree,
        betweenness_centrality=betweenness,
        eigenvector_centrality=eigenvector,
        pagerank=pagerank,
        most_central_node=most_central,
    )
