from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AUTO_TRADE_")

    env: str = "dev"
    database_url: str = "sqlite:///data/auto_trade.db"

    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""

    sct_key: str = ""

    default_strategy: dict = Field(default_factory=lambda: {
        "symbol": "",
        "market": "US",
        "buy_low": 0.0,
        "sell_high": 0.0,
        "short_selling": False,
    })

    default_risk: dict = Field(default_factory=lambda: {
        "max_daily_loss": 5000.0,
        "max_consecutive_losses": 3,
    })

    def ensure_data_dir(self) -> None:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_data_dir()
