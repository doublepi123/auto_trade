"""P342: Regime-based allocation.

Computes per-regime mean/std/Sharpe-like ratio for each asset, then
allocates weights via softmax over mean/vol ratios in the current regime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class RegimeAllocationResult:
    current_regime: str
    recommended_weights: dict[str, float]
    regime_stats: dict[str, dict[str, dict[str, float]]]
    regime_label: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_regime": self.current_regime,
            "recommended_weights": dict(self.recommended_weights),
            "regime_stats": {
                regime: {asset: dict(stats) for asset, stats in assets.items()}
                for regime, assets in self.regime_stats.items()
            },
            "regime_label": self.regime_label,
        }


def regime_allocation_report(
    returns_panel: dict[str, list[float]],
    regimes: list[str],
    *,
    current_regime: str,
) -> RegimeAllocationResult:
    """Allocate weights based on regime-conditional mean/volatility ratios.

    For each regime, compute per-asset mean, std, and Sharpe-like ratio.
    In the current_regime, allocate via softmax over mean/std ratios.
    """
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    if not isinstance(regimes, list) or not regimes:
        raise ValueError("regimes must be a non-empty list")
    if current_regime not in set(regimes):
        raise ValueError(f"current_regime '{current_regime}' not found in regimes list")

    assets = sorted(returns_panel.keys())
    series_map: dict[str, list[float]] = {}
    for asset in assets:
        series_map[asset] = validate_series(
            returns_panel[asset], name=f"returns_panel['{asset}']", min_len=2
        )

    # Check all series same length and matches regimes length
    lengths = {len(s) for s in series_map.values()}
    if len(lengths) != 1:
        raise ValueError("returns_panel series must have equal length")
    n = next(iter(lengths))
    if len(regimes) != n:
        raise ValueError("regimes length must match returns_panel series length")

    # Per-regime stats
    unique_regimes = sorted(set(regimes))
    regime_stats: dict[str, dict[str, dict[str, float]]] = {}

    for regime in unique_regimes:
        indices = [i for i, r in enumerate(regimes) if r == regime]
        regime_stats[regime] = {}
        for asset in assets:
            asset_regime_returns = [series_map[asset][i] for i in indices]
            if len(asset_regime_returns) < 2:
                regime_stats[regime][asset] = {
                    "mean": mean(asset_regime_returns) if asset_regime_returns else 0.0,
                    "std": 0.0,
                    "sharpe": 0.0,
                }
                continue
            mu = mean(asset_regime_returns)
            sigma = std(asset_regime_returns)
            sharpe = mu / sigma if sigma > 1e-12 else 0.0
            regime_stats[regime][asset] = {
                "mean": mu,
                "std": sigma,
                "sharpe": sharpe,
            }

    # Compute recommended weights for current_regime
    current_stats = regime_stats[current_regime]
    # Use Sharpe-like ratio for allocation (mean/vol ratio)
    ratios = {a: max(current_stats[a]["sharpe"], -100.0) for a in assets}

    # Softmax over ratios
    max_ratio = max(ratios.values())
    exp_sum = 0.0
    exp_vals: dict[str, float] = {}
    for a in assets:
        exp_val = math.exp(ratios[a] - max_ratio)
        exp_vals[a] = exp_val
        exp_sum += exp_val

    if exp_sum < 1e-12:
        # Fallback: equal weight
        recommended_weights = {a: 1.0 / len(assets) for a in assets}
    else:
        recommended_weights = {a: exp_vals[a] / exp_sum for a in assets}

    # Determine regime_label for current_regime based on mean returns
    current_mean = mean([current_stats[a]["mean"] for a in assets])
    if current_mean > 0.002:  # small positive threshold
        regime_label = "bull"
    elif current_mean < -0.002:
        regime_label = "bear"
    else:
        regime_label = "neutral"

    return RegimeAllocationResult(
        current_regime=current_regime,
        recommended_weights=recommended_weights,
        regime_stats=regime_stats,
        regime_label=regime_label,
    )
