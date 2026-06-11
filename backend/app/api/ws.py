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


def _api_key_matches(provided: str) -> bool:
    return bool(provided) and secrets.compare_digest(provided, settings.api_key)


async def _authenticate_websocket(ws: WebSocket, query_api_key: str) -> bool:
    if not settings.api_key:
        return settings.env in ("dev", "test")
    if _api_key_matches(query_api_key):
        return True
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
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
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)
