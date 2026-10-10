from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.runner as runner_module
from app.core.broker import Quote
from app.core.engine import StrategyParams
from app.core.execution_session import resolve_execution_session
from app.runner import AppRunner


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _FakeBroker:
    def __init__(self, clock: _FakeClock, instant: datetime) -> None:
        self.clock = clock
        self.instant = instant
        self.calls: list[float] = []
        self.failure: str | None = None

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        self.calls.append(self.clock.now)
        if self.failure:
            # Model a network wait longer than the healthy polling interval.
            self.clock.now += 61.0
            if self.failure == "raise":
                raise RuntimeError("quote channel unavailable")
            return []
        return [self.quote(symbols[0])]

    def quote(self, symbol: str = "AAPL.US") -> Quote:
        return Quote(
            symbol=symbol, last_price=123.45, bid=123.4, ask=123.5,
            timestamp=self.instant.isoformat(),
        )


def _runner_at(
    monkeypatch: pytest.MonkeyPatch, local_time: str,
) -> tuple[AppRunner, _FakeBroker, _FakeClock, datetime]:
    instant = datetime.fromisoformat(local_time).replace(tzinfo=ZoneInfo("America/New_York"))

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    clock = _FakeClock()
    monkeypatch.setattr(runner_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runner_module, "datetime", _FakeDatetime)
    runner = AppRunner()
    broker = _FakeBroker(clock, instant)
    monkeypatch.setattr(runner, "broker", broker)
    runner._running = True
    runner.engine.params = StrategyParams(
        symbol="AAPL.US", market="US", buy_low=100.0, sell_high=200.0,
    )
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.extended_hours_protective_exits_enabled = True
    runner._trade_svc.overnight_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    runner._last_quote_at = 900.0
    # Exercise real quote freshness bookkeeping, without order evaluation/I/O.
    monkeypatch.setattr(runner, "_on_quote", lambda quote, **kwargs: runner._remember_quote(quote))
    return runner, broker, clock, instant


_EXECUTABLE_TIMES = [
    ("2026-10-09T04:00:00", "PRE"),
    ("2026-10-09T10:00:00", "RTH"),
    ("2026-10-09T19:59:00", "POST"),
    ("2026-10-08T20:00:00", "OVERNIGHT"),
    ("2026-10-09T03:49:00", "OVERNIGHT"),
    ("2026-10-11T20:00:00", "OVERNIGHT"),
]


@pytest.mark.parametrize("local_time,phase", _EXECUTABLE_TIMES)
def test_active_session_predicate_covers_every_executable_phase(monkeypatch, local_time, phase):
    runner, _, _, instant = _runner_at(monkeypatch, local_time)
    assert resolve_execution_session("US", instant, overnight_enabled=True).phase == phase
    assert runner._market_in_active_session("US", instant=instant)
    assert runner._market_in_active_session("US")


@pytest.mark.parametrize("local_time,phase", _EXECUTABLE_TIMES)
def test_stale_quote_refresh_runs_in_every_executable_phase(monkeypatch, local_time, phase):
    runner, broker, _, instant = _runner_at(monkeypatch, local_time)
    assert resolve_execution_session("US", instant, overnight_enabled=True).phase == phase
    runner._refresh_quote_if_stale()
    assert broker.calls == [1000.0]
    assert runner._last_quote_at == 1000.0


@pytest.mark.parametrize("local_time", ["2026-10-10T10:00:00", "2026-12-25T10:00:00"])
def test_stale_quote_refresh_skips_closed_market(monkeypatch, local_time):
    runner, broker, _, _ = _runner_at(monkeypatch, local_time)
    assert not runner._market_in_active_session("US")
    runner._refresh_quote_if_stale()
    assert broker.calls == []


@pytest.mark.parametrize("failure", ["raise", "empty"])
def test_failure_backoff_is_measured_from_attempt_end(monkeypatch, failure):
    runner, broker, clock, _ = _runner_at(monkeypatch, "2026-10-09T10:00:00")
    broker.failure = failure
    runner._refresh_quote_if_stale()
    for spacing in (15.0, 30.0, 60.0, 120.0, 120.0):
        failure_ended = clock.now
        calls = len(broker.calls)
        clock.now = failure_ended + spacing - 0.01
        runner._refresh_quote_if_stale()
        assert len(broker.calls) == calls
        clock.now = failure_ended + spacing
        runner._refresh_quote_if_stale()
        assert len(broker.calls) == calls + 1
        assert broker.calls[-1] == failure_ended + spacing


def _fail_three_times(runner: AppRunner, broker: _FakeBroker, clock: _FakeClock) -> None:
    broker.failure = "raise"
    runner._refresh_quote_if_stale()
    for spacing in (15.0, 30.0):
        clock.now += spacing
        runner._refresh_quote_if_stale()
    assert len(broker.calls) == 3


def test_success_resets_failure_spacing_to_base(monkeypatch):
    runner, broker, clock, _ = _runner_at(monkeypatch, "2026-10-09T10:00:00")
    _fail_three_times(runner, broker, clock)
    clock.now += 60.0
    broker.failure = None
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 4
    clock.now += 14.99
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 4
    clock.now += 0.01
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 5
    # A failure after recovery is once again the first failure.
    broker.failure = "empty"
    clock.now += 15.0
    runner._refresh_quote_if_stale()
    clock.now += 15.0
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 7


def test_fresh_primary_push_resets_failure_spacing_to_base(monkeypatch):
    runner, broker, clock, _ = _runner_at(monkeypatch, "2026-10-09T10:00:00")
    _fail_three_times(runner, broker, clock)
    clock.now += 1.0
    runner._remember_quote(broker.quote())
    clock.now += 14.99
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 3
    clock.now += 0.01
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 4
    clock.now += 15.0
    runner._refresh_quote_if_stale()
    assert len(broker.calls) == 5
