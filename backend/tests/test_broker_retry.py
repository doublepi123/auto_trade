from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.core.broker import BrokerGateway


class _TransientErr(Exception):
    ...


@pytest.fixture
def gw(monkeypatch):
    from app.core import broker as broker_mod

    monkeypatch.setattr(broker_mod, "RETRYABLE_EXC", (_TransientErr,))
    class FakeModule:
        class OrderSide:
            Buy = "Buy"
            Sell = "Sell"

        class OrderType:
            LO = "LO"

        class TimeInForceType:
            Day = "DAY"

    monkeypatch.setattr(broker_mod, "_import_openapi", lambda: FakeModule)
    return BrokerGateway()


def test_call_with_retry_eventually_succeeds(gw, monkeypatch):
    audit = MagicMock()
    gw._audit = audit
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _TransientErr("rate limit")
        return "ok"

    monkeypatch.setattr("time.sleep", lambda s: None)
    result = gw._call_with_retry(flaky, op="submit_limit_order", max_retries=3, base_ms=10)
    assert result == "ok"
    assert len(calls) == 3
    assert audit.record.call_count == 2
    args = audit.record.call_args_list[0]
    assert args.args[0] == "BROKER_RETRY"


def test_call_with_retry_exhausts_then_raises(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    def always_fail():
        raise _TransientErr("rate limit")

    with pytest.raises(_TransientErr):
        gw._call_with_retry(always_fail, op="submit_limit_order", max_retries=2, base_ms=10)


def test_call_with_retry_max_retries_zero_calls_once(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def fn():
        calls.append(1)
        raise _TransientErr("rate limit")

    with pytest.raises(_TransientErr):
        gw._call_with_retry(fn, op="get_quote", max_retries=0, base_ms=10)
    assert len(calls) == 1


def test_call_with_retry_non_retryable_does_not_retry(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    class _Reject(Exception):
        ...

    calls = []

    def fn():
        calls.append(1)
        raise _Reject("balance insufficient")

    with pytest.raises(_Reject):
        gw._call_with_retry(fn, op="submit_limit_order", max_retries=3, base_ms=10)
    assert len(calls) == 1


def test_submit_limit_order_does_not_retry_on_transient_error(gw, monkeypatch):
    """submit_limit_order must NOT retry — a retry could create duplicate live orders."""
    monkeypatch.setattr(gw, "_init_clients", lambda: None)
    calls = []

    def fake_trade_ctx_submit(*a, **kw):
        calls.append(1)
        raise _TransientErr("rate limit")

    gw._quote_ctx = MagicMock()
    gw._trade_ctx = MagicMock(submit_order=fake_trade_ctx_submit)
    with pytest.raises(_TransientErr):
        gw.submit_limit_order(
            symbol="AAPL.US",
            side="BUY",
            quantity=Decimal("10"),
            price=Decimal("100.0"),
        )
    # Must be called exactly once — no retry
    assert len(calls) == 1


def test_get_quote_uses_quote_retry_max(gw, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(settings, "broker_quote_retry_max", 1)
    monkeypatch.setattr(gw, "_init_clients", lambda: None)
    calls = []

    def fake_quote(*a, **kw):
        calls.append(1)
        raise _TransientErr("rate limit")

    gw._quote_ctx = MagicMock(quote=fake_quote)
    gw._trade_ctx = MagicMock()
    with pytest.raises(_TransientErr):
        gw.get_quote("AAPL.US")
    assert len(calls) == 2


def test_call_with_retry_exponential_backoff_values(gw, monkeypatch):
    """B6: Verify the exponential backoff delay sequence.

    With base_ms=1000 and max_retries=3, the delay sequence (attempt=0,1,2)
    should be [500ms, 1000ms, 2000ms] = [0.5s, 1s, 2s].
    """
    delays: list[float] = []

    def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("time.sleep", fake_sleep)

    calls = []

    def always_fail():
        calls.append(1)
        raise _TransientErr("rate limit")

    with pytest.raises(_TransientErr):
        gw._call_with_retry(always_fail, op="test", max_retries=3, base_ms=1000)

    # attempt=0: 2^(0-1)=0.5s, attempt=1: 2^(1-1)=1s, attempt=2: 2^(2-1)=2s
    assert len(delays) == 3
    assert delays[0] == pytest.approx(0.5, rel=1e-9)
    assert delays[1] == pytest.approx(1.0, rel=1e-9)
    assert delays[2] == pytest.approx(2.0, rel=1e-9)
