from pathlib import Path

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

    def test_deepseek_model_defaults_to_v4_pro_thinking_max_256k(self) -> None:
        s = Settings()
        assert s.deepseek_model == "deepseek-v4-pro"
        assert s.deepseek_reasoning_effort == "max"
        assert s.deepseek_thinking_type == "enabled"
        assert s.deepseek_max_tokens == 262144
