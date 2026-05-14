from __future__ import annotations

import asyncio
import json
import logging
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings

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
                await conn.send_text(json.dumps(message))
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
                await conn.send_text(json.dumps({"type": "ping"}))
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

    if settings.api_key:
        try:
            auth_msg = await asyncio.wait_for(ws.receive_text(), timeout=10)
            if len(auth_msg) > 4096:
                logger.warning("WebSocket auth message too large (%d bytes) from %s", len(auth_msg), ws.client)
                await ws.close(code=4001)
                return
            auth_data = json.loads(auth_msg) if auth_msg.startswith("{") else {"token": auth_msg}
            token = auth_data.get("token", auth_data.get("api_key", ""))
            if not secrets.compare_digest(token, settings.api_key):
                logger.warning("invalid API key for WebSocket from %s", ws.client)
                await ws.close(code=4001)
                return
        except Exception:
            logger.warning("WebSocket auth failed from %s", ws.client)
            await ws.close(code=4001)
            return

    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        await manager.disconnect(ws)
