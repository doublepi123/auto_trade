# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""Strict USD cash evidence via BrokerGateway.get_strict_usd_cash_snapshot.

Phase1 SPY passive lane, writer A. The snapshot must come from exactly one
explicit ``USD`` ``cash_infos.available_cash`` entry of the real SDK
``TradeContext.account_balance(currency=None) -> List[AccountBalance]``
response (verified against installed longport 3.0.23 ``openapi.pyi``:
``AccountBalance.cash_infos: List[CashInfo]``,
``CashInfo.currency: str``, ``CashInfo.available_cash: Decimal``).

No fallback to ``total_cash``/margin/buy_power/other currencies is allowed,
missing/ambiguous/malformed/non-finite/negative USD fails closed with
``CashEvidenceUnavailable``, and the aware-UTC request window brackets only
the successful request (retries time each attempt; cached data is never
re-stamped). The legacy lenient ``get_cash`` must remain unchanged.

The fake ``AccountBalance`` objects below intentionally omit fields the SDK
carries but this path must NOT read (``total_cash``, ``buy_power``,
``net_assets``...): any read of a fallback field shows up as an assertion
failure ("amount fabricated from <field>") because the fakes only populate
the USD ``cash_infos`` entry with the honest value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import broker as broker_module
from app.core.broker import BrokerGateway
from app.core.cash_evidence import CashEvidenceUnavailable, UsdCashSnapshot


PROVENANCE = "account_balance.cash_infos.available_cash"


@dataclass
class _FakeCashInfo:
    currency: str = "USD"
    available_cash: object = "4802.17"


@dataclass
class _FakeAccountBalance:
    """Mirrors installed longport 3.0.23 ``AccountBalance`` surface.

    Fallback fields carry honest SDK-like defaults (the SDK always
    populates them); no-fallback tests overwrite them with huge bait values
    that the assertions would catch if the gateway read them.
    """

    cash_infos: list[_FakeCashInfo] = field(default_factory=list)
    currency: str = "USD"
    total_cash: object = "0"
    buy_power: object = "0"
    remaining_finance_amount: object = "0"
    net_assets: object = "0"


class _FakeTradeContext:
    """Records ``account_balance`` calls; returns queued responses/exceptions."""

    def __init__(
        self,
        outcomes: list[object] | None = None,
        default: object | None = None,
    ) -> None:
        self.calls = 0
        self._outcomes = list(outcomes or [])
        self._default = default

    def account_balance(self, currency: str | None = None):
        self.calls += 1
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        if self._default is not None:
            return self._default
        return [ _FakeAccountBalance(cash_infos=[_FakeCashInfo()]) ]


class _SleepRecorder:
    """Records retry backoff sleeps without pausing the test."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _gateway_with(ctx: _FakeTradeContext) -> BrokerGateway:
    gw = BrokerGateway()
    gw._trade_ctx = ctx
    gw._quote_ctx = object()
    return gw


def _usd_response(amount: object = "4802.17", **extra: object) -> list[_FakeAccountBalance]:
    entry = _FakeCashInfo(currency="USD", available_cash=amount)
    return [_FakeAccountBalance(cash_infos=[entry], **extra)]  # type: ignore[arg-type]


class TestStrictUsdExtraction:
    def test_extracts_single_explicit_usd_entry(self) -> None:
        ctx = _FakeTradeContext(default=_usd_response("4802.17"))
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert isinstance(snapshot, UsdCashSnapshot)
        assert snapshot.amount == Decimal("4802.17")
        assert snapshot.currency == "USD"
        assert snapshot.provenance == PROVENANCE
        assert ctx.calls == 1

    def test_ignores_huge_total_cash_buy_power_and_margin_bait(self) -> None:
        # Adversarial: fallback fields dwarf the honest USD cash. Old
        # lenient behavior (get_cash's total_cash fallback) would return
        # 9_999_999; strict must report only available_cash.
        ctx = _FakeTradeContext(
            default=_usd_response(
                "4802.17",
                total_cash="9999999",
                buy_power="88888888",
                remaining_finance_amount="7777777",
                net_assets="66666666",
            )
        )
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("4802.17")

    def test_accepts_zero_cash(self) -> None:
        ctx = _FakeTradeContext(default=_usd_response("0"))
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("0")

    def test_accepts_decimal_object_value(self) -> None:
        # The real SDK returns Decimal on available_cash.
        ctx = _FakeTradeContext(default=_usd_response(Decimal("123.45")))
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("123.45")

    def test_ignores_other_currency_entries(self) -> None:
        entries = [
            _FakeCashInfo(currency="HKD", available_cash="70000"),
            _FakeCashInfo(currency="USD", available_cash="4802.17"),
            _FakeCashInfo(currency="CNH", available_cash="50000"),
        ]
        ctx = _FakeTradeContext(
            default=[_FakeAccountBalance(cash_infos=entries)]
        )
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("4802.17")

    def test_ignores_case_sensitive_currency_mismatch(self) -> None:
        # "usd" is not an explicit USD entry; must not be adopted.
        ctx = _FakeTradeContext(
            default=[_FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="usd", available_cash="1")])]
        )
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_multi_account_response_uses_single_unique_usd(self) -> None:
        items = [
            _FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="HKD", available_cash="70000")]),
            _FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="USD", available_cash="4802.17")]),
        ]
        ctx = _FakeTradeContext(default=items)
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("4802.17")


class TestStrictRejections:
    def test_missing_usd_entry_rejects(self) -> None:
        ctx = _FakeTradeContext(
            default=[_FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="HKD", available_cash="70000")])]
        )
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_missing_cash_infos_rejects(self) -> None:
        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=[])])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_empty_response_list_rejects(self) -> None:
        ctx = _FakeTradeContext(default=[])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_bare_object_response_wrapped_and_validated(self) -> None:
        # SDK contract returns List[AccountBalance]; a bare object is wrapped
        # defensively exactly like legacy get_cash, then strictly validated.
        ctx = _FakeTradeContext(default=_FakeAccountBalance(cash_infos=[_FakeCashInfo()]))
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert snapshot.amount == Decimal("4802.17")

    def test_ambiguous_two_usd_entries_same_value_rejects(self) ->  None:
        entries = [
            _FakeCashInfo(currency="USD", available_cash="4802.17"),
            _FakeCashInfo(currency="USD", available_cash="4802.17"),
        ]
        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=entries)])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_ambiguous_two_usd_entries_different_value_rejects(self) -> None:
        entries = [
            _FakeCashInfo(currency="USD", available_cash="4802.17"),
            _FakeCashInfo(currency="USD", available_cash="9999.99"),
        ]
        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=entries)])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_ambiguous_usd_across_accounts_rejects(self) -> None:
        items = [
            _FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="USD", available_cash="4802.17")]),
            _FakeAccountBalance(cash_infos=[_FakeCashInfo(currency="USD", available_cash="100")]),
        ]
        ctx = _FakeTradeContext(default=items)
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    @pytest.mark.parametrize(
        "bad_value",
        ["NaN", "Infinity", "-Infinity", "-1", "-0.01", "abc", "", None],
    )
    def test_malformed_or_invalid_amount_rejects(self, bad_value: object) -> None:
        ctx = _FakeTradeContext(default=_usd_response(bad_value))
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_missing_amount_attribute_rejects(self) -> None:
        class _NoAmount:
            currency = "USD"

        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=[_NoAmount()])])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_missing_currency_attribute_rejects(self) -> None:
        class _Bare:
            available_cash = "4802.17"

        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=[_Bare()])])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()

    def test_no_fallback_to_item_level_currency_usd(self) -> None:
        # AccountBalance.currency == USD but cash_infos has no USD entry:
        # must NOT fall back to item-level fields (old get_cash did).
        item = _FakeAccountBalance(cash_infos=[], currency="USD")
        item.total_cash = "5000"
        ctx = _FakeTradeContext(default=[item])
        with pytest.raises(CashEvidenceUnavailable):
            _gateway_with(ctx).get_strict_usd_cash_snapshot()


class TestRequestWindow:
    def test_window_is_aware_utc_and_ordered(self) -> None:
        ctx = _FakeTradeContext(default=_usd_response())
        before = datetime.now(timezone.utc)
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()
        after = datetime.now(timezone.utc)

        assert snapshot.request_started_at.tzinfo is not None
        assert snapshot.request_completed_at.tzinfo is not None
        assert snapshot.request_started_at.utcoffset() == timedelta(0)
        assert snapshot.request_completed_at.utcoffset() == timedelta(0)
        assert before <= snapshot.request_started_at <= snapshot.request_completed_at <= after

    def test_started_before_completed_with_slow_fake(self) -> None:
        started_events: list[datetime] = []

        class _SlowCtx(_FakeTradeContext):
            def account_balance(self, currency: str | None = None):
                started_events.append(datetime.now(timezone.utc))
                return super().account_balance(currency)

        ctx = _SlowCtx(default=_usd_response())
        snapshot = _gateway_with(ctx).get_strict_usd_cash_snapshot()

        assert len(started_events) == 1
        assert started_events[0] <= snapshot.request_completed_at
        assert snapshot.request_started_at <= started_events[0]


class TestRetryTiming:
    def test_retry_after_transient_error_times_the_successful_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # First attempt: transient network failure. Second: success.
        # The snapshot window must bracket ONLY the successful attempt.
        fail_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        success_start = datetime(2026, 9, 30, 12, 0, 5, tzinfo=timezone.utc)
        success_end = datetime(2026, 9, 30, 12, 0, 6, tzinfo=timezone.utc)

        times = [fail_time, success_start, success_end]
        monkeypatch.setattr(broker_module, "_utc_now", lambda: times.pop(0))

        outcomes: list[object] = [ConnectionError("transport reset"), _usd_response()]
        ctx = _FakeTradeContext(outcomes=outcomes)
        gw = _gateway_with(ctx)
        monkeypatch.setattr(broker_module.settings, "broker_retry_max", 2)
        monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
        sleeps = _SleepRecorder()
        monkeypatch.setattr(broker_module.time, "sleep", sleeps)

        snapshot = gw.get_strict_usd_cash_snapshot()

        assert ctx.calls == 2
        assert sleeps.delays == [0.0]
        # Window covers only the successful request: started at 12:00:05,
        # completed 12:00:06. The failed 12:00:00 attempt must not appear.
        assert snapshot.request_started_at == success_start
        assert snapshot.request_completed_at == success_end

    def test_retry_after_evidence_unavailable_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # CashEvidenceUnavailable is a RuntimeError, NOT in RETRYABLE_EXC,
        # so ambiguity must surface immediately without burning retries.
        ctx = _FakeTradeContext(
            outcomes=[[_FakeAccountBalance(cash_infos=[
                _FakeCashInfo(currency="USD", available_cash="4802.17"),
                _FakeCashInfo(currency="USD", available_cash="9999.99"),
            ])]]
        )
        gw = _gateway_with(ctx)
        monkeypatch.setattr(broker_module.settings, "broker_retry_max", 3)
        monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
        sleeps = _SleepRecorder()
        monkeypatch.setattr(broker_module.time, "sleep", sleeps)

        with pytest.raises(CashEvidenceUnavailable):
            gw.get_strict_usd_cash_snapshot()
        assert ctx.calls == 1
        assert sleeps.delays == []

    def test_retry_exhaustion_raises_original_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _FakeTradeContext(outcomes=[ConnectionError("down")] * 4)
        gw = _gateway_with(ctx)
        monkeypatch.setattr(broker_module.settings, "broker_retry_max", 2)
        monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
        sleeps = _SleepRecorder()
        monkeypatch.setattr(broker_module.time, "sleep", sleeps)

        with pytest.raises(ConnectionError, match="down"):
            gw.get_strict_usd_cash_snapshot()
        assert ctx.calls == 3
        assert sleeps.delays == [0.0, 0.0]


class TestNoCacheRestamping:
    def test_sequential_calls_refetch_and_do_not_restamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two calls must produce two broker requests with fresh windows:
        # the second snapshot may never re-use the first call's timestamps.
        first_start = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        first_end = first_start + timedelta(milliseconds=5)
        second_start = datetime(2026, 9, 30, 12, 0, 10, tzinfo=timezone.utc)
        second_end = second_start + timedelta(milliseconds=5)
        times = [first_start, first_end, second_start, second_end]
        monkeypatch.setattr(broker_module, "_utc_now", lambda: times.pop(0))

        ctx = _FakeTradeContext(default=_usd_response())
        gw = _gateway_with(ctx)
        first = gw.get_strict_usd_cash_snapshot()
        second = gw.get_strict_usd_cash_snapshot()

        assert ctx.calls == 2
        assert first.request_started_at == first_start
        assert first.request_completed_at == first_end
        assert second.request_started_at == second_start
        assert second.request_completed_at == second_end
        assert second.request_started_at > first.request_completed_at

    def test_after_failed_call_next_success_has_fresh_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Failure then success: success window must not include failure time.
        fail_time = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        success_start = datetime(2026, 9, 30, 12, 0, 3, tzinfo=timezone.utc)
        success_end = datetime(2026, 9, 30, 12, 0, 4, tzinfo=timezone.utc)
        times = [fail_time, success_start, success_end]
        monkeypatch.setattr(broker_module, "_utc_now", lambda: times.pop(0))

        outcomes: list[object] = [ConnectionError("reset"), _usd_response()]
        ctx = _FakeTradeContext(outcomes=outcomes)
        gw = _gateway_with(ctx)
        monkeypatch.setattr(broker_module.settings, "broker_retry_max", 1)
        monkeypatch.setattr(broker_module.settings, "broker_retry_base_ms", 0)
        monkeypatch.setattr(broker_module.time, "sleep", lambda _d: None)

        snapshot = gw.get_strict_usd_cash_snapshot()

        assert ctx.calls == 2
        assert snapshot.request_started_at == success_start
        assert snapshot.request_completed_at == success_end


class TestLegacyGetCashUnchanged:
    def test_get_cash_usd_prefers_cash_infos_available_cash(self) -> None:
        ctx = _FakeTradeContext(default=_usd_response("4802.17", total_cash="9999999"))
        assert _gateway_with(ctx).get_cash("USD") == Decimal("4802.17")

    def test_get_cash_usd_falls_back_to_total_cash(self) -> None:
        # Legacy lenient behavior pinned: total_cash fallback must survive
        # the strict refactor untouched.
        item = _FakeAccountBalance(cash_infos=[], currency="USD")
        item.total_cash = "5000"
        ctx = _FakeTradeContext(default=[item])
        assert _gateway_with(ctx).get_cash("USD") == Decimal("5000")

    def test_get_cash_none_returns_first_usd_or_hkd(self) -> None:
        entries = [
            _FakeCashInfo(currency="HKD", available_cash="70000"),
            _FakeCashInfo(currency="USD", available_cash="4802.17"),
        ]
        ctx = _FakeTradeContext(default=[_FakeAccountBalance(cash_infos=entries)])
        assert _gateway_with(ctx).get_cash() == Decimal("70000")

    def test_get_cash_missing_currency_returns_zero_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = _FakeTradeContext(
            default=[_FakeAccountBalance(
                cash_infos=[_FakeCashInfo(currency="EUR", available_cash="100")],
                currency="EUR",
            )]
        )
        with caplog.at_level("WARNING", logger="auto_trade.broker"):
            assert _gateway_with(ctx).get_cash("USD") == Decimal("0")
        assert "no USD item found" in caplog.text
