"""P345: Dynamic risk contribution — rolling-window risk contribution time series.

Computes per-asset risk contribution via rolling-window sample covariance,
detects contribution drift, and reports summary statistics. Pure Python,
no scipy/numpy. Panel limited to ≤ 50 assets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DynamicRiskContributionResult", "dynamic_risk_contribution_report"]

_MAX_ASSETS = 50


def _sample_covariance(returns_panel: dict[str, list[float]], start: int, window: int) -> dict[tuple[str, str], float]:
    """Compute sample covariance matrix over a window slice.

    Returns {(asset_i, asset_j): cov_ij} for all pairs.
    """
    assets = list(returns_panel.keys())
    n = len(assets)
    cov: dict[tuple[str, str], float] = {}
    if window < 2:
        return cov

    # Pre-slice returns
    sliced: dict[str, list[float]] = {}
    for a in assets:
        sliced[a] = returns_panel[a][start : start + window]

    # Compute means
    means: dict[str, float] = {}
    for a in assets:
        vals = sliced[a]
        means[a] = sum(vals) / window

    # Compute covariances
    for i in range(n):
        ai = assets[i]
        vi = sliced[ai]
        mi = means[ai]
        for j in range(i, n):
            aj = assets[j]
            vj = sliced[aj]
            mj = means[aj]
            cov_val = sum((vi[k] - mi) * (vj[k] - mj) for k in range(window)) / (window - 1)
            cov[(ai, aj)] = cov_val
            if i != j:
                cov[(aj, ai)] = cov_val

    return cov


def _portfolio_vol(cov: dict[tuple[str, str], float], weights: dict[str, float], assets: list[str]) -> float:
    """Compute portfolio volatility sqrt(w' Σ w)."""
    var = 0.0
    for ai in assets:
        wi = weights.get(ai, 0.0)
        for aj in assets:
            wj = weights.get(aj, 0.0)
            cov_ij = cov.get((ai, aj), 0.0)
            var += wi * wj * cov_ij
    return math.sqrt(max(var, 0.0))


def _risk_contribution(
    cov: dict[tuple[str, str], float],
    weights: dict[str, float],
    assets: list[str],
    port_vol: float,
) -> dict[str, float]:
    """Compute risk contributions per asset.

    risk_contrib_i = w_i * (Σ w)_i / σ_p
    """
    if port_vol < 1e-15:
        return {a: 0.0 for a in assets}

    contrib: dict[str, float] = {}
    for a in assets:
        # (Σ w)_a = sum_j cov(a, j) * w_j
        cov_w = sum(cov.get((a, aj), 0.0) * weights.get(aj, 0.0) for aj in assets)
        contrib[a] = weights.get(a, 0.0) * cov_w / port_vol

    return contrib


@dataclass(frozen=True)
class DynamicRiskContributionResult:
    contributions: dict[str, list[float]] = field(default_factory=dict)
    drift_flags: dict[str, bool] = field(default_factory=dict)
    summary: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contributions": self.contributions,
            "drift_flags": self.drift_flags,
            "summary": self.summary,
        }


def dynamic_risk_contribution_report(
    returns_panel: dict[str, list[float]],
    weights: dict[str, float],
    *,
    window: int = 20,
    periods_per_year: int = 252,
) -> DynamicRiskContributionResult:
    """Compute rolling-window risk contribution time series for each asset.

    Args:
        returns_panel: {asset: [period_returns]} mapping. All series must have
            equal length, non-empty, and contain finite numbers. Max 50 assets.
        weights: {asset: weight} mapping. Must match returns_panel keys.
        window: Rolling window size for covariance estimation (default 20).
        periods_per_year: Periods per year (for drift threshold, default 252).

    Returns:
        DynamicRiskContributionResult with contributions time series,
        drift detection flags, and per-asset summary.

    Raises:
        ValueError: On invalid/missing/empty inputs or panel > 50 assets.
    """
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("weights must be a non-empty dict")

    # Validate panel size limit
    if len(returns_panel) > _MAX_ASSETS:
        raise ValueError(f"returns_panel must have at most {_MAX_ASSETS} assets")

    # Validate assets match
    panel_assets = set(returns_panel.keys())
    weight_assets = set(weights.keys())
    if panel_assets != weight_assets:
        missing_in_weights = panel_assets - weight_assets
        missing_in_panel = weight_assets - panel_assets
        msg_parts: list[str] = []
        if missing_in_weights:
            msg_parts.append(f"assets in returns_panel but not in weights: {missing_in_weights}")
        if missing_in_panel:
            msg_parts.append(f"assets in weights but not in returns_panel: {missing_in_panel}")
        raise ValueError("; ".join(msg_parts))

    # Validate equal-length, finite series
    length: int | None = None
    for name, series in returns_panel.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
        for v in series:
            if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"returns_panel['{name}'] contains non-finite values")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise ValueError(f"returns_panel['{name}'] length {len(series)} != {length}")

    if length is None or length < 2:
        raise ValueError("returns_panel must have at least 2 periods")

    # Validate weights are finite
    for name, w in weights.items():
        if not math.isfinite(float(w)):
            raise ValueError(f"weights['{name}'] must be finite")

    if not isinstance(window, int) or window < 2:
        raise ValueError("window must be an int >= 2")

    assets = list(returns_panel.keys())
    n_windows = max(0, length - window + 1)

    # Rolling risk contributions
    contributions: dict[str, list[float]] = {a: [] for a in assets}
    for t in range(n_windows):
        cov = _sample_covariance(returns_panel, t, window)
        port_vol = _portfolio_vol(cov, weights, assets)
        rc = _risk_contribution(cov, weights, assets, port_vol)
        for a in assets:
            contributions[a].append(rc[a])

    # Drift detection: if the std of contribution changes > mean * threshold, flag drift
    drift_flags: dict[str, bool] = {}
    summary: dict[str, dict[str, float]] = {}
    drift_threshold = 0.3  # 30% relative std → drift detected

    for a in assets:
        series = contributions[a]
        if len(series) < 2:
            drift_flags[a] = False
            summary[a] = {"avg_contribution_pct": abs(series[0]) if series else 0.0}
            continue

        mean_val = sum(series) / len(series)
        var_val = sum((v - mean_val) ** 2 for v in series) / (len(series) - 1)
        std_val = math.sqrt(max(var_val, 0.0))
        cv = std_val / abs(mean_val) if abs(mean_val) > 1e-15 else float("inf")
        drift_flags[a] = cv > drift_threshold

        # avg_contribution_pct: average of absolute risk contribution as fraction
        # of total (which should sum to portfolio_vol ≈ 1 when normalised)
        avg_pct = abs(mean_val)
        summary[a] = {"avg_contribution_pct": avg_pct}

    return DynamicRiskContributionResult(
        contributions=contributions,
        drift_flags=drift_flags,
        summary=summary,
    )
