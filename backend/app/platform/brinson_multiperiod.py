"""P202: multi-period Brinson attribution + linking.

The single-period Brinson-Fachler model (in :mod:`app.platform.brinson`) does
not compose across periods: compounding introduces cross-product terms that
break naive summation. This module implements the standard **Brinson-Ibbotson
(Brinson-Hood-Beebower) arithmetic linking** and a **Frongello geometric
linking** variant so that a sequence of period attributions reconciles to the
multi-period active return.

Linking algorithms:

For periods ``t = 1..T`` with portfolio return ``rP_t``, benchmark ``rB_t``, and
per-period allocation/selection/interaction effects (``A_t``, ``S_t``,
``I_t``):

* Arithmetic (BHB) — ``AL = sum_t A_t`` etc. Reconciles exactly to the geometric
  active return only when period returns are small; for larger moves a residual
  (the "interaction of interactions") remains and is reported explicitly.
* Geometric (Frongello) — each period's effect is scaled by the product of
  prior benchmark growth, so compounding is respected. The geometric explained
  total reconciles more tightly to the multi-period active return.

The residual (active return − linked explained) is surfaced in both modes so the
caller can see the unreconciled cross-product — every commercial attribution
system reports this same gap.
"""

from __future__ import annotations

from typing import Any

from app.platform.brinson import brinson_attribution

__all__ = [
    "brinson_multi_period",
    "link_arithmetic",
    "link_geometric",
]


def brinson_multi_period(
    periods: list[dict[str, dict[str, float]]],
) -> dict[str, Any]:
    """Run single-period Brinson for each period, then link the effects.

    ``periods`` is a list of period dicts, each shaped like::

        {
            "portfolio_weights": {sector: w},
            "portfolio_returns": {sector: r},
            "benchmark_weights": {sector: w},
            "benchmark_returns": {sector: r},
        }

    Returns the linked totals (allocation/selection/interaction) under both the
    arithmetic and geometric linking schemes, the multi-period
    portfolio/benchmark/active returns, the per-period detail, and a
    reconciliation residual per scheme.
    """
    if not periods:
        return _empty_result()

    per_period: list[dict[str, Any]] = []
    single_results: list[dict[str, Any]] = []
    geometric_portfolio = 1.0
    geometric_benchmark = 1.0

    for period in periods:
        single = brinson_attribution(
            portfolio_weights=period["portfolio_weights"],
            portfolio_returns=period["portfolio_returns"],
            benchmark_weights=period["benchmark_weights"],
            benchmark_returns=period["benchmark_returns"],
        )
        single_results.append(single)
        geometric_portfolio *= (1.0 + single["portfolio_return"])
        geometric_benchmark *= (1.0 + single["benchmark_return"])

    effects = [
        {
            "allocation": s["total_allocation"],
            "selection": s["total_selection"],
            "interaction": s["total_interaction"],
        }
        for s in single_results
    ]
    benchmark_returns = [s["benchmark_return"] for s in single_results]

    arith = link_arithmetic(effects)
    geom = link_geometric(effects, benchmark_returns)

    portfolio_total = geometric_portfolio - 1.0
    benchmark_total = geometric_benchmark - 1.0
    active_total = geometric_portfolio - geometric_benchmark

    per_period = [
        {
            "period_index": i,
            "portfolio_return": s["portfolio_return"],
            "benchmark_return": s["benchmark_return"],
            "active_return": s["active_return"],
            "allocation": s["total_allocation"],
            "selection": s["total_selection"],
            "interaction": s["total_interaction"],
        }
        for i, s in enumerate(single_results)
    ]

    return {
        "periods": len(periods),
        "portfolio_return": portfolio_total,
        "benchmark_return": benchmark_total,
        "active_return": active_total,
        "linking": {
            "arithmetic": {
                **arith,
                "residual": active_total - arith["explained"],
            },
            "geometric": {
                **geom,
                "residual": active_total - geom["explained"],
            },
        },
        "per_period": per_period,
    }


def link_arithmetic(
    effects: list[dict[str, float]],
) -> dict[str, float]:
    """Sum per-period allocation/selection/interaction effects (BHB link)."""
    alloc = sum(e["allocation"] for e in effects)
    sel = sum(e["selection"] for e in effects)
    intr = sum(e["interaction"] for e in effects)
    return {
        "allocation": alloc,
        "selection": sel,
        "interaction": intr,
        "explained": alloc + sel + intr,
    }


def link_geometric(
    effects: list[dict[str, float]],
    benchmark_returns: list[float],
) -> dict[str, float]:
    """Frongello geometric link: scale each effect by prior benchmark growth."""
    alloc = sel = intr = 0.0
    for i, eff in enumerate(effects):
        growth = 1.0
        for j in range(i):
            growth *= (1.0 + benchmark_returns[j])
        alloc += eff["allocation"] * growth
        sel += eff["selection"] * growth
        intr += eff["interaction"] * growth
    return {
        "allocation": alloc,
        "selection": sel,
        "interaction": intr,
        "explained": alloc + sel + intr,
    }


def _empty_result() -> dict[str, Any]:
    return {
        "periods": 0,
        "portfolio_return": 0.0,
        "benchmark_return": 0.0,
        "active_return": 0.0,
        "linking": {
            "arithmetic": {"allocation": 0.0, "selection": 0.0, "interaction": 0.0,
                           "explained": 0.0, "residual": 0.0},
            "geometric": {"allocation": 0.0, "selection": 0.0, "interaction": 0.0,
                          "explained": 0.0, "residual": 0.0},
        },
        "per_period": [],
    }