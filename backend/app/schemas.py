from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class StrategyConfigSchema(BaseModel):
    symbol: str = Field(default="", max_length=50)
    market: str = Field(default="US")
    buy_low: Optional[float] = Field(default=None, gt=0)
    sell_high: Optional[float] = Field(default=None, gt=0)
    short_selling: bool = Field(default=False)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_consecutive_losses: int = Field(default=3, ge=1)
    sct_key: str = Field(default="", max_length=200)

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("US", "HK"):
            raise ValueError("market must be US or HK")
        return v

    @field_validator("sell_high")
    @classmethod
    def validate_sell_high(cls, v: Optional[float], info: Any) -> Optional[float]:
        if v is None:
            return v
        buy_low = info.data.get("buy_low")
        if buy_low is not None and v <= buy_low:
            raise ValueError("sell_high must be greater than buy_low")
        return v


class StrategyResponse(BaseModel):
    id: int
    symbol: str
    market: str
    buy_low: float
    sell_high: float
    short_selling: bool
    max_daily_loss: float
    max_consecutive_losses: int
    sct_key: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class StatusResponse(BaseModel):
    engine_state: str
    paused: bool
    kill_switch: bool
    daily_pnl: float
    consecutive_losses: int
    last_price: float
    last_trigger_price: float
    last_trigger_at: Optional[datetime]

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: int
    broker_order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str
    created_at: datetime
    filled_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ControlRequest(BaseModel):
    reason: str = Field(default="manual")


class MessageResponse(BaseModel):
    message: str
