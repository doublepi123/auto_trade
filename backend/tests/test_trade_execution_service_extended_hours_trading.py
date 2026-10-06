"""AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED — execution-layer behaviour.

Flag effective (on + not paper) => US PRE/POST long entries and reduce-only
exits may submit as LO/Day with outside_rth=ANY_TIME. UNAVAILABLE phases
(overnight, weekend, holiday, half-day post), HK, RTH_ONLY mode, and every
flag-off / paper configuration keep today's behaviour byte-for-byte.

The calendar functions are wrapped around the REAL implementations with a
pinned instant — clock injection only, no fake phase logic.
"""

from __future__ import annotations

from datetime import datetime
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
from app.core.execution_session import (
    is_extended_closing_window as _real_is_extended_closing_window,
    resolve_execution_session as _real_resolve,
)
from app.core.market_calendar import (
    is_closing_window as _real_is_closing_window,
    is_trading_hours as _real_is_trading_hours,
)
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import trade_execution_service as execution


_ET = ZoneInfo("America/New_York")


def _et(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_ET).astimezone(
        datetime.now().tzinfo or _ET,
    )


class _FakeEntryBroker(BrokerGateway):
    """Records every mutation; positions/margin controllable per test."""

    def __init__(self, *, positions: list[Position] | None = None) -> None:
        self.submissions: list[tuple[str, str, Decimal, Decimal, str | None]] = []
        self.calls: list[str] = []
        self._positions = positions if positions is not None else []

    def get_positions(self) -> list[Position]:
        self.calls.append("get_positions")
        return self._positions

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
        return OrderStatusResult(order_id, "SUBMITTED")

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        self.calls.append(f"cancel:{order_id}")
        return OrderStatusResult(order_id, "CANCELLED")


def _pin_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Pin the service's calendar reads to ``instant`` using the REAL logic."""

    def is_trading_hours(market: str, at: datetime | None = None) -> bool:
        return _real_is_trading_hours(market, instant)

    def resolve_execution_session(
        market: str,
        at: datetime | None = None,
        *,
        overnight_enabled: bool = False,
    ):
        return _real_resolve(
            market, instant, overnight_enabled=overnight_enabled,
        )

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

    monkeypatch.setattr(execution, "is_trading_hours", is_trading_hours)
    monkeypatch.setattr(
        execution, "resolve_execution_session", resolve_execution_session,
    )
    monkeypatch.setattr(execution, "is_closing_window", is_closing_window)
    monkeypatch.setattr(
        execution, "is_extended_closing_window", is_extended_closing_window,
    )


def _service(**kwargs: object) -> execution.TradeExecutionService:
    kwargs.setdefault("entry_cutoff_minutes_before_close", 45)
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
    market: str = "US",
    symbol: str = "TSLA.US",
    session_mode: str = "ANY",
) -> execution.OrderStatus | None:
    return service.execute(
        "BUY",
        symbol,
        Quote(symbol, 100, 99.9, 100.1, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
        market=market,
        trading_session_mode=session_mode,
    )


def _execute_exit(
    service: execution.TradeExecutionService,
    broker: _FakeEntryBroker,
    *,
    market: str = "US",
    symbol: str = "TSLA.US",
    session_mode: str = "ANY",
) -> execution.OrderStatus | None:
    return service.execute(
        "SELL",
        symbol,
        Quote(symbol, 100, 99.9, 100.1, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
        market=market,
        trading_session_mode=session_mode,
        reduce_only=True,
        allow_loss_exit=True,
    )


# 2026-10-19 is a Monday; 2026-10-24 a Saturday; 2026-12-25 the Christmas holiday.
_INSTANTS = {
    "pre": datetime(2026, 10, 19, 8, 0, tzinfo=_ET),        # 08:00 ET Monday
    "post": datetime(2026, 10, 19, 17, 30, tzinfo=_ET),     # 17:30 ET Monday
    "rth": datetime(2026, 10, 19, 12, 0, tzinfo=_ET),       # 12:00 ET Monday
    "overnight_23": datetime(2026, 10, 19, 23, 0, tzinfo=_ET),
    "overnight_02": datetime(2026, 10, 20, 2, 0, tzinfo=_ET),
    "saturday": datetime(2026, 10, 24, 12, 0, tzinfo=_ET),
    "holiday": datetime(2026, 12, 25, 12, 0, tzinfo=_ET),
    "half_day_post": datetime(2026, 11, 27, 14, 0, tzinfo=_ET),  # Black Friday
    "half_day_1220": datetime(2026, 11, 27, 12, 20, tzinfo=_ET),  # Black Friday
    "post_1920": datetime(2026, 10, 19, 19, 20, tzinfo=_ET),
    "post_1910": datetime(2026, 10, 19, 19, 10, tzinfo=_ET),
    "rth_1520": datetime(2026, 10, 19, 15, 20, tzinfo=_ET),
    "post_1950": datetime(2026, 10, 19, 19, 50, tzinfo=_ET),
}


class TestFlagEffectiveEntries:
    @pytest.mark.parametrize(
        "phase",
        ["overnight_23", "overnight_02", "saturday", "holiday", "half_day_post"],
    )
    def test_unavailable_phases_buy_refused_zero_calls(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert broker.submissions == []
        assert broker.calls == []

    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_pre_post_buy_submits_with_any_time(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SUBMITTED", result
        assert broker.submissions
        assert broker.submissions[0][-1] == "ANY_TIME"
        assert broker.submissions[0][1] == "BUY"

    def test_hk_buy_refused_even_with_flag(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, datetime(2026, 10, 19, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")))
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker, market="HK", symbol="0700.HK")

        assert result is not None and result.status == "SKIPPED"
        assert broker.submissions == []
        assert broker.calls == []

    def test_rth_buy_unchanged_no_outside_rth(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS["rth"])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SUBMITTED"
        assert broker.submissions[0][-1] is None

    def test_rth_only_mode_still_refuses_non_rth_entry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS["post"])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker, session_mode="RTH_ONLY")

        assert result is not None and result.status == "SKIPPED"
        assert "non-RTH" in (result.reason or "")
        assert broker.submissions == []
        assert broker.calls == []


class TestCloseWindowsWithFlagEffective:
    """Entry cutoff / flatten measured from the last executable phase end."""

    def _entry_service(self, **kwargs: object) -> execution.TradeExecutionService:
        return _service(extended_hours_trading_enabled=True, **kwargs)

    def test_entry_refused_at_1920_et(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 19:20 ET Monday is after the 19:15 cutoff (45 min before 20:00).
        _pin_clock(monkeypatch, _INSTANTS["post_1920"])
        service = self._entry_service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "entry cutoff" in (result.reason or "")
        assert broker.submissions == []
        assert broker.calls == []

    def test_entry_allowed_at_1910_et(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 19:10 ET Monday is before the 19:15 cutoff.
        _pin_clock(monkeypatch, _INSTANTS["post_1910"])
        service = self._entry_service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SUBMITTED", result
        assert broker.submissions[0][-1] == "ANY_TIME"

    def test_no_cutoff_at_1520_et_with_flag_effective(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 15:20 ET is inside the LEGACY 45-min window but far from 20:00.
        _pin_clock(monkeypatch, _INSTANTS["rth_1520"])
        service = self._entry_service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SUBMITTED", result

    def test_flag_off_cutoff_at_1520_et_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 15:20 ET inside the legacy window: refused exactly as today.
        _pin_clock(monkeypatch, _INSTANTS["rth_1520"])
        service = _service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "entry cutoff" in (result.reason or "")

    def test_paper_with_flag_cutoff_at_1520_et_like_flag_off(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS["rth_1520"])
        service = _service(
            extended_hours_trading_enabled=True,
            paper_account_confirmed=True,
        )
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "entry cutoff" in (result.reason or "")

    def test_half_day_cutoff_relative_to_1300_et(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Black Friday: last executable phase ends 13:00 ET; the 45-minute
        # cutoff is 12:15 ET, so 12:20 ET refuses an entry.
        _pin_clock(monkeypatch, _INSTANTS["half_day_1220"])
        service = self._entry_service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "entry cutoff" in (result.reason or "")


class TestFlagOffAndPaperParity:
    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_flag_off_keeps_any_mode_buy_refusal(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service()
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "ANY mode cannot open a long position" in (result.reason or "")
        assert broker.submissions == []
        assert broker.calls == []

    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_paper_with_flag_on_behaves_like_off(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(
            extended_hours_trading_enabled=True,
            paper_account_confirmed=True,
        )
        broker = _FakeEntryBroker()

        result = _execute_entry(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert "ANY mode cannot open a long position" in (result.reason or "")
        assert broker.submissions == []
        assert broker.calls == []


class TestFlagEffectiveExits:
    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_pre_post_reduce_only_sell_submits_any_time(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker(
            positions=[Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))],
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})

        result = _execute_exit(service, broker)

        assert result is not None and result.status == "SUBMITTED", result
        assert broker.submissions
        assert broker.submissions[0][-1] == "ANY_TIME"
        assert broker.submissions[0][1] == "SELL"

    def test_overnight_exit_intent_held_zero_orders_no_pause(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin_clock(monkeypatch, _INSTANTS["overnight_23"])
        service = _service(extended_hours_trading_enabled=True)
        broker = _FakeEntryBroker(
            positions=[Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))],
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})

        result = _execute_exit(service, broker)

        assert result is not None and result.status == "SKIPPED"
        assert broker.submissions == []
        # Intent held, not paused: the position stays tracked for a later
        # executable phase, with no risk pause raised.
        position = service.tracked_position("TSLA.US")
        assert position is not None and position.quantity == Decimal("10")
        assert result.reason is not None and "non-RTH" in result.reason
        assert "overnight" in result.reason

    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_paper_protective_exits_refusal_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        # The paper refusal is the OLDER protective-exits flag's behaviour:
        # paper + protective_exits ON (trading flag off) keeps refusing with
        # the unchanged paper reason on the RTH_ONLY exit path.
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(
            extended_hours_protective_exits_enabled=True,
            paper_account_confirmed=True,
        )
        broker = _FakeEntryBroker(
            positions=[Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))],
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})

        result = _execute_exit(service, broker, session_mode="RTH_ONLY")

        assert result is not None
        assert "paper account" in (result.reason or "")
        assert broker.submissions == []


class TestPaperWithFlagMatchesFlagOff:
    """CONTRACT B/E: paper + flag ON must equal flag OFF on every path.

    Parent review found the ANY-mode non-RTH reduce-only exit gate read the
    raw flag only: on paper+flag-on a POST-phase SELL was SKIPPED with the
    paper reason while flag-off submitted a plain order. These tests pin
    parity for exits and for the shared-decision enable-gate reason text.
    """

    def _flag_off_outcome(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> tuple[
        execution.OrderStatus | None,
        list[tuple[str, str, Decimal, Decimal, str | None]],
        str | None,
    ]:
        """Run the flag-off ANY-mode POST reduce-only SELL; return outcome."""
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service()
        broker = _FakeEntryBroker(
            positions=[Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))],
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})
        result = _execute_exit(service, broker)
        reason = result.reason if result is not None else None
        return result, broker.submissions, reason

    @pytest.mark.parametrize("phase", ["pre", "post"])
    def test_paper_with_flag_on_any_mode_exit_matches_flag_off(
        self, monkeypatch: pytest.MonkeyPatch, phase: str,
    ) -> None:
        # Flag off: no ANY-mode exit gate exists, the reduce-only SELL in a
        # POST phase submits as a plain LO/Day order (no outside_rth).
        off_result, off_submissions, _ = self._flag_off_outcome(monkeypatch, phase)
        assert off_result is not None and off_result.status == "SUBMITTED", off_result
        # Paper + flag ON must produce the SAME outcome, not the paper skip.
        _pin_clock(monkeypatch, _INSTANTS[phase])
        service = _service(
            extended_hours_trading_enabled=True,
            paper_account_confirmed=True,
        )
        broker = _FakeEntryBroker(
            positions=[Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))],
        )
        service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})

        result = _execute_exit(service, broker)

        assert result is not None and result.status == "SUBMITTED", result
        assert result.status == off_result.status
        assert broker.submissions == off_submissions
        assert broker.submissions[0][-1] is None
        assert "paper account" not in (result.reason or "")

    def test_paper_with_flag_on_exit_decision_reason_matches_flag_off(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The shared-decision enable gate must also keep flag-off semantics
        # on paper+flag-on: "extended-hours protective exits are disabled",
        # NOT the paper reason (reason is user-visible via the runner's
        # last_action_message on the RTH_ONLY protective-exit wait path).
        _pin_clock(monkeypatch, _INSTANTS["post"])
        off = _service()
        off_decision = off.extended_hours_exit_decision(
            action="SELL", symbol="TSLA.US", market="US", reduce_only=True,
        )
        assert not off_decision.permitted
        assert off_decision.reason == "extended-hours protective exits are disabled"

        paper = _service(
            extended_hours_trading_enabled=True,
            paper_account_confirmed=True,
        )
        paper_decision = paper.extended_hours_exit_decision(
            action="SELL", symbol="TSLA.US", market="US", reduce_only=True,
        )
        assert not paper_decision.permitted
        assert paper_decision.reason == off_decision.reason
