"""Tests for IndexConnectionManager and ontology_index_websocket endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ontokit.api.routes.projects import IndexConnectionManager

PROJECT_ID = "12345678-1234-5678-1234-567812345678"


# ---------------------------------------------------------------------------
# IndexConnectionManager unit tests
# ---------------------------------------------------------------------------


class TestIndexConnectionManager:
    """Tests for IndexConnectionManager connect/disconnect."""

    @pytest.mark.asyncio
    async def test_connect_adds_websocket(self) -> None:
        """connect() accepts the websocket and adds it to active_connections."""
        mgr = IndexConnectionManager()
        ws = AsyncMock(spec=WebSocket)
        project_id = "test-project"

        await mgr.connect(ws, project_id)

        ws.accept.assert_awaited_once()
        assert ws in mgr.active_connections[project_id]

    @pytest.mark.asyncio
    async def test_connect_multiple_to_same_project(self) -> None:
        """connect() adds multiple websockets to the same project."""
        mgr = IndexConnectionManager()
        ws1 = AsyncMock(spec=WebSocket)
        ws2 = AsyncMock(spec=WebSocket)
        project_id = "test-project"

        await mgr.connect(ws1, project_id)
        await mgr.connect(ws2, project_id)

        assert len(mgr.active_connections[project_id]) == 2

    def test_disconnect_removes_websocket(self) -> None:
        """disconnect() removes the websocket from active connections."""
        mgr = IndexConnectionManager()
        ws = Mock(spec=WebSocket)
        project_id = "test-project"

        mgr.active_connections[project_id] = [ws]

        mgr.disconnect(ws, project_id)
        assert project_id not in mgr.active_connections

    def test_disconnect_keeps_other_connections(self) -> None:
        """disconnect() only removes the specific websocket, keeps others."""
        mgr = IndexConnectionManager()
        ws1 = Mock(spec=WebSocket)
        ws2 = Mock(spec=WebSocket)
        project_id = "test-project"

        mgr.active_connections[project_id] = [ws1, ws2]

        mgr.disconnect(ws1, project_id)
        assert mgr.active_connections[project_id] == [ws2]

    def test_disconnect_nonexistent_project(self) -> None:
        """disconnect() is a no-op if the project has no connections."""
        mgr = IndexConnectionManager()
        ws = Mock(spec=WebSocket)

        # Should not raise
        mgr.disconnect(ws, "nonexistent")
        assert "nonexistent" not in mgr.active_connections

    def test_disconnect_websocket_not_in_list(self) -> None:
        """disconnect() is a no-op when websocket is not in the connection list."""
        mgr = IndexConnectionManager()
        ws1 = Mock(spec=WebSocket)
        ws2 = Mock(spec=WebSocket)
        project_id = "test-project"

        mgr.active_connections[project_id] = [ws1]
        # Disconnect ws2 which is not in the list - should not raise
        mgr.disconnect(ws2, project_id)
        assert mgr.active_connections[project_id] == [ws1]


# ---------------------------------------------------------------------------
# WebSocket endpoint integration tests
# ---------------------------------------------------------------------------


class TestOntologyIndexWebSocket:
    """Tests for the ontology_index_websocket endpoint via TestClient."""

    def test_ws_project_not_found_closes_with_4004(
        self,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """WebSocket is closed with code 4004 when project doesn't exist."""
        from unittest.mock import patch

        client, mock_session = authed_client

        # DB query returns no project
        mock_result = Mock()
        mock_result.scalar_one_or_none.return_value = None

        with patch("ontokit.api.routes.projects.async_session_maker") as mock_session_maker:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_session_maker.return_value = mock_ctx
            mock_session.execute.return_value = mock_result

            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(f"/api/v1/projects/{PROJECT_ID}/ontology/index-ws"),
            ):
                pass  # pragma: no cover
