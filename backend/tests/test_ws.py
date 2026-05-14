import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.ws import ConnectionManager, manager


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

    def test_manager_singleton(self) -> None:
        assert isinstance(manager, ConnectionManager)
