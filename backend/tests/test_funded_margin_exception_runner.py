# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Funded full-margin exception — runner wiring (binding callable + raw caps).

The binding is evaluated LAZILY at sizing/pre-submit because
``_configure_live_safety`` runs before credentials load
(``_initialize_runner`` order). ``_apply_credentials`` must publish the
CURRENT fingerprint only while all three credential parts are present.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from app.config import Settings
from app.runner import AppRunner
from app.services.credentials_service import PlainCredentials

FP_FULL = "2" * 64


def _iter_block_values(block: dict) -> Iterator[object]:
    """Yield every scalar in the (nested) diagnostics block."""
    stack: list[object] = [block]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        else:
            yield current


class _NewBroker:
    def __init__(self) -> None:
        self.closed = False

    def register_disconnect_hook(self, _hook) -> None:
        pass

    def get_positions(self):
        return []

    def get_today_orders(self):
        return []

    def close(self) -> None:
        self.closed = True


def _apply_credentials(
    runner: AppRunner,
    *,
    app_key: str = "key-1",
    app_secret: str = "secret-1",
    access_token: str = "token-1",
) -> None:
    runner._apply_credentials(
        PlainCredentials(
            longbridge_app_key=app_key,
            longbridge_app_secret=app_secret,
            longbridge_access_token=access_token,
        ),
        resubscribe=False,
    )


def _full_fingerprint(app_key: str, app_secret: str, access_token: str) -> str:
    return AppRunner._credential_identity_fingerprint({
        "LONGPORT_APP_KEY": app_key,
        "LONGPORT_APP_SECRET": app_secret,
        "LONGPORT_ACCESS_TOKEN": access_token,
    })


class TestCredentialFingerprintProvider:
    def test_provider_exists_and_fails_closed_before_credentials(self) -> None:
        runner = AppRunner()
        provider = runner._funded_margin_fingerprint_provider
        assert callable(provider)
        # Before any credentials load the binding fails closed: "".
        assert provider() == ""

    def test_provider_publishes_complete_credentials_only(self, monkeypatch) -> None:
        runner = AppRunner()
        new_broker = _NewBroker()
        monkeypatch.setattr(runner, "_build_broker", lambda _audit: new_broker)

        _apply_credentials(runner)
        expected = _full_fingerprint("key-1", "secret-1", "token-1")
        assert runner._funded_margin_fingerprint_provider() == expected

    def test_incomplete_credentials_return_empty(self, monkeypatch) -> None:
        runner = AppRunner()
        new_broker = _NewBroker()
        monkeypatch.setattr(runner, "_build_broker", lambda _audit: new_broker)

        _apply_credentials(
            runner,
            app_key="key-1",
            app_secret="secret-1",
            access_token="",
        )
        # Test settings carry no LONGPORT_* fallbacks, so the missing token
        # keeps the credentials incomplete and the binding fails closed.
        assert runner._funded_margin_fingerprint_provider() == ""

    def test_rotation_updates_the_current_fingerprint(self, monkeypatch) -> None:
        runner = AppRunner()
        monkeypatch.setattr(runner, "_build_broker", lambda _audit: _NewBroker())

        _apply_credentials(runner)
        first = runner._funded_margin_fingerprint_provider()

        _apply_credentials(
            runner,
            app_key="key-2",
            app_secret="secret-2",
            access_token="token-2",
        )
        second = runner._funded_margin_fingerprint_provider()

        assert first != second
        assert second == _full_fingerprint("key-2", "secret-2", "token-2")


def _fake_config(*, qty: int, notional: float, risk: float):
    class FakeConfig:
        symbol = "AAPL.US"
        market = "US"
        buy_low = 100.0
        sell_high = 200.0
        short_selling = False
        min_profit_amount = 0.0
        auto_resume_minutes = 3
        max_daily_loss = 5000.0
        max_consecutive_losses = 3
        fee_rate_us = 0.0005
        fee_rate_hk = 0.003
        min_repricing_pct = 0.003
        llm_action_cooldown_seconds = 60
        trading_session_mode = "ANY"
        margin_safety_factor = 0.75
        max_position_quantity = qty
        max_position_notional = notional
        max_risk_per_trade = risk
        stop_loss_pct = 1.0

    return FakeConfig()


class TestRawStrategyCapHandover:
    def test_configure_live_safety_hands_over_raw_caps(self) -> None:
        runner = AppRunner()
        runner._configure_live_safety(
            _fake_config(qty=800, notional=20000.0, risk=240.0),
        )

        # Clamped fields keep today's semantics (strategy values below the
        # ceiling stay; values above would clamp)...
        assert runner._trade_svc.max_position_quantity == 100  # 800 -> clamp
        assert runner._trade_svc.max_position_notional == 5000.0  # clamp
        assert runner._trade_svc.max_risk_per_trade == 240.0  # below 250
        # ...while the RAW strategy values ride along for the exception
        # resolver (never re-min'ed against the clamped cached values).
        assert runner._trade_svc.raw_strategy_max_position_quantity == 800
        assert runner._trade_svc.raw_strategy_max_position_notional == 20000.0
        assert runner._trade_svc.raw_strategy_max_risk_per_trade == 240.0

    def test_missing_raw_values_fall_back_to_the_clamped_caps(self) -> None:
        runner = AppRunner()
        # A legacy double WITHOUT the raw fields exercises the getattr
        # fallback: raw caps fall back to the (clamped) Settings values.
        config = _fake_config(qty=800, notional=20000.0, risk=240.0)
        legacy = type("LegacyConfig", (), {
            name: getattr(config, name)
            for name in (
                "symbol", "market", "buy_low", "sell_high", "short_selling",
                "min_profit_amount", "auto_resume_minutes", "max_daily_loss",
                "max_consecutive_losses", "fee_rate_us", "fee_rate_hk",
                "min_repricing_pct", "llm_action_cooldown_seconds",
                "trading_session_mode", "margin_safety_factor",
                "stop_loss_pct",
            )
        })
        runner._configure_live_safety(legacy)

        assert runner._trade_svc.raw_strategy_max_position_quantity == 100
        assert runner._trade_svc.raw_strategy_max_position_notional == 5000.0
        assert runner._trade_svc.raw_strategy_max_risk_per_trade == 250.0
    def test_reload_strategy_refreshes_raw_caps(self, monkeypatch) -> None:
        from app.services.strategy_service import StrategyService

        runner = AppRunner()
        config = _fake_config(qty=600, notional=15000.0, risk=200.0)
        monkeypatch.setattr(
            StrategyService, "__init__", lambda self, db: None,
        )
        monkeypatch.setattr(
            StrategyService, "get_config", lambda self: config,
        )
        monkeypatch.setattr(
            runner._state_svc, "load_symbol_runtime", lambda *args: None,
        )
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])

        runner.reload_strategy()

        assert runner._trade_svc.raw_strategy_max_position_quantity == 600
        assert runner._trade_svc.raw_strategy_max_position_notional == 15000.0
        assert runner._trade_svc.raw_strategy_max_risk_per_trade == 200.0


class TestFundedMarginDiagnosticsBlock:
    def test_diagnostics_block_reports_disabled(self) -> None:
        runner = AppRunner()
        block = runner.diagnostics()["funded_margin"]

        assert block["enabled"] is False
        assert block["binding_status"] == "DISABLED"
        # Never leak the fingerprint value or any credential: assert it
        # for real (no vacuous `or True`), over both the block payload
        # and the runner's captured logs.
        block_repr = repr(block)
        assert "fingerprint" not in block_repr.lower() or "fingerprint" == ""
        for secret in (
            "longbridge_app_key",
            "longbridge_app_secret",
            "longbridge_access_token",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            assert secret not in block_repr
        assert not any(
            isinstance(value, str) and len(value) == 64
            for value in _iter_block_values(block)
        )

    def test_diagnostics_block_reports_matched_binding(
        self,
        monkeypatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        runner = AppRunner()
        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(
            runner, "_build_broker", lambda _audit: _NewBroker(),
        )
        _apply_credentials(runner)
        current_fp = runner._funded_margin_fingerprint_provider()
        assert current_fp != ""

        svc = runner._trade_svc
        svc.funded_margin_enabled = True
        svc.funded_margin_account_fingerprint = current_fp
        svc.funded_margin_requested_quantity = 1000
        svc.funded_margin_requested_notional = 25000.0
        svc.funded_margin_requested_risk = 250.0
        svc.raw_strategy_max_position_quantity = 1000
        svc.raw_strategy_max_position_notional = 25000.0
        svc.raw_strategy_max_risk_per_trade = 250.0
        svc.entry_cutoff_minutes_before_close = 90
        runner.engine.params.flatten_minutes_before_close = 30

        block = runner.diagnostics()["funded_margin"]
        assert block["enabled"] is True
        assert block["configured"] is True
        assert block["binding_status"] == "MATCHED"
        assert block["effective_caps"]["quantity"] == 1000
        assert block["effective_caps"]["notional"] == 25000.0
        assert block["effective_caps"]["risk"] == 250.0
        assert block["cutoff_minutes"] == 90
        assert block["flatten_minutes"] == 30
        # No secrets in the block or in captured logs: neither the
        # fingerprint value nor any credential part.
        assert current_fp not in repr(block)
        assert not any(
            isinstance(value, str) and value == current_fp
            for value in _iter_block_values(block)
        )
        for secret in (
            "longbridge_app_key",
            "longbridge_app_secret",
            "longbridge_access_token",
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        ):
            assert secret not in repr(block)
        assert caplog.text == "" or (
            current_fp not in caplog.text
            and "longbridge_app_secret" not in caplog.text
        )


class TestSettingsIntegration:
    def test_settings_configured_state_resolves(self, monkeypatch) -> None:
        for name in (
            "AUTO_TRADE_FUNDED_MARGIN_ENABLED",
            "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_ENABLED", "true")
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT", FP_FULL,
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY", "1000",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL", "25000",
        )
        monkeypatch.setenv(
            "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE", "250",
        )

        settings = Settings()
        assert settings.funded_margin_enabled is True
        assert settings.funded_margin_account_fingerprint == FP_FULL
        # Configured: enabled AND not paper AND valid fingerprint AND all
        # three requests > 0. The floors are raised by the validator.
        # Extended hours is NOT forced off (owner decision 2026-10-06:
        # the 15-minute forced liquidation is the separate
        # intraday-financing account, not this ordinary margin exception).
        # This env does not set the extended flag, so the default stays off.
        assert settings.hard_entry_cutoff_minutes_before_close == 90
        assert settings.hard_flatten_minutes_before_close == 30
        assert settings.extended_hours_trading_enabled is False

    def test_settings_defaults_are_off_and_inert(self, monkeypatch, tmp_path) -> None:
        for name in (
            "AUTO_TRADE_FUNDED_MARGIN_ENABLED",
            "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL",
            "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.chdir(tmp_path)

        settings = Settings()
        assert settings.funded_margin_enabled is False
        assert settings.funded_margin_account_fingerprint == ""
        assert settings.funded_margin_max_position_quantity == 0
        assert settings.funded_margin_max_position_notional == 0.0
        assert settings.funded_margin_max_risk_per_trade == 0.0
        # Flag off: the hard floors keep today's values byte-for-byte.
        assert settings.hard_entry_cutoff_minutes_before_close == 45
        assert settings.hard_flatten_minutes_before_close == 15
