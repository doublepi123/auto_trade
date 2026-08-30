from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any


def _write_protocol_payload(fd: int, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    remaining = memoryview(encoded)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise RuntimeError("position probe protocol write failed")
        remaining = remaining[written:]


def main() -> int:
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    classify_retryable = None
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
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
    finally:
        os.close(protocol_fd)


if __name__ == "__main__":
    raise SystemExit(main())
