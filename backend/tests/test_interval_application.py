from __future__ import annotations

import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_interval.db"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, StrategyConfig
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService


class TestIntervalApplicationService:
    @classmethod
    def setup_class(cls) -> None:
        engine = create_engine(os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        cls.engine = engine

    def _get_db(self) -> Session:
        return Session(bind=self.engine)

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _create_config(self, db: Session) -> StrategyConfig:
        svc = StrategyService(db)
        config = svc.update_config({
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 180.0,
            "sell_high": 220.0,
            "short_selling": False,
            "max_daily_loss": 5000.0,
            "max_consecutive_losses": 3,
        })
        return config

    @pytest.fixture
    def service(self) -> IntervalApplicationService:
        return IntervalApplicationService()

    def test_apply_flat(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="flat",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 192.0,
                "suggested_sell_high": 208.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is True
        assert result["applied"] is True
        assert result["buy_low"] == 192.0
        assert result["sell_high"] == 208.0

    def test_apply_direct_applies_both_bounds(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_direct_suggestion(
            db,
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 195.0,
                "suggested_sell_high": 205.0,
                "confidence_score": 0.85,
            },
        )

        db.refresh(config)
        assert result["success"] is True
        assert result["applied"] is True
        assert config.buy_low == 195.0
        assert config.sell_high == 205.0
        assert config.llm_applied_buy_low == 195.0
        assert config.llm_applied_sell_high == 205.0

    def test_apply_long_sell_higher(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.sell_high = 204.0
        db.commit()

        result = service.apply_suggestion(
            db,
            engine_state="long",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 196.0,
                "suggested_sell_high": 210.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is True
        assert result["applied"] is True
        assert result["sell_high"] == 210.0
        assert "LONG state" in result["reason"]

    def test_apply_long_sell_lower(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="long",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 195.0,
                "suggested_sell_high": 210.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is True
        assert result["applied"] is True
        assert result["sell_high"] == 210.0

    def test_apply_long_does_not_raise_buy_low(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.buy_low = 219.0
        config.sell_high = 224.0
        db.commit()

        result = service.apply_suggestion(
            db,
            engine_state="long",
            current_price=222.0,
            suggestion={
                "suggested_buy_low": 221.0,
                "suggested_sell_high": 226.0,
                "confidence_score": 0.8,
            },
        )

        assert result["success"] is True
        assert config.buy_low == 219.0
        assert config.sell_high == 226.0

    def test_apply_long_allows_lower_buy_low(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.buy_low = 219.0
        config.sell_high = 224.0
        db.commit()

        result = service.apply_suggestion(
            db,
            engine_state="long",
            current_price=222.0,
            suggestion={
                "suggested_buy_low": 217.0,
                "suggested_sell_high": 226.0,
                "confidence_score": 0.8,
            },
        )

        assert result["success"] is True
        assert config.buy_low == 217.0
        assert config.sell_high == 226.0

    def test_apply_short_buy_lower(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.buy_low = 198.0
        config.sell_high = 210.0
        db.commit()

        result = service.apply_suggestion(
            db,
            engine_state="short",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 195.0,
                "suggested_sell_high": 210.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is True
        assert result["applied"] is True
        assert result["buy_low"] == 195.0
        assert "SHORT state" in result["reason"]

    def test_apply_short_buy_higher(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="short",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 190.0,
                "suggested_sell_high": 205.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is True
        assert result["applied"] is True
        assert result["buy_low"] == 190.0

    def test_reject_low_confidence(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="flat",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 190.0,
                "suggested_sell_high": 230.0,
                "confidence_score": 0.5,
            },
        )

        assert result["success"] is False
        assert "confidence" in result["reason"].lower()

    def test_reject_interval_too_wide_before_state_application(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="flat",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 150.0,
                "suggested_sell_high": 205.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is False
        assert "width" in result["reason"].lower() or "%" in result["reason"]

    def test_reject_interval_too_wide(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)

        result = service.apply_suggestion(
            db,
            engine_state="flat",
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 100.0,
                "suggested_sell_high": 350.0,
                "confidence_score": 0.85,
            },
        )

        assert result["success"] is False
        assert "width" in result["reason"].lower() or "%" in result["reason"]
