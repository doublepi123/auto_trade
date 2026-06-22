from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

from app.platform.backtest_service import PlatformBacktestService

__all__ = ["OptimizerService"]


def _grid_combos(param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(param_grid.keys())
    values = [param_grid[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


class OptimizerService:
    """策略参数寻优（参考 Lean IOptimizer / vectorbt 网格搜索）。"""

    def __init__(self, backtest: PlatformBacktestService | None = None) -> None:
        self.backtest = backtest or PlatformBacktestService()

    def _run_one(
        self,
        strategy_name: str,
        params: dict[str, Any],
        symbols: list[str],
        bars: list[dict[str, Any]],
        initial_cash: Decimal,
    ) -> dict[str, Any]:
        result = self.backtest.run(
            strategy_name=strategy_name,
            params=params,
            symbols=symbols,
            bars=bars,
            initial_cash=initial_cash,
        )
        analytics = result.get("analytics", {})
        stats = result.get("stats", {})
        return {
            "params": params,
            "sharpe": analytics.get("sharpe", 0.0),
            "sortino": analytics.get("sortino", 0.0),
            "max_drawdown": analytics.get("max_drawdown", 0.0),
            "final_nav": stats.get("final_nav", 0.0),
            "pnl": stats.get("pnl", 0.0),
            "num_fills": stats.get("num_fills", 0),
        }

    def grid_search(
        self,
        strategy_name: str,
        param_grid: dict[str, list[Any]],
        symbols: list[str],
        bars: list[dict[str, Any]],
        metric: str = "sharpe",
        top_k: int = 10,
        initial_cash: Decimal = Decimal("100000"),
    ) -> dict[str, Any]:
        combos = _grid_combos(param_grid)
        results = [self._run_one(strategy_name, c, symbols, bars, initial_cash) for c in combos]
        results.sort(key=lambda r: r.get(metric, 0.0) or 0.0, reverse=True)
        return {"ranked": results[:top_k], "total_combos": len(combos), "metric": metric}

    def walk_forward(
        self,
        strategy_name: str,
        param_grid: dict[str, list[Any]],
        symbols: list[str],
        bars: list[dict[str, Any]],
        split_fraction: float = 0.5,
        top_k: int = 5,
        metric: str = "sharpe",
        initial_cash: Decimal = Decimal("100000"),
    ) -> dict[str, Any]:
        if not bars:
            return {"in_sample_ranked": [], "out_of_sample": [], "split_at": 0, "metric": metric}
        split = max(1, int(len(bars) * split_fraction))
        is_bars = bars[:split]
        oos_bars = bars[split:]
        is_result = self.grid_search(
            strategy_name,
            param_grid,
            symbols,
            is_bars,
            metric=metric,
            top_k=top_k,
            initial_cash=initial_cash,
        )
        oos: list[dict[str, Any]] = []
        for entry in is_result["ranked"]:
            if not oos_bars:
                oos.append(
                    {
                        **entry,
                        "in_sample_sharpe": entry.get(metric, 0.0),
                        "out_of_sample_sharpe": None,
                        "out_of_sample_pnl": None,
                    }
                )
                continue
            oos_run = self._run_one(strategy_name, entry["params"], symbols, oos_bars, initial_cash)
            oos.append(
                {
                    "params": entry["params"],
                    "in_sample_sharpe": entry.get("sharpe", 0.0),
                    "out_of_sample_sharpe": oos_run.get("sharpe", 0.0),
                    "out_of_sample_pnl": oos_run.get("pnl", 0.0),
                }
            )
        return {
            "in_sample_ranked": is_result["ranked"],
            "out_of_sample": oos,
            "split_at": split,
            "metric": metric,
        }
