from __future__ import annotations

import random
from typing import Any

__all__ = ["MonteCarloAnalyzer"]


class MonteCarloAnalyzer:
    """蒙特卡洛稳健性（参考 vectorbt / QuantStats rolling）：对每笔交易 PnL 做有放回重采样，
    生成多条等价历史，输出最终 PnL 分位、最大回撤分布、破产/亏损概率与分位路径。

    使用注入 seed 的 random.Random 以保证确定性。输入 trade_pnls 为每笔交易的绝对 PnL（正/负）。
    """

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def analyze(
        self,
        trade_pnls: list[float],
        num_simulations: int = 1000,
        horizon: int | None = None,
        ruin_threshold: float | None = None,
    ) -> dict[str, Any]:
        if not trade_pnls:
            return {
                "num_simulations": num_simulations,
                "final_pnl": {"p5": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p95": 0.0},
                "mean_final_pnl": 0.0,
                "prob_loss": 0.0,
                "prob_ruin": 0.0,
                "max_drawdown": {"p50": 0.0, "p95": 0.0},
                "percentile_paths": [],
            }
        rng = random.Random(self.seed)
        horizon = horizon or len(trade_pnls)
        if horizon < 1:
            horizon = 1
        finals: list[float] = []
        max_dds: list[float] = []
        sample_paths: list[list[float]] = []
        sample_target = min(100, num_simulations)
        for i in range(num_simulations):
            cum = 0.0
            peak = 0.0
            max_dd = 0.0
            path: list[float] = []
            for _ in range(horizon):
                r = rng.choice(trade_pnls)
                cum += r
                if cum > peak:
                    peak = cum
                dd = peak - cum
                if dd > max_dd:
                    max_dd = dd
                if i < sample_target:
                    path.append(cum)
            finals.append(cum)
            max_dds.append(max_dd)
            if i < sample_target:
                sample_paths.append(path)

        sorted_finals = sorted(finals)
        sorted_dds = sorted(max_dds)

        def percentile(sorted_list: list[float], p: float) -> float:
            if not sorted_list:
                return 0.0
            k = max(0, min(len(sorted_list) - 1, int(round(p * (len(sorted_list) - 1)))))
            return sorted_list[k]

        mean_final = sum(finals) / len(finals)
        prob_loss = sum(1 for f in finals if f < 0) / len(finals)
        if ruin_threshold is None:
            prob_ruin = 0.0
        else:
            prob_ruin = sum(1 for f in finals if f <= ruin_threshold) / len(finals)

        # build percentile paths from the sampled paths (by index across paths)
        path_len = min((len(p) for p in sample_paths), default=0)
        percentile_paths: list[dict[str, Any]] = []
        if path_len > 0:
            for pct in (0.5,):
                line: list[float] = []
                for idx in range(path_len):
                    col = sorted(p[idx] for p in sample_paths if len(p) > idx)
                    line.append(percentile(col, pct))
                percentile_paths.append({"percentile": pct, "path": line})

        return {
            "num_simulations": num_simulations,
            "horizon": horizon,
            "final_pnl": {
                "p5": percentile(sorted_finals, 0.05),
                "p25": percentile(sorted_finals, 0.25),
                "p50": percentile(sorted_finals, 0.50),
                "p75": percentile(sorted_finals, 0.75),
                "p95": percentile(sorted_finals, 0.95),
            },
            "mean_final_pnl": mean_final,
            "prob_loss": prob_loss,
            "prob_ruin": prob_ruin,
            "max_drawdown": {
                "p50": percentile(sorted_dds, 0.50),
                "p95": percentile(sorted_dds, 0.95),
            },
            "percentile_paths": percentile_paths,
        }
