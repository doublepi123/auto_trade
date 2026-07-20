from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
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

    @field_validator("env")
    @classmethod
    def _validate_env(cls, v: str) -> str:
        allowed = {"dev", "test", "prod"}
        if v not in allowed:
            raise ValueError(f"env must be one of {allowed}, got '{v}'")
        return v

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, v: Any) -> str:
        return str(v).strip().lower()

    @model_validator(mode="after")
    def _warn_non_prod_with_api_key(self) -> "Settings":
        if self.api_key and self.env != "prod":
            logger.warning(
                "AUTO_TRADE_API_KEY is set but env='%s' (not 'prod'). "
                "Consider setting env='prod' for production deployments.",
                self.env,
            )
        return self
    database_url: str = "sqlite:///./data/auto_trade.db"

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
    notify_dedup_window_seconds: float = Field(
        default=300.0,
        ge=0,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_NOTIFY_DEDUP_WINDOW_SECONDS",
    )

    api_key: str = ""

    audit_request_summary_limit: int = Field(
        default=2048,
        validation_alias="AUTO_TRADE_AUDIT_REQUEST_SUMMARY_LIMIT",
    )
    audit_trusted_proxy_cidrs: str = Field(
        default="",
        validation_alias="AUTO_TRADE_AUDIT_TRUSTED_PROXY_CIDRS",
    )

    broker_retry_max: int = Field(default=3, validation_alias="AUTO_TRADE_BROKER_RETRY_MAX")
    broker_quote_retry_max: int = Field(default=1, validation_alias="AUTO_TRADE_BROKER_QUOTE_RETRY_MAX")
    broker_retry_base_ms: int = Field(default=1000, validation_alias="AUTO_TRADE_BROKER_RETRY_BASE_MS")

    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_api_url: str = "https://api.deepseek.com/v1/chat/completions"
    deepseek_model: str = Field(default="deepseek-v4-pro", validation_alias="DEEPSEEK_MODEL")
    deepseek_reasoning_effort: Literal["high", "max"] = Field(
        default="max",
        validation_alias="DEEPSEEK_REASONING_EFFORT",
    )
    deepseek_max_tokens: int = Field(default=262144, validation_alias="DEEPSEEK_MAX_TOKENS")
    deepseek_thinking_type: Literal["enabled", "disabled"] = Field(
        default="enabled",
        validation_alias="DEEPSEEK_THINKING_TYPE",
    )
    llm_provider: Literal["deepseek", "minimax"] = Field(
        default="deepseek",
        validation_alias="AUTO_TRADE_LLM_PROVIDER",
    )
    minimax_api_key: str = Field(default="", validation_alias="MINIMAX_API_KEY")
    minimax_base_url: str = Field(
        default="https://api.minimaxi.com/v1",
        validation_alias="MINIMAX_BASE_URL",
    )
    minimax_api_url: str = Field(
        default="",
        validation_alias="MINIMAX_API_URL",
    )
    minimax_model: str = Field(default="MiniMax-M3", validation_alias="MINIMAX_MODEL")
    minimax_thinking_type: Literal["adaptive", "disabled"] = Field(
        default="adaptive",
        validation_alias="MINIMAX_THINKING_TYPE",
    )
    minimax_max_completion_tokens: int = Field(
        default=8192,
        ge=1,
        le=131072,
        validation_alias="MINIMAX_MAX_COMPLETION_TOKENS",
    )
    llm_interval_cron_minutes: int = Field(
        default=2,
        validation_alias="AUTO_TRADE_LLM_INTERVAL_MINUTES",
    )
    llm_max_symbols_per_cycle: int = Field(
        default=1,
        ge=1,
        le=100,
        validation_alias="AUTO_TRADE_LLM_MAX_SYMBOLS_PER_CYCLE",
    )
    llm_max_analyses_per_hour: int = Field(
        default=30,
        ge=1,
        le=10000,
        validation_alias="AUTO_TRADE_LLM_MAX_ANALYSES_PER_HOUR",
    )
    llm_interaction_retention_days: int = Field(
        default=90,
        ge=0,
        le=3650,
        validation_alias="AUTO_TRADE_LLM_INTERACTION_RETENTION_DAYS",
    )
    llm_no_action_retention_days: int = Field(
        default=14,
        ge=0,
        le=3650,
        validation_alias="AUTO_TRADE_LLM_NO_ACTION_RETENTION_DAYS",
    )
    llm_context_snapshot_max_bytes: int = Field(
        default=16384,
        ge=2048,
        le=1048576,
        validation_alias="AUTO_TRADE_LLM_CONTEXT_SNAPSHOT_MAX_BYTES",
    )
    llm_storage_maintenance_interval_minutes: int = Field(
        default=360,
        ge=5,
        le=10080,
        validation_alias="AUTO_TRADE_LLM_STORAGE_MAINTENANCE_INTERVAL_MINUTES",
    )
    llm_storage_maintenance_batch_size: int = Field(
        default=250,
        ge=10,
        le=5000,
        validation_alias="AUTO_TRADE_LLM_STORAGE_MAINTENANCE_BATCH_SIZE",
    )
    llm_interval_volatility_threshold_pct: float = Field(
        default=1.0,
        gt=0,
        le=100,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_LLM_VOLATILITY_THRESHOLD_PCT",
    )
    llm_min_confidence: float = Field(
        default=0.7,
        ge=0,
        le=1,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_LLM_MIN_CONFIDENCE",
    )
    llm_max_stripe_width_pct: float = Field(
        default=8.0,
        gt=0,
        le=100,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_LLM_MAX_STRIPE_WIDTH_PCT",
    )
    llm_max_interval_bound_deviation_pct: float = Field(
        default=5.0,
        gt=0,
        le=100,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_LLM_MAX_INTERVAL_BOUND_DEVIATION_PCT",
    )
    llm_experiment_name: str = Field(default="", validation_alias="AUTO_TRADE_LLM_EXPERIMENT_NAME")
    min_exit_profit_pct: float = Field(default=0.2, ge=0, le=20, allow_inf_nan=False)
    engine_cooldown_seconds: int = Field(default=60, ge=0, le=3600, validation_alias="AUTO_TRADE_ENGINE_COOLDOWN_SECONDS")
    trading_open_warmup_minutes: int = Field(
        default=5,
        ge=0,
        le=60,
        validation_alias="AUTO_TRADE_TRADING_OPEN_WARMUP_MINUTES",
    )
    allow_short_entries: bool = Field(
        default=False,
        validation_alias="AUTO_TRADE_ALLOW_SHORT_ENTRIES",
        description=(
            "Compatibility field only. P0 permanently rejects short entries; "
            "setting this to true does not enable live short selling."
        ),
    )
    hard_allow_position_addons: bool = Field(
        default=False,
        validation_alias="AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS",
        description=(
            "Compatibility field only. P0 permanently rejects position add-ons; "
            "setting this to true does not enable live add-ons."
        ),
    )
    hard_max_position_quantity: int = Field(
        default=100,
        ge=1,
        validation_alias="AUTO_TRADE_HARD_MAX_POSITION_QUANTITY",
    )
    hard_max_position_notional: float = Field(
        default=5000.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL",
    )
    hard_max_risk_per_trade: float = Field(
        default=250.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_HARD_MAX_RISK_PER_TRADE",
    )
    hard_stop_loss_pct: float = Field(
        default=1.0,
        gt=0,
        le=20,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_HARD_STOP_LOSS_PCT",
    )
    hard_max_holding_minutes: int = Field(
        default=60,
        ge=1,
        le=10_080,
        validation_alias="AUTO_TRADE_HARD_MAX_HOLDING_MINUTES",
    )
    hard_entry_cutoff_minutes_before_close: int = Field(
        default=45,
        ge=1,
        le=180,
        validation_alias="AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE",
    )
    hard_flatten_minutes_before_close: int = Field(
        default=15,
        ge=1,
        le=180,
        validation_alias="AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE",
    )
    llm_shadow_mode: bool = Field(
        default=True,
        validation_alias="AUTO_TRADE_LLM_SHADOW_MODE",
        description=(
            "Compatibility field only. P0 permanently keeps LLM order decisions in shadow mode; "
            "setting this to false does not enable live LLM orders."
        ),
    )
    llm_max_order_price_deviation_pct: float = Field(
        default=1.0,
        gt=0,
        le=20,
        allow_inf_nan=False,
        validation_alias="AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT",
    )

    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8080",
        validation_alias="AUTO_TRADE_CORS_ORIGINS",
    )

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

    platform_mode: bool = Field(default=False, validation_alias="AUTO_TRADE_PLATFORM_MODE")

    @model_validator(mode="after")
    def merge_longbridge_credentials(self) -> "Settings":
        # P0 values are deployment-tunable only in the safer direction.  These
        # clamps are the final boundary even when the process is not launched
        # through the Compose files.
        self.allow_short_entries = False
        self.hard_allow_position_addons = False
        self.llm_shadow_mode = True
        self.hard_max_position_quantity = min(self.hard_max_position_quantity, 100)
        self.hard_max_position_notional = min(self.hard_max_position_notional, 5000.0)
        self.hard_max_risk_per_trade = min(self.hard_max_risk_per_trade, 250.0)
        self.hard_stop_loss_pct = min(self.hard_stop_loss_pct, 1.0)
        self.hard_max_holding_minutes = min(self.hard_max_holding_minutes, 60)
        self.hard_entry_cutoff_minutes_before_close = max(
            self.hard_entry_cutoff_minutes_before_close,
            45,
        )
        self.hard_flatten_minutes_before_close = max(
            self.hard_flatten_minutes_before_close,
            15,
        )
        if self.hard_flatten_minutes_before_close > self.hard_entry_cutoff_minutes_before_close:
            self.hard_entry_cutoff_minutes_before_close = (
                self.hard_flatten_minutes_before_close
            )
        self.llm_min_confidence = max(self.llm_min_confidence, 0.7)
        self.llm_max_stripe_width_pct = min(self.llm_max_stripe_width_pct, 8.0)
        self.llm_max_interval_bound_deviation_pct = min(
            self.llm_max_interval_bound_deviation_pct,
            5.0,
        )
        self.llm_max_order_price_deviation_pct = min(
            self.llm_max_order_price_deviation_pct,
            1.0,
        )
        self.longbridge_app_key = self.longbridge_app_key or self.longport_app_key or self.legacy_longbridge_app_key
        self.longbridge_app_secret = (
            self.longbridge_app_secret or self.longport_app_secret or self.legacy_longbridge_app_secret
        )
        self.longbridge_access_token = (
            self.longbridge_access_token or self.longport_access_token or self.legacy_longbridge_access_token
        )
        return self

    @model_validator(mode="after")
    def validate_api_key_configuration(self) -> "Settings":
        if not self.api_key:
            if self.env not in ("dev", "test"):
                raise ValueError(
                    "AUTO_TRADE_API_KEY is required outside dev/test environments"
                )
            logger.warning(
                "AUTO_TRADE_API_KEY is empty — the API is running without authentication. "
                "This is allowed only in dev/test environments."
            )
        return self

    @model_validator(mode="after")
    def warn_misconfigured_deepseek_key(self) -> "Settings":
        """Warn if the user set AUTO_TRADE_DEEPSEEK_API_KEY, which is silently ignored."""
        import os

        if os.environ.get("AUTO_TRADE_DEEPSEEK_API_KEY"):
            logger.warning(
                "AUTO_TRADE_DEEPSEEK_API_KEY is set but will be ignored. "
                "The DeepSeek API key uses env var DEEPSEEK_API_KEY (no AUTO_TRADE_ prefix)."
            )
        return self

    @model_validator(mode="after")
    def warn_misconfigured_minimax_key(self) -> "Settings":
        """Warn if the user set AUTO_TRADE_MINIMAX_API_KEY, which is silently ignored."""
        import os

        if os.environ.get("AUTO_TRADE_MINIMAX_API_KEY"):
            logger.warning(
                "AUTO_TRADE_MINIMAX_API_KEY is set but will be ignored. "
                "The MiniMax API key uses env var MINIMAX_API_KEY (no AUTO_TRADE_ prefix)."
            )
        return self

    def ensure_data_dir(self) -> None:
        data_dir = Path("data")
        data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_data_dir()
