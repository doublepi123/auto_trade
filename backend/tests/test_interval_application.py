from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import database
from app.models import StrategyConfig
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService


database.init_db()


class TestIntervalApplicationService:
    def _get_db(self):
        return database.SessionLocal()

    def _cleanup(self) -> None:
        db = self._get_db()
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _create_config(self, db: Session) -> StrategyConfig:
        svc = StrategyService(db)
        config, _diff = svc.update_config({
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

    def test_apply_direct_rejects_interval_narrower_than_min_profit_amount(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.min_profit_amount = 1.5
        db.commit()

        result = service.apply_direct_suggestion(
            db,
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 199.5,
                "suggested_sell_high": 200.5,
                "confidence_score": 0.85,
            },
        )

        db.refresh(config)
        assert result["success"] is False
        assert result["applied"] is False
        assert "minimum profit" in result["reason"].lower()
        assert config.buy_low == 180.0
        assert config.sell_high == 220.0

    def test_apply_direct_uses_reference_quantity_for_min_profit_amount(self, service: IntervalApplicationService) -> None:
        self._cleanup()
        db = self._get_db()
        config = self._create_config(db)
        config.min_profit_amount = 1.5
        db.commit()

        result = service.apply_direct_suggestion(
            db,
            current_price=200.0,
            suggestion={
                "suggested_buy_low": 199.75,
                "suggested_sell_high": 200.25,
                "confidence_score": 0.85,
            },
            reference_quantity=10,
        )

        db.refresh(config)
        assert result["success"] is True
        assert result["applied"] is True
        assert config.buy_low == 199.75
        assert config.sell_high == 200.25

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
        """LONG 状态允许 LLM 下调 buy_low 实现追价加仓。

        参见 docs/Roadmap.md '迭代 0 / 0.2 渐进式平滑过渡策略' 与 'P7'' 段：
        2026-05-25 决议保留此追价行为，规则被文档化为方案 B。
        如未来改回 "持仓状态下只放宽不收紧"，需同步更新 Roadmap。
        """
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
