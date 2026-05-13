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

    def connect(self, ws: WebSocket) -> None:
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for conn in self.active_connections:
            try:
                await conn.send_text(json.dumps(message))
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.disconnect(conn)

    async def cleanup_stale(self) -> None:
        dead: list[WebSocket] = []
        for conn in self.active_connections:
            try:
                await conn.send_text(json.dumps({"type": "ping"}))
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.disconnect(conn)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    if settings.api_key:
        try:
            auth_msg = await asyncio.wait_for(ws.receive_text(), timeout=10)
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

    manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(ws)
