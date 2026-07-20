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
