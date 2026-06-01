from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_prefix="AUTO_TRADE_",
        populate_by_name=True,
        extra="ignore",
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

    audit_request_summary_limit: int = Field(
        default=2048,
        validation_alias="AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT",
    )

    broker_retry_max: int = Field(default=3, validation_alias="AUTO_TRADE_BROKER_RETRY_MAX")
    broker_quote_retry_max: int = Field(default=1, validation_alias="AUTO_TRADE_BROKER_QUOTE_RETRY_MAX")
    broker_retry_base_ms: int = Field(default=1000, validation_alias="AUTO_TRADE_BROKER_RETRY_BASE_MS")

    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_api_url: str = "https://api.deepseek.com/v1/chat/completions"
    deepseek_model: str = Field(default="deepseek-v4-flash", validation_alias="DEEPSEEK_MODEL")
    deepseek_reasoning_effort: Literal["high", "max"] = Field(
        default="max",
        validation_alias="DEEPSEEK_REASONING_EFFORT",
    )
    deepseek_max_tokens: int = Field(default=262144, validation_alias="DEEPSEEK_MAX_TOKENS")
    deepseek_thinking_type: Literal["enabled", "disabled"] = Field(
        default="enabled",
        validation_alias="DEEPSEEK_THINKING_TYPE",
    )
    llm_interval_cron_minutes: int = 2
    llm_interval_volatility_threshold_pct: float = 1.0
    llm_min_confidence: float = 0.7
    llm_max_stripe_width_pct: float = 8.0
    llm_experiment_name: str = Field(default="", validation_alias="AUTO_TRADE_LLM_EXPERIMENT_NAME")
    min_exit_profit_pct: float = 0.2
    engine_cooldown_seconds: int = Field(default=60, ge=0, le=3600, validation_alias="AUTO_TRADE_ENGINE_COOLDOWN_SECONDS")

    default_strategy: dict[str, Any] = Field(default_factory=lambda: {
        "symbol": "",
        "market": "US",
        "buy_low": 0.0,
        "sell_high": 0.0,
        "short_selling": False,
    })

    default_risk: dict[str, Any] = Field(default_factory=lambda: {
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
