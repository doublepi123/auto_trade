import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocket

from app.api import ws as ws_module
from app.api.ws import ConnectionManager, manager, websocket_endpoint


class TestConnectionManager:
    def test_init_empty_connections(self) -> None:
        mgr = ConnectionManager()
        assert mgr.active_connections == []

    @pytest.mark.asyncio
    async def test_connect(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        assert ws in mgr.active_connections

    @pytest.mark.asyncio
    async def test_disconnect_when_connected(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        mgr.active_connections.append(ws)
        await mgr.disconnect(ws)
        assert ws not in mgr.active_connections

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.disconnect(ws)

    @pytest.mark.asyncio
    async def test_broadcast_to_active_connections(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        await mgr.broadcast({"type": "status", "value": 123})
        ws.send_text.assert_awaited_once_with(json.dumps({"type": "status", "value": 123}))

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self) -> None:
        mgr = ConnectionManager()
        ws_good = AsyncMock()
        ws_dead = AsyncMock()
        ws_dead.send_text.side_effect = Exception("connection lost")
        await mgr.connect(ws_good)
        await mgr.connect(ws_dead)
        await mgr.broadcast({"type": "ping"})
        assert ws_dead not in mgr.active_connections
        assert ws_good in mgr.active_connections

    @pytest.mark.asyncio
    async def test_cleanup_stale_removes_dead_connections(self) -> None:
        mgr = ConnectionManager()
        ws_good = AsyncMock()
        ws_dead = AsyncMock()
        ws_dead.send_text.side_effect = Exception("connection lost")
        await mgr.connect(ws_good)
        await mgr.connect(ws_dead)
        await mgr.cleanup_stale()
        assert ws_dead not in mgr.active_connections
        assert ws_good in mgr.active_connections
        ws_good.send_text.assert_awaited_once_with(json.dumps({"type": "ping"}))

    @pytest.mark.asyncio
    async def test_cleanup_stale_keeps_alive_connections(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        await mgr.cleanup_stale()
        assert ws in mgr.active_connections

    def test_manager_singleton(self) -> None:
        assert isinstance(manager, ConnectionManager)


class TestWebSocketEndpoint:
    @pytest.mark.asyncio
    async def test_no_auth_when_api_key_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "")
        ws = AsyncMock()
        ws.receive_text.side_effect = ["ping", Exception("stop")]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        assert ws.close.call_count == 0
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_auth_with_valid_token(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.side_effect = [
            json.dumps({"token": "secret123"}),
            "ping",
            Exception("stop"),
        ]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_auth_with_invalid_token(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.return_value = json.dumps({"token": "wrong"})

        await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4001)

    @pytest.mark.asyncio
    async def test_auth_with_plain_token(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.side_effect = [
            "secret123",
            "ping",
            Exception("stop"),
        ]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_auth_with_api_key_field(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.side_effect = [
            json.dumps({"api_key": "secret123"}),
            "ping",
            Exception("stop"),
        ]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_auth_message_too_large(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.return_value = "x" * 5000

        await websocket_endpoint(ws)

        ws.close.assert_awaited_once_with(code=4001)

    @pytest.mark.asyncio
    async def test_auth_timeout(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "secret123")
        ws = AsyncMock()
        ws.receive_text.side_effect = TimeoutError()

        await websocket_endpoint(ws)

        ws.close.assert_awaited_once_with(code=4001)

    @pytest.mark.asyncio
    async def test_ping_pong(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "")
        ws = AsyncMock()
        ws.receive_text.side_effect = ["ping", Exception("stop")]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_disconnect_handling(self, monkeypatch) -> None:
        monkeypatch.setattr(ws_module.settings, "api_key", "")
        ws = AsyncMock()
        from starlette.websockets import WebSocketDisconnect
        ws.receive_text.side_effect = WebSocketDisconnect()

        await websocket_endpoint(ws)

        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_connections(self) -> None:
        mgr = ConnectionManager()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await mgr.connect(ws1)
        await mgr.connect(ws2)
        await mgr.broadcast({"msg": "hello"})
        ws1.send_text.assert_awaited_once_with(json.dumps({"msg": "hello"}))
        ws2.send_text.assert_awaited_once_with(json.dumps({"msg": "hello"}))

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self) -> None:
        mgr = ConnectionManager()
        ws = AsyncMock()
        await mgr.connect(ws)
        await mgr.disconnect(ws)
        await mgr.disconnect(ws)
        assert ws not in mgr.active_connections
