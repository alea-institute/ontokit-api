"""Tests for IndexConnectionManager and ontology_index_websocket endpoint."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from ontokit.api.routes.projects import (
    IndexConnectionManager,
    ontology_index_websocket,
)

PROJECT_ID = "12345678-1234-5678-1234-567812345678"
PROJECT_UUID = UUID(PROJECT_ID)


# ---------------------------------------------------------------------------
# IndexConnectionManager unit tests
# ---------------------------------------------------------------------------


class TestIndexConnectionManager:
    """Tests for IndexConnectionManager connect/disconnect."""

    @pytest.mark.asyncio
    async def test_connect_adds_websocket(self) -> None:
        mgr = IndexConnectionManager()
        ws = AsyncMock(spec=WebSocket)
        await mgr.connect(ws, "proj")
        assert ws in mgr.active_connections["proj"]

    @pytest.mark.asyncio
    async def test_connect_multiple_to_same_project(self) -> None:
        mgr = IndexConnectionManager()
        ws1 = AsyncMock(spec=WebSocket)
        ws2 = AsyncMock(spec=WebSocket)
        await mgr.connect(ws1, "proj")
        await mgr.connect(ws2, "proj")
        assert len(mgr.active_connections["proj"]) == 2

    def test_disconnect_removes_websocket(self) -> None:
        mgr = IndexConnectionManager()
        ws = Mock(spec=WebSocket)
        mgr.active_connections["proj"] = [ws]
        mgr.disconnect(ws, "proj")
        assert "proj" not in mgr.active_connections

    def test_disconnect_keeps_other_connections(self) -> None:
        mgr = IndexConnectionManager()
        ws1 = Mock(spec=WebSocket)
        ws2 = Mock(spec=WebSocket)
        mgr.active_connections["proj"] = [ws1, ws2]
        mgr.disconnect(ws1, "proj")
        assert mgr.active_connections["proj"] == [ws2]

    def test_disconnect_nonexistent_project(self) -> None:
        mgr = IndexConnectionManager()
        mgr.disconnect(Mock(spec=WebSocket), "nonexistent")

    def test_disconnect_websocket_not_in_list(self) -> None:
        mgr = IndexConnectionManager()
        ws1 = Mock(spec=WebSocket)
        ws2 = Mock(spec=WebSocket)
        mgr.active_connections["proj"] = [ws1]
        mgr.disconnect(ws2, "proj")
        assert mgr.active_connections["proj"] == [ws1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_pubsub(messages: list[Any]) -> tuple[AsyncMock, AsyncMock]:
    """Return (mock_pool, mock_pubsub) that delivers messages then returns None."""
    call_count = 0

    async def fake_get_message(
        ignore_subscribe_messages: bool = True,  # noqa: ARG001
        timeout: float = 0.1,  # noqa: ARG001
    ) -> dict[str, Any] | None:
        nonlocal call_count
        call_count += 1
        if call_count <= len(messages):
            msg = messages[call_count - 1]
            data = json.dumps(msg) if isinstance(msg, dict) else msg
            return {"type": "message", "data": data}
        return None

    mock_pubsub = AsyncMock()
    mock_pubsub.get_message = fake_get_message
    mock_pool = AsyncMock()
    mock_pool.pubsub = Mock(return_value=mock_pubsub)
    return mock_pool, mock_pubsub


# ---------------------------------------------------------------------------
# ontology_index_websocket tests (auth delegated to authenticate_ws)
# ---------------------------------------------------------------------------


class TestOntologyIndexWebSocketUnit:
    """Tests for ontology_index_websocket message handling."""

    @pytest.mark.asyncio
    async def test_auth_failure_returns_early(self) -> None:
        """Returns without connecting when authenticate_ws returns False."""
        ws = AsyncMock(spec=WebSocket)
        mock_auth = AsyncMock(return_value=False)
        mock_mgr = AsyncMock()

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", mock_auth),
            patch("ontokit.api.routes.projects.index_ws_manager", mock_mgr),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connects_and_forwards_matching_message(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        index_msg = {"type": "index_complete", "project_id": PROJECT_ID, "entity_count": 100}
        mock_pool, mock_pubsub = _mock_pubsub([index_msg])
        mock_mgr = AsyncMock()

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", mock_mgr),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.connect.assert_awaited_once()
        ws.send_json.assert_awaited_once_with(index_msg)
        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_messages_for_other_projects(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        other_msg = {"type": "index_complete", "project_id": "other-project"}
        mock_pool, _ = _mock_pubsub([other_msg])

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", AsyncMock()),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token="t")

        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=[TimeoutError, WebSocketDisconnect(code=1000)])

        valid_msg = {"type": "index_started", "project_id": PROJECT_ID}
        mock_pool, _ = _mock_pubsub(["not valid json{{{", valid_msg])

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", AsyncMock()),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token="t")

        ws.send_json.assert_awaited_once_with(valid_msg)

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(self) -> None:
        ws = AsyncMock(spec=WebSocket)

        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=RuntimeError("Redis down"))
        mock_pool = AsyncMock()
        mock_pool.pubsub = Mock(return_value=mock_pubsub)
        mock_mgr = AsyncMock()

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", mock_mgr),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()
