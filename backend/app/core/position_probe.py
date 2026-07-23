from __future__ import annotations

import json
import os
import sys
from typing import Any


def _write_protocol_payload(fd: int, payload: dict[str, Any]) -> None:
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
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            from app.core.broker import (
                _fetch_position_snapshot_payload_from_env,
            )

            positions = _fetch_position_snapshot_payload_from_env()
        except Exception as exc:
            _write_protocol_payload(
                protocol_fd,
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                },
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
