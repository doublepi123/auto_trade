# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Funded full-margin sizing exception — account-bound, default OFF.

Owner decision (2026-10-05/06): a REAL margin account may use the broker's
full margin buying power for the primary range-lane US BUY only, through a
default-OFF exception that binds to the CURRENT credential fingerprint.
Flag-off / paper / unbound paths must stay byte-for-byte identical to the
clamped funded caps (100 / 5000 / 250) with ZERO extra broker calls.
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal

import pytest

from app.core.broker import (
    BrokerGateway,
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
)
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import trade_execution_service as trade_svc_module
from app.services.trade_execution_service import (
    ApprovedOrder,
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
    _PendingOrder,
    _PreSubmitRiskRequest,
)

FP_A = "a" * 64
FP_B = "b" * 64


class _FakeMarginBroker(BrokerGateway):
    """Hand-written fake: records every margin call for shape assertions."""

    def __init__(
        self,
        margin_max: str = "20",
        positions: list[Position] | None = None,
    ) -> None:
        self.margin_calls: list[tuple[str, str, Decimal, str | None]] = []
        self.margin_max = Decimal(margin_max)
        self.positions = list(positions or [])
        self.submissions: list[OrderResult] = []
        self.status_results: dict[str, OrderStatusResult] = {}
        self.cancel_calls: list[str] = []

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        self.margin_calls.append((symbol, side, price, currency))
        return self.margin_max

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        result = OrderResult(
            f"order-{len(self.submissions)}",
            symbol,
            side,
            quantity,
            price,
            "SUBMITTED",
        )
        self.submissions.append(result)
        return result

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return self.status_results[order_id]

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        self.cancel_calls.append(order_id)
        return OrderStatusResult(order_id, "CANCELLED")


def _make_service(
    *,
    provider=None,
    paper: bool = False,
    raw_caps: tuple[int, float, float] | None = None,
    clamped_caps: tuple[int, float, float] = (100, 5000.0, 250.0),
    stop_loss_pct: float = 0.25,
    margin_safety_factor: float | None = 1.0,
    skips: list[str] | None = None,
) -> TradeExecutionService:
    """Build the service and ARM the exception through plain attributes.

    Arming by attribute assignment (not constructor kwargs) keeps the RED
    run a genuine behavioural failure: without the implementation the
    attributes are inert and sizing keeps today's clamped behaviour.
    """
    if skips is None:
        skips = []
    if raw_caps is None:
        raw_caps = clamped_caps
    svc = TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda *_reason: None,
        record_order_skipped=(
            lambda _s, _a, _r, payload: skips.append(str(payload["skip_category"]))
        ),
        max_position_quantity=clamped_caps[0],
        max_position_notional=clamped_caps[1],
        max_risk_per_trade=clamped_caps[2],
        stop_loss_pct=stop_loss_pct,
        margin_safety_factor=margin_safety_factor,
        paper_account_confirmed=paper,
        final_order_quote_check=(
            lambda _b, _s, _a, p: FinalOrderQuoteCheckResult(
                p, bid=p, ask=p,
            )
        ),
    )
    svc.funded_margin_fingerprint_provider = (
        provider if provider is not None else (lambda: FP_A)
    )
    svc.funded_margin_enabled = True
    svc.funded_margin_account_fingerprint = FP_A
    svc.funded_margin_requested_quantity = 1000
    svc.funded_margin_requested_notional = 25000.0
    svc.funded_margin_requested_risk = 250.0
    svc.raw_strategy_max_position_quantity = raw_caps[0]
    svc.raw_strategy_max_position_notional = raw_caps[1]
    svc.raw_strategy_max_risk_per_trade = raw_caps[2]
    return svc


def _disarm(svc: TradeExecutionService) -> TradeExecutionService:
    svc.funded_margin_enabled = False
    svc.funded_margin_fingerprint_provider = None
    return svc


class _ArmedMixin:
    @pytest.fixture(autouse=True)
    def _open_market(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            trade_svc_module, "is_trading_hours", lambda _m: True,
        )
        monkeypatch.setattr(
            trade_svc_module, "is_closing_window", lambda *_args: False,
        )
        monkeypatch.setattr(
            trade_svc_module, "is_opening_warmup", lambda *_args: False,
        )


class TestFundedMarginBindingMatched(_ArmedMixin):
    def test_range_buy_sizes_to_full_margin_when_caps_do_not_bind(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="20")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        # margin_max_qty 20 @ factor 1.0; caps 1000/25000/250 do not bind.
        assert qty == 20
        assert broker.margin_calls == [
            ("TSLA.US", "BUY", Decimal("379"), "USD"),
        ]

    def test_cap_bound_case_proceeds_with_capped_quantity_and_records_factor(
        self,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="200")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        # Notional binds: 25000/379 = 65.96 -> 65 (< 200 capacity).
        assert qty == 65
        diagnostics = svc.funded_margin_diagnostics()
        assert diagnostics["last_limiting_factor"] == "NOTIONAL_CAP"

    def test_cap_bound_sizing_marks_the_execution_context(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="200")
        svc._active_execution_context = {}

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 65
        # The limiting factor rides along with the order's ledger context
        # so the capped entry is never silent in the trade-event payload.
        assert svc._active_execution_context.get(
            "funded_margin_limiting_factor",
        ) == "NOTIONAL_CAP"

    def test_risk_cap_records_risk_limiting_factor(self) -> None:
        svc = _make_service(
            raw_caps=(1000, 250000.0, 250.0), stop_loss_pct=1.0,
        )
        broker = _FakeMarginBroker(margin_max="2000")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        # Risk binds: 250 / (379 * 1%) = 65.96 -> 65.
        assert qty == 65
        assert (
            svc.funded_margin_diagnostics()["last_limiting_factor"]
            == "RISK_CAP"
        )

    def test_quantity_cap_records_quantity_limiting_factor(self) -> None:
        svc = _make_service(raw_caps=(50, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="200")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 50
        assert (
            svc.funded_margin_diagnostics()["last_limiting_factor"]
            == "QUANTITY_CAP"
        )

    def test_factor_above_one_sizes_zero(self) -> None:
        svc = _make_service(
            raw_caps=(1000, 25000.0, 250.0), margin_safety_factor=1.5,
        )
        broker = _FakeMarginBroker(margin_max="20")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 0

    def test_factor_zero_sizes_zero(self) -> None:
        svc = _make_service(
            raw_caps=(1000, 25000.0, 250.0), margin_safety_factor=0.0,
        )
        broker = _FakeMarginBroker(margin_max="20")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 0


class TestFundedMarginBindingFailClosed(_ArmedMixin):
    def test_mismatch_uses_clamped_caps_with_no_extra_broker_calls(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc.funded_margin_account_fingerprint = FP_B
        broker = _FakeMarginBroker(margin_max="1000")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        # Clamped funded caps: notional 5000/379 = 13.19 -> 13.
        assert qty == 13
        # Identical call shape/count to the flag-off path: ONE margin call.
        assert broker.margin_calls == [
            ("TSLA.US", "BUY", Decimal("379"), "USD"),
        ]
        assert (
            svc.funded_margin_diagnostics()["binding_status"] == "MISMATCH"
        )

    def test_incomplete_credentials_fail_closed(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc.funded_margin_fingerprint_provider = lambda: ""
        broker = _FakeMarginBroker(margin_max="1000")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 13
        assert broker.margin_calls == [
            ("TSLA.US", "BUY", Decimal("379"), "USD"),
        ]
        assert svc.funded_margin_diagnostics()["binding_status"] == (
            "CREDENTIALS_INCOMPLETE"
        )

    def test_paper_account_never_uses_the_exception(self) -> None:
        svc = _make_service(paper=True, raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="1000")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 13
        assert svc.funded_margin_diagnostics()["binding_status"] == "PAPER"

    def test_disabled_never_uses_the_exception(self) -> None:
        svc = _disarm(_make_service(raw_caps=(1000, 25000.0, 250.0)))
        broker = _FakeMarginBroker(margin_max="1000")

        qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert qty == 13
        assert svc.funded_margin_diagnostics()["binding_status"] == "DISABLED"

    def test_flag_off_broker_calls_byte_identical_to_legacy(self) -> None:
        legacy = _disarm(_make_service(raw_caps=None))
        modern = _disarm(_make_service())
        legacy_broker = _FakeMarginBroker(margin_max="1000")
        modern_broker = _FakeMarginBroker(margin_max="1000")

        legacy_qty = legacy._entry_quantity_from_margin_power(
            legacy_broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )
        modern_qty = modern._entry_quantity_from_margin_power(
            modern_broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )

        assert legacy_qty == modern_qty == 13
        assert legacy_broker.margin_calls == modern_broker.margin_calls
        assert len(modern_broker.margin_calls) == 1

    def test_passive_lane_never_uses_the_exception(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc._active_execution_context = {"passive_lane": "SPY_PASSIVE"}

        assert svc._range_entry_limits_for("TSLA.US", "BUY", "US") is None
        # The binding itself stays MATCHED; the lane is simply excluded.
        assert (
            svc.funded_margin_diagnostics()["binding_status"] == "MATCHED"
        )

    def test_opening_momentum_source_never_uses_the_exception(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc._active_execution_context = {
            "strategy_source": "OPENING_MOMENTUM",
        }

        assert svc._range_entry_limits_for("TSLA.US", "BUY", "US") is None

    def test_sell_short_never_uses_the_exception(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))

        assert svc._range_entry_limits_for("TSLA.US", "SELL_SHORT", "US") is None

    def test_hk_market_never_uses_the_exception(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))

        assert svc._range_entry_limits_for("00005.HK", "BUY", "HK") is None
        assert svc._range_entry_limits_for("00005.HK", "BUY") is None


class TestFundedMarginPreSubmit(_ArmedMixin):
    def _request(
        self, qty: str = "20", price: str = "379",
    ) -> _PreSubmitRiskRequest:
        return _PreSubmitRiskRequest(
            action="BUY",
            symbol="TSLA.US",
            quantity=Decimal(qty),
            price=Decimal(price),
        )

    def test_matched_pre_submit_re_estimates_capacity_at_approved_price(
        self,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="20")

        sizing_qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )
        assert sizing_qty == 20
        approved = svc.pre_submit_risk_check(self._request(), broker)

        assert isinstance(approved, ApprovedOrder)
        assert approved.quantity == Decimal("20")
        assert approved.price == Decimal("379")
        # Sizing + pre-submit each estimate capacity at the same shape.
        assert broker.margin_calls == [
            ("TSLA.US", "BUY", Decimal("379"), "USD"),
            ("TSLA.US", "BUY", Decimal("379"), "USD"),
        ]

    def test_rotation_between_sizing_and_pre_submit_rejects(self) -> None:
        current = {"fp": FP_A}
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc.funded_margin_fingerprint_provider = lambda: current["fp"]
        broker = _FakeMarginBroker(margin_max="20")

        sizing_qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )
        assert sizing_qty == 20

        # Credential rotation: the CURRENT fingerprint no longer matches.
        current["fp"] = FP_B
        result = svc.pre_submit_risk_check(self._request("20"), broker)

        # Fail closed: the clamped funded caps reject the oversized order.
        assert isinstance(result, OrderStatus)
        assert result.status == "SKIPPED"
        assert "exceeds cap" in str(result.reason)

    def test_approved_price_higher_with_smaller_capacity_rejects(self) -> None:
        class _ShrinkingBroker(_FakeMarginBroker):
            def estimate_margin_max_quantity(
                self,
                symbol: str,
                side: str,
                price: Decimal,
                currency: str | None = None,
            ) -> Decimal:
                self.margin_calls.append((symbol, side, price, currency))
                # Capacity collapses at the second (pre-submit) estimate.
                if len(self.margin_calls) > 1:
                    return Decimal("5")
                return Decimal("20")

        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _ShrinkingBroker(margin_max="20")
        sizing_qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )
        assert sizing_qty == 20

        # Fresh executable price (380) is above the request price (379):
        # approved price becomes 380 and capacity re-estimated there is 5.
        svc._final_order_quote_check = (
            lambda _b, _s, _a, _p: FinalOrderQuoteCheckResult(
                Decimal("380"), bid=Decimal("380"), ask=Decimal("380"),
            )
        )
        result = svc.pre_submit_risk_check(self._request("20", "379"), broker)

        assert isinstance(result, OrderStatus)
        assert result.status == "SKIPPED"
        assert "margin capacity" in str(result.reason)

    def test_capacity_unavailable_rejects(self) -> None:
        class _ZeroCapacityBroker(_FakeMarginBroker):
            def estimate_margin_max_quantity(
                self,
                symbol: str,
                side: str,
                price: Decimal,
                currency: str | None = None,
            ) -> Decimal:
                self.margin_calls.append((symbol, side, price, currency))
                return Decimal("0")

        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _ZeroCapacityBroker()

        result = svc.pre_submit_risk_check(self._request(), broker)

        assert isinstance(result, OrderStatus)
        assert result.status == "SKIPPED"
        assert "margin capacity" in str(result.reason)

    def test_flag_off_pre_submit_adds_no_broker_call(self) -> None:
        svc = _disarm(_make_service())
        broker = _FakeMarginBroker(margin_max="20")

        approved = svc.pre_submit_risk_check(self._request("13"), broker)

        assert isinstance(approved, ApprovedOrder)
        # Legacy pre-submit never calls estimate_margin_max_quantity.
        assert broker.margin_calls == []

    def test_cap_bound_entry_passes_pre_submit_and_is_reported(
        self,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="200")

        sizing_qty = svc._entry_quantity_from_margin_power(
            broker, "TSLA.US", "BUY", Decimal("379"), "USD",
        )
        assert sizing_qty == 65

        approved = svc.pre_submit_risk_check(self._request("65"), broker)
        assert isinstance(approved, ApprovedOrder)
        assert approved.quantity == Decimal("65")
        # The limiting factor is visible in diagnostics (never silent).
        assert svc.funded_margin_diagnostics()["last_limiting_factor"] == (
            "NOTIONAL_CAP"
        )

    def test_reductions_pass_under_mismatch(self) -> None:
        svc = _make_service()
        svc.funded_margin_account_fingerprint = FP_B
        broker = _FakeMarginBroker(margin_max="1000")

        result = svc.pre_submit_risk_check(
            _PreSubmitRiskRequest(
                action="SELL",
                symbol="TSLA.US",
                quantity=Decimal("20"),
                price=Decimal("379"),
            ),
            broker,
        )

        # Reductions return before limits: the binding never blocks exits.
        assert isinstance(result, ApprovedOrder)
        assert broker.margin_calls == []

    def test_sell_short_still_rejected_by_the_boundary(self) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker(margin_max="1000")

        result = svc.pre_submit_risk_check(
            _PreSubmitRiskRequest(
                action="SELL_SHORT",
                symbol="TSLA.US",
                quantity=Decimal("20"),
                price=Decimal("379"),
            ),
            broker,
        )

        assert isinstance(result, OrderStatus)
        assert result.status == "SKIPPED"
        assert "short entries" in str(result.reason)


class TestFundedMarginPendingCutoff:
    """Contract item G: a pending range BUY cannot outlive the cutoff.

    A pending limit BUY submitted before the entry cutoff must not
    remain live into the flatten window: the existing 30s
    pending-timeout path cancels (or finalizes) it long before either
    window opens.
    """

    def _pending_buy(self, broker: _FakeMarginBroker) -> _PendingOrder:
        return _PendingOrder(
            broker=broker,
            broker_order_id="pending-1",
            symbol="TSLA.US",
            action="BUY",
            quantity=Decimal("65"),
            price=Decimal("379"),
            engine_snapshot=None,
            submitted_at=time.monotonic() - 120.0,
            next_status_check_at=0.0,
        )

    def test_pending_range_buy_times_out_and_cancels_before_flatten(
        self,
    ) -> None:
        svc = _disarm(_make_service())
        broker = _FakeMarginBroker(margin_max="20")
        pending = self._pending_buy(broker)
        svc.load_pending_orders([pending])
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "SUBMITTED", Decimal("0"), Decimal("0"),
        )

        svc._handle_pending_order_timeout(
            replace(pending, timeout_recovery_attempted=True),
            risk=None,
            notifier=None,
        )

        # After the timeout the entry is no longer a live pending order.
        assert svc.pending_order_for("TSLA.US") is None

    def test_pending_timeout_drives_cancel_of_live_order(self) -> None:
        svc = _disarm(_make_service())
        broker = _FakeMarginBroker(margin_max="20")
        pending = self._pending_buy(broker)
        svc.load_pending_orders([pending])
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "SUBMITTED", Decimal("0"), Decimal("0"),
        )

        svc._handle_pending_order_timeout(
            replace(pending, timeout_recovery_attempted=True),
            risk=None,
            notifier=None,
        )

        # The live order was cancelled through the broker.
        assert broker.cancel_calls == ["pending-1"]

    def test_cutoff_blocks_a_new_range_buy_inside_the_cutoff_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            trade_svc_module, "is_trading_hours", lambda _m: True,
        )
        monkeypatch.setattr(
            trade_svc_module,
            "is_closing_window",
            lambda market, minutes: int(minutes) >= 30,
        )
        monkeypatch.setattr(
            trade_svc_module, "is_opening_warmup", lambda *_args: False,
        )
        skips: list[str] = []
        svc = _make_service(skips=skips)
        svc.entry_cutoff_minutes_before_close = 90
        broker = _FakeMarginBroker(margin_max="1000")

        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            market="US",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "entry cutoff" in str(status.reason)
        assert broker.submissions == []
        assert skips == ["SESSION"]
