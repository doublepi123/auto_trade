"""Strategy hard-bound projections — read-only load-time projection report.

Reports the configured value, bound type, hard bound, projected value, and
constrained boolean for the seven numeric fields supported by the existing
``hard_ceiling_*`` / ``hard_floor_*`` helpers. This is a LOAD-TIME projection
derived from the persisted ``StrategyConfig`` row and the deployment ``Settings``
hard bounds — NOT runtime state. It never accesses, constructs, reloads, or
mutates the runner.
"""
from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.orm import Session

from app.config import settings
from app.models import StrategyConfig
from app.services.runtime_state_service import (
    hard_ceiling_float,
    hard_ceiling_int,
    hard_floor_int,
)

__all__ = ["HardBoundProjectionService", "PROJECTION_SEMANTICS_LABEL"]

PROJECTION_SEMANTICS_LABEL = "LOAD_TIME_PROJECTION_NOT_RUNTIME_STATE"

_BoundType = Literal["ceiling", "floor"]


def _project_float_ceiling(
    configured: Any, hard_bound: float
) -> tuple[float, str, float, bool]:
    projected = hard_ceiling_float(configured, hard_bound)
    constrained = projected < float(configured) if _is_finite_number(configured) else True
    return float(configured) if _is_finite_number(configured) else 0.0, "ceiling", hard_bound, constrained


def _project_int_ceiling(
    configured: Any, hard_bound: int
) -> tuple[int, str, int, bool]:
    projected = hard_ceiling_int(configured, hard_bound)
    constrained = projected < int(configured) if _is_int(configured) else True
    return int(configured) if _is_int(configured) else 0, "ceiling", hard_bound, constrained


def _project_int_floor(
    configured: Any, hard_bound: int
) -> tuple[int, str, int, bool]:
    projected = hard_floor_int(configured, hard_bound)
    constrained = projected > int(configured) if _is_int(configured) else True
    return int(configured) if _is_int(configured) else 0, "floor", hard_bound, constrained


def _is_finite_number(value: Any) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    import math

    return math.isfinite(v) and v > 0


def _is_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


class HardBoundProjectionService:
    """Read-only load-time hard-bound projection report.

    Derives projections from the persisted ``StrategyConfig`` and deployment
    ``Settings`` using the SAME ``hard_ceiling_*`` / ``hard_floor_*`` helpers
    the runtime uses at load time. Never accesses or mutates the runner.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def build(self) -> dict[str, Any]:
        config = (
            self._db.query(StrategyConfig)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        fields: list[dict[str, Any]] = []

        # 1. stop_loss_pct — ceiling
        cfg_val = getattr(config, "stop_loss_pct", None) if config else None
        c, bt, hb, con = _project_float_ceiling(cfg_val, settings.hard_stop_loss_pct)
        fields.append(self._row("stop_loss_pct", cfg_val, c, bt, hb, con))

        # 2. max_holding_minutes — ceiling
        cfg_val = getattr(config, "max_holding_minutes", None) if config else None
        c, bt, hb, con = _project_int_ceiling(cfg_val, settings.hard_max_holding_minutes)
        fields.append(self._row("max_holding_minutes", cfg_val, c, bt, hb, con))

        # 3. entry_cutoff_minutes_before_close — floor
        cfg_val = getattr(config, "entry_cutoff_minutes_before_close", None) if config else None
        c, bt, hb, con = _project_int_floor(
            cfg_val, settings.hard_entry_cutoff_minutes_before_close
        )
        fields.append(self._row("entry_cutoff_minutes_before_close", cfg_val, c, bt, hb, con))

        # 4. flatten_minutes_before_close — floor
        cfg_val = getattr(config, "flatten_minutes_before_close", None) if config else None
        c, bt, hb, con = _project_int_floor(
            cfg_val, settings.hard_flatten_minutes_before_close
        )
        fields.append(self._row("flatten_minutes_before_close", cfg_val, c, bt, hb, con))

        # 5. hard_max_position_quantity — ceiling (settings-level, config has no equivalent)
        fields.append(
            self._settings_ceiling_int_row(
                "hard_max_position_quantity", settings.hard_max_position_quantity
            )
        )

        # 6. hard_max_position_notional — ceiling
        fields.append(
            self._settings_ceiling_float_row(
                "hard_max_position_notional", settings.hard_max_position_notional
            )
        )

        # 7. hard_max_risk_per_trade — ceiling
        fields.append(
            self._settings_ceiling_float_row(
                "hard_max_risk_per_trade", settings.hard_max_risk_per_trade
            )
        )

        return {
            "semantics": PROJECTION_SEMANTICS_LABEL,
            "fields": fields,
            "fixed_sizing_caps_bypassed_by_full_buying_power": bool(
                settings.full_buying_power_usage_enabled
            ),
        }

    @staticmethod
    def _row(
        name: str,
        configured_raw: Any,
        configured_value: float | int,
        bound_type: str,
        hard_bound: float | int,
        constrained: bool,
    ) -> dict[str, Any]:
        return {
            "field": name,
            "configured_value": configured_value,
            "configured_present": configured_raw is not None,
            "bound_type": bound_type,
            "hard_bound": hard_bound,
            "projected_value": (
                hard_ceiling_float(configured_raw, float(hard_bound))
                if bound_type == "ceiling" and isinstance(hard_bound, float)
                else hard_ceiling_int(configured_raw, int(hard_bound))
                if bound_type == "ceiling"
                else hard_floor_int(configured_raw, int(hard_bound))
            ),
            "constrained": constrained,
        }

    @staticmethod
    def _settings_ceiling_int_row(name: str, value: int) -> dict[str, Any]:
        return {
            "field": name,
            "configured_value": value,
            "configured_present": True,
            "bound_type": "ceiling",
            "hard_bound": value,
            "projected_value": value,
            "constrained": False,
        }

    @staticmethod
    def _settings_ceiling_float_row(name: str, value: float) -> dict[str, Any]:
        return {
            "field": name,
            "configured_value": value,
            "configured_present": True,
            "bound_type": "ceiling",
            "hard_bound": value,
            "projected_value": value,
            "constrained": False,
        }
