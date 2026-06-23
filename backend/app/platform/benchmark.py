from __future__ import annotations

from typing import Any

__all__ = ["BenchmarkAnalytics"]


def _returns(equity: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        out.append((equity[i] / prev - 1.0) if prev != 0 else 0.0)
    return out


class BenchmarkAnalytics:
    """相对绩效（参考 pyfolio benchmark / empyrical alpha/beta/information_ratio）。"""

    def __init__(self, periods_per_year: int = 252) -> None:
        self.periods_per_year = periods_per_year

    def alpha_beta(self, strategy: list[float], benchmark: list[float]) -> dict[str, Any]:
        sr = _returns(strategy)
        br = _returns(benchmark)
        n = min(len(sr), len(br))
        if n < 2:
            return {
                "beta": 0.0,
                "alpha": 0.0,
                "correlation": 0.0,
                "tracking_error": 0.0,
                "information_ratio": 0.0,
            }
        sr = sr[:n]
        br = br[:n]
        ms = sum(sr) / n
        mb = sum(br) / n
        cov = sum((s - ms) * (b - mb) for s, b in zip(sr, br)) / n
        var_b = sum((b - mb) ** 2 for b in br) / n
        var_s = sum((s - ms) ** 2 for s in sr) / n
        beta = (cov / var_b) if var_b > 0 else 0.0
        alpha = (ms - beta * mb) * self.periods_per_year  # annualized
        correlation = (cov / ((var_s * var_b) ** 0.5)) if (var_s > 0 and var_b > 0) else 0.0
        diffs = [s - b for s, b in zip(sr, br)]
        md = sum(diffs) / n
        tracking_var = sum((d - md) ** 2 for d in diffs) / n
        tracking_error = (tracking_var ** 0.5) * (self.periods_per_year ** 0.5)
        information_ratio = ((md * self.periods_per_year) / tracking_error) if tracking_error > 0 else 0.0
        return {
            "beta": beta,
            "alpha": alpha,
            "correlation": correlation,
            "tracking_error": tracking_error,
            "information_ratio": information_ratio,
        }

    def capture_ratios(self, strategy: list[float], benchmark: list[float]) -> dict[str, Any]:
        sr = _returns(strategy)
        br = _returns(benchmark)
        n = min(len(sr), len(br))
        if n == 0:
            return {"up_capture": 0.0, "down_capture": 0.0}
        sr = sr[:n]
        br = br[:n]
        up_b = [b for b in br if b > 0]
        down_b = [b for b in br if b < 0]
        up_s = [s for s, b in zip(sr, br) if b > 0]
        down_s = [s for s, b in zip(sr, br) if b < 0]
        up_capture = ((sum(up_s) / len(up_s)) / (sum(up_b) / len(up_b))) if up_b else 0.0
        down_capture = ((sum(down_s) / len(down_s)) / (sum(down_b) / len(down_b))) if down_b else 0.0
        return {"up_capture": up_capture, "down_capture": down_capture}

    def relative(self, strategy: list[float], benchmark: list[float]) -> dict[str, Any]:
        result = self.alpha_beta(strategy, benchmark)
        result.update(self.capture_ratios(strategy, benchmark))
        result["strategy_return"] = (
            (strategy[-1] / strategy[0] - 1.0) if len(strategy) >= 2 and strategy[0] > 0 else 0.0
        )
        result["benchmark_return"] = (
            (benchmark[-1] / benchmark[0] - 1.0) if len(benchmark) >= 2 and benchmark[0] > 0 else 0.0
        )
        result["excess_return"] = result["strategy_return"] - result["benchmark_return"]
        return result
