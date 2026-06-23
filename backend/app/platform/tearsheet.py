from __future__ import annotations

import csv
import io
import json
from typing import Any

from app.platform.analyzers import analyze_backtest

__all__ = ["TearsheetBuilder", "TearsheetExporter"]


class TearsheetBuilder:
    """把回测结果聚合为完整 tearsheet 字典（参考 pyfolio create_full_tearsheet / QuantStats）。"""

    def build(self, result: dict[str, Any]) -> dict[str, Any]:
        analytics = result.get("analytics", {})
        stats = result.get("stats", {})
        analyzer = analyze_backtest(result)
        equity = result.get("equity_curve", [])
        summary = {
            "total_return": analytics.get("total_return", 0.0),
            "annual_volatility": analytics.get("annual_volatility", 0.0),
            "sharpe": analytics.get("sharpe", 0.0),
            "sortino": analytics.get("sortino", 0.0),
            "max_drawdown": analytics.get("max_drawdown", 0.0),
            "calmar": analytics.get("calmar", 0.0),
            "win_rate": analytics.get("win_rate", 0.0),
            "profit_factor": analytics.get("profit_factor"),
            "num_trades": analytics.get("num_trades", 0),
            "final_nav": stats.get("final_nav", 0.0),
            "realized_pnl": stats.get("realized_pnl", 0.0),
            "num_bars": stats.get("num_bars", 0),
        }
        return {
            "summary": summary,
            "equity_curve": equity,
            "returns": analyzer["returns"],
            "drawdown": analyzer["drawdown"],
            "trades": analyzer["trades"],
        }


class TearsheetExporter:
    """Tearsheet 导出（参考 QuantStats CSV/JSON 导出）。"""

    @staticmethod
    def to_json(tearsheet: dict[str, Any]) -> str:
        def _default(o: Any) -> Any:
            if isinstance(o, float) and (o != o):  # NaN
                return None
            return str(o)

        return json.dumps(tearsheet, default=_default)

    @staticmethod
    def to_csv(tearsheet: dict[str, Any]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow(["section", "key", "value"])
        for key, value in tearsheet["summary"].items():
            writer.writerow(["summary", key, value])

        writer.writerow([])
        writer.writerow(["equity_curve"])
        writer.writerow(["timestamp", "nav"])
        for pt in tearsheet.get("equity_curve", []):
            writer.writerow([pt.get("timestamp"), pt.get("nav")])

        writer.writerow([])
        writer.writerow(["returns"])
        writer.writerow(
            ["num_periods", "cumulative_return", "best_period", "worst_period", "positive_pct"]
        )
        r = tearsheet.get("returns", {})
        writer.writerow(
            [
                r.get("num_periods"),
                r.get("cumulative_return"),
                r.get("best_period"),
                r.get("worst_period"),
                r.get("positive_pct"),
            ]
        )

        writer.writerow([])
        writer.writerow(["drawdown"])
        writer.writerow(["max_drawdown", "max_drawdown_duration"])
        d = tearsheet.get("drawdown", {})
        writer.writerow([d.get("max_drawdown"), d.get("max_drawdown_duration")])

        writer.writerow([])
        writer.writerow(["trades"])
        writer.writerow(["num_trades", "win_rate", "profit_factor", "expectancy"])
        t = tearsheet.get("trades", {})
        writer.writerow(
            [t.get("num_trades"), t.get("win_rate"), t.get("profit_factor"), t.get("expectancy")]
        )
        return buf.getvalue()
