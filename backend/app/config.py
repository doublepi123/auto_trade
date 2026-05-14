from __future__ import annotations

import logging
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_prefix="AUTO_TRADE_",
        populate_by_name=True,
    )

    env: str = "dev"
    database_url: str = "sqlite:///data/auto_trade.db"

    longbridge_app_key: str = ""
    longbridge_app_secret: str = ""
    longbridge_access_token: str = ""

    longport_app_key: str = Field(default="", validation_alias="LONGPORT_APP_KEY")
    longport_app_secret: str = Field(default="", validation_alias="LONGPORT_APP_SECRET")
    longport_access_token: str = Field(default="", validation_alias="LONGPORT_ACCESS_TOKEN")
    legacy_longbridge_app_key: str = Field(default="", validation_alias="LONGBRIDGE_APP_KEY")
    legacy_longbridge_app_secret: str = Field(default="", validation_alias="LONGBRIDGE_APP_SECRET")
    legacy_longbridge_access_token: str = Field(default="", validation_alias="LONGBRIDGE_ACCESS_TOKEN")

    sct_key: str = ""

    api_key: str = ""

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

    @model_validator(mode="after")
    def merge_longbridge_credentials(self) -> "Settings":
        self.longbridge_app_key = self.longbridge_app_key or self.longport_app_key or self.legacy_longbridge_app_key
        self.longbridge_app_secret = (
            self.longbridge_app_secret or self.longport_app_secret or self.legacy_longbridge_app_secret
        )
        self.longbridge_access_token = (
            self.longbridge_access_token or self.longport_access_token or self.legacy_longbridge_access_token
        )
        return self

    @model_validator(mode="after")
    def warn_empty_api_key(self) -> "Settings":
        if not self.api_key:
            logger.warning(
                "AUTO_TRADE_API_KEY is empty — the API is running without authentication. "
                "Set a non-empty API key via environment variable or .env file to secure the application."
            )
        return self

    def ensure_data_dir(self) -> None:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_data_dir()
