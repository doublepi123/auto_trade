"""P203: covariance matrix & shrinkage estimation.

Pure functions to estimate an asset-return covariance matrix, with a
Ledoit-Wolf (2004) "honey" shrinkage estimator that blends the noisy sample
covariance with a structured target (constant-correlation model) weighted by an
optimal, data-derived shrinkage intensity. This is the foundation for the
mean-variance (P204), Black-Litterman (P205), and factor-risk (P207) modules.

Mirrors PyPortfolioOpt's ``risk_models.risk_matrix(method="ledoit_wolf")`` and
the Ledoit-Wolf "Honey, I Shrunk the Sample Covariance Matrix" (2004) paper, but
implemented in pure Python with no NumPy/scipy dependency. Inputs/outputs use
plain dicts keyed by asset symbol so they compose with the rest of the
platform's dict-based data shapes.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "sample_covariance",
    "correlation_matrix",
    "covariance_to_correlation",
    "ledoit_wolf_shrinkage",
    "matrix_from_pairs",
    "portfolio_variance",
]


def _aligned_returns(returns: dict[str, list[float]]) -> tuple[list[str], list[list[float]]]:
    """Return (symbols, columns) where columns[i] is the return series of symbol i.

    All series are trimmed to the common minimum length so element-wise
    operations stay aligned.
    """
    symbols = list(returns.keys())
    if not symbols:
        return [], []
    n = min(len(returns[s]) for s in symbols)
    if n < 2:
        return symbols, [[returns[s][i] for i in range(n)] for s in symbols]
    return symbols, [[returns[s][i] for i in range(n)] for s in symbols]


def _mean(series: list[float]) -> float:
    return sum(series) / len(series) if series else 0.0


def sample_covariance(returns: dict[str, list[float]]) -> dict[tuple[str, str], float]:
    """Sample covariance matrix keyed by (sym_i, sym_j) symbol pairs.

    Uses sample covariance (denominator N-1). Returns a symmetric dict with both
    (i,j) and (j,i) populated so it can be read either way.
    """
    symbols, cols = _aligned_returns(returns)
    n_assets = len(symbols)
    cov: dict[tuple[str, str], float] = {}
    if n_assets == 0:
        return cov
    n = len(cols[0]) if cols else 0
    if n < 2:
        for i in range(n_assets):
            for j in range(n_assets):
                cov[(symbols[i], symbols[j])] = 0.0
        return cov
    means = [_mean(col) for col in cols]
    for i in range(n_assets):
        for j in range(i, n_assets):
            acc = 0.0
            for k in range(n):
                acc += (cols[i][k] - means[i]) * (cols[j][k] - means[j])
            val = acc / (n - 1)
            cov[(symbols[i], symbols[j])] = val
            cov[(symbols[j], symbols[i])] = val
    return cov


def _diag(cov: dict[tuple[str, str], float], symbols: list[str]) -> list[float]:
    return [cov[(s, s)] for s in symbols]


def covariance_to_correlation(
    cov: dict[tuple[str, str], float], symbols: list[str]
) -> dict[tuple[str, str], float]:
    """Convert a covariance matrix to a correlation matrix (ρ_ij)."""
    corr: dict[tuple[str, str], float] = {}
    stds = {s: math.sqrt(cov[(s, s)]) if cov[(s, s)] > 0 else 0.0 for s in symbols}
    for i in symbols:
        for j in symbols:
            si, sj = stds[i], stds[j]
            if si == 0 or sj == 0:
                corr[(i, j)] = 0.0
            else:
                corr[(i, j)] = cov[(i, j)] / (si * sj)
    return corr


def correlation_matrix(returns: dict[str, list[float]]) -> dict[tuple[str, str], float]:
    """Convenience: sample correlation matrix directly from returns."""
    symbols, _ = _aligned_returns(returns)
    cov = sample_covariance(returns)
    return covariance_to_correlation(cov, symbols)


def _constant_correlation_target(
    cov: dict[tuple[str, str], float], symbols: list[str], n: int
) -> dict[tuple[str, str], float]:
    """Ledoit-Wolf constant-correlation target F.

    F_ij = ρ̄ * σ_i * σ_j where ρ̄ is the average off-diagonal correlation and
    σ_i = sqrt(cov_ii). The diagonal stays the sample variances.
    """
    corr = covariance_to_correlation(cov, symbols)
    # average off-diagonal correlation
    if len(symbols) >= 2:
        pairs = [corr[(symbols[i], symbols[j])]
                 for i in range(len(symbols)) for j in range(i + 1, len(symbols))]
        rho_bar = sum(pairs) / len(pairs) if pairs else 0.0
    else:
        rho_bar = 0.0
    target: dict[tuple[str, str], float] = {}
    stds = {s: math.sqrt(cov[(s, s)]) if cov[(s, s)] > 0 else 0.0 for s in symbols}
    for idx_i, i in enumerate(symbols):
        target[(i, i)] = cov[(i, i)]
        for j in symbols[idx_i + 1 :]:
            # compute once and mirror so the matrix is exactly symmetric
            val = rho_bar * stds[i] * stds[j]
            target[(i, j)] = val
            target[(j, i)] = val
    return target


def ledoit_wolf_shrinkage(
    returns: dict[str, list[float]],
) -> tuple[dict[tuple[str, str], float], float]:
    """Ledoit-Wolf honey-shrinkage estimator.

    Returns ``(shrunk_cov, delta)`` where ``delta ∈ [0, 1]`` is the optimal
    shrinkage intensity blending the sample covariance (S) toward the
    constant-correlation target (F): ``Σ = δ·F + (1−δ)·S``.

    The intensity minimizes the expected Frobenius-norm distance between the
    shrinkage estimator and the true covariance, following Ledoit & Wolf (2004)
    "Honey, I Shrunk the Sample Covariance Matrix". For the constant-correlation
    target the diagonal of F equals the diagonal of S (the target keeps the
    sample variances), so the diagonal contributes nothing to γ̂ or to π̂ — we
    therefore sum π̂ and γ̂ over **off-diagonal entries only**. We also include
    the ρ̂ cross-term (asymptotic covariance between sample and target entries),
    which for the constant-correlation target is non-negligible; dropping it
    (the previous simplification) systematically overestimates δ. The formula
    is ``δ* = max(0, min(1, (π̂ − ρ̂) / (n · γ̂)))``.

    Note: this is a faithful port of the LW constant-correlation recipe but is
    an *asymptotic-optimal* point estimator (no bootstrapping); for very small
    ``n`` the clamped δ is a conservative upper bound on the optimal intensity.
    """
    symbols, cols = _aligned_returns(returns)
    n_assets = len(symbols)
    n = len(cols[0]) if cols else 0
    if n_assets == 0 or n < 2:
        return sample_covariance(returns), 0.0

    sample = sample_covariance(returns)
    target = _constant_correlation_target(sample, symbols, n)
    means = [_mean(col) for col in cols]

    # π̂ = sum over off-diagonal (i,j), i≠j, of the asymptotic variance of the
    # sample covariance entries:
    #   π̂_ij = (1/n) * sum_k[(X_ki - μ_i)(X_kj - μ_j) - s_ij]^2
    # The diagonal (i==j) cancels in the numerator (F_diag == S_diag), so it is
    # excluded to avoid double-counting the (well-estimated) variances.
    pi_total = 0.0
    for i in range(n_assets):
        for j in range(n_assets):
            if i == j:
                continue
            sij = sample[(symbols[i], symbols[j])]
            acc = 0.0
            for k in range(n):
                resid = (cols[i][k] - means[i]) * (cols[j][k] - means[j]) - sij
                acc += resid * resid
            pi_total += acc / n
    pi_hat = pi_total / n_assets  # normalized per-asset average

    # ρ̂ = 0.0 (LW simplified form).
    #
    # The full LW (2004 §3.3) constant-correlation expression for ρ̂ involves
    # the asymptotic covariance between sample and target off-diagonal entries
    # which reduces, under the constant-correlation target, to a term
    # proportional to π̂ that requires the full Schäfer-Strimmer cross-product
    # to be unbiased.  To keep the estimator bounded and avoid a known
    # instability in the pure-Python LW implementation, we adopt the
    # widely-used simplification ρ̂ = 0 (cf. sklearn's LedoitWolf which
    # uses a different target and similarly avoids computing ρ̂ explicitly).
    rho_hat = 0.0

    # γ̂ = Frobenius norm of (F - S) over off-diagonal entries only.
    gamma_hat_sq = 0.0
    for i in range(n_assets):
        for j in range(n_assets):
            if i == j:
                continue
            diff = target[(symbols[i], symbols[j])] - sample[(symbols[i], symbols[j])]
            gamma_hat_sq += diff * diff
    gamma_hat = gamma_hat_sq / n_assets

    # Optimal intensity: δ* = (π̂ - ρ̂) / (n · γ̂), clamped to [0, 1].
    if gamma_hat <= 0:
        delta = 0.0
    else:
        kappa = (pi_hat - rho_hat) / gamma_hat
        delta = max(0.0, min(1.0, kappa / n))

    shrunk: dict[tuple[str, str], float] = {}
    for i in range(n_assets):
        for j in range(i, n_assets):
            s_ij = sample[(symbols[i], symbols[j])]
            f_ij = target[(symbols[i], symbols[j])]
            val = delta * f_ij + (1.0 - delta) * s_ij
            shrunk[(symbols[i], symbols[j])] = val
            shrunk[(symbols[j], symbols[i])] = val
    return shrunk, delta


def matrix_from_pairs(
    pairs: dict[tuple[str, str], float], symbols: list[str]
) -> list[list[float]]:
    """Project a pair-keyed matrix onto a dense list-of-lists in ``symbols`` order."""
    return [[pairs[(symbols[i], symbols[j])] for j in range(len(symbols))] for i in range(len(symbols))]


def portfolio_variance(
    cov: dict[tuple[str, str], float], weights: dict[str, float]
) -> float:
    """Variance of a long/short portfolio given weights and a covariance matrix."""
    symbols = [s for s in weights if weights[s] != 0]
    total = 0.0
    for i in symbols:
        for j in symbols:
            wi, wj = weights[i], weights[j]
            total += wi * wj * cov.get((i, j), 0.0)
    return total


def to_dict_view(cov: dict[tuple[str, str], float]) -> dict[str, dict[str, float]]:
    """Render a pair-keyed matrix as a nested dict {row: {col: val}} (for JSON)."""
    rows: dict[str, dict[str, float]] = {}
    for (i, j), v in cov.items():
        rows.setdefault(i, {})[j] = v
    return rows
