"""Strategy presets — named param snapshots for one-click re-application."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import StrategyPreset
from app.schemas import StrategyPresetOut


def _parse_params(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class StrategyPresetService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def create(self, name: str, params: dict[str, Any]) -> StrategyPresetOut:
        preset = StrategyPreset(
            name=name.strip(),
            params_json=json.dumps(params, ensure_ascii=False),
        )
        self._db.add(preset)
        self._db.commit()
        self._db.refresh(preset)
        return self._to_out(preset)

    def list_presets(self) -> list[StrategyPresetOut]:
        rows = self._db.scalars(select(StrategyPreset).order_by(desc(StrategyPreset.created_at)))
        return [self._to_out(r) for r in rows]

    def get(self, preset_id: int) -> StrategyPresetOut | None:
        preset = self._db.get(StrategyPreset, preset_id)
        return self._to_out(preset) if preset is not None else None

    def get_params(self, preset_id: int) -> dict[str, Any] | None:
        preset = self._db.get(StrategyPreset, preset_id)
        if preset is None:
            return None
        return _parse_params(preset.params_json)

    def delete(self, preset_id: int) -> bool:
        preset = self._db.get(StrategyPreset, preset_id)
        if preset is None:
            return False
        self._db.delete(preset)
        self._db.commit()
        return True

    @staticmethod
    def _to_out(preset: StrategyPreset) -> StrategyPresetOut:
        return StrategyPresetOut(
            id=preset.id,
            name=preset.name,
            params=_parse_params(preset.params_json),
            created_at=preset.created_at,
        )
