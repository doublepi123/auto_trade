"""P206: Hierarchical Risk Parity (HRP).

López de Prado's (2016) "Building Diversified Portfolios that Outperform
Out-of-Sample" allocation. HRP sidesteps the instability of mean-variance
inversion by:

1. building a correlation-distance matrix,
2. clustering assets hierarchally (single-linkage, quasi-diagonalization), and
3. splitting capital top-down via recursive bisection on the inverse-variance
   allocations within each cluster.

Reference: López de Prado HRP; PyHRP / scikit-portfolio implementations. Pure
Python (no scipy/numpy) — the linkage step is a deterministic recursive
quasi-diagonalization mirroring the paper's algorithm.

A :class:`HRPModel` implements the platform's
:class:`~app.platform.construction.PortfolioConstructionModel` Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.platform.construction import PortfolioConstructionModel
from app.platform.covariance import covariance_to_correlation, ledoit_wolf_shrinkage, sample_covariance

__all__ = ["hrp_weights", "HRPModel", "correlation_distance", "recursive_bisection"]


def correlation_distance(corr: dict[tuple[str, str], float], symbols: list[str]) -> dict[tuple[str, str], float]:
    """Distance d_ij = sqrt(0.5 · (1 − ρ_ij)) (López de Prado's metric)."""
    dist: dict[tuple[str, str], float] = {}
    for i in symbols:
        for j in symbols:
            d = (0.5 * (1.0 - corr[(i, j)])) ** 0.5
            dist[(i, j)] = d
    return dist


def _quasi_diagonalize(
    dist: dict[tuple[str, str], float], symbols: list[str]
) -> list[str]:
    """Single-linkage hierarchical ordering of symbols via greedy agglomeration.

    Returns ``symbols`` re-ordered so that the most-correlated assets are
    adjacent — the quasi-diagonalized order HRP allocates over.
    """
    if len(symbols) <= 1:
        return list(symbols)
    # Each cluster starts as a single-element list; merge closest clusters.
    clusters: list[list[str]] = [[s] for s in symbols]
    while len(clusters) > 1:
        # find the closest pair of clusters (min single-linkage distance)
        best = None
        best_pair = (0, 1)
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = _cluster_distance(clusters[a], clusters[b], dist)
                if best is None or d < best:
                    best = d
                    best_pair = (a, b)
        a, b = best_pair
        merged = clusters[a] + clusters[b]
        clusters = [c for i, c in enumerate(clusters) if i not in (a, b)]
        clusters.append(merged)
    return clusters[0] if clusters else []


def _cluster_distance(
    ca: list[str], cb: list[str], dist: dict[tuple[str, str], float]
) -> float:
    """Single linkage: the minimum distance between any member of ca and cb."""
    return min(dist[(a, b)] for a in ca for b in cb)


def _inverse_variance_weights(
    subset: list[str], cov: dict[tuple[str, str], float]
) -> dict[str, float]:
    ivp = {s: 1.0 / cov[(s, s)] if cov[(s, s)] > 0 else 0.0 for s in subset}
    total = sum(ivp.values())
    if total <= 0:
        return {s: 1.0 / len(subset) for s in subset}
    return {s: ivp[s] / total for s in subset}


def _cluster_variance(
    subset: list[str],
    cov: dict[tuple[str, str], float],
    weights: dict[str, float],
) -> float:
    total = 0.0
    for i in subset:
        for j in subset:
            total += weights[i] * weights[j] * cov[(i, j)]
    return total


def recursive_bisection(
    order: list[str], cov: dict[tuple[str, str], float]
) -> dict[str, float]:
    """Top-down recursive bisection allocation over a quasi-diagonalized order."""
    weights = {s: 1.0 for s in order}
    clusters: list[list[str]] = [list(order)]
    while clusters:
        # pop a cluster of size > 1 and split in half
        cluster = clusters.pop(0)
        if len(cluster) <= 1:
            continue
        mid = len(cluster) // 2
        left, right = cluster[:mid], cluster[mid:]
        w_left = _inverse_variance_weights(left, cov)
        w_right = _inverse_variance_weights(right, cov)
        var_left = _cluster_variance(left, cov, w_left)
        var_right = _cluster_variance(right, cov, w_right)
        denom = var_left + var_right
        if denom <= 0:
            alpha = 0.5
        else:
            alpha = 1.0 - var_left / denom  # weight going to left cluster
        for s in left:
            weights[s] *= alpha
        for s in right:
            weights[s] *= (1.0 - alpha)
        clusters.append(left)
        clusters.append(right)
    return weights


def hrp_weights(
    returns: dict[str, list[float]] | None = None,
    cov: dict[tuple[str, str], float] | None = None,
) -> dict[str, float]:
    """Full HRP allocation pipeline → {symbol: weight}, summing to 1.0.

    Supply either a return panel (covariance estimated via Ledoit-Wolf shrinkage)
    or a precomputed covariance matrix.
    """
    if cov is None:
        if returns is None:
            return {}
        symbols = list(returns.keys())
        cov, _ = ledoit_wolf_shrinkage(returns)
    else:
        symbols = sorted({s for pair in cov for s in pair})
    if len(symbols) <= 1:
        return {s: 1.0 for s in symbols}
    corr = covariance_to_correlation(cov, symbols)
    dist = correlation_distance(corr, symbols)
    order = _quasi_diagonalize(dist, symbols)
    weights = recursive_bisection(order, cov)
    total = sum(weights.values())
    if total > 0:
        weights = {s: w / total for s, w in weights.items()}
    return weights


@dataclass(frozen=True)
class HRPModel:
    """PortfolioConstructionModel that allocates via Hierarchical Risk Parity.

    The model holds an optional return panel; when provided, HRP runs over the
    Ledoit-Wolf shrinkage covariance. Without one, it falls back to equal weight
    (HRP needs a covariance matrix, which the Protocol's volatilities alone can't
    fully specify)."""

    returns_panel: dict[str, list[float]] | None = None
    cov: dict[tuple[str, str], float] | None = None
    name: str = "hrp"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        if self.returns_panel is not None:
            panel = {s: self.returns_panel.get(s, []) for s in active}
            panel = {s: v for s, v in panel.items() if len(v) >= 2}
            if len(panel) >= 2:
                w = hrp_weights(returns=panel)
                return {s: Decimal(str(w.get(s, 0.0))) for s in active}
        if self.cov is not None:
            cov_active = {(a, b): self.cov.get((a, b), 0.0) for a in active for b in active}
            w = hrp_weights(cov=cov_active)
            return {s: Decimal(str(w.get(s, 0.0))) for s in active}
        # Degenerate fallback: equal weight among active.
        ew = Decimal("1") / Decimal(len(active))
        return {s: ew for s in active}
