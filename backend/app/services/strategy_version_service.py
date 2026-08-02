from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import StrategyConfig, StrategyParamVersion

# Columns captured in each version snapshot (the tunable scalar params).
_VERSIONED_COLUMNS = (
    "symbol", "market", "buy_low", "sell_high", "short_selling",
    "min_profit_amount", "auto_resume_minutes", "max_daily_loss",
    "max_drawdown_amount",
    "max_consecutive_losses", "fee_rate_us", "fee_rate_hk",
    "min_repricing_pct", "llm_action_cooldown_seconds",
    "trading_session_mode", "margin_safety_factor",
    "allow_position_addons", "max_position_quantity", "max_position_notional",
    "max_risk_per_trade", "stop_loss_pct", "max_holding_minutes",
    "entry_cutoff_minutes_before_close", "flatten_minutes_before_close",
    "llm_order_execution_enabled",
    "report_schedule_enabled", "report_schedule_interval_hours", "report_schedule_symbol",
)


class StrategyVersionService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _snapshot(self, config: StrategyConfig) -> dict[str, Any]:
        return {col: getattr(config, col) for col in _VERSIONED_COLUMNS}

    def record_version(self, config: StrategyConfig, actor_hash: str | None = None) -> StrategyParamVersion:
        row = StrategyParamVersion(
            params_json=json.dumps(self._snapshot(config), default=str),
            actor_hash=actor_hash,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def list_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            self.db.query(StrategyParamVersion)
            .order_by(StrategyParamVersion.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "actor_hash": r.actor_hash,
                "params": json.loads(r.params_json),
            }
            for r in rows
        ]

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        row = self.db.query(StrategyParamVersion).filter_by(id=version_id).first()
        if row is None:
            return None
        return json.loads(row.params_json)

    def load_version_pair(
        self,
        from_version_id: int,
        to_version_id: int,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Load two version snapshots for diffing.

        Returns ``(from_params, to_params)`` or ``None`` if either ID is
        missing. Read-only: never writes, flushes, or commits. The caller
        maps a ``None`` result to a side-specific 404.
        """
        from_row = self.db.query(StrategyParamVersion).filter_by(id=from_version_id).first()
        if from_row is None:
            return None
        to_row = self.db.query(StrategyParamVersion).filter_by(id=to_version_id).first()
        if to_row is None:
            return None
        return json.loads(from_row.params_json), json.loads(to_row.params_json)


def build_version_diff(
    from_params: dict[str, Any],
    to_params: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Compare two version snapshots over ``_VERSIONED_COLUMNS`` only.

    Unknown stored JSON keys are ignored. Iteration order follows the
    ``_VERSIONED_COLUMNS`` tuple so output is deterministic.

    - ``added``: missing from source / present in target.
    - ``removed``: present in source / missing in target.
    - ``changed``: present in both and unequal. Type is compared first
      (``1`` vs ``1.0`` differ because ``int`` != ``float``), then value;
      this preserves JSON type and null transitions.
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for col in _VERSIONED_COLUMNS:
        in_from = col in from_params
        in_to = col in to_params
        if in_from and not in_to:
            removed.append({"field": col, "from_value": from_params[col], "to_value": None})
        elif in_to and not in_from:
            added.append({"field": col, "from_value": None, "to_value": to_params[col]})
        elif in_from and in_to:
            from_val = from_params[col]
            to_val = to_params[col]
            # bool is a subclass of int in Python; compare exact types so
            # True != 1, and so int 1 vs float 1.0 are distinct snapshots.
            if type(from_val) is not type(to_val) or from_val != to_val:
                changed.append({"field": col, "from_value": from_val, "to_value": to_val})

    return {"added": added, "removed": removed, "changed": changed}
