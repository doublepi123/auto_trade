# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import os
from types import SimpleNamespace

from app import runner as runner_module
from app.models import CredentialConfig
from app.runner import AppRunner


class FakeSession:
    def __init__(self, config: CredentialConfig) -> None:
        self._config = config
        self.closed = False

    def query(self, model: object) -> object:
        class _Query:
            def __init__(self, config: CredentialConfig, model: object) -> None:
                self._config = config
                self._model = model

            def filter(self, *_args: object, **_kwargs: object) -> _Query:
                return self

            def order_by(self, *_args: object, **_kwargs: object) -> _Query:
                return self

            def first(self) -> CredentialConfig | None:
                if getattr(self._model, "__name__", "") != "CredentialConfig":
                    return None
                return self._config

        return _Query(self._config, model)

    def close(self) -> None:
        self.closed = True

    def add(self, _config: CredentialConfig) -> None:
        pass

    def commit(self) -> None:
        pass

    def refresh(self, _config: CredentialConfig) -> None:
        pass


class FakeBrokerGateway:
    instances: list[FakeBrokerGateway] = []

    def __init__(self) -> None:
        self.closed = False
        self.subscriptions: list[tuple[str, object]] = []
        FakeBrokerGateway.instances.append(self)

    def close(self) -> None:
        self.closed = True

    def subscribe_quotes(self, symbol: str, callback: object) -> None:
        self.subscriptions.append((symbol, callback))

    def unsubscribe_quotes(self) -> None:
        self.subscriptions.clear()


class FailingSubscribeBrokerGateway(FakeBrokerGateway):
    def subscribe_quotes(self, symbol: str, callback: object) -> None:
        raise RuntimeError("subscribe failed")


class FakeNotifier:
    def __init__(self, sct_key: str) -> None:
        self.sct_key = sct_key


class FakeThread:
    def __init__(self, target: object = None, daemon: bool = False) -> None:
        self.target = target
        self.daemon = daemon
        self.started = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        pass


class FakeStrategyService:
    def __init__(self, _db: object) -> None:
        pass

    def get_config(self) -> object:
        return SimpleNamespace(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=200.0,
            short_selling=False,
            max_daily_loss=5000.0,
            max_consecutive_losses=3,
            sct_key="strategy-sct",
        )

    def get_runtime_state(self) -> object:
        return SimpleNamespace(
            engine_state="flat",
            last_price=0.0,
            last_trigger_price=0.0,
            last_trigger_at=None,
            daily_pnl=0.0,
            consecutive_losses=0,
            kill_switch=False,
            paused=False,
        )


class TestRunnerCredentials:
    def test_start_loads_database_credentials(self, monkeypatch) -> None:
        FakeBrokerGateway.instances = []
        credential = CredentialConfig(
            longbridge_app_key="db-key",
            longbridge_app_secret="db-secret",
            longbridge_access_token="db-token",
            sct_key="db-sct",
        )

        monkeypatch.setattr(runner_module, "BrokerGateway", FakeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "StrategyService", FakeStrategyService)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))
        monkeypatch.setattr(runner_module.threading, "Thread", FakeThread)

        runner = AppRunner()
        runner.start()

        assert os.environ.get("LONGPORT_APP_KEY") == "db-key"
        assert os.environ.get("LONGPORT_APP_SECRET") == "db-secret"
        assert os.environ.get("LONGPORT_ACCESS_TOKEN") == "db-token"
        assert runner.notifier.sct_key == "db-sct"

        runner.stop()

    def test_reload_credentials_replaces_runtime_clients(self, monkeypatch) -> None:
        FakeBrokerGateway.instances = []
        credential = CredentialConfig(
            longbridge_app_key="reload-key",
            longbridge_app_secret="reload-secret",
            longbridge_access_token="reload-token",
            sct_key="reload-sct",
        )

        monkeypatch.setattr(runner_module, "BrokerGateway", FakeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))

        runner = AppRunner()
        old_broker = runner.broker
        runner._running = True
        runner._quotes_subscribed = False
        runner.engine.params.symbol = "AAPL.US"

        runner.reload_credentials()

        assert old_broker.closed is True
        assert os.environ.get("LONGPORT_APP_KEY") == "reload-key"
        assert os.environ.get("LONGPORT_APP_SECRET") == "reload-secret"
        assert os.environ.get("LONGPORT_ACCESS_TOKEN") == "reload-token"
        assert runner.notifier.sct_key == "reload-sct"
        assert runner.broker.subscriptions[0][0] == "AAPL.US"

    def test_reload_credentials_keeps_broker_and_notifier_when_resubscribe_fails(self, monkeypatch) -> None:
        credential = CredentialConfig(
            longbridge_app_key="reload-key",
            longbridge_app_secret="reload-secret",
            longbridge_access_token="reload-token",
            sct_key="reload-sct",
        )

        monkeypatch.setattr(runner_module, "BrokerGateway", FailingSubscribeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))

        runner = AppRunner()
        old_broker = runner.broker
        old_notifier = runner.notifier
        runner._running = True
        runner.engine.params.symbol = "AAPL.US"

        runner.reload_credentials()

        assert runner.broker is old_broker
        assert runner.notifier is old_notifier

    def test_reload_credentials_uses_env_sct_key_fallback(self, monkeypatch) -> None:
        credential = CredentialConfig(
            longbridge_app_key="reload-key",
            longbridge_app_secret="reload-secret",
            longbridge_access_token="reload-token",
            sct_key="",
        )

        monkeypatch.setattr(runner_module, "BrokerGateway", FakeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))
        monkeypatch.setattr(runner_module.settings, "sct_key", "env-sct", raising=False)

        runner = AppRunner()
        runner.reload_credentials()

        assert runner.notifier.sct_key == "env-sct"

    def test_reload_credentials_removes_blank_longport_env_vars(self, monkeypatch) -> None:
        credential = CredentialConfig(
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
            sct_key="",
        )
        monkeypatch.setenv("LONGPORT_APP_KEY", "old-key")
        monkeypatch.setenv("LONGPORT_APP_SECRET", "old-secret")
        monkeypatch.setenv("LONGPORT_ACCESS_TOKEN", "old-token")
        monkeypatch.setattr(runner_module, "BrokerGateway", FakeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))

        runner = AppRunner()
        runner.reload_credentials()

        assert "LONGPORT_APP_KEY" not in os.environ
        assert "LONGPORT_APP_SECRET" not in os.environ
        assert "LONGPORT_ACCESS_TOKEN" not in os.environ

    def test_blank_database_credentials_keep_config_env_credentials(self, monkeypatch) -> None:
        credential = CredentialConfig(
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
            sct_key="",
        )
        monkeypatch.setattr(runner_module.settings, "longbridge_app_key", "env-key", raising=False)
        monkeypatch.setattr(runner_module.settings, "longbridge_app_secret", "env-secret", raising=False)
        monkeypatch.setattr(runner_module.settings, "longbridge_access_token", "env-token", raising=False)
        monkeypatch.setattr(runner_module, "BrokerGateway", FakeBrokerGateway)
        monkeypatch.setattr(runner_module, "ServerChanNotifier", FakeNotifier)
        monkeypatch.setattr(runner_module, "SessionLocal", lambda: FakeSession(credential))

        runner = AppRunner()
        runner.reload_credentials()

        assert os.environ.get("LONGPORT_APP_KEY") == "env-key"
        assert os.environ.get("LONGPORT_APP_SECRET") == "env-secret"
        assert os.environ.get("LONGPORT_ACCESS_TOKEN") == "env-token"
