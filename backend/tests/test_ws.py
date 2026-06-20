import asyncio
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
    async def test_ws_accept_and_ping_pong(self) -> None:
        ws = AsyncMock()
        ws.receive_text.side_effect = ["ping", Exception("stop")]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_ws_rejects_when_api_key_required_and_auth_missing(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "secret-key")
        monkeypatch.setattr(settings, "env", "prod")
        ws = AsyncMock()
        ws.receive_text.side_effect = asyncio.TimeoutError()

        await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1008, reason="Invalid or missing API key")

    @pytest.mark.asyncio
    async def test_ws_rejects_in_prod_when_api_key_unset(self, monkeypatch) -> None:
        """P0-1: prod environment + empty api_key must reject (not silently accept).

        Previously the WS path used `return settings.env in ("dev","test")`,
        diverging from auth.py:require_api_key which rejects the same config
        over HTTP. This test pins the aligned behavior.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "")
        monkeypatch.setattr(settings, "env", "prod")
        ws = AsyncMock()

        await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1008, reason="Invalid or missing API key")

    @pytest.mark.asyncio
    async def test_ws_accepts_in_dev_when_api_key_unset(self, monkeypatch) -> None:
        """dev environment + empty api_key must accept (backwards compatible)."""
        from app.config import settings
        import app.api.ws as ws_module

        ws_module._auth_disabled_warned = False  # reset warning latch
        monkeypatch.setattr(settings, "api_key", "")
        monkeypatch.setattr(settings, "env", "dev")
        ws = AsyncMock()
        ws.receive_text.side_effect = ["ping", Exception("stop")]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_ws_rejects_in_staging_when_api_key_unset(self, monkeypatch) -> None:
        """Any non-dev/non-test env must reject when api_key is unset."""
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "")
        monkeypatch.setattr(settings, "env", "staging")
        ws = AsyncMock()

        await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1008, reason="Invalid or missing API key")

    @pytest.mark.asyncio
    async def test_ws_accepts_post_connect_auth_message(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "secret-key")
        monkeypatch.setattr(settings, "env", "prod")
        ws = AsyncMock()
        ws.receive_text.side_effect = [
            json.dumps({"type": "auth", "api_key": "secret-key"}),
            "ping",
            Exception("stop"),
        ]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_ws_accepts_proxy_header_api_key(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "secret-key")
        monkeypatch.setattr(settings, "env", "prod")
        ws = AsyncMock()
        ws.headers = {"x-api-key": "secret-key"}
        ws.receive_text.side_effect = ["ping", Exception("stop")]

        with pytest.raises(Exception):
            await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.send_text.assert_any_await(json.dumps({"type": "pong"}))
        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_disconnect_handling(self) -> None:
        ws = AsyncMock()
        from starlette.websockets import WebSocketDisconnect
        ws.receive_text.side_effect = WebSocketDisconnect()

        await websocket_endpoint(ws)

        await manager.disconnect(ws)

    @pytest.mark.asyncio
    async def test_ws_auth_handles_websocket_disconnect(self, monkeypatch) -> None:
        """Issue I2-3: WebSocketDisconnect during auth must be caught
        gracefully (not crash with an unhandled exception).
        """
        from app.config import settings
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setattr(settings, "api_key", "secret-key")
        monkeypatch.setattr(settings, "env", "prod")
        ws = AsyncMock()
        ws.receive_text.side_effect = WebSocketDisconnect()

        await websocket_endpoint(ws)

        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1008, reason="Invalid or missing API key")

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
