from __future__ import annotations

from typing import Any

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
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
