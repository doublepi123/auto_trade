"""P378: Empirical copula stress testing.

Estimates an empirical copula from a panel of asset returns (using rank
transform to uniform pseudo-observations), then samples joint stress
scenarios from the lower tail. Reports tail correlation, systemic loss
per scenario, and the worst scenario.
"""

from __future__ import annotations

import math
import random as _random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.platform.factor_utils import mean, std

__all__ = ["CopulaStressResult", "copula_stress_report"]

_MAX_ASSETS = 50
_MAX_PERIODS = 5000


@dataclass(frozen=True)
class CopulaStressResult:
    scenarios: list[dict[str, float]] = field(default_factory=list)
    tail_correlation: float = 0.0
    systemic_loss: list[float] = field(default_factory=list)
    worst_scenario_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenarios": [dict(s) for s in self.scenarios],
            "tail_correlation": self.tail_correlation,
            "systemic_loss": list(self.systemic_loss),
            "worst_scenario_index": self.worst_scenario_index,
        }


def _validate_panel(
    returns_panel: dict[str, list[float]],
) -> tuple[list[str], list[list[float]], int]:
    """Validate and convert returns panel to matrix form.

    Returns (asset_names, returns_matrix, n_periods).
    """
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")

    if len(returns_panel) > _MAX_ASSETS:
        raise ValueError(
            f"returns_panel must contain at most {_MAX_ASSETS} assets"
        )

    asset_names = list(returns_panel.keys())
    n_periods = 0
    returns_matrix: list[list[float]] = []

    for name in asset_names:
        series = returns_panel[name]
        if isinstance(series, dict) or isinstance(series, (str, bytes)):
            raise ValueError(f"returns for '{name}' must be a sequence of finite numbers")
        if not isinstance(series, list):
            raise ValueError(f"returns for '{name}' must be a list")
        if len(series) < 2:
            raise ValueError(f"returns for '{name}' must have at least 2 observations")

        vals: list[float] = []
        for v in series:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"returns for '{name}' must be finite numbers")
            fv = float(v)
            if not math.isfinite(fv):
                raise ValueError(f"returns for '{name}' must be finite numbers")
            vals.append(fv)

        if n_periods == 0:
            n_periods = len(vals)
        elif len(vals) != n_periods:
            raise ValueError(
                f"all assets must have the same number of observations "
                f"(got {len(vals)} for '{name}', expected {n_periods})"
            )
        returns_matrix.append(vals)

    return asset_names, returns_matrix, n_periods


def _rank_to_uniform(values: list[float]) -> list[float]:
    """Convert values to uniform pseudo-observations via fractional ranking."""
    n = len(values)
    if n < 2:
        return values
    # Sort indices by value
    indexed = [(val, idx) for idx, val in enumerate(values)]
    indexed.sort(key=lambda x: x[0])

    # Assign uniform values: (rank) / n (fractional for ties)
    result = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and indexed[j][0] == indexed[i][0]:
            j += 1
        # Average rank for tied group: (i + j - 1) / 2 + 1
        avg_rank = (i + j - 1) / 2.0 + 1.0
        uniform_val = avg_rank / n
        for k in range(i, j):
            result[indexed[k][1]] = uniform_val
        i = j
    return result


def _spearman_correlation(x: list[float], y: list[float]) -> float:
    """Compute Spearman rank correlation."""
    n = len(x)
    if n < 2:
        return 0.0
    ranks_x = _rank_to_uniform(x)
    ranks_y = _rank_to_uniform(y)
    mx = mean(ranks_x)
    my = mean(ranks_y)
    cov = 0.0
    vx = 0.0
    vy = 0.0
    for i in range(n):
        dx = ranks_x[i] - mx
        dy = ranks_y[i] - my
        cov += dx * dy
        vx += dx * dx
        vy += dy * dy
    if vx == 0.0 or vy == 0.0:
        return 0.0
    return cov / math.sqrt(vx * vy)


def copula_stress_report(
    returns_panel: dict[str, list[float]],
    *,
    quantile: float = 0.05,
    n_scenarios: int = 100,
    seed: int = 42,
) -> CopulaStressResult:
    """Generate stress scenarios from empirical copula tail sampling.

    Args:
        returns_panel: {asset_name: [returns]} mapping.
        quantile: Tail quantile threshold in (0, 1). Default 0.05.
        n_scenarios: Number of stress scenarios to generate.
        seed: Random seed for reproducibility.

    Returns:
        CopulaStressResult with scenarios, tail_correlation, systemic_loss,
        and worst_scenario_index.

    Raises:
        ValueError: If inputs are invalid.
    """
    if not isinstance(quantile, (int, float)) or isinstance(quantile, bool):
        raise ValueError("quantile must be a finite number in (0, 1)")
    q = float(quantile)
    if not (0.0 < q < 1.0):
        raise ValueError("quantile must be in (0, 1)")

    if not isinstance(n_scenarios, int) or isinstance(n_scenarios, bool):
        raise ValueError("n_scenarios must be a positive int")
    if n_scenarios < 1:
        raise ValueError("n_scenarios must be >= 1")

    asset_names, returns_matrix, n_periods = _validate_panel(returns_panel)
    n_assets = len(asset_names)

    # Convert each asset's returns to uniform pseudo-observations
    uniform_data: list[list[float]] = []
    for i in range(n_assets):
        uniform_data.append(_rank_to_uniform(returns_matrix[i]))

    # Identify tail observations: those where the average uniform rank is <= quantile
    # For copula tail: flag rows where product of uniforms is in the lower tail
    # We use a simple criterion: the concatenated rank sum is in the lower tail
    # Compute a composite tail score for each period
    tail_scores: list[float] = []
    for t in range(n_periods):
        # Average rank across all assets
        avg_rank = sum(uniform_data[i][t] for i in range(n_assets)) / n_assets
        tail_scores.append(avg_rank)

    # Tail periods are those below the quantile
    threshold = sorted(tail_scores)[max(0, int(n_periods * q) - 1)]
    tail_indices = [t for t in range(n_periods) if tail_scores[t] <= threshold]

    if not tail_indices:
        # Fallback: use all periods
        tail_indices = list(range(n_periods))

    # Resample from tail periods to generate scenarios
    rng = _random.Random(seed)
    scenarios: list[dict[str, float]] = []
    systemic_loss_list: list[float] = []

    for _ in range(n_scenarios):
        idx = rng.choice(tail_indices)
        scenario: dict[str, float] = {}
        loss = 0.0
        for i, name in enumerate(asset_names):
            ret = returns_matrix[i][idx]
            scenario[name] = float(ret)
            loss += abs(ret)
        scenarios.append(scenario)
        systemic_loss_list.append(float(loss))

    # Compute tail correlation: Spearman on sorted tail-flagged returns
    tail_corr = 0.0
    if n_assets >= 2 and tail_indices:
        tail_returns_0 = [returns_matrix[0][t] for t in tail_indices]
        tail_returns_1 = [returns_matrix[1][t] for t in tail_indices]
        tail_corr = _spearman_correlation(tail_returns_0, tail_returns_1)

    # Find worst scenario (largest systemic loss)
    worst_idx = 0
    if systemic_loss_list:
        worst_idx = max(range(len(systemic_loss_list)),
                        key=lambda i: systemic_loss_list[i])

    return CopulaStressResult(
        scenarios=scenarios,
        tail_correlation=float(tail_corr),
        systemic_loss=systemic_loss_list,
        worst_scenario_index=int(worst_idx),
    )
