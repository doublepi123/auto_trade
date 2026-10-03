"""AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED — Settings-level behaviour.

Owner decisions (2026-10-03): prepare US pre-market / after-hours long entries
and reduce-only exits behind a switch that is OFF by default and has NO effect
on a paper-attested account. Longbridge paper accounts do not support US
extended-hours trading, so paper + flag on must fail closed to off.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import Settings


class TestExtendedHoursTradingSetting:
    def _clear_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(
            "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED",
            raising=False,
        )
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)

    def test_default_is_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._clear_flags(monkeypatch)
        # Read the true code default, not any developer-local .env.
        monkeypatch.chdir(tmp_path)

        settings = Settings()
        assert settings.extended_hours_trading_enabled is False
        assert settings.extended_hours_trading_effective() is False

    @pytest.mark.parametrize("value", ["true", "1"])
    def test_opt_in_reads_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", value)
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)

        settings = Settings()
        assert settings.extended_hours_trading_enabled is True
        # Not paper-attested: the flag is effective.
        assert settings.extended_hours_trading_effective() is True

    def test_paper_attestation_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        settings = Settings()
        assert settings.extended_hours_trading_enabled is True
        with caplog.at_level(logging.WARNING):
            assert settings.extended_hours_trading_effective() is False
        assert any(
            "ignored: paper accounts do not support US extended-hours trading"
            in record.message
            for record in caplog.records
        )

    def test_flag_off_is_ineffective_even_when_not_paper(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "false")
        monkeypatch.delenv(
            "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED",
            raising=False,
        )
        monkeypatch.chdir(tmp_path)

        settings = Settings()
        assert settings.extended_hours_trading_enabled is False
        assert settings.extended_hours_trading_effective() is False

    def test_paper_warning_emitted_once_not_per_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        settings = Settings()
        with caplog.at_level(logging.WARNING):
            settings.extended_hours_trading_effective()
            settings.extended_hours_trading_effective()
        warnings = [
            record
            for record in caplog.records
            if "ignored: paper accounts do not support US extended-hours trading"
            in record.message
        ]
        # Repeated resolutions must not spam the log.
        assert len(warnings) == 1
