from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.models import ExperimentResult, PromptVersion


class ABTestManager:
    """Manages prompt versions and A/B test experiments."""

    def __init__(self, db: Session) -> None:
        self.db: Session = db

    def list_experiment_names(self) -> list[str]:
        rows = (
            self.db.query(ExperimentResult.experiment_name)
            .distinct()
            .order_by(ExperimentResult.experiment_name.asc())
            .all()
        )
        return [name for (name,) in rows if name]

    def create_version(
        self, name: str, version: str, description: str, template: str
    ) -> PromptVersion:
        pv = PromptVersion(
            name=name,
            version=version,
            description=description,
            template=template,
        )
        self.db.add(pv)
        self.db.commit()
        self.db.refresh(pv)
        return pv

    def list_versions(self) -> list[PromptVersion]:
        return (
            self.db.query(PromptVersion)
            .order_by(PromptVersion.id)
            .all()
        )

    def get_active_version(self) -> PromptVersion | None:
        return (
            self.db.query(PromptVersion)
            .filter(PromptVersion.is_active == True)  # noqa: E712
            .first()
        )

    def activate_version(self, version_id: int) -> None:
        version = self.db.get(PromptVersion, version_id)
        if version is None:
            raise ValueError(f"PromptVersion {version_id} not found")
        self.db.query(PromptVersion).update({PromptVersion.is_active: False})
        version.is_active = True
        self.db.commit()

    def select_variant(self, symbol: str, experiment_name: str) -> PromptVersion | None:
        """Select a variant deterministically based on symbol hash."""
        versions = (
            self.db.query(PromptVersion)
            .filter(PromptVersion.is_active == True)  # noqa: E712
            .filter(PromptVersion.name == experiment_name)
            .all()
        )
        if not versions:
            return None
        hash_val = int(hashlib.sha256(f"{experiment_name}:{symbol}".encode()).hexdigest(), 16)
        idx = hash_val % len(versions)
        return versions[idx]

    def record_result(
        self,
        *,
        experiment_name: str,
        variant_name: str,
        interaction_id: int | None = None,
        order_action: str = "NONE",
        predicted_direction: str = "",
        actual_pnl: float = 0.0,
        was_profitable: bool | None = None,
    ) -> ExperimentResult:
        result = ExperimentResult(
            experiment_name=experiment_name,
            variant_name=variant_name,
            interaction_id=interaction_id,
            order_action=order_action,
            predicted_direction=predicted_direction,
            actual_pnl=actual_pnl,
            was_profitable=was_profitable,
        )
        self.db.add(result)
        self.db.commit()
        self.db.refresh(result)
        return result

    def get_experiment_summary(self, experiment_name: str) -> list[dict[str, Any]]:
        results = (
            self.db.query(ExperimentResult)
            .filter(ExperimentResult.experiment_name == experiment_name)
            .all()
        )
        by_variant: dict[str, list[ExperimentResult]] = {}
        for r in results:
            by_variant.setdefault(r.variant_name, []).append(r)

        summary = []
        for variant_name, items in by_variant.items():
            total = len(items)
            profitable = sum(1 for i in items if i.was_profitable)
            avg_pnl = sum(i.actual_pnl for i in items) / total if total > 0 else 0.0
            summary.append({
                "variant_name": variant_name,
                "total_count": total,
                "profitable_count": profitable,
                "avg_pnl": avg_pnl,
                "win_rate": profitable / total if total > 0 else 0.0,
            })
        return summary
