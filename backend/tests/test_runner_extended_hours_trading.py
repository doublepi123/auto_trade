"""Runner-side extended-hours trading flag effects.

With AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED effective (on + not paper):
- the flatten window is measured from the end of the last executable phase
  (20:00 ET normally, RTH close on half days) — not the RTH close;
- the silent-feed resubscribe in-session filter counts US PRE/POST as
  in-session.
Flag off: today's semantics unchanged (RTH-close windows; RTH-only filter).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import runner as runner_module
from app.runner import AppRunner

_ET = ZoneInfo("America/New_York")


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_ET)


def _runner_flag_on(monkeypatch: pytest.MonkeyPatch) -> AppRunner:
    runner = AppRunner()
    monkeypatch.setattr(runner, "_get_trading_session_mode", lambda: "ANY")
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    return runner


def _runner_flag_off(monkeypatch: pytest.MonkeyPatch) -> AppRunner:
    runner = AppRunner()
    monkeypatch.setattr(runner, "_get_trading_session_mode", lambda: "ANY")
    runner._trade_svc.extended_hours_trading_enabled = False
    runner._trade_svc.paper_account_confirmed = False
    return runner


class TestFlattenWindowUsesLastExecutableClose:
    def test_flatten_at_1950_et_with_flag_effective(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _runner_flag_on(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T19:50:00"),
        ) is True

    def test_no_flatten_at_1550_et_with_flag_effective(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 15:50 ET is inside the legacy 15-minute window; with the flag
        # effective the flatten window starts 19:45, so 15:50 must NOT flatten.
        runner = _runner_flag_on(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T15:50:00"),
        ) is False

    def test_half_day_flatten_relative_to_1300_et(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Black Friday 12:50 ET: 10 minutes before the 13:00 last-phase end.
        runner = _runner_flag_on(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-11-27T12:50:00"),
        ) is True

    def test_no_flatten_at_1950_et_on_half_day(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Black Friday 19:50 ET: post-market unsupported; nothing executable.
        runner = _runner_flag_on(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-11-27T19:50:00"),
        ) is False

    def test_flag_off_flatten_at_1550_et_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _runner_flag_off(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T15:50:00"),
        ) is True

    def test_flag_off_no_flatten_at_1950_et(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _runner_flag_off(monkeypatch)
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T19:50:00"),
        ) is False

    def test_paper_with_flag_on_keeps_legacy_window(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _runner_flag_on(monkeypatch)
        runner._trade_svc.paper_account_confirmed = True
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T15:50:00"),
        ) is True
        assert runner._in_flatten_window(
            "US", 15, instant=_et("2026-10-19T19:50:00"),
        ) is False


class TestResubscribeInSessionFilter:
    @pytest.mark.parametrize(
        ("instant", "expected"),
        [
            ("2026-10-19T08:00:00", True),   # PRE
            ("2026-10-19T17:30:00", True),   # POST
            ("2026-10-19T23:00:00", False),  # overnight
            ("2026-10-19T12:00:00", True),   # RTH
            ("2026-10-24T12:00:00", False),  # Saturday
            ("2026-11-27T14:00:00", False),  # half-day post unsupported
        ],
    )
    def test_flag_effective_counts_pre_post(
        self, monkeypatch: pytest.MonkeyPatch, instant: str, expected: bool,
    ) -> None:
        runner = _runner_flag_on(monkeypatch)
        assert runner._market_in_active_session(
            "US", instant=_et(instant),
        ) is expected

    @pytest.mark.parametrize(
        ("instant", "expected"),
        [
            ("2026-10-19T08:00:00", False),  # PRE excluded flag-off
            ("2026-10-19T17:30:00", False),  # POST excluded flag-off
            ("2026-10-19T12:00:00", True),   # RTH unchanged
        ],
    )
    def test_flag_off_excludes_pre_post(
        self, monkeypatch: pytest.MonkeyPatch, instant: str, expected: bool,
    ) -> None:
        runner = _runner_flag_off(monkeypatch)
        assert runner._market_in_active_session(
            "US", instant=_et(instant),
        ) is expected

    def test_paper_with_flag_on_excludes_pre_post(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = _runner_flag_on(monkeypatch)
        runner._trade_svc.paper_account_confirmed = True
        assert runner._market_in_active_session(
            "US", instant=_et("2026-10-19T17:30:00"),
        ) is False


class TestRunnerWiresFlagFromSettings:
    def test_trade_svc_receives_settings_flag_at_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import Settings

        settings = Settings()
        runner = AppRunner()
        assert (
            runner._trade_svc.extended_hours_trading_enabled
            is settings.extended_hours_trading_enabled
        )
        assert (
            runner._trade_svc.paper_account_confirmed
            is settings.paper_account_confirmed
        )
        assert not settings.extended_hours_trading_enabled
