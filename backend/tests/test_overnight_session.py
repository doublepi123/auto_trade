"""US overnight (20:00-03:50 ET) execution, default off.

Overnight belongs to trading day N = the next exchange date after 20:00 ET.
It runs only when N is a full US trading day. Half days and the night before
a holiday fail closed. The overnight span is separate from PRE/RTH/POST so
cutoff and flatten before 20:00 stay measured from 20:00 ET.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.core.broker import (
    BrokerGateway,
    ExtendedHoursUnsupportedError,
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
)
from app.core.execution_session import (
    extended_last_executable_close,
    is_extended_closing_window,
    is_extended_closing_window as _real_is_extended_closing_window,
    outside_rth_for_phase,
    resolve_execution_session,
)
from app.core.market_calendar import (
    is_closing_window as _real_is_closing_window,
    is_trading_hours as _real_is_trading_hours,
)
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import interval_recenter_service as recenter
from app.services import trade_execution_service as execution
from app.services.interval_recenter_service import IntervalRecenterService


_ET = ZoneInfo("America/New_York")


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_ET)


def _utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc)


class TestOvernightPhase:
    def test_tuesday_2100_enabled_is_overnight_span(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-06T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "OVERNIGHT"
        assert decision.extended_hours_executable is True
        assert decision.phase_started_at == _utc(_et("2026-10-06T20:00:00"))
        assert decision.phase_ends_at == _utc(_et("2026-10-07T03:50:00"))

    def test_tuesday_2100_disabled_keeps_old_unavailable_text(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-06T21:00:00"), overnight_enabled=False,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "overnight session is not supported by this system"
        assert decision.extended_hours_executable is False

    def test_sunday_night_belongs_to_monday(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-04T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "OVERNIGHT"
        assert decision.phase_started_at == _utc(_et("2026-10-04T20:00:00"))
        assert decision.phase_ends_at == _utc(_et("2026-10-05T03:50:00"))

    def test_friday_night_has_no_overnight(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-09T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "no overnight session tonight"

    def test_saturday_morning_has_no_overnight(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-10T02:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "no overnight session tonight"

    def test_monday_morning_is_overnight_for_monday(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-10-05T02:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "OVERNIGHT"
        assert decision.phase_ends_at == _utc(_et("2026-10-05T03:50:00"))

    def test_night_before_thanksgiving_is_unavailable(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-11-25T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "no overnight session tonight"

    def test_night_before_black_friday_half_day_is_unavailable(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-11-26T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "no overnight session tonight"

    def test_night_before_christmas_eve_half_day_is_unavailable(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-12-23T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "UNAVAILABLE"
        assert decision.reason == "no overnight session tonight"

    def test_gap_before_pre_is_unavailable_and_0400_is_pre(self) -> None:
        gap = resolve_execution_session(
            "US", _et("2026-10-07T03:55:00"), overnight_enabled=True,
        )
        pre = resolve_execution_session(
            "US", _et("2026-10-07T04:00:00"), overnight_enabled=True,
        )
        assert gap.phase == "UNAVAILABLE"
        assert pre.phase == "PRE"

    def test_dst_fallback_sunday_ends_0850_utc(self) -> None:
        decision = resolve_execution_session(
            "US", _et("2026-11-01T21:00:00"), overnight_enabled=True,
        )
        assert decision.phase == "OVERNIGHT"
        assert decision.phase_ends_at == datetime(2026, 11, 2, 8, 50, tzinfo=timezone.utc)


class TestOvernightCloseWindows:
    def test_cutoff_and_flatten_use_0350_only_when_enabled(self) -> None:
        late = _et("2026-10-07T02:30:00")
        early = _et("2026-10-07T02:10:00")
        assert is_extended_closing_window(
            "US", 90, late, overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 90, early, overnight_enabled=True,
        ) is False
        assert is_extended_closing_window(
            "US", 30, _et("2026-10-07T03:20:00"), overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 30, _et("2026-10-07T03:19:00"), overnight_enabled=True,
        ) is False
        assert is_extended_closing_window(
            "US", 30, _et("2026-10-07T03:49:00"), overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 30, _et("2026-10-07T03:50:00"), overnight_enabled=True,
        ) is False

    def test_post_span_unchanged_and_just_after_2000_is_not_cutoff(self) -> None:
        post = _et("2026-10-06T18:30:00")
        flatten = _et("2026-10-06T19:30:00")
        rollover = _et("2026-10-06T20:00:01")
        assert is_extended_closing_window(
            "US", 90, post, overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 90, _et("2026-10-06T18:29:00"), overnight_enabled=True,
        ) is False
        assert is_extended_closing_window(
            "US", 30, flatten, overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 30, _et("2026-10-06T19:59:00"), overnight_enabled=True,
        ) is True
        assert is_extended_closing_window(
            "US", 90, rollover, overnight_enabled=True,
        ) is False
        close = extended_last_executable_close(
            "US", post, overnight_enabled=True,
        )
        assert close == datetime(2026, 10, 6, 20, 0, tzinfo=_ET)


def test_outside_rth_for_phase_maps_overnight_separately() -> None:
    assert outside_rth_for_phase("PRE") == "ANY_TIME"
    assert outside_rth_for_phase("POST") == "ANY_TIME"
    assert outside_rth_for_phase("OVERNIGHT") == "OVERNIGHT"
    assert outside_rth_for_phase("RTH") is None
    assert outside_rth_for_phase("UNAVAILABLE") is None


class _FakeSessionModule:
    class OrderSide:
        Buy = "Buy"
        Sell = "Sell"

    class OrderType:
        LO = "LO"

    class TimeInForceType:
        Day = "Day"

    class OutsideRTH:
        AnyTime = "AnyTime"
        Overnight = "Overnight"


def test_broker_overnight_passes_sdk_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict[str, object]] = []

    class TradeContext:
        def submit_order(self, **kwargs: object) -> SimpleNamespace:
            called.append(kwargs)
            return SimpleNamespace(order_id="order-on", status="Submitted")

    monkeypatch.setattr(
        "app.core.broker._import_openapi", lambda: _FakeSessionModule,
    )
    gateway = BrokerGateway()
    gateway._trade_ctx = TradeContext()
    gateway._quote_ctx = object()

    gateway.submit_limit_order(
        "TSLA.US", "BUY", Decimal("1"), Decimal("100"), outside_rth="OVERNIGHT",
    )

    assert called[0]["outside_rth"] is _FakeSessionModule.OutsideRTH.Overnight


def test_broker_unknown_session_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.broker._import_openapi", lambda: _FakeSessionModule,
    )
    gateway = BrokerGateway()
    gateway._trade_ctx = SimpleNamespace()
    gateway._quote_ctx = object()
    with pytest.raises(ExtendedHoursUnsupportedError):
        gateway.submit_limit_order(
            "TSLA.US", "BUY", Decimal("1"), Decimal("100"), outside_rth="NIGHT",
        )


class _SubQuote:
    def __init__(self, last_done: object, timestamp: object) -> None:
        self.last_done = last_done
        self.timestamp = timestamp


class _QuoteItem:
    def __init__(self, **kwargs: object) -> None:
        self.symbol = kwargs.get("symbol", "TSLA.US")
        self.last_done = kwargs.get("last_done", 100)
        self.timestamp = kwargs.get("timestamp")
        self.pre_market_quote = kwargs.get("pre_market_quote")
        self.post_market_quote = kwargs.get("post_market_quote")
        self.overnight_quote = kwargs.get("overnight_quote")
        self.bid = 99
        self.ask = 101


def test_overnight_quote_ignored_unless_trading_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.broker.settings",
        SimpleNamespace(overnight_trading_effective=lambda: False),
    )
    item = _QuoteItem(
        last_done=100.0,
        timestamp=datetime(2026, 10, 6, 16, 0),
        overnight_quote=_SubQuote(
            Decimal("111.5"), datetime(2026, 10, 6, 21, 15),
        ),
    )
    price, _timestamp = BrokerGateway._extended_session_last_trade(item)
    assert price == 100.0


def test_freshest_print_selects_overnight_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _QuoteItem(
        last_done=100.0,
        timestamp=datetime(2026, 10, 6, 16, 0),
        overnight_quote=_SubQuote(
            Decimal("111.5"), datetime(2026, 10, 6, 21, 15),
        ),
    )
    monkeypatch.setattr(
        "app.core.broker.settings",
        SimpleNamespace(overnight_trading_effective=lambda: True),
    )
    price, timestamp = BrokerGateway._extended_session_last_trade(item)
    assert price == 111.5
    assert "21:15" in timestamp


class _FakeEntryBroker(BrokerGateway):
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, Decimal, Decimal, str | None]] = []
        self.calls: list[str] = []

    def get_positions(self) -> list[Position]:
        self.calls.append("get_positions")
        return []

    def estimate_margin_max_quantity(
        self, symbol: str, side: str, price: Decimal, currency: str | None = None,
    ) -> Decimal:
        self.calls.append("estimate_margin_max_quantity")
        return Decimal("10")

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
        *, outside_rth: str | None = None,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price, outside_rth))
        return OrderResult("entry-1", symbol, side, quantity, price, "SUBMITTED")

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "SUBMITTED", outside_rth="OVERNIGHT")

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "CANCELLED")


class _FrozenDateTime(datetime):
    _frozen = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        current = cls._frozen
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)  # type: ignore[arg-type]


def _pin_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    def is_trading_hours(market: str, at: datetime | None = None) -> bool:
        return _real_is_trading_hours(market, instant)

    def is_closing_window(
        market: str, minutes: int, at: datetime | None = None,
    ) -> bool:
        return _real_is_closing_window(market, minutes, instant)

    def is_extended_closing_window(
        market: str,
        minutes: int,
        at: datetime | None = None,
        *,
        overnight_enabled: bool = False,
    ) -> bool:
        return _real_is_extended_closing_window(
            market, minutes, instant, overnight_enabled=overnight_enabled,
        )

    _FrozenDateTime._frozen = instant.astimezone(timezone.utc)
    monkeypatch.setattr(execution, "is_trading_hours", is_trading_hours)
    monkeypatch.setattr(execution, "is_closing_window", is_closing_window)
    monkeypatch.setattr(
        execution, "is_extended_closing_window", is_extended_closing_window,
    )
    monkeypatch.setattr(execution, "datetime", _FrozenDateTime)


def _service(**kwargs: object) -> execution.TradeExecutionService:
    kwargs.setdefault("entry_cutoff_minutes_before_close", 90)
    return execution.TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda *_args: None,
        max_position_quantity=1_000_000,
        max_position_notional=1_000_000_000.0,
        max_risk_per_trade=10_000_000.0,
        stop_loss_pct=1.0,
        final_order_quote_check=lambda _broker, _symbol, _action, price: (
            execution.FinalOrderQuoteCheckResult(executable_price=price)
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def _execute_entry(
    service: execution.TradeExecutionService,
    broker: _FakeEntryBroker,
    *,
    bid: float = 99.95,
    ask: float = 100.0,
) -> execution.OrderStatus | None:
    return service.execute(
        "BUY",
        "TSLA.US",
        Quote("TSLA.US", 100, bid, ask, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
        market="US",
        trading_session_mode="ANY",
        expected_exit_price=Decimal("110"),
        min_profit_amount=Decimal("0"),
        fee_rate=Decimal("0"),
    )


class TestOvernightEntries:
    def test_overnight_entry_submits_overnight_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        broker = _FakeEntryBroker()
        result = _execute_entry(service, broker)
        assert result is not None and result.status == "SUBMITTED", result
        assert broker.submissions[0][-1] == "OVERNIGHT"

    def test_pre_and_post_still_submit_any_time(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for instant in (_et("2026-10-06T08:00:00"), _et("2026-10-06T17:30:00")):
            _pin_clock(monkeypatch, instant)
            service = _service(
                extended_hours_trading_enabled=True,
                overnight_trading_enabled=True,
            )
            broker = _FakeEntryBroker()
            result = _execute_entry(service, broker)
            assert result is not None and result.status == "SUBMITTED", result
            assert broker.submissions[0][-1] == "ANY_TIME"

    def test_flag_off_overnight_entry_is_session_skip(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=False,
        )
        broker = _FakeEntryBroker()
        result = _execute_entry(service, broker)
        assert result is not None and result.status == "SKIPPED"
        assert result.skip_category == "SESSION"
        assert broker.submissions == []


def test_pending_overnight_mismatch_latches_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
    service = _service(
        extended_hours_trading_enabled=True,
        overnight_trading_enabled=True,
    )
    broker = _FakeEntryBroker()
    submitted = _execute_entry(service, broker)
    assert submitted is not None and submitted.status == "SUBMITTED"
    pending = service.pending_order_for("TSLA.US")
    assert pending is not None and pending.extended_hours_key is not None
    assert pending.extended_hours_key[1] == "OVERNIGHT"
    pending = replace(pending, next_status_check_at=0, submitted_at=time.monotonic())

    service._reconcile_pending_order(
        pending, risk=RiskController(), notifier=ServerChanNotifier(""),
    )
    assert not any(
        key[1] == "OVERNIGHT" for key in service._extended_hours_unsupported
    )

    broker.get_order_status = lambda order_id: OrderStatusResult(  # type: ignore[method-assign]
        order_id, "SUBMITTED", outside_rth="ANY_TIME",
    )
    pending = replace(pending, next_status_check_at=0, submitted_at=time.monotonic())
    service._reconcile_pending_order(
        pending, risk=RiskController(), notifier=ServerChanNotifier(""),
    )
    assert any(
        key[1] == "OVERNIGHT" for key in service._extended_hours_unsupported
    )


class TestOvernightSpreadCap:
    def test_wide_overnight_entry_skipped_fee(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        broker = _FakeEntryBroker()
        # 0.2% of mid.
        result = _execute_entry(service, broker, bid=99.9, ask=100.1)
        assert result is not None and result.status == "SKIPPED"
        assert result.skip_category == "FEE"
        assert result.reason == "overnight spread too wide"
        assert broker.submissions == []

    def test_wide_spread_checked_without_expected_exit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        broker = _FakeEntryBroker()
        result = service.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 100, 99.9, 100.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            market="US",
            trading_session_mode="ANY",
        )
        assert result is not None and result.status == "SKIPPED"
        assert result.reason == "overnight spread too wide"
        assert broker.submissions == []

    def test_tight_overnight_entry_proceeds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        broker = _FakeEntryBroker()
        result = _execute_entry(service, broker, bid=99.975, ask=100.025)
        assert result is not None and result.status == "SUBMITTED", result

    def test_wide_exit_still_proceeds(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T21:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("1"), Decimal("100"))})
        broker = _FakeEntryBroker()
        broker.get_positions = lambda: [  # type: ignore[method-assign]
            Position("TSLA.US", "LONG", Decimal("1"), Decimal("100")),
        ]
        result = service.execute(
            "SELL",
            "TSLA.US",
            Quote("TSLA.US", 100, 99.0, 101.0, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            market="US",
            trading_session_mode="ANY",
            reduce_only=True,
            allow_loss_exit=True,
        )
        assert result is not None and result.status == "SUBMITTED", result
        assert broker.submissions[0][-1] == "OVERNIGHT"

    def test_rth_wide_spread_is_not_this_guard(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _et("2026-10-06T12:00:00"))
        service = _service(
            extended_hours_trading_enabled=True,
            overnight_trading_enabled=True,
        )
        broker = _FakeEntryBroker()
        result = _execute_entry(service, broker, bid=99.0, ask=101.0)
        assert result is None or result.reason != "overnight spread too wide"
        assert broker.submissions


class TestOvernightSettings:
    def test_default_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.delenv("AUTO_TRADE_OVERNIGHT_TRADING_ENABLED", raising=False)
        monkeypatch.delenv("LONGPORT_ENABLE_OVERNIGHT", raising=False)
        monkeypatch.chdir(tmp_path)
        settings = Settings()
        assert settings.overnight_trading_enabled is False
        assert settings.overnight_trading_effective() is False

    def test_flag_on_without_env_logs_one_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_OVERNIGHT_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)
        monkeypatch.delenv("LONGPORT_ENABLE_OVERNIGHT", raising=False)
        settings = Settings()
        with caplog.at_level(logging.ERROR):
            assert settings.overnight_trading_effective() is False
            assert settings.overnight_trading_effective() is False
        errors = [
            record for record in caplog.records
            if record.levelno >= logging.ERROR
            and "LONGPORT_ENABLE_OVERNIGHT" in record.message
        ]
        assert len(errors) == 1

    def test_env_true_and_extended_effective_is_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_OVERNIGHT_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)
        monkeypatch.setenv("LONGPORT_ENABLE_OVERNIGHT", "true")
        settings = Settings()
        assert settings.overnight_trading_effective() is True

    def test_paper_confirmed_is_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_OVERNIGHT_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")
        monkeypatch.setenv("LONGPORT_ENABLE_OVERNIGHT", "true")
        settings = Settings()
        assert settings.overnight_trading_effective() is False

    def test_env_one_is_not_exact_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AUTO_TRADE_OVERNIGHT_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)
        monkeypatch.setenv("LONGPORT_ENABLE_OVERNIGHT", "1")
        settings = Settings()
        assert settings.overnight_trading_effective() is False


def test_recenter_allowed_in_overnight_only_when_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = _et("2026-10-06T21:00:00")
    config = SimpleNamespace(trading_session_mode="ANY")
    monkeypatch.setattr(
        recenter.settings, "extended_hours_trading_enabled", True, raising=False,
    )
    monkeypatch.setattr(
        recenter.settings, "paper_account_confirmed", False, raising=False,
    )
    monkeypatch.setattr(
        recenter.settings, "overnight_trading_enabled", False, raising=False,
    )
    monkeypatch.delenv("LONGPORT_ENABLE_OVERNIGHT", raising=False)
    assert IntervalRecenterService._session_allows_recenter(
        config, "US", instant,
    ) is False
    monkeypatch.setattr(
        recenter.settings, "overnight_trading_enabled", True, raising=False,
    )
    monkeypatch.setenv("LONGPORT_ENABLE_OVERNIGHT", "true")
    assert IntervalRecenterService._session_allows_recenter(
        config, "US", instant,
    ) is True
