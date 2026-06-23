from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

from app.platform.backtest_service import PlatformBacktestService

__all__ = ["SmartOptimizer"]


def _quasi_random_samples(
    param_choices: dict[str, list[Any]],
    num_trials: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    """Quasi-random 参数采样（seeded 随机，参考 Optuna 初始化/TPE 前采样的均匀覆盖意图）。"""
    samples: list[dict[str, Any]] = []
    keys = list(param_choices.keys())
    if not keys:
        return samples
    for _ in range(num_trials):
        samples.append({k: rng.choice(param_choices[k]) for k in keys})
    return samples


class SmartOptimizer:
    """智能参数搜索（参考 Optuna TPE / Hyperband successive-halving）。

    流程：① 准随机采样 N 组参数；② 在前半 bars 上评分，按中位剪枝保留 top-half（successive halving）；
    ③ 在全量 bars 上对幸存者终评，返回 top_k 排名。比同等预算的 grid 更省，因为粗筛只用半量数据。
    """

    def __init__(self, seed: int = 42, backtest: PlatformBacktestService | None = None) -> None:
        self.seed = seed
        self.backtest = backtest or PlatformBacktestService()

    def _score(
        self,
        strategy_name: str,
        params: dict[str, Any],
        symbols: list[str],
        bars: list[dict[str, Any]],
        metric: str,
        initial_cash: Decimal,
    ) -> float:
        if not bars:
            return 0.0
        try:
            result = self.backtest.run(
                strategy_name=strategy_name,
                params=params,
                symbols=symbols,
                bars=bars,
                initial_cash=initial_cash,
            )
            value = result.get("analytics", {}).get(metric, 0.0)
            return float(value) if value is not None else 0.0
        except Exception:
            return 0.0

    def search(
        self,
        strategy_name: str,
        param_choices: dict[str, list[Any]],
        symbols: list[str],
        bars: list[dict[str, Any]],
        metric: str = "sharpe",
        num_trials: int = 20,
        top_k: int = 5,
        initial_cash: Decimal = Decimal("100000"),
    ) -> dict[str, Any]:
        rng = random.Random(self.seed)
        samples = _quasi_random_samples(param_choices, num_trials, rng)
        if not samples:
            return {"ranked": [], "num_trials": 0, "survivors": 0, "metric": metric}

        split = max(1, len(bars) // 2)
        coarse_bars = bars[:split]
        coarse_scored: list[tuple[dict[str, Any], float]] = [
            (c, self._score(strategy_name, c, symbols, coarse_bars, metric, initial_cash))
            for c in samples
        ]
        coarse_scored.sort(key=lambda cs: cs[1], reverse=True)
        survivor_count = max(1, len(coarse_scored) // 2)
        survivors_with_coarse: list[tuple[dict[str, Any], float]] = coarse_scored[:survivor_count]

        final_scored: list[tuple[dict[str, Any], float, float]] = [
            (
                c,
                coarse_s,
                self._score(strategy_name, c, symbols, bars, metric, initial_cash),
            )
            for c, coarse_s in survivors_with_coarse
        ]
        final_scored.sort(key=lambda t: t[2], reverse=True)
        ranked = [
            {"params": c, metric: s, "coarse_score": coarse_s}
            for c, coarse_s, s in final_scored[:top_k]
        ]
        return {
            "ranked": ranked,
            "num_trials": len(samples),
            "survivors": len(survivors_with_coarse),
            "metric": metric,
        }
