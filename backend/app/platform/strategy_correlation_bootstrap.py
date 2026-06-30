"""P387: Strategy correlation bootstrap.

Estimate pairwise strategy correlations and their 95 % confidence
intervals via a stationary block bootstrap.  Resample blocks of
contiguous observations to preserve time-series dependence, then
compute the Pearson correlation matrix for each bootstrap replicate.

Pure Python — no numpy / scipy.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import pearson, validate_series

__all__ = [
    "StrategyCorrelationBootstrapResult",
    "strategy_correlation_bootstrap_report",
]


@dataclass(frozen=True)
class StrategyCorrelationBootstrapResult:
    """Frozen carrier for the bootstrap correlation report.

    Attributes
    ----------
    correlation_matrix: {pair_name: {mean, ci_lower, ci_upper}}.
    significant_pairs: Pairs whose CI does not contain zero.
    diversification_significant: Whether any pair has mean < 0.5 and CI
                                entirely below 0.5.
    """

    correlation_matrix: dict[str, dict[str, float]]
    significant_pairs: list[str]
    diversification_significant: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_matrix": self.correlation_matrix,
            "significant_pairs": self.significant_pairs,
            "diversification_significant": self.diversification_significant,
        }


def _block_bootstrap_indices(
    n: int, block_size: int, rng: random.Random
) -> list[int]:
    """Generate indices for one stationary block bootstrap replicate.

    Reference: Politis & Romano (1994).
    """
    if n <= block_size:
        # Wrap-around single block
        return list(range(n))

    # Number of blocks needed to cover n observations
    num_blocks = (n + block_size - 1) // block_size
    indices: list[int] = []

    for _ in range(num_blocks):
        start = rng.randint(0, n - block_size)
        for j in range(block_size):
            indices.append((start + j) % n)

    return indices[:n]


def strategy_correlation_bootstrap_report(
    strategy_returns: dict[str, list[float]],
    *,
    n_bootstrap: int = 500,
    seed: int = 42,
    block_size: int = 5,
) -> StrategyCorrelationBootstrapResult:
    """Estimate strategy correlations and CIs via block bootstrap.

    Parameters
    ----------
    strategy_returns: {strategy_name: [return_series]} — all same length.
    n_bootstrap: Number of bootstrap replicates (default 500).
    seed: Random seed for reproducibility (default 42).
    block_size: Stationary block length (default 5).

    Returns
    -------
    StrategyCorrelationBootstrapResult with CIs and significance flags.

    Raises
    ------
    ValueError: If inputs are invalid or misaligned.
    """
    if not isinstance(strategy_returns, dict) or len(strategy_returns) < 2:
        raise ValueError("strategy_returns must contain at least two strategies")
    if len(strategy_returns) > 50:
        raise ValueError("strategy_returns must contain at most 50 strategies")
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    if n_bootstrap < 2:
        raise ValueError("n_bootstrap must be >= 2")

    validated: dict[str, list[float]] = {}
    for name, series in strategy_returns.items():
        name_str = str(name)
        validated[name_str] = validate_series(
            series, name=f"strategy_returns['{name_str}']", min_len=5
        )

    lengths = {len(v) for v in validated.values()}
    if len(lengths) != 1:
        raise ValueError("all strategy return series must have equal length")

    n = list(lengths)[0]
    strategy_names = list(validated.keys())
    ns = len(strategy_names)

    if block_size > n:
        block_size = n

    # Generate all strategy pairs
    pairs: list[tuple[str, str]] = []
    for i in range(ns):
        for j in range(i + 1, ns):
            pairs.append((strategy_names[i], strategy_names[j]))

    # Store bootstrap correlation samples for each pair
    bootstrap_samples: dict[str, list[float]] = {}
    for a, b in pairs:
        bootstrap_samples[f"{a}|{b}"] = []

    rng = random.Random(seed)

    for _ in range(n_bootstrap):
        indices = _block_bootstrap_indices(n, block_size, rng)

        # Extract bootstrap samples for each strategy
        boot_data: dict[str, list[float]] = {}
        for name in strategy_names:
            boot_data[name] = [validated[name][idx] for idx in indices]

        # Compute correlations for this bootstrap
        for a, b in pairs:
            corr = pearson(boot_data[a], boot_data[b])
            bootstrap_samples[f"{a}|{b}"].append(corr)

    # Compute percentiles and means
    correlation_matrix: dict[str, dict[str, float]] = {}
    significant_pairs: list[str] = []

    for a, b in pairs:
        key = f"{a}|{b}"
        samples = bootstrap_samples[key]
        samples_sorted = sorted(samples)
        mean_corr = sum(samples) / len(samples)
        idx_lower = max(0, int(0.025 * len(samples_sorted)))
        idx_upper = min(len(samples_sorted) - 1, int(0.975 * len(samples_sorted)))
        ci_lower = samples_sorted[idx_lower]
        ci_upper = samples_sorted[idx_upper]

        correlation_matrix[key] = {
            "mean": mean_corr,
            "ci_lower": ci_lower,
            "ci_upper": ci_upper,
        }

        # Significant if CI does not contain 0
        if ci_lower > 0 or ci_upper < 0:
            significant_pairs.append(key)

    # Diversification significant: any pair has mean < 0.5 and CI entirely < 0.5
    diversification_significant = any(
        correlation_matrix[f"{a}|{b}"]["ci_upper"] < 0.5
        for a, b in pairs
    )

    return StrategyCorrelationBootstrapResult(
        correlation_matrix=correlation_matrix,
        significant_pairs=significant_pairs,
        diversification_significant=diversification_significant,
    )
