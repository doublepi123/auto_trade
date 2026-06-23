from __future__ import annotations

from typing import Any

__all__ = ["brinson_attribution"]


def brinson_attribution(
    portfolio_weights: dict[str, float],
    portfolio_returns: dict[str, float],
    benchmark_weights: dict[str, float],
    benchmark_returns: dict[str, float],
) -> dict[str, Any]:
    """Brinson-Fachler 归因：把主动收益拆为 allocation/selection/interaction（按 sector）。

    所有 dict 需使用相同的 sector key 集合（缺省按 0 处理）。
    """
    sectors = sorted(set(portfolio_weights) | set(benchmark_weights))
    rP_total = sum(portfolio_weights.get(s, 0.0) * portfolio_returns.get(s, 0.0) for s in sectors)
    rB_total = sum(benchmark_weights.get(s, 0.0) * benchmark_returns.get(s, 0.0) for s in sectors)

    per_sector: list[dict[str, Any]] = []
    total_alloc = 0.0
    total_sel = 0.0
    total_inter = 0.0
    for s in sectors:
        wP = portfolio_weights.get(s, 0.0)
        wB = benchmark_weights.get(s, 0.0)
        rP = portfolio_returns.get(s, 0.0)
        rB = benchmark_returns.get(s, 0.0)
        allocation = (wP - wB) * (rB - rB_total)
        selection = wB * (rP - rB)
        interaction = (wP - wB) * (rP - rB)
        total_alloc += allocation
        total_sel += selection
        total_inter += interaction
        per_sector.append(
            {
                "sector": s,
                "portfolio_weight": wP,
                "benchmark_weight": wB,
                "portfolio_return": rP,
                "benchmark_return": rB,
                "allocation": allocation,
                "selection": selection,
                "interaction": interaction,
            }
        )
    active_return = rP_total - rB_total
    explained = total_alloc + total_sel + total_inter
    return {
        "portfolio_return": rP_total,
        "benchmark_return": rB_total,
        "active_return": active_return,
        "total_allocation": total_alloc,
        "total_selection": total_sel,
        "total_interaction": total_inter,
        "explained": explained,
        "residual": active_return - explained,
        "per_sector": per_sector,
    }
