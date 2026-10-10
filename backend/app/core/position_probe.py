from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any


def _write_protocol_payload(
    fd: int,
    payload: Mapping[str, Any],
    *,
    terminate: bool = False,
) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if terminate:
        encoded += b"\n"
    remaining = memoryview(encoded)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise RuntimeError("position probe protocol write failed")
        remaining = remaining[written:]


def _read_request_line(fd: int) -> bytes | None:
    chunks = bytearray()
    while True:
        chunk = os.read(fd, 1)
        if not chunk:
            return None if not chunks else bytes(chunks)
        chunks.extend(chunk)
        if chunk == b"\n":
            return bytes(chunks)


def _request_id(request: bytes) -> int:
    text = request.decode("utf-8", errors="replace").strip()
    if not text.isdecimal():
        raise ValueError("position probe request id is missing")
    request_id = int(text)
    if request_id < 1:
        raise ValueError("position probe request id is missing")
    return request_id


def _read_request(request: bytes) -> tuple[int, str | None]:
    if not request.lstrip().startswith(b"{"):
        return _request_id(request), None
    from app.core.broker_read_codec import strict_loads
    payload = strict_loads(request.decode("utf-8"))
    if (
        type(payload) is not dict or set(payload) != {"request_id", "op"}
        or type(payload["request_id"]) is not int or payload["request_id"] < 1
        or type(payload["op"]) is not str or payload["op"] not in ("today_orders", "account")
    ):
        raise ValueError("invalid isolated broker read request")
    return payload["request_id"], payload["op"]


def _serve_one_request(
    protocol_fd: int,
    trade_ctx: Any,
    classify_retryable: Any,
    build_error_payload: Any,
    request_id: int,
    op: str | None = None,
) -> bool:
    from app.core.broker import (
        _fetch_position_snapshot_from_context,
        _fetch_today_orders_from_context,
        _fetch_account_from_context,
    )

    try:
        if op is None:
            result = _fetch_position_snapshot_from_context(trade_ctx)
        else:
            from app.core.broker_read_codec import encode_result
            parsed = (_fetch_today_orders_from_context(trade_ctx) if op == "today_orders"
                      else _fetch_account_from_context(trade_ctx))
            result = encode_result(op, parsed)
    except Exception as exc:
        retryable = bool(classify_retryable(exc))
        payload = dict(build_error_payload(exc, retryable=retryable))
        payload["request_id"] = request_id
        if op is not None:
            payload["op"] = op
        _write_protocol_payload(protocol_fd, payload, terminate=True)
        return False
    payload = {"status": "ok", "request_id": request_id}
    if op is None:
        payload["positions"] = result
    else:
        payload.update(op=op, result=result)
    _write_protocol_payload(protocol_fd, payload, terminate=True)
    sys.stdout.flush()
    return True


def _serve_persistent(protocol_fd: int) -> int:
    from app.core.broker import (
        _is_retryable_exception,
        _open_persistent_position_context,
    )
    from app.core.position_probe_diagnostics import (
        build_position_probe_error_payload,
    )

    trade_ctx = None
    stdin_fd = sys.stdin.fileno()
    try:
        while True:
            request = _read_request_line(stdin_fd)
            if request is None:
                return 0
            try:
                request_id, op = _read_request(request)
            except ValueError as exc:
                _write_protocol_payload(
                    protocol_fd,
                    build_position_probe_error_payload(exc, retryable=False),
                    terminate=True,
                )
                return 1
            if trade_ctx is None:
                try:
                    trade_ctx = _open_persistent_position_context()
                except Exception as exc:
                    payload = dict(
                        build_position_probe_error_payload(
                            exc,
                            retryable=bool(_is_retryable_exception(exc)),
                        )
                    )
                    payload["request_id"] = request_id
                    if op is not None:
                        payload["op"] = op
                    _write_protocol_payload(protocol_fd, payload, terminate=True)
                    return 1
            if not _serve_one_request(
                protocol_fd,
                trade_ctx,
                _is_retryable_exception,
                build_position_probe_error_payload,
                request_id,
                op,
            ):
                return 1
    finally:
        close = getattr(trade_ctx, "close", None) if trade_ctx is not None else None
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _serve_one_shot(protocol_fd: int) -> int:
    classify_retryable = None
    try:
        from app.core.broker import (
            _fetch_position_snapshot_payload_from_env,
            _is_retryable_exception,
        )
        from app.core.position_probe_diagnostics import (
            build_position_probe_error_payload,
        )

        classify_retryable = _is_retryable_exception
        positions = _fetch_position_snapshot_payload_from_env()
    except Exception as exc:
        retryable = bool(
            classify_retryable(exc)
            if classify_retryable is not None
            else False
        )
        _write_protocol_payload(
            protocol_fd,
            build_position_probe_error_payload(exc, retryable=retryable),
        )
        return 1
    _write_protocol_payload(
        protocol_fd,
        {
            "status": "ok",
            "positions": positions,
        },
    )
    return 0


def main() -> int:
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        persistent = os.environ.get("AUTO_TRADE_POSITION_PROBE_ONESHOT") != "1"
        if persistent and not os.isatty(sys.stdin.fileno()):
            return _serve_persistent(protocol_fd)
        return _serve_one_shot(protocol_fd)
    finally:
        os.close(protocol_fd)


if __name__ == "__main__":
    raise SystemExit(main())
