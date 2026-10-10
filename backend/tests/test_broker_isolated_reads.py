from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.core import broker as broker_module
from app.config import settings
from app.core.position_probe_diagnostics import (
    PositionProbeConnectionError, PositionProbeProtocolError,
    PositionProbeRuntimeError, PositionProbeTimeoutError,
)


_SDK_SOURCE = '''
import ctypes, os, time
from pathlib import Path
from types import SimpleNamespace as NS
from datetime import datetime, timezone, timedelta
from decimal import Decimal
class Config:
    @staticmethod
    def from_env(): return Config()
class OpenApiException(Exception): pass
class QuoteContext:
    def __init__(self, config): raise AssertionError("child must not open QuoteContext")
class _FakeEnum:
    def __init__(self, text): self.text = text
    def __str__(self): return self.text
class TradeContext:
    def __init__(self, config): pass
    def close(self): pass
    def stock_positions(self): return []
    def _wait(self):
        if os.environ.get("READ_HOLD"):
            Path(os.environ["READ_GATE"]).write_text("started")
            ctypes.PyDLL(None).usleep(800_000)
        if os.environ.get("READ_ERROR"):
            raise ConnectionError("network unavailable")
    def today_orders(self):
        self._wait()
        if os.environ.get("READ_EMPTY"): return []
        orders = [NS(order_id="" if os.environ.get("READ_MISSING_ID") else "id-1",
            symbol="AAPL.US", side=_FakeEnum("OrderSide.Buy"),
            status=_FakeEnum("OrderStatus.Filled"), submitted_quantity=Decimal("2.000"),
            submitted_price=Decimal("123.4567890123456789"), executed_quantity=Decimal("2"),
            executed_price=Decimal("123.40"),
            submitted_at=datetime(2026,10,8,9,30,tzinfo=timezone(timedelta(hours=-4))),
            updated_at=datetime(2026,10,8,9,31,tzinfo=timezone(timedelta(hours=-4))))]
        if os.environ.get("READ_DATES") == "created":
            orders[0].created_at = datetime(2026,10,8,9,29,tzinfo=timezone.utc)
        elif os.environ.get("READ_DATES") == "no_dates":
            orders[0].submitted_at = orders[0].updated_at = None
        return orders
    def account_balance(self):
        self._wait()
        currencies = os.environ.get("READ_CURRENCIES", "USD,HKD,EUR").split(",")
        return [NS(currency=c, net_assets=Decimal("1234.5678"), risk_level=1,
            margin_call=Decimal("1"), init_margin=Decimal("2"), maintenance_margin=Decimal("3"),
            max_finance_amount=Decimal("4"), remaining_finance_amount=Decimal("5"),
            buy_power=Decimal("6"), cash_infos=[NS(currency=c,
            available_cash=Decimal("7.000"), frozen_cash=Decimal("8"))]) for c in currencies]
'''


@pytest.fixture
def isolated_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    package = tmp_path / "longport"
    package.mkdir()
    (package / "__init__.py").write_text("")
    sdk_path = package / "openapi.py"
    sdk_path.write_text(_SDK_SOURCE)
    spec = importlib.util.spec_from_file_location("_fake_read_sdk", sdk_path)
    assert spec is not None and spec.loader is not None
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(broker_module, "_import_openapi", lambda: sdk)
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", True)
    monkeypatch.setattr(settings, "broker_position_snapshot_timeout_seconds", 5.0)
    gateway = broker_module.BrokerGateway()
    gateway._trade_ctx = sdk.TradeContext(sdk.Config())
    monkeypatch.setattr(gateway, "_init_clients", lambda: None)
    yield gateway
    gateway.close()


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
def test_gil_read_does_not_freeze_parent(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str) -> None:
    gate = tmp_path / "gate"
    monkeypatch.setenv("READ_GATE", str(gate))
    monkeypatch.setenv("READ_HOLD", "1")
    # Warm the actual persistent child before measuring, including on old code.
    assert isolated_gateway.get_positions() == []
    errors: list[Exception] = []
    def invoke() -> None:
        try:
            getattr(isolated_gateway, method)()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=invoke)
    started = time.monotonic()
    thread.start()
    try:
        while not gate.exists() and time.monotonic() - started < 4:
            time.sleep(0.002)
        elapsed = time.monotonic() - started
        assert gate.exists()
        assert elapsed < 0.65, f"parent frozen for {elapsed:.3f}s by SDK GIL hold"
        assert thread.is_alive(), "SDK call must still be in flight"
    finally:
        thread.join(6)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
@pytest.mark.parametrize("variant", ["default", "empty", "EUR", "EUR,JPY", "HKD,USD", "created", "no_dates"])
def test_direct_isolated_parity(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, method: str, variant: str) -> None:
    if variant == "empty":
        monkeypatch.setenv("READ_EMPTY", "1")
    elif variant in ("created", "no_dates"):
        monkeypatch.setenv("READ_DATES", variant)
    elif variant != "default":
        monkeypatch.setenv("READ_CURRENCIES", variant)
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", False)
    direct = getattr(isolated_gateway, method)()
    assert isolated_gateway._position_probe_worker is None
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", True)
    isolated = getattr(isolated_gateway, method)()
    assert direct == isolated
    if method == "get_today_orders" and variant != "empty":
        assert isolated[0].status == "FILLED"
        if variant == "no_dates":
            assert isolated[0].filled_at is None and isolated[0].created_at is None
        else:
            assert isolated[0].filled_at is not None and isolated[0].created_at is not None


def test_missing_order_id_fails_shared_parser(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("READ_MISSING_ID", "1")
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", False)
    with pytest.raises(ValueError, match="without broker_order_id"):
        isolated_gateway.get_today_orders()
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", True)
    with pytest.raises(PositionProbeRuntimeError) as caught:
        isolated_gateway.get_today_orders()
    assert caught.value.diagnostics.error_type == "ValueError"
    assert "without broker_order_id" in caught.value.diagnostics.error_message
    assert "today_orders" in str(caught.value)
    assert isolated_gateway._position_probe_worker is None


def _scripted_command(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], *, wrong_id: bool = False) -> None:
    id_expression = "999" if wrong_id else 'request["request_id"]'
    script = (
        "import sys,json,os\n"
        f"body=json.loads({json.dumps(json.dumps(payload))})\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        f" body['request_id']={id_expression}\n"
        " print(json.dumps(body),flush=True)\n"
        " if body.get('status')=='error': sys.exit(1)\n"
    )
    monkeypatch.setattr(broker_module, "_POSITION_PROBE_COMMAND", (sys.executable, "-c", script))


@pytest.mark.parametrize("tamper", ["extra", "missing", "decimal_number", "decimal_nan", "json_nan", "wrong_id", "oversized", "wrong_op", "datetime", "extra_envelope", "bool_id"])
def test_strict_protocol_discards(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, tamper: str) -> None:
    from app.core.broker_read_codec import encode_result
    result = encode_result("today_orders", broker_module._fetch_today_orders_from_context(isolated_gateway._trade_ctx))
    payload: dict[str, Any] = {"status": "ok", "op": "today_orders", "result": result}
    row = result[0]
    if tamper == "extra": row["extra"] = "x"
    elif tamper == "missing": del row["price"]
    elif tamper == "decimal_number": row["price"] = 123
    elif tamper == "decimal_nan": row["price"] = "NaN"
    elif tamper == "json_nan": row["price"] = float("nan")
    elif tamper == "oversized":
        monkeypatch.setattr(broker_module, "_POSITION_PROBE_MAX_OUTPUT_BYTES", 512)
        row["symbol"] = "x" * 1024
    elif tamper == "wrong_op": payload["op"] = "account"
    elif tamper == "datetime": row["created_at"] = "2026-01-01"
    elif tamper == "extra_envelope": payload["extra"] = 1
    _scripted_command(monkeypatch, payload, wrong_id=tamper == "wrong_id")
    if tamper == "bool_id":
        command = broker_module._POSITION_PROBE_COMMAND
        monkeypatch.setattr(broker_module, "_POSITION_PROBE_COMMAND", (command[0], command[1], command[2].replace("body['request_id']=request[\"request_id\"]", "body['request_id']=True")))
    with pytest.raises(PositionProbeProtocolError) as caught:
        isolated_gateway.get_today_orders()
    assert "today_orders" in str(caught.value)
    assert "position snapshot" not in str(caught.value)
    assert isolated_gateway._position_probe_worker is None


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
def test_timeout_discards_and_respawns(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str) -> None:
    monkeypatch.setenv("READ_HOLD", "1")
    monkeypatch.setenv("READ_GATE", str(tmp_path / "gate"))
    assert isolated_gateway.get_positions() == []
    old = isolated_gateway._position_probe_worker
    monkeypatch.setattr(settings, "broker_position_snapshot_timeout_seconds", 0.15)
    monkeypatch.setattr(settings, "broker_retry_max", 0)
    with pytest.raises(PositionProbeTimeoutError) as caught:
        getattr(isolated_gateway, method)()
    assert ("today_orders" if method == "get_today_orders" else "account") in str(caught.value)
    assert isolated_gateway._position_probe_worker is None
    monkeypatch.delenv("READ_HOLD")
    monkeypatch.setattr(settings, "broker_position_snapshot_timeout_seconds", 5.0)
    getattr(isolated_gateway, method)()
    assert isolated_gateway._position_probe_worker is not old


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
def test_retry_ladder_and_error_mapping(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setenv("READ_ERROR", "1")
    monkeypatch.setattr(settings, "broker_retry_max", 2)
    monkeypatch.setattr(settings, "broker_retry_base_ms", 0)
    real_popen = subprocess.Popen
    calls: list[Any] = []
    def spawn(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        calls.append(process)
        return process
    monkeypatch.setattr(broker_module.subprocess, "Popen", spawn)
    with pytest.raises(PositionProbeConnectionError) as caught:
        getattr(isolated_gateway, method)()
    assert len(calls) == 3
    assert all(p.poll() is not None for p in calls)
    assert "position snapshot" not in str(caught.value)
    assert isolated_gateway._position_probe_worker is None


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
def test_disabled_never_spawns(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(settings, "broker_position_snapshot_isolation_enabled", False)
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled isolation must not spawn")
    monkeypatch.setattr(broker_module.subprocess, "Popen", forbidden)
    getattr(isolated_gateway, method)()


@pytest.mark.parametrize("cleanup", ["discard", "kill_and_reap"])
def test_late_reaper_retains_process(cleanup: str) -> None:
    reaped = threading.Event()
    class _FakePopen:
        stdin = stdout = stderr = None
        pid = 123456
        returncode: int | None = None
        def poll(self) -> None: return None
        def kill(self) -> None: pass
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("fake", timeout)
            self.returncode = -9
            reaped.set()
            return -9
    process: Any = _FakePopen()
    worker = broker_module._PositionProbeWorker()
    if cleanup == "discard":
        worker._process = process
        worker.discard()
        assert worker._process is None
    else:
        worker._kill_and_reap(process)
    assert reaped.wait(1)
    assert process.returncode == -9


@pytest.mark.parametrize("tamper", ["missing", "extra", "decimal", "nan", "risk_level", "null_list"])
def test_account_codec_strictness(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, tamper: str) -> None:
    from app.core.broker_read_codec import encode_result
    result = encode_result("account", broker_module._fetch_account_from_context(isolated_gateway._trade_ctx))
    if tamper == "missing": del result["currency"]
    elif tamper == "extra": result["cash_balances"][0]["extra"] = "x"
    elif tamper == "decimal": result["net_assets"][0]["amount"] = 12.0
    elif tamper == "nan": result["margin_infos"][0]["buy_power"] = "NaN"
    elif tamper == "risk_level": result["margin_infos"][0]["risk_level"] = True
    elif tamper == "null_list": result["cash_balances"] = None
    _scripted_command(monkeypatch, {"status": "ok", "op": "account", "result": result})
    with pytest.raises(PositionProbeProtocolError, match="account"):
        isolated_gateway.get_account()
    assert isolated_gateway._position_probe_worker is None


@pytest.mark.parametrize("wire_request", [b'{"request_id":true,"op":"account"}\n', b'{"request_id":1,"op":"submit"}\n', b'{"request_id":1,"op":"account","extra":1}\n', b'{"op":"account"}\n', b'{"request_id":1,"request_id":2,"op":"account"}\n'])
def test_child_request_strictness(wire_request: bytes) -> None:
    from app.core.position_probe import _read_request
    with pytest.raises(ValueError):
        _read_request(wire_request)


def test_same_worker_and_context_for_all_reads(isolated_gateway: Any) -> None:
    assert isolated_gateway.get_positions() == []
    worker = isolated_gateway._position_probe_worker
    process = worker._process
    isolated_gateway.get_today_orders()
    isolated_gateway.get_account()
    assert isolated_gateway.get_positions() == []
    assert isolated_gateway._position_probe_worker is worker
    assert worker._process is process


def test_nonretryable_error_is_not_retried(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_command(monkeypatch, {
        "status": "error", "op": "account", "error_type": "ValueError", "retryable": False,
        "sdk_error_code": "", "sdk_error_category": "VALUEERROR", "error_message": "invalid response",
    })
    monkeypatch.setattr(settings, "broker_retry_max", 2)
    with pytest.raises(PositionProbeRuntimeError, match="account"):
        isolated_gateway.get_account()
    assert isolated_gateway._position_probe_worker is None


@pytest.mark.parametrize("method", ["get_today_orders", "get_account"])
def test_child_death_discards_and_next_read_respawns(isolated_gateway: Any, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    real_command = broker_module._POSITION_PROBE_COMMAND
    monkeypatch.setattr(broker_module, "_POSITION_PROBE_COMMAND", (sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(1)"))
    with pytest.raises(PositionProbeProtocolError):
        getattr(isolated_gateway, method)()
    assert isolated_gateway._position_probe_worker is None
    monkeypatch.setattr(broker_module, "_POSITION_PROBE_COMMAND", real_command)
    getattr(isolated_gateway, method)()
    assert isolated_gateway._position_probe_worker is not None
