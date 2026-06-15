from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings

logger = logging.getLogger("auto_trade.ws")

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _current_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def connect(self, ws: WebSocket) -> None:
        async with self._current_lock():
            self.active_connections.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._current_lock():
            if ws in self.active_connections:
                self.active_connections.remove(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        async with self._current_lock():
            connections = list(self.active_connections)
        dead: list[WebSocket] = []
        for conn in connections:
            try:
                await asyncio.wait_for(conn.send_text(json.dumps(message)), timeout=1.0)
            except Exception:
                dead.append(conn)
        if dead:
            async with self._current_lock():
                for conn in dead:
                    if conn in self.active_connections:
                        self.active_connections.remove(conn)

    async def cleanup_stale(self) -> None:
        async with self._current_lock():
            connections = list(self.active_connections)
        dead: list[WebSocket] = []
        for conn in connections:
            try:
                await asyncio.wait_for(conn.send_text(json.dumps({"type": "ping"})), timeout=1.0)
            except Exception:
                dead.append(conn)
        if dead:
            async with self._current_lock():
                for conn in dead:
                    if conn in self.active_connections:
                        self.active_connections.remove(conn)


manager = ConnectionManager()

_WS_AUTH_TIMEOUT_SECONDS = 5.0
# Cap the size of the post-accept auth frame to avoid a slow-client / attacker
# uploading an arbitrarily large text frame during the auth window.
_WS_AUTH_MAX_FRAME_BYTES = 4096
# Generous cap for the post-auth read loop. The handler only responds to
# "ping" today, but we still bound memory in case a future handler tries to
# parse the body.
_WS_MAX_FRAME_BYTES = 1 * 1024 * 1024  # 1 MiB


def _api_key_matches(provided: str) -> bool:
    return bool(provided) and secrets.compare_digest(provided, settings.api_key)


_auth_disabled_warned = False


async def _authenticate_websocket(ws: WebSocket, query_api_key: str) -> bool:
    """Validate API key for WebSocket connection.

    Behavior when api_key is empty:
      - dev/test: auth is disabled (requests pass through), warning logged once.
      - non-dev/test: rejected - must configure AUTO_TRADE_API_KEY.

    Mirrors backend/app/api/auth.py:require_api_key. Keeping both paths aligned
    prevents the production deployment gap where /ws would accept connections
    while HTTP endpoints reject them under the same configuration.
    """
    global _auth_disabled_warned
    if not settings.api_key:
        if settings.env not in ("dev", "test"):
            logger.error(
                "AUTO_TRADE_API_KEY not configured in %s environment - rejecting WS connection",
                settings.env,
            )
            return False
        if not _auth_disabled_warned:
            logger.warning(
                "AUTO_TRADE_API_KEY not configured - WS auth disabled (dev/test mode only)"
            )
            _auth_disabled_warned = True
        return True
    if _api_key_matches(query_api_key):
        return True
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError, WebSocketDisconnect):
        return False
    if len(raw.encode("utf-8")) > _WS_AUTH_MAX_FRAME_BYTES:
        logger.warning("WS auth frame exceeds %d bytes; rejecting", _WS_AUTH_MAX_FRAME_BYTES)
        return False
    if raw == "ping":
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if payload.get("type") != "auth":
        return False
    return _api_key_matches(str(payload.get("api_key") or ""))


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, api_key: str = "") -> None:
    await ws.accept()
    if not await _authenticate_websocket(ws, api_key):
        await ws.close(code=1008, reason="Invalid or missing API key")
        return
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if len(data.encode("utf-8")) > _WS_MAX_FRAME_BYTES:
                logger.warning("WS frame exceeds %d bytes; closing connection", _WS_MAX_FRAME_BYTES)
                await ws.close(code=1009, reason="Message too big")
                return
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)
