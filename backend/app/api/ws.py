from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("auto_trade.ws")

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.active_connections.append(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self.active_connections:
                self.active_connections.remove(ws)

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            connections = list(self.active_connections)
        dead: list[WebSocket] = []
        for conn in connections:
            try:
                await asyncio.wait_for(conn.send_text(json.dumps(message)), timeout=1.0)
            except Exception:
                dead.append(conn)
        if dead:
            async with self._lock:
                for conn in dead:
                    if conn in self.active_connections:
                        self.active_connections.remove(conn)

    async def cleanup_stale(self) -> None:
        async with self._lock:
            connections = list(self.active_connections)
        dead: list[WebSocket] = []
        for conn in connections:
            try:
                await asyncio.wait_for(conn.send_text(json.dumps({"type": "ping"})), timeout=1.0)
            except Exception:
                dead.append(conn)
        if dead:
            async with self._lock:
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
