from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import ExperimentResult


class PerformanceTracker:
    """Tracks and analyzes LLM prediction performance."""

    def __init__(self, db: Session) -> None:
        self.db: Session = db

    def get_overall_stats(self, experiment_name: str) -> dict[str, Any]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        if not results:
            return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0}

        total = len(results)
        resolved = [r for r in results if r.was_profitable is not None]
        profitable = sum(1 for r in resolved if r.was_profitable)
        total_pnl = sum(r.actual_pnl for r in results)
        resolved_total = len(resolved)

        return {
            "total_trades": total,
            "win_rate": profitable / resolved_total if resolved_total > 0 else 0.0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / total,
        }

    def compare_variants(self, experiment_name: str) -> list[dict[str, Any]]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        by_variant: dict[str, list[ExperimentResult]] = {}
        for r in results:
            by_variant.setdefault(r.variant_name, []).append(r)

        comparison = []
        for variant, items in by_variant.items():
            total = len(items)
            resolved = [i for i in items if i.was_profitable is not None]
            profitable = sum(1 for i in resolved if i.was_profitable)
            total_pnl = sum(i.actual_pnl for i in items)
            resolved_total = len(resolved)
            comparison.append({
                "variant": variant,
                "total_trades": total,
                "win_rate": profitable / resolved_total if resolved_total > 0 else 0.0,
                "total_pnl": total_pnl,
                "avg_pnl": total_pnl / total if total > 0 else 0.0,
            })
        return comparison

    def get_recommendations(self, experiment_name: str) -> list[str]:
        comparison = self.compare_variants(experiment_name)
        recommendations = []

        for variant in comparison:
            if variant["total_trades"] < 10:
                recommendations.append(
                    f"变体 '{variant['variant']}' 样本不足（{variant['total_trades']} 笔），建议继续收集数据"
                )
            elif variant["win_rate"] < 0.4:
                recommendations.append(
                    f"变体 '{variant['variant']}' 胜率偏低（{variant['win_rate']:.1%}），建议优化 prompt 或停用"
                )
            elif variant["win_rate"] > 0.6:
                recommendations.append(
                    f"变体 '{variant['variant']}' 表现优秀（胜率 {variant['win_rate']:.1%}），建议设为主要版本"
                )

        if not recommendations:
            recommendations.append("所有变体表现正常，无需特别调整")

        return recommendations
