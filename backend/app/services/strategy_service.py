from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import RuntimeState, StrategyConfig


class StrategyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_config(self) -> StrategyConfig:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()
            self.db.add(config)
            self.db.commit()
        return config

    def update_config(self, data: dict) -> StrategyConfig:
        config = self.db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if config is None:
            config = StrategyConfig()

        updatable_fields = [
            "symbol", "market", "buy_low", "sell_high",
            "short_selling", "max_daily_loss", "max_consecutive_losses", "sct_key",
        ]
        for field in updatable_fields:
            if field in data:
                setattr(config, field, data[field])

        config.updated_at = datetime.now(timezone.utc)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        return config

    def get_runtime_state(self) -> RuntimeState:
        state = self.db.query(RuntimeState).order_by(RuntimeState.id.desc()).first()
        if state is None:
            state = RuntimeState()
            self.db.add(state)
            self.db.commit()
        return state

    def update_runtime_state(self, **kwargs: object) -> RuntimeState:
        state = self.get_runtime_state()
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.updated_at = datetime.now(timezone.utc)
        self.db.add(state)
        self.db.commit()
        self.db.refresh(state)
        return state
