"""P338: Turnover Frontier — net Sharpe ratio vs portfolio turnover.

Models the trade-off between gross alpha and turnover costs. For each
turnover rate, computes the cost drag and net Sharpe ratio, then identifies
the breakeven and optimal turnover levels.

Pure Python, no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnoverFrontierResult:
    """Turnover-vs-Sharpe frontier."""
    frontier: list[dict[str, float]]
    breakeven_turnover: float
    optimal_turnover: float

    def to_dict(self) -> dict[str, object]:
        return {
            "frontier": self.frontier,
            "breakeven_turnover": self.breakeven_turnover,
            "optimal_turnover": self.optimal_turnover,
        }


def _validate_panel(returns_panel: dict[str, list[float]]) -> None:
    if not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    length: int | None = None
    for name, series in returns_panel.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
        for v in series:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
                raise ValueError(f"returns_panel['{name}'] contains non-finite value: {v}")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise ValueError("all return series in panel must have equal length")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def turnover_frontier_report(
    returns_panel: dict[str, list[float]],
    *,
    turnover_rates: list[float] | None = None,
    cost_per_turnover: float = 0.001,
    periods_per_year: int = 252,
) -> TurnoverFrontierResult:
    """Compute net Sharpe ratio frontier across different turnover rates.

    Uses equal-weighted portfolio of all assets in the panel. For each turnover
    rate, estimates the gross Sharpe from the equal-weighted portfolio, computes
    the cost drag, and derives the net Sharpe.

    Args:
        returns_panel: Dict mapping asset name to list of period returns.
        turnover_rates: List of annual turnover rates. Default [0.01, 0.05, 0.1, 0.2, 0.5, 1.0].
        cost_per_turnover: Cost per unit of turnover (default 0.001 = 10 bps).
        periods_per_year: Annualization factor.

    Returns:
        TurnoverFrontierResult with frontier (list of {turnover, gross_sharpe, net_sharpe, cost_drag}),
        breakeven_turnover, and optimal_turnover.
    """
    _validate_panel(returns_panel)

    if isinstance(cost_per_turnover, bool) or not isinstance(cost_per_turnover, (int, float)) or not math.isfinite(float(cost_per_turnover)):
        raise ValueError("cost_per_turnover must be a finite number")
    if float(cost_per_turnover) < 0:
        raise ValueError("cost_per_turnover must be >= 0")
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year < 1:
        raise ValueError("periods_per_year must be an int >= 1")

    if turnover_rates is None:
        turnover_rates = [0.01, 0.05, 0.1, 0.2, 0.5, 1.0]
    if not turnover_rates:
        raise ValueError("turnover_rates must be a non-empty list")
    turnover_rates = sorted(set(turnover_rates))  # dedup
    for t in turnover_rates:
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(float(t)):
            raise ValueError(f"turnover_rates contains non-finite value: {t}")
        if t < 0:
            raise ValueError(f"turnover_rates must be non-negative, got {t}")

    # Build equal-weighted portfolio return series
    assets = list(returns_panel.keys())
    n_assets = len(assets)
    T = len(returns_panel[assets[0]])
    if T < 2:
        raise ValueError("each return series must have at least 2 periods")

    ew_returns: list[float] = []
    for t in range(T):
        total = 0.0
        for asset in assets:
            total += returns_panel[asset][t]
        ew_returns.append(total / n_assets)

    # Gross Sharpe ratio (annualized)
    ew_mean = _mean(ew_returns)
    ew_std = _std(ew_returns)
    if ew_std > 0:
        gross_sharpe = (ew_mean / ew_std) * math.sqrt(periods_per_year)
    else:
        gross_sharpe = 0.0

    # For each turnover rate, compute net Sharpe
    # cost_drag = turnover * cost_per_turnover * periods_per_year (annualized drag)
    # net_sharpe = gross_sharpe - cost_drag / (ew_std * sqrt(periods_per_year))
    # Simplified: cost_drag_annualized / annualized_vol
    annualized_vol = ew_std * math.sqrt(periods_per_year) if ew_std > 0 else 0.0

    frontier: list[dict[str, float]] = []
    breakeven_turnover = 0.0
    optimal_turnover = 0.0
    max_net_sharpe = float("-inf")

    for t in sorted(turnover_rates):
        cost_drag = t * cost_per_turnover * periods_per_year  # annualized cost
        if annualized_vol > 0:
            net_sharpe = gross_sharpe - cost_drag / annualized_vol
        else:
            net_sharpe = gross_sharpe

        frontier.append({
            "turnover": t,
            "gross_sharpe": gross_sharpe,
            "net_sharpe": net_sharpe,
            "cost_drag": cost_drag,
        })

        if net_sharpe > max_net_sharpe:
            max_net_sharpe = net_sharpe
            optimal_turnover = t

    # Breakeven turnover: where net_sharpe crosses 0 (linear interpolation)
    breakeven_turnover = 0.0
    if gross_sharpe > 0 and annualized_vol > 0:
        # gross_sharpe - (t * cost_per_turnover * periods_per_year) / annualized_vol = 0
        # t = gross_sharpe * annualized_vol / (cost_per_turnover * periods_per_year)
        if cost_per_turnover > 0:
            breakeven_turnover = gross_sharpe * annualized_vol / (cost_per_turnover * periods_per_year)
    # If all net_sharpes are <= 0, breakeven_turnover stays 0
    # If all net_sharpes > 0, breakeven is beyond the range; we interpolate
    # Use the frontier entries to find where net_sharpe crosses zero
    if frontier:
        all_positive = all(e["net_sharpe"] > 0 for e in frontier)
        if not all_positive:
            for i in range(1, len(frontier)):
                prev = frontier[i - 1]
                curr = frontier[i]
                if prev["net_sharpe"] >= 0 and curr["net_sharpe"] <= 0:
                    # Linear interpolation
                    if prev["net_sharpe"] - curr["net_sharpe"] != 0:
                        frac = prev["net_sharpe"] / (prev["net_sharpe"] - curr["net_sharpe"])
                        breakeven_turnover = prev["turnover"] + frac * (curr["turnover"] - prev["turnover"])
                    else:
                        breakeven_turnover = prev["turnover"]
                    break

    # If optimal is at the minimum, use that
    if max_net_sharpe <= 0 and frontier:
        optimal_turnover = frontier[0]["turnover"]

    return TurnoverFrontierResult(
        frontier=frontier,
        breakeven_turnover=breakeven_turnover,
        optimal_turnover=optimal_turnover,
    )
