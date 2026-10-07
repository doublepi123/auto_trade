# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Round-2 regression tests: gate-p3a review-1 findings 1, 2 and 4.

Finding 1 (P0): the raw strategy-cap handover in
``AppRunner._configure_live_safety`` must never raise on
None/non-numeric/NaN/inf/negative/bool values; invalid values leave the
funded-margin exception INERT for that cap (raw falls back to None) while
the clamped hard_ceiling fields and the rest of the method behave exactly
as at 432dc793.

Finding 2 (P1): when the funded-margin exception is EFFECTIVE for the
order (range US BUY, binding MATCHED), the final submission re-checks RTH
and the entry cutoff immediately before the single broker mutation; past
either, skip with SESSION and NO submit. Flag-off/paper/unbound/reduction
paths gain no new check.

Finding 4 (P2): the opening-momentum exclusion must consult a marker the
runner ACTUALLY passes; plus the explicit LLM exclusion (defence in
depth — P0 shadow mode already makes LLM orders unreachable).
"""

from __future__ import annotations

import time as _time
from decimal import Decimal

import pytest

from app.core.broker import (
    OrderResult,
    OrderStatusResult,
    Position,
    Quote,
)
from app.core.risk import RiskController
from app.runner import AppRunner
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


class _FakeMarginBroker2:
    """Minimal fake: records margin calls, submissions, cancels, statuses."""

    def __init__(
        self,
        margin_max: str = "20",
        *,
        cancel_exception: Exception | None = None,
        cancel_status: str | None = None,
    ) -> None:
        self.margin_calls: list[tuple[str, str, Decimal, str | None]] = []
        self.margin_max = Decimal(margin_max)
        self.positions: list[Position] = []
        self.submissions: list[OrderResult] = []
        self.status_results: dict[str, OrderStatusResult] = {}
        self.cancel_calls: list[str] = []
        self.cancel_exception = cancel_exception
        self.cancel_status = cancel_status
        self.status_calls: list[str] = []
        self.margin_sleep_seconds = 0.0

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        if self.margin_sleep_seconds:
            _time.sleep(self.margin_sleep_seconds)
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
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_status is not None:
            partial_qty = Decimal("0")
            partial_price = Decimal("0")
            base = self.status_results.get(order_id)
            if base is not None and base.executed_quantity > 0:
                partial_qty = base.executed_quantity
                partial_price = (
                    base.executed_price
                    if base.executed_price > 0
                    else Decimal("0")
                )
            return OrderStatusResult(
                order_id, self.cancel_status, partial_qty, partial_price,
            )
        return OrderStatusResult(order_id, "CANCELLED")


def _make_service(
    *,
    provider=None,
    paper: bool = False,
    raw_caps: tuple[object, object, object] | None = None,
    clamped_caps: tuple[int, float, float] = (100, 5000.0, 250.0),
    stop_loss_pct: float = 0.25,
    margin_safety_factor: float | None = 1.0,
    skips: list[str] | None = None,
    risk_events: list[str] | None = None,
) -> TradeExecutionService:
    if skips is None:
        skips = []
    if risk_events is None:
        risk_events = []
    if raw_caps is None:
        raw_caps = clamped_caps
    svc = TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args, **_kwargs: True,
        record_risk_event=risk_events.append,
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
            lambda _b, _s, _a, p: FinalOrderQuoteCheckResult(p, bid=p, ask=p)
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


class _OpenMarketMixin:
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


# ---------------------------------------------------------------------------
# Finding 1 — OFF/PAPER robustness of the raw strategy-cap handover
# ---------------------------------------------------------------------------


class TestRawCapHandoverNeverRaises:
    """None/non-numeric/NaN/inf/negative/bool raw values must not raise.

    The round-1 ``int()/float()`` coercion raises on None/"invalid"/inf
    even with the flag OFF, breaking startup and reload_strategy.
    """

    @staticmethod
    def _config_with_raw(*, qty, notional, risk) -> object:
        class _Cfg:
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
            stop_loss_pct = 1.0
            entry_cutoff_minutes_before_close = 0
            llm_order_execution_enabled = False
            max_position_quantity = qty
            max_position_notional = notional
            max_risk_per_trade = risk

        return _Cfg()

    def test_invalid_raw_values_leave_the_exception_inert_not_raising(
        self,
    ) -> None:
        runner = AppRunner()
        for qty, notional, risk in [
            (None, 5000.0, 250.0),
            (100, None, 250.0),
            (100, 5000.0, None),
            ("invalid", 5000.0, 250.0),
            (100, "invalid", 250.0),
            (100, 5000.0, "invalid"),
            (float("nan"), 5000.0, 250.0),
            (100, float("nan"), 250.0),
            (100, 5000.0, float("nan")),
            (float("inf"), 5000.0, 250.0),
            (100, float("inf"), 250.0),
            (100, 5000.0, float("inf")),
            (-5, 5000.0, 250.0),
            (100, -5000.0, 250.0),
            (100, 5000.0, -250.0),
        ]:
            runner._configure_live_safety(
                self._config_with_raw(qty=qty, notional=notional, risk=risk),
            )
            svc = runner._trade_svc
            # Clamped caps: the base 432dc793 behaviour (invalid -> hard).
            assert svc.max_position_quantity == 100, (qty, notional, risk)
            assert svc.max_position_notional == 5000.0
            assert svc.max_risk_per_trade == 250.0
            # The exception is INERT for that cap: raw falls back to None,
            # so the resolver can never treat an invalid value as a cap.
            if qty in (None, "invalid", float("nan"), float("inf"), -5):
                assert svc.raw_strategy_max_position_quantity is None
            if notional in (None, "invalid", float("nan"), float("inf"), -5000.0):
                assert svc.raw_strategy_max_position_notional is None
            if risk in (None, "invalid", float("nan"), float("inf"), -250.0):
                assert svc.raw_strategy_max_risk_per_trade is None

    def test_bool_raw_values_leave_the_exception_inert(self) -> None:
        # bool is an int subclass; int(True)=1 — accepted as a "raw" value
        # it must NOT be: the exception stays inert for that cap. The
        # CLAMPED fields keep the exact 432dc793 behaviour for bools
        # (hard_ceiling_int(True, 100) == 1).
        runner = AppRunner()
        runner._configure_live_safety(
            self._config_with_raw(qty=True, notional=True, risk=True),
        )
        svc = runner._trade_svc
        assert svc.max_position_quantity == 1
        assert svc.max_position_notional == 1.0
        assert svc.max_risk_per_trade == 1.0
        assert svc.raw_strategy_max_position_quantity is None
        assert svc.raw_strategy_max_position_notional is None
        assert svc.raw_strategy_max_risk_per_trade is None

    def test_valid_raw_values_still_ride_along(self) -> None:
        runner = AppRunner()
        runner._configure_live_safety(
            self._config_with_raw(qty=800, notional=20000.0, risk=240.0),
        )
        svc = runner._trade_svc
        assert svc.raw_strategy_max_position_quantity == 800
        assert svc.raw_strategy_max_position_notional == 20000.0
        assert svc.raw_strategy_max_risk_per_trade == 240.0

    def test_reload_strategy_with_invalid_raw_does_not_raise(self) -> None:
        from app.services.strategy_service import StrategyService

        runner = AppRunner()
        config = self._config_with_raw(
            qty=None, notional=float("nan"), risk="invalid",
        )
        monkey_patches = pytest.MonkeyPatch()
        monkey_patches.setattr(
            StrategyService, "__init__", lambda self, db: None,
        )
        monkey_patches.setattr(
            StrategyService, "get_config", lambda self: config,
        )
        monkey_patches.setattr(
            runner._state_svc, "load_symbol_runtime", lambda *args: None,
        )
        monkey_patches.setattr(runner.broker, "get_positions", lambda: [])
        try:
            runner.reload_strategy()
        finally:
            monkey_patches.undo()
        svc = runner._trade_svc
        assert svc.max_position_quantity == 100
        assert svc.max_position_notional == 5000.0
        assert svc.max_risk_per_trade == 250.0
        assert svc.raw_strategy_max_position_quantity is None
        assert svc.raw_strategy_max_position_notional is None
        assert svc.raw_strategy_max_risk_per_trade is None

    def test_paper_settings_with_invalid_raw_still_never_raise(self) -> None:
        # PAPER: same robustness when the paper attestation is on (the
        # settings-level armed state); the handover itself must not raise.
        runner = AppRunner()
        cfg = self._config_with_raw(
            qty=float("inf"), notional=None, risk=float("nan"),
        )
        runner._configure_live_safety(cfg)
        svc = runner._trade_svc
        assert svc.max_position_quantity == 100
        assert svc.max_position_notional == 5000.0
        assert svc.max_risk_per_trade == 250.0
        assert svc.raw_strategy_max_position_quantity is None
        assert svc.raw_strategy_max_position_notional is None
        assert svc.raw_strategy_max_risk_per_trade is None


# ---------------------------------------------------------------------------
# Finding 2 — final-submit cutoff/RTH re-check when the exception is effective
# ---------------------------------------------------------------------------


class TestFinalSubmitCutoffRecheck(_OpenMarketMixin):
    def _execute_buy(self, svc, broker, monkeypatch_market_state) -> OrderStatus | None:
        return svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            _NullNotifier(),
            "USD",
            market="US",
        )

    def test_cutoff_crossing_during_capacity_query_skips_with_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker2(margin_max="20")

        # Deterministic crossing: the pre-submit capacity re-estimate is
        # the LAST blocking broker read before the mutation; flip the
        # calendar the moment it happens.
        def _closing_window(_market, minutes):
            return bool(broker.margin_calls)

        monkeypatch.setattr(
            trade_svc_module, "is_closing_window", _closing_window,
        )

        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            _NullNotifier(),
            "USD",
            market="US",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "entry cutoff" in str(status.reason)
        # Zero broker submits: the re-check fired before the mutation.
        assert broker.submissions == []

    def test_control_without_crossing_submits_exactly_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker2(margin_max="20")

        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            _NullNotifier(),
            "USD",
            market="US",
        )

        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    def test_rth_exit_during_capacity_query_skips_with_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker2(margin_max="20")

        # RTH ends the moment the capacity re-estimate happens.
        def _trading_hours(_market):
            return not bool(broker.margin_calls)

        monkeypatch.setattr(
            trade_svc_module, "is_trading_hours", _trading_hours,
        )

        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            _NullNotifier(),
            "USD",
            market="US",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "RTH" in str(status.reason)
        assert broker.submissions == []

    def test_flag_off_path_unchanged_when_clock_crosses(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = _disarm(_make_service(raw_caps=(1000, 25000.0, 250.0)))
        broker = _FakeMarginBroker2(margin_max="20")

        # Same crossing calendar as the armed test: cutoff "arrives" the
        # moment any margin estimate happens.
        def _closing_window(_market, minutes):
            return bool(broker.margin_calls)

        monkeypatch.setattr(
            trade_svc_module, "is_closing_window", _closing_window,
        )

        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            RiskController(),
            _NullNotifier(),
            "USD",
            market="US",
        )

        # Flag OFF: no new check — the crossing after sizing is
        # invisible, exactly the 432dc793 behaviour.
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    def test_reduction_under_effective_binding_gains_no_new_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        broker = _FakeMarginBroker2(margin_max="20")
        broker.positions = [
            Position("TSLA.US", "LONG", Decimal("10"), Decimal("300")),
        ]
        closing_calls = {"n": 0}

        def _closing_window(_market, minutes):
            closing_calls["n"] += 1
            return True

        monkeypatch.setattr(
            trade_svc_module, "is_closing_window", _closing_window,
        )
        risk = RiskController()
        # A reduction at the entry cutoff must still submit: the new
        # check is entry-scoped, reductions keep today's behaviour (the
        # entry-cutoff gate at execute() time only consults entries, so
        # closing_calls stays 0 — no new check anywhere on this path).
        status = svc.execute(
            "SELL",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            risk,
            _NullNotifier(),
            "USD",
            market="US",
            allow_loss_exit=True,
        )
        assert status is not None
        assert status.status in {"SUBMITTED", "FILLED"}
        assert len(broker.submissions) == 1
        assert closing_calls["n"] == 0


class _NullNotifier:
    def notify_order(self, *_args, **_kwargs) -> None:
        pass

    def notify_risk_event(self, *_args, **_kwargs) -> None:
        pass


# ---------------------------------------------------------------------------
# Finding 4 — explicit lane exclusions (opening momentum + LLM)
# ---------------------------------------------------------------------------


class TestRunnerContextLaneExclusion:
    """The resolver must read the marker the runner actually passes."""

    def test_runner_built_range_context_allows_the_exception(self) -> None:
        runner = AppRunner()
        decision = _RunnerDecisionStub(action="BUY")
        quote = Quote("TSLA.US", 379, 378.9, 379.1, "")
        context = runner._execution_ledger_context(decision, quote, "stub")
        assert context["execution_initiator"] == "RANGE"
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc._active_execution_context = dict(context)
        limits = svc._range_entry_limits_for("TSLA.US", "BUY", "US")
        assert limits is not None
        assert int(limits.max_quantity) == 1000

    def test_runner_built_llm_context_excludes_the_exception(self) -> None:
        # The LLM call site passes initiator="LLM" into the same builder;
        # the marker rides at the top level the resolver checks.
        runner = AppRunner()
        decision = _RunnerDecisionStub(action="BUY")
        quote = Quote("TSLA.US", 379, 378.9, 379.1, "")
        context = runner._execution_ledger_context(
            decision, quote, "LLM trade action", initiator="LLM",
        )
        assert context["execution_initiator"] == "LLM"
        svc = _make_service(raw_caps=(1000, 25000.0, 250.0))
        svc._active_execution_context = dict(context)
        assert svc._range_entry_limits_for("TSLA.US", "BUY", "US") is None


class _RunnerDecisionStub:
    """Just enough of _QuoteTriggerDecision for the ledger builders."""

    def __init__(self, action: str) -> None:
        self.result = _TriggerResultStub(action)
        self.trigger_params = None
        self.trigger_engine = None
        self.reduce_only = False
        self.reduction_cause = ""
        self.reduction_intent = None


class _TriggerResultStub:
    def __init__(self, action: str) -> None:
        self.action = action
        self.description = "stub"


def _tsla_quote() -> Quote:
    return Quote("TSLA.US", 379, 378.9, 379.1, "")


# ---------------------------------------------------------------------------
# Finding 3 — pending range ENTRY vs cutoff/flatten (contract item G)
# ---------------------------------------------------------------------------


class _FakeMonotonicClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestPendingEntryCutoffRecovery:
    """Bounded cancel retries driven by the REAL reconcile loop.

    UNIT COVERAGE ONLY (round-3 note): these tests construct
    ``_PendingOrder`` directly with ``funded_margin_entry=True`` to pin
    the retry mechanics in isolation. The REAL-path proofs — submit →
    restart recovery and submit → persistence-failure recovery — live in
    ``tests/test_funded_margin_exception_round3.py``
    (``TestExceptionFlagSurvivesRestart`` /
    ``TestExceptionFlagSurvivesPersistenceFailure``) and drive the flag
    through the actual submit/persist/recover pipeline.

    Round-1's "NO GAP" claim is withdrawn: the timeout path permanently
    sets ``timeout_recovery_attempted=True`` after the FIRST attempt, so a
    failed first cancel or a broker that keeps reporting live left the
    order live forever. For pending range ENTRY orders while the exception
    is effective, retries must continue until a terminal state is
    confirmed; if that never happens before the flatten window, keep the
    existing uncertainty/pause semantics, record a risk event and notify.
    """

    def _armed_service(
        self,
        *,
        clock: _FakeMonotonicClock,
        monkeypatch: pytest.MonkeyPatch,
        risk_events: list[str],
        notified: list[tuple[str, str]],
    ) -> TradeExecutionService:
        svc = _make_service(
            raw_caps=(1000, 25000.0, 250.0),
            risk_events=risk_events,
        )
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 1
        monkeypatch.setattr(
            trade_svc_module.time, "monotonic", clock,
        )
        return svc

    def _pending_buy(self, broker, submitted_at: float) -> _PendingOrder:
        return _PendingOrder(
            broker=broker,
            broker_order_id="pending-1",
            symbol="TSLA.US",
            action="BUY",
            quantity=Decimal("65"),
            price=Decimal("379"),
            engine_snapshot=None,
            submitted_at=submitted_at,
            next_status_check_at=0.0,
            funded_margin_entry=True,
        )

    def test_first_cancel_fails_then_succeeds_on_later_reconcile(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = _FakeMonotonicClock()
        risk_events: list[str] = []
        notified: list[tuple[str, str]] = []
        svc = self._armed_service(
            clock=clock, monkeypatch=monkeypatch, risk_events=risk_events,
            notified=notified,
        )
        broker = _FakeMarginBroker2(
            margin_max="20", cancel_exception=RuntimeError("cancel failed"),
        )
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "SUBMITTED", Decimal("0"), Decimal("0"),
        )
        pending = self._pending_buy(broker, submitted_at=clock() - 120)
        svc.load_pending_orders([pending])
        risk = RiskController()

        # First reconcile: the status query works, cancel FAILS -> the
        # pause + risk-event + notification semantics fire...
        svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))
        assert broker.cancel_calls == ["pending-1"]
        assert risk.paused is True
        assert any("pending-1" in r for r in risk_events)
        assert any(topic == "ORDER_TIMEOUT" for topic, _m in notified)

        # ...and the order is NOT dropped from tracking.
        assert svc.pending_order_for("TSLA.US") is not None

        # Broker recovers; a later reconcile must RETRY the cancel (the
        # permanent timeout_recovery_attempted latch would forbid it).
        broker.cancel_exception = None
        clock.advance(2)
        svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))

        assert broker.cancel_calls == ["pending-1", "pending-1"]
        # Terminal state confirmed: no longer pending.
        assert svc.pending_order_for("TSLA.US") is None

    def test_broker_keeps_reporting_submitted_keeps_retrying_then_alerts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = _FakeMonotonicClock()
        risk_events: list[str] = []
        notified: list[tuple[str, str]] = []
        svc = self._armed_service(
            clock=clock, monkeypatch=monkeypatch, risk_events=risk_events,
            notified=notified,
        )
        broker = _FakeMarginBroker2(
            margin_max="20", cancel_status="SUBMITTED",
        )
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "SUBMITTED", Decimal("0"), Decimal("0"),
        )
        pending = self._pending_buy(broker, submitted_at=clock() - 120)
        svc.load_pending_orders([pending])
        risk = RiskController()

        # Initial reconcile: the original timeout branch runs the FIRST
        # attempt (cancel accepted but not terminal).
        svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))
        assert broker.cancel_calls == ["pending-1"]
        assert risk.paused is True
        assert svc.pending_order_for("TSLA.US") is not None

        # Later reconciles retry the cancel — the permanent
        # timeout_recovery_attempted latch would forbid this.
        for expected in (2, 3):
            clock.advance(2)
            svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))
            assert broker.cancel_calls == ["pending-1"] * expected

        # After the bounded cap the reconcile loop stops cancelling but
        # the order stays tracked, a risk event is recorded, and manual
        # intervention is notified for.
        clock.advance(2)
        before = len(broker.cancel_calls)
        svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))
        assert len(broker.cancel_calls) == before == 3
        assert svc.pending_order_for("TSLA.US") is not None
        assert any("manual" in r.lower() for r in risk_events)
        assert any(
            topic == "PENDING_ENTRY_UNCONFIRMED" for topic, _m in notified
        )

    def test_partial_fill_is_booked_then_cancel_confirms(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = _FakeMonotonicClock()
        risk_events: list[str] = []
        notified: list[tuple[str, str]] = []
        svc = self._armed_service(
            clock=clock, monkeypatch=monkeypatch, risk_events=risk_events,
            notified=notified,
        )
        broker = _FakeMarginBroker2(
            margin_max="20", cancel_status="CANCELLED",
        )
        # Timeout status query: partially filled but still live; the
        # cancel then confirms the terminal partial fill of 20 @ 379.
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "PARTIAL_FILLED", Decimal("20"), Decimal("379"),
        )
        pending = self._pending_buy(broker, submitted_at=clock() - 120)
        svc.load_pending_orders([pending])
        risk = RiskController()
        booked: list[tuple[str, Decimal, Decimal]] = []
        svc._persist_entry = (
            lambda symbol, qty, cost: booked.append((symbol, qty, cost))
        )
        # Terminal-status persistence must succeed so the partial fill is
        # finalized through the normal settlement path.
        svc._update_order_status = lambda *_args, **_kw: True

        svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))

        # The cancel confirmed a terminal partial fill: 20 booked through
        # the existing settlement path (no invented cost basis), the
        # pending cleared.
        assert broker.cancel_calls == ["pending-1"]
        assert booked == [
            ("TSLA.US", Decimal("20"), Decimal("20") * Decimal("379")),
        ]
        assert svc.pending_order_for("TSLA.US") is None

    def test_cutoff_flatten_crossing_with_unconfirmed_order_alerts_and_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = _FakeMonotonicClock()
        risk_events: list[str] = []
        notified: list[tuple[str, str]] = []
        svc = self._armed_service(
            clock=clock, monkeypatch=monkeypatch, risk_events=risk_events,
            notified=notified,
        )
        broker = _FakeMarginBroker2(
            margin_max="20", cancel_exception=RuntimeError("cancel down"),
        )
        broker.status_results["pending-1"] = OrderStatusResult(
            "pending-1", "SUBMITTED", Decimal("0"), Decimal("0"),
        )
        pending = self._pending_buy(broker, submitted_at=clock() - 120)
        svc.load_pending_orders([pending])
        risk = RiskController()

        # Reconciles exhaust the bounded retries; the flatten window then
        # opens (is_closing_window True from now on).
        for _ in range(5):
            clock.advance(2)
            svc.reconcile(risk=risk, notify_risk_event=_record_into(notified))
        assert svc.pending_order_for("TSLA.US") is not None
        assert any(
            topic == "PENDING_ENTRY_UNCONFIRMED" for topic, _m in notified
        )
        monkeypatch.setattr(
            trade_svc_module, "is_closing_window", lambda *_args: True,
        )

        # A NEW range BUY inside the flatten/cutoff window is blocked
        # (SESSION) while the unconfirmed entry remains outstanding.
        skips: list[str] = []
        svc._record_order_skipped = (
            lambda _s, _a, _r, payload: skips.append(
                str(payload["skip_category"]),
            )
        )
        status = svc.execute(
            "BUY",
            "TSLA.US",
            Quote("TSLA.US", 379, 378.9, 379.1, ""),
            broker,
            risk,
            _NullNotifier(),
            "USD",
            market="US",
        )
        assert status is not None
        assert status.status == "SKIPPED"
        assert skips == ["SESSION"]
        assert broker.submissions == []
        # The unconfirmed order is still tracked.
        assert svc.pending_order_for("TSLA.US") is not None


def _record_into(
    notified: list[tuple[str, str]],
) -> object:
    def _notify(topic: str, message: str) -> None:
        notified.append((topic, message))

    return _notify
