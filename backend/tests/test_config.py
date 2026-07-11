from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


class TestSettings:
    def test_default_values(self, monkeypatch) -> None:
        monkeypatch.delenv("AUTO_TRADE_ENV", raising=False)
        monkeypatch.delenv("AUTO_TRADE_DATABASE_URL", raising=False)
        s = Settings()
        assert s.env == "dev"
        assert s.database_url == "sqlite:///data/auto_trade.db"

    def test_default_strategy_empty(self) -> None:
        s = Settings()
        assert s.default_strategy["symbol"] == ""
        assert s.default_strategy["market"] == "US"

    def test_production_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTO_TRADE_ENV", "prod")
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")

        with pytest.raises(
            ValidationError,
            match="AUTO_TRADE_API_KEY is required outside dev/test environments",
        ):
            Settings()

    @pytest.mark.parametrize("environment", ["dev", "test"])
    def test_non_production_allows_empty_api_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        environment: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_ENV", environment)
        monkeypatch.setenv("AUTO_TRADE_API_KEY", "")

        assert Settings().api_key == ""

    def test_p0_live_safety_defaults_fail_closed(self) -> None:
        s = Settings()
        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.llm_shadow_mode is True
        assert s.llm_max_order_price_deviation_pct == 1
        assert s.llm_max_interval_bound_deviation_pct == 5

    def test_p0_environment_cannot_loosen_hard_safety_limits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        overrides = {
            "AUTO_TRADE_ALLOW_SHORT_ENTRIES": "true",
            "AUTO_TRADE_HARD_ALLOW_POSITION_ADDONS": "true",
            "AUTO_TRADE_LLM_SHADOW_MODE": "false",
            "AUTO_TRADE_HARD_MAX_POSITION_QUANTITY": "10000",
            "AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL": "1000000",
            "AUTO_TRADE_HARD_MAX_RISK_PER_TRADE": "100000",
            "AUTO_TRADE_HARD_STOP_LOSS_PCT": "10",
            "AUTO_TRADE_HARD_MAX_HOLDING_MINUTES": "1440",
            "AUTO_TRADE_HARD_ENTRY_CUTOFF_MINUTES_BEFORE_CLOSE": "1",
            "AUTO_TRADE_HARD_FLATTEN_MINUTES_BEFORE_CLOSE": "1",
            "AUTO_TRADE_LLM_MIN_CONFIDENCE": "0.1",
            "AUTO_TRADE_LLM_MAX_STRIPE_WIDTH_PCT": "50",
            "AUTO_TRADE_LLM_MAX_INTERVAL_BOUND_DEVIATION_PCT": "50",
            "AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT": "10",
        }
        for name, value in overrides.items():
            monkeypatch.setenv(name, value)

        s = Settings()

        assert s.allow_short_entries is False
        assert s.hard_allow_position_addons is False
        assert s.llm_shadow_mode is True
        assert s.hard_max_position_quantity == 100
        assert s.hard_max_position_notional == 5000
        assert s.hard_max_risk_per_trade == 250
        assert s.hard_stop_loss_pct == 1
        assert s.hard_max_holding_minutes == 60
        assert s.hard_entry_cutoff_minutes_before_close == 45
        assert s.hard_flatten_minutes_before_close == 15
        assert s.llm_min_confidence == 0.7
        assert s.llm_max_stripe_width_pct == 8
        assert s.llm_max_interval_bound_deviation_pct == 5
        assert s.llm_max_order_price_deviation_pct == 1

    @pytest.mark.parametrize("value", ["-0.1", "1.1", "nan", "inf"])
    def test_rejects_invalid_llm_min_confidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_LLM_MIN_CONFIDENCE", value)

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("AUTO_TRADE_LLM_VOLATILITY_THRESHOLD_PCT", "nan"),
            ("AUTO_TRADE_LLM_VOLATILITY_THRESHOLD_PCT", "inf"),
            ("AUTO_TRADE_LLM_MAX_ORDER_PRICE_DEVIATION_PCT", "inf"),
            ("AUTO_TRADE_MIN_EXIT_PROFIT_PCT", "nan"),
            ("AUTO_TRADE_HARD_MAX_POSITION_NOTIONAL", "inf"),
            ("AUTO_TRADE_HARD_MAX_RISK_PER_TRADE", "nan"),
            ("AUTO_TRADE_HARD_STOP_LOSS_PCT", "inf"),
        ],
    )
    def test_rejects_non_finite_live_safety_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError):
            Settings()

    def test_ensure_data_dir(self) -> None:
        s = Settings()
        data_dir = Path("data")
        s.ensure_data_dir()
        assert data_dir.exists()

    def test_reads_official_longport_credentials_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        for name in (
            "AUTO_TRADE_LONGBRIDGE_APP_KEY",
            "AUTO_TRADE_LONGBRIDGE_APP_SECRET",
            "AUTO_TRADE_LONGBRIDGE_ACCESS_TOKEN",
            "LONGBRIDGE_APP_KEY",
            "LONGBRIDGE_APP_SECRET",
            "LONGBRIDGE_ACCESS_TOKEN",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "LONGPORT_APP_KEY=official-key\n"
            "LONGPORT_APP_SECRET=official-secret\n"
            "LONGPORT_ACCESS_TOKEN=official-token\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.longbridge_app_key == "official-key"
        assert s.longbridge_app_secret == "official-secret"
        assert s.longbridge_access_token == "official-token"

    def test_reads_legacy_longbridge_credentials_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        for name in (
            "AUTO_TRADE_LONGBRIDGE_APP_KEY",
            "AUTO_TRADE_LONGBRIDGE_APP_SECRET",
            "AUTO_TRADE_LONGBRIDGE_ACCESS_TOKEN",
            "LONGBRIDGE_APP_KEY",
            "LONGBRIDGE_APP_SECRET",
            "LONGBRIDGE_ACCESS_TOKEN",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "LONGBRIDGE_APP_KEY=legacy-key\n"
            "LONGBRIDGE_APP_SECRET=legacy-secret\n"
            "LONGBRIDGE_ACCESS_TOKEN=legacy-token\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.longbridge_app_key == "legacy-key"
        assert s.longbridge_app_secret == "legacy-secret"
        assert s.longbridge_access_token == "legacy-token"

    def test_ignores_credential_master_key_from_parent_env_file(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("AUTO_TRADE_ENV", raising=False)
        monkeypatch.delenv("CREDENTIAL_MASTER_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "CREDENTIAL_MASTER_KEY=local-encryption-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.env == "dev"

    def test_reads_deepseek_api_key_from_unprefixed_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("AUTO_TRADE_DEEPSEEK_API_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "DEEPSEEK_API_KEY=sk-test-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.deepseek_api_key == "sk-test-key"

    def test_reads_minimax_api_key_from_unprefixed_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("AUTO_TRADE_MINIMAX_API_KEY", raising=False)

        root = tmp_path / "project"
        backend = root / "backend"
        backend.mkdir(parents=True)
        (root / ".env").write_text(
            "MINIMAX_API_KEY=mm-test-key\n",
            encoding="utf-8",
        )

        monkeypatch.chdir(backend)
        s = Settings()

        assert s.minimax_api_key == "mm-test-key"

    def test_deepseek_model_defaults_to_v4_pro_thinking_max_256k(self) -> None:
        s = Settings()
        assert s.deepseek_model == "deepseek-v4-pro"
        assert s.deepseek_reasoning_effort == "max"
        assert s.deepseek_thinking_type == "enabled"
        assert s.deepseek_max_tokens == 262144

    def test_llm_provider_defaults_to_deepseek_and_minimax_defaults_are_available(self) -> None:
        s = Settings()

        assert s.llm_provider == "deepseek"
        assert s.minimax_base_url == "https://api.minimaxi.com/v1"
        assert s.minimax_api_url == ""
        assert s.minimax_model == "MiniMax-M3"
        assert s.minimax_thinking_type == "adaptive"
        assert s.minimax_max_completion_tokens == 8192
