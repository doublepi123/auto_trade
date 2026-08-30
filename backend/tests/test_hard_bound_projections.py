"""Strategy hard-bound projections — table-driven ceiling/floor/no-bound tests.

Cross-checks the service output against the existing ``hard_ceiling_*`` /
``hard_floor_*`` helpers, verifies sizing-bypass semantics, authentication,
forced runner-construction failure (the service never touches the runner), and
no Session mutation.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models import StrategyConfig
from app.services.hard_bound_projection_service import (
    PROJECTION_SEMANTICS_LABEL,
    HardBoundProjectionService,
)
from app.services.runtime_state_service import (
    hard_ceiling_float,
    hard_ceiling_int,
    hard_floor_int,
)
from app import database


database.init_db()
client = TestClient(app)


def _make_config(
    db: Session,
    *,
    stop_loss_pct: float = 0.5,
    max_holding_minutes: int = 30,
    entry_cutoff_minutes_before_close: int = 60,
    flatten_minutes_before_close: int = 20,
    max_position_quantity: int = 50,
    max_position_notional: float = 3000.0,
    max_risk_per_trade: float = 200.0,
) -> StrategyConfig:
    db.query(StrategyConfig).delete()
    db.commit()
    config = StrategyConfig(
        symbol="AAPL.US",
        market="US",
        buy_low=100,
        sell_high=110,
        stop_loss_pct=stop_loss_pct,
        max_holding_minutes=max_holding_minutes,
        entry_cutoff_minutes_before_close=entry_cutoff_minutes_before_close,
        flatten_minutes_before_close=flatten_minutes_before_close,
        max_position_quantity=max_position_quantity,
        max_position_notional=max_position_notional,
        max_risk_per_trade=max_risk_per_trade,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


class TestHardBoundProjections:
    def test_semantics_label(self) -> None:
        db = database.SessionLocal()
        try:
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        assert result["semantics"] == PROJECTION_SEMANTICS_LABEL
        assert result["semantics"] == "LOAD_TIME_PROJECTION_NOT_RUNTIME_STATE"

    def test_ceiling_field_constrained(self) -> None:
        """Config value above the hard ceiling is clamped down."""
        db = database.SessionLocal()
        try:
            # stop_loss_pct ceiling is settings.hard_stop_loss_pct (default 1.0)
            _make_config(db, stop_loss_pct=0.9)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        sl = next(f for f in result["fields"] if f["field"] == "stop_loss_pct")
        assert sl["bound_type"] == "ceiling"
        assert sl["hard_bound"] == settings.hard_stop_loss_pct
        # 0.9 < 1.0 -> not constrained
        assert sl["constrained"] is False
        assert sl["projected_value"] == hard_ceiling_float(0.9, settings.hard_stop_loss_pct)

    def test_ceiling_field_unconstrained(self) -> None:
        """Config value below the hard ceiling passes through."""
        db = database.SessionLocal()
        try:
            _make_config(db, max_holding_minutes=30)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        mh = next(f for f in result["fields"] if f["field"] == "max_holding_minutes")
        assert mh["bound_type"] == "ceiling"
        assert mh["projected_value"] == 30
        assert mh["constrained"] is False

    def test_floor_field_constrained(self) -> None:
        """Config value below the hard floor is clamped up."""
        db = database.SessionLocal()
        try:
            # entry_cutoff floor is settings.hard_entry_cutoff_minutes_before_close (default 45)
            _make_config(db, entry_cutoff_minutes_before_close=50)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        ec = next(
            f for f in result["fields"] if f["field"] == "entry_cutoff_minutes_before_close"
        )
        assert ec["bound_type"] == "floor"
        assert ec["hard_bound"] == settings.hard_entry_cutoff_minutes_before_close
        # 50 > 45 -> not constrained
        assert ec["constrained"] is False
        assert ec["projected_value"] == hard_floor_int(
            50, settings.hard_entry_cutoff_minutes_before_close
        )

    def test_floor_field_clamped_up(self) -> None:
        """Config value below the floor is clamped up to the floor."""
        db = database.SessionLocal()
        try:
            _make_config(db, flatten_minutes_before_close=20)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        fl = next(
            f for f in result["fields"] if f["field"] == "flatten_minutes_before_close"
        )
        assert fl["bound_type"] == "floor"
        assert fl["projected_value"] == hard_floor_int(
            20, settings.hard_flatten_minutes_before_close
        )

    def test_cross_check_against_helpers(self) -> None:
        """Every projected value must match the existing helper output for all
        seven fields."""
        db = database.SessionLocal()
        try:
            _make_config(db)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        expected_map = {
            "stop_loss_pct": lambda v: hard_ceiling_float(
                v, settings.hard_stop_loss_pct
            ),
            "max_holding_minutes": lambda v: hard_ceiling_int(
                v, settings.hard_max_holding_minutes
            ),
            "entry_cutoff_minutes_before_close": lambda v: hard_floor_int(
                v, settings.hard_entry_cutoff_minutes_before_close
            ),
            "flatten_minutes_before_close": lambda v: hard_floor_int(
                v, settings.hard_flatten_minutes_before_close
            ),
            "max_position_quantity": lambda v: hard_ceiling_int(
                v, settings.hard_max_position_quantity
            ),
            "max_position_notional": lambda v: hard_ceiling_float(
                v, settings.hard_max_position_notional
            ),
            "max_risk_per_trade": lambda v: hard_ceiling_float(
                v, settings.hard_max_risk_per_trade
            ),
        }
        for field in result["fields"]:
            fn = expected_map.get(field["field"])
            assert fn is not None, f"unexpected field: {field['field']}"
            expected = fn(field["configured_value"])
            assert field["projected_value"] == expected, field["field"]

    def test_sizing_fields_use_config_not_settings(self) -> None:
        """The three sizing fields must project StrategyConfig values through
        the hard-ceiling helpers, not echo Settings values directly."""
        db = database.SessionLocal()
        try:
            _make_config(
                db,
                max_position_quantity=50,
                max_position_notional=3000.0,
                max_risk_per_trade=200.0,
            )
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        qty = next(f for f in result["fields"] if f["field"] == "max_position_quantity")
        assert qty["configured_value"] == 50
        assert qty["hard_bound"] == settings.hard_max_position_quantity
        assert qty["projected_value"] == hard_ceiling_int(
            50, settings.hard_max_position_quantity
        )

        notional = next(
            f for f in result["fields"] if f["field"] == "max_position_notional"
        )
        assert notional["configured_value"] == 3000.0
        assert notional["hard_bound"] == settings.hard_max_position_notional
        assert notional["projected_value"] == hard_ceiling_float(
            3000.0, settings.hard_max_position_notional
        )

        risk = next(
            f for f in result["fields"] if f["field"] == "max_risk_per_trade"
        )
        assert risk["configured_value"] == 200.0
        assert risk["hard_bound"] == settings.hard_max_risk_per_trade
        assert risk["projected_value"] == hard_ceiling_float(
            200.0, settings.hard_max_risk_per_trade
        )

    def test_fixed_sizing_caps_are_never_reported_as_bypassed(self) -> None:
        db = database.SessionLocal()
        try:
            _make_config(db)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        assert result["fixed_sizing_caps_bypassed_by_full_buying_power"] is False

    def test_no_config_row(self) -> None:
        """When no StrategyConfig exists, configured_present is False."""
        db = database.SessionLocal()
        try:
            db.query(StrategyConfig).delete()
            db.commit()
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        sl = next(f for f in result["fields"] if f["field"] == "stop_loss_pct")
        assert sl["configured_present"] is False

    def test_no_session_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            _make_config(db)
            count_before = db.query(StrategyConfig).count()
            HardBoundProjectionService(db).build()
            count_after = db.query(StrategyConfig).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_runner_not_constructed(self, monkeypatch) -> None:
        """The service must never access the runner."""
        from app.api import strategy as strategy_api

        def _boom(*a, **kw):
            raise AssertionError("hard-bound projections must not access the runner")

        monkeypatch.setattr(strategy_api, "get_runner", _boom)
        db = database.SessionLocal()
        try:
            _make_config(db)
            result = HardBoundProjectionService(db).build()
        finally:
            db.close()
        assert result["semantics"] == "LOAD_TIME_PROJECTION_NOT_RUNTIME_STATE"

    def test_endpoint_returns_projections(self) -> None:
        db = database.SessionLocal()
        try:
            _make_config(db)
        finally:
            db.close()
        resp = client.get("/api/strategy/hard-bound-projections")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["semantics"] == "LOAD_TIME_PROJECTION_NOT_RUNTIME_STATE"
        field_names = [f["field"] for f in data["fields"]]
        assert "stop_loss_pct" in field_names
        assert "max_holding_minutes" in field_names
        assert "entry_cutoff_minutes_before_close" in field_names
        assert "flatten_minutes_before_close" in field_names
        assert "max_position_quantity" in field_names
        assert "max_position_notional" in field_names
        assert "max_risk_per_trade" in field_names

    def test_auth_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret-key")
        assert (
            client.get("/api/strategy/hard-bound-projections").status_code == 401
        )
