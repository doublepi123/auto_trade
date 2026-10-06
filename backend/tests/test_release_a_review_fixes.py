"""Review fixes for phase-boundary submit, RTH_ONLY windows, and stale prints.

These tests pin the three P1s rejected on Release A, including OVERNIGHT.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.core.broker import (
    BrokerGateway,
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
)
from app.core.engine import EngineState, StrategyParams
from app.core.market_calendar import (
    is_closing_window as _real_is_closing_window,
    is_trading_hours as _real_is_trading_hours,
)
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app import runner as runner_module
from app.runner import AppRunner
from app.services import trade_execution_service as execution
from app.services.trade_execution_service import TradeExecutionService


_ET = ZoneInfo("America/New_York")


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_ET)


class _Clock(datetime):
    instant = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        current = cls.instant
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)  # type: ignore[arg-type]


class _PhaseBroker(BrokerGateway):
    def __init__(self) -> None:
        self.submissions: list[str | None] = []
        self.positions = [
            Position("TSLA.US", "LONG", Decimal("1"), Decimal("100")),
        ]
        self.clock: type[_Clock] | None = None
        self.advance_to: datetime | None = None
        self.quotes = [Quote("TSLA.US", 100, 99.9, 100.1, "")]

    def get_positions(self) -> list[Position]:
        if self.clock is not None and self.advance_to is not None:
            self.clock.instant = self.advance_to.astimezone(timezone.utc)
        return self.positions

    def estimate_margin_max_quantity(
        self, symbol: str, side: str, price: Decimal, currency: str | None = None,
    ) -> Decimal:
        if self.clock is not None and self.advance_to is not None:
            self.clock.instant = self.advance_to.astimezone(timezone.utc)
        return Decimal("10")

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
        *, outside_rth: str | None = None,
    ) -> OrderResult:
        self.submissions.append(outside_rth)
        return OrderResult("oid", symbol, side, quantity, price, "SUBMITTED")

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return self.quotes

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "SUBMITTED")


def _service(**kwargs: object) -> TradeExecutionService:
    kwargs.setdefault("entry_cutoff_minutes_before_close", 90)
    kwargs.setdefault(
        "final_order_quote_check",
        lambda _broker, _symbol, _action, price: execution.FinalOrderQuoteCheckResult(
            executable_price=price, bid=Decimal("99.9"), ask=Decimal("100.1"),
        ),
    )
    return TradeExecutionService(
        record_order=lambda *_a: None,
        update_order_status=lambda *_a: None,
        record_risk_event=lambda *_a: None,
        max_position_quantity=1_000_000,
        max_position_notional=1_000_000_000.0,
        max_risk_per_trade=10_000_000.0,
        stop_loss_pct=1.0,
        **kwargs,  # type: ignore[arg-type]
    )


def _pin(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> type[_Clock]:
    _Clock.instant = instant.astimezone(timezone.utc)
    monkeypatch.setattr(execution, "datetime", _Clock)
    monkeypatch.setattr(runner_module, "datetime", _Clock)

    def is_trading_hours(market: str, at: datetime | None = None) -> bool:
        return _real_is_trading_hours(market, at or _Clock.instant)

    def is_closing_window(
        market: str, minutes: int, at: datetime | None = None,
    ) -> bool:
        return _real_is_closing_window(market, minutes, at or _Clock.instant)

    monkeypatch.setattr(execution, "is_trading_hours", is_trading_hours)
    monkeypatch.setattr(execution, "is_closing_window", is_closing_window)
    monkeypatch.setattr(runner_module, "is_trading_hours", is_trading_hours)
    monkeypatch.setattr(runner_module, "is_closing_window", is_closing_window)
    return _Clock


def test_sell_crossing_1600_does_not_submit_plain_order_then_retries_any_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _pin(monkeypatch, _et("2026-10-06T15:59:59"))
    service = _service(extended_hours_trading_enabled=True)
    service.load_tracked_entries({"TSLA.US": (Decimal("1"), Decimal("100"))})
    broker = _PhaseBroker()
    broker.clock = clock
    broker.advance_to = _et("2026-10-06T16:00:01")

    first = service.execute(
        "SELL", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY", reduce_only=True,
        allow_loss_exit=True,
    )

    assert first is not None and first.status == "SKIPPED"
    assert first.skip_category == "SESSION"
    assert "RTH -> POST" in (first.reason or "")
    assert broker.submissions == []
    assert not service._extended_hours_unsupported

    _Clock.instant = _et("2026-10-06T16:00:02").astimezone(timezone.utc)
    second = service.execute(
        "SELL", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY", reduce_only=True,
        allow_loss_exit=True,
    )
    assert second is not None and second.status == "SUBMITTED", second
    assert broker.submissions == ["ANY_TIME"]


def test_post_to_overnight_skips_then_next_eval_submits_overnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _pin(monkeypatch, _et("2026-10-06T19:59:59"))
    service = _service(
        extended_hours_trading_enabled=True,
        overnight_trading_enabled=True,
    )
    service.load_tracked_entries({"TSLA.US": (Decimal("1"), Decimal("100"))})
    broker = _PhaseBroker()
    broker.clock = clock
    broker.advance_to = _et("2026-10-06T20:00:01")

    first = service.execute(
        "SELL", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY", reduce_only=True,
        allow_loss_exit=True,
    )
    assert first is not None and first.status == "SKIPPED"
    assert first.skip_category == "SESSION"
    assert "POST -> OVERNIGHT" in (first.reason or "")
    assert broker.submissions == []

    _Clock.instant = _et("2026-10-06T20:00:02").astimezone(timezone.utc)
    second = service.execute(
        "SELL", "TSLA.US", Quote("TSLA.US", 100, 99.95, 100.0, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY", reduce_only=True,
        allow_loss_exit=True,
    )
    assert second is not None and second.status == "SUBMITTED", second
    assert broker.submissions == ["OVERNIGHT"]


def test_funded_buy_crossing_1600_still_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _pin(monkeypatch, _et("2026-10-06T15:59:59"))
    service = _service(extended_hours_trading_enabled=True)
    broker = _PhaseBroker()
    broker.positions = []
    broker.clock = clock
    broker.advance_to = _et("2026-10-06T16:00:01")

    result = service.execute(
        "BUY", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY",
        expected_exit_price=Decimal("110"),
    )
    assert result is not None and result.status == "SKIPPED"
    assert result.skip_category == "SESSION"
    assert broker.submissions == []


def test_flag_off_phase_cross_keeps_legacy_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _pin(monkeypatch, _et("2026-10-06T15:59:59"))
    service = _service(extended_hours_trading_enabled=False)
    service.load_tracked_entries({"TSLA.US": (Decimal("1"), Decimal("100"))})
    broker = _PhaseBroker()
    broker.clock = clock
    broker.advance_to = _et("2026-10-06T16:00:01")

    result = service.execute(
        "SELL", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY", reduce_only=True,
        allow_loss_exit=True,
    )
    assert result is not None and result.status == "SUBMITTED"
    assert broker.submissions == [None]


def test_rth_only_flag_on_uses_rth_close_not_2000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(monkeypatch, _et("2026-10-06T15:40:00"))
    service = _service(
        extended_hours_trading_enabled=True,
        entry_cutoff_minutes_before_close=45,
    )
    broker = _PhaseBroker()
    broker.positions = []
    result = service.execute(
        "BUY", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="RTH_ONLY",
        expected_exit_price=Decimal("110"),
    )
    assert result is not None and result.status == "SKIPPED"
    assert "entry cutoff" in (result.reason or "")
    assert broker.submissions == []

    runner = AppRunner()
    runner._trading_session_mode = "RTH_ONLY"
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-06T15:30:00"),
    ) is True
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-06T15:59:00"),
    ) is True
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-06T19:30:00"),
    ) is False


def test_any_flag_on_keeps_extended_and_overnight_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(monkeypatch, _et("2026-10-06T15:40:00"))
    service = _service(
        extended_hours_trading_enabled=True,
        overnight_trading_enabled=True,
        entry_cutoff_minutes_before_close=90,
    )
    broker = _PhaseBroker()
    broker.positions = []
    result = service.execute(
        "BUY", "TSLA.US", Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker, RiskController(), ServerChanNotifier(""), "USD",
        market="US", trading_session_mode="ANY",
        expected_exit_price=Decimal("110"),
    )
    assert result is None or result.status != "SKIPPED" or "entry cutoff" not in (
        result.reason or ""
    )

    runner = AppRunner()
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.overnight_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-06T19:30:00"),
    ) is True
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-06T15:40:00"),
    ) is False
    assert runner._in_flatten_window(
        "US", 30, instant=_et("2026-10-07T03:20:00"),
    ) is True


def _entry_runner(monkeypatch: pytest.MonkeyPatch) -> AppRunner:
    runner = AppRunner()
    runner._running = True
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.overnight_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    runner.engine.params = StrategyParams(
        symbol="TSLA.US", market="US", buy_low=100, sell_high=110,
    )
    runner.engine.state = EngineState.FLAT
    monkeypatch.setattr(runner, "_get_trading_session_mode", lambda: "ANY")
    return runner


def _quote_at(instant: datetime) -> Quote:
    return Quote(
        "TSLA.US", 99.0, 98.9, 99.1,
        instant.astimezone(timezone.utc).isoformat(),
    )


def test_post_entry_rejects_print_from_rth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _entry_runner(monkeypatch)
    _pin(monkeypatch, _et("2026-10-06T16:00:05"))
    assert runner._entry_print_in_current_phase(
        _quote_at(_et("2026-10-06T15:59:59")), "US",
    ) is False


def test_post_entry_accepts_print_from_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _entry_runner(monkeypatch)
    _pin(monkeypatch, _et("2026-10-06T16:00:05"))
    assert runner._entry_print_in_current_phase(
        _quote_at(_et("2026-10-06T16:00:03")), "US",
    ) is True


def test_overnight_entry_rejects_post_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _entry_runner(monkeypatch)
    _pin(monkeypatch, _et("2026-10-06T20:00:05"))
    assert runner._entry_print_in_current_phase(
        _quote_at(_et("2026-10-06T19:59:59")), "US",
    ) is False


def test_pre_entry_rejects_overnight_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _entry_runner(monkeypatch)
    _pin(monkeypatch, _et("2026-10-07T04:00:05"))
    assert runner._entry_print_in_current_phase(
        _quote_at(_et("2026-10-07T03:49:00")), "US",
    ) is False


def test_stop_does_not_wait_on_blocked_notifier_drain() -> None:
    runner = AppRunner()
    started = threading.Event()
    release = threading.Event()

    def blocked_drain() -> int:
        started.set()
        release.wait(timeout=30)
        return 0

    runner._notification_retry_queue.drain = blocked_drain  # type: ignore[method-assign]
    runner._running = True
    begin = time.monotonic()
    try:
        runner.stop()
    finally:
        release.set()
    elapsed = time.monotonic() - begin
    assert started.is_set()
    assert elapsed < 6.0
    assert runner._running is False
