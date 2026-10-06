# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""P3b: funded-margin exception no longer forbids extended-hours entries.

Owner decision plus research: the 15-minute forced liquidation applies only
to the separate Longbridge intraday-financing account, not the ordinary
margin account this exception binds. Configured + extended enabled stays
enabled. The final-submit re-check allows an executable US PRE/POST phase
when extended-hours trading is effective, and still skips inside the
entry cutoff anchored to the extended close. The configured exception
uses the ordinary 45/15 floors (owner decision 2026-10-07).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.core.broker import OrderResult, OrderStatusResult, Position, Quote
from app.core.execution_session import (
    is_extended_closing_window as _real_is_extended_closing_window,
    resolve_execution_session as _real_resolve,
)
from app.core.market_calendar import (
    is_closing_window as _real_is_closing_window,
    is_trading_hours as _real_is_trading_hours,
)
from app.core.risk import RiskController
from app.services import trade_execution_service as trade_svc_module
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    TradeExecutionService,
)

FP = "b" * 64
_ET = ZoneInfo("America/New_York")
# 2026-10-06 is a Tuesday, a normal US session.
_PRE = datetime(2026, 10, 6, 5, 0, tzinfo=_ET)
_POST = datetime(2026, 10, 6, 17, 0, tzinfo=_ET)
_CUTOFF = datetime(2026, 10, 6, 18, 31, tzinfo=_ET)
_AFTER_CLOSE = datetime(2026, 10, 6, 20, 30, tzinfo=_ET)


class _FakeBroker:
    def __init__(self, margin_max: str = "8") -> None:
        self.margin_max = Decimal(margin_max)
        self.submissions: list[tuple[str, str, Decimal, Decimal, str | None]] = []
        self.positions: list[Position] = []

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        return self.margin_max

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        outside_rth: str | None = None,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price, outside_rth))
        return OrderResult(
            f"order-{len(self.submissions)}",
            symbol,
            side,
            quantity,
            price,
            "SUBMITTED",
        )

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "SUBMITTED")

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "CANCELLED")


class _NullNotifier:
    def notify_order(self, *_args: object, **_kwargs: object) -> None:
        return None

    def notify_risk_event(self, *_args: object, **_kwargs: object) -> None:
        return None


def _pin(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setattr(
        trade_svc_module,
        "is_trading_hours",
        lambda market, at=None: _real_is_trading_hours(market, instant),
    )
    monkeypatch.setattr(
        trade_svc_module,
        "resolve_execution_session",
        lambda market, at=None: _real_resolve(market, instant),
    )
    monkeypatch.setattr(
        trade_svc_module,
        "is_closing_window",
        lambda market, minutes, at=None: _real_is_closing_window(
            market, minutes, instant,
        ),
    )
    monkeypatch.setattr(
        trade_svc_module,
        "is_extended_closing_window",
        lambda market, minutes, at=None: _real_is_extended_closing_window(
            market, minutes, instant,
        ),
    )
    monkeypatch.setattr(
        trade_svc_module,
        "is_opening_warmup",
        lambda *_args, **_kwargs: False,
    )


def _service(
    *,
    extended: bool,
    paper: bool = False,
) -> TradeExecutionService:
    svc = TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args, **_kwargs: True,
        record_risk_event=lambda *_args: None,
        record_order_skipped=lambda *_args, **_kwargs: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=0.25,
        margin_safety_factor=1.0,
        paper_account_confirmed=paper,
        extended_hours_trading_enabled=extended,
        entry_cutoff_minutes_before_close=90,
        final_order_quote_check=(
            lambda _b, _s, _a, price: FinalOrderQuoteCheckResult(
                price, bid=price, ask=price,
            )
        ),
    )
    svc.funded_margin_fingerprint_provider = lambda: FP
    svc.funded_margin_enabled = True
    svc.funded_margin_account_fingerprint = FP
    svc.funded_margin_requested_quantity = 1000
    svc.funded_margin_requested_notional = 25000.0
    svc.funded_margin_requested_risk = 250.0
    svc.raw_strategy_max_position_quantity = 1000
    svc.raw_strategy_max_position_notional = 25000.0
    svc.raw_strategy_max_risk_per_trade = 250.0
    return svc


def _buy(
    svc: TradeExecutionService,
    broker: _FakeBroker,
):
    return svc.execute(
        "BUY",
        "TSLA.US",
        Quote("TSLA.US", 100, 99.9, 100.1, ""),
        broker,
        RiskController(),
        _NullNotifier(),
        "USD",
        market="US",
    )


def _clear_funded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AUTO_TRADE_FUNDED_MARGIN_ENABLED",
        "AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL",
        "AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE",
        "AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED",
        "AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED",
    ):
        monkeypatch.delenv(name, raising=False)


def _arm_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_funded(monkeypatch)
    monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_ENABLED", "true")
    monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_ACCOUNT_FINGERPRINT", FP)
    monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_QUANTITY", "1000")
    monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_MAX_POSITION_NOTIONAL", "25000")
    monkeypatch.setenv("AUTO_TRADE_FUNDED_MARGIN_MAX_RISK_PER_TRADE", "250")


class TestConfiguredExtendedHoursStaysEnabled:
    def test_configured_plus_extended_enabled_stays_enabled_and_effective(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _arm_configured(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.delenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", raising=False)

        settings = Settings()

        assert settings.funded_margin_configuration().configured is True
        assert settings.extended_hours_trading_enabled is True
        assert settings.extended_hours_trading_effective() is True
        # Owner decision 2026-10-07: configured keeps the ordinary floors.
        assert settings.hard_entry_cutoff_minutes_before_close == 45
        assert settings.hard_flatten_minutes_before_close == 15
        assert settings.extended_hours_protective_exits_enabled is False

    def test_configured_plus_extended_disabled_stays_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _arm_configured(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "false")

        settings = Settings()

        assert settings.extended_hours_trading_enabled is False
        assert settings.extended_hours_trading_effective() is False
        assert settings.hard_entry_cutoff_minutes_before_close == 45
        assert settings.hard_flatten_minutes_before_close == 15

    def test_configured_extended_on_paper_is_not_effective(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _arm_configured(monkeypatch)
        monkeypatch.setenv("AUTO_TRADE_EXTENDED_HOURS_TRADING_ENABLED", "true")
        monkeypatch.setenv("AUTO_TRADE_PAPER_ACCOUNT_CONFIRMED", "true")

        settings = Settings()

        # Paper attestation disarms the funded exception. Extended stays
        # operator-enabled but is not effective on a paper account.
        assert settings.funded_margin_configuration().configured is False
        assert settings.extended_hours_trading_enabled is True
        assert settings.extended_hours_trading_effective() is False


class TestFinalSubmitExtendedRecheck:
    def test_pre_0500_passes_and_submits_any_time_within_funded_caps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _PRE)
        svc = _service(extended=True)
        broker = _FakeBroker(margin_max="8")

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1
        symbol, side, quantity, _price, outside_rth = broker.submissions[0]
        assert (symbol, side, outside_rth) == ("TSLA.US", "BUY", "ANY_TIME")
        assert quantity == Decimal("8")
        assert quantity <= Decimal("1000")

    def test_post_1700_passes_final_recheck(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _POST)
        svc = _service(extended=True)
        broker = _FakeBroker(margin_max="8")

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SUBMITTED"
        assert broker.submissions[0][4] == "ANY_TIME"
        assert broker.submissions[0][2] == Decimal("8")

    def test_1831_inside_extended_cutoff_skips_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _CUTOFF)
        svc = _service(extended=True)
        broker = _FakeBroker()

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SKIPPED"
        assert "cutoff" in str(status.reason)
        assert broker.submissions == []

    def test_2030_unavailable_skips_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _AFTER_CLOSE)
        svc = _service(extended=True)
        broker = _FakeBroker()

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []

    def test_funded_effective_extended_not_effective_pre_skips(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _PRE)
        svc = _service(extended=False)
        broker = _FakeBroker()

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []

    def test_flag_off_pre_unchanged_session_skip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, _PRE)
        svc = _service(extended=False)
        svc.funded_margin_enabled = False
        broker = _FakeBroker()

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []


class TestRthToPostBoundary:
    """An RTH approval must not become a plain POST submission."""

    def test_rth_approval_crossing_1600_skips_without_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before_close = datetime(2026, 10, 6, 15, 59, 59, tzinfo=_ET)
        after_close = datetime(2026, 10, 6, 16, 0, 1, tzinfo=_ET)
        phase = {"instant": before_close}

        def is_trading_hours(market: str, at=None) -> bool:
            return _real_is_trading_hours(market, phase["instant"])

        def resolve_execution_session(market: str, at=None):
            return _real_resolve(market, phase["instant"])

        def is_closing_window(market: str, minutes: int, at=None) -> bool:
            return _real_is_closing_window(market, minutes, phase["instant"])

        def is_extended_closing_window(market: str, minutes: int, at=None) -> bool:
            return _real_is_extended_closing_window(
                market, minutes, phase["instant"],
            )

        monkeypatch.setattr(trade_svc_module, "is_trading_hours", is_trading_hours)
        monkeypatch.setattr(
            trade_svc_module, "resolve_execution_session", resolve_execution_session,
        )
        monkeypatch.setattr(trade_svc_module, "is_closing_window", is_closing_window)
        monkeypatch.setattr(
            trade_svc_module,
            "is_extended_closing_window",
            is_extended_closing_window,
        )
        monkeypatch.setattr(
            trade_svc_module, "is_opening_warmup", lambda *_a, **_k: False,
        )
        svc = _service(extended=True)
        broker = _FakeBroker(margin_max="8")
        original = broker.estimate_margin_max_quantity

        def crossing_estimate(*args, **kwargs):
            phase["instant"] = after_close
            return original(*args, **kwargs)

        broker.estimate_margin_max_quantity = crossing_estimate  # type: ignore[method-assign]

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []

    def test_rth_approval_that_stays_in_rth_submits_without_outside_rth(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pin(monkeypatch, datetime(2026, 10, 6, 12, 0, tzinfo=_ET))
        svc = _service(extended=True)
        broker = _FakeBroker(margin_max="8")

        status = _buy(svc, broker)

        assert status is not None
        assert status.status == "SUBMITTED"
        assert broker.submissions[0][4] is None


def test_env_example_no_longer_says_funded_forces_extended_off() -> None:
    text = Path(__file__).resolve().parents[2].joinpath(".env.example").read_text()
    block = text.split("AUTO_TRADE_FUNDED_MARGIN_ENABLED", 1)[0]
    funded = block.rsplit("# 注资账户满融开仓例外", 1)[-1]
    assert "强制关闭扩展时段" not in funded
