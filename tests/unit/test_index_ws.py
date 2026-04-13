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
FAKE_TOKEN = "fake-jwt-token"


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
        ws.accept.assert_awaited_once()
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


def _fake_token_payload() -> Mock:
    """Return a mock TokenPayload from validate_token."""
    payload = Mock()
    payload.sub = "user-1"
    payload.name = "Test User"
    payload.email = "test@example.com"
    payload.preferred_username = "testuser"
    payload.roles = ["owner"]
    return payload


def _mock_auth_and_project(
    project_exists: bool = True,
) -> tuple[AsyncMock, AsyncMock]:
    """Return (mock_validate_token, mock_project_service_cls) patches."""
    mock_validate = AsyncMock(return_value=_fake_token_payload())

    mock_svc = AsyncMock()
    if project_exists:
        mock_svc.get.return_value = Mock()  # project response
    else:
        from fastapi import HTTPException

        mock_svc.get.side_effect = HTTPException(status_code=404, detail="Not found")

    return mock_validate, mock_svc


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
# ontology_index_websocket async unit tests
# ---------------------------------------------------------------------------


class TestOntologyIndexWebSocketUnit:
    """Direct async tests for ontology_index_websocket function."""

    @pytest.mark.asyncio
    async def test_no_token_closes_with_4001(self) -> None:
        """Closes with 4001 when no token is provided."""
        ws = AsyncMock(spec=WebSocket)
        await ontology_index_websocket(ws, PROJECT_UUID, token=None)
        ws.close.assert_awaited_once_with(code=4001, reason="Authentication required")

    @pytest.mark.asyncio
    async def test_invalid_token_closes_with_4001(self) -> None:
        """Closes with 4001 when token validation fails."""
        ws = AsyncMock(spec=WebSocket)
        mock_validate = AsyncMock(side_effect=ValueError("bad token"))

        with patch("ontokit.core.auth.validate_token", mock_validate):
            await ontology_index_websocket(ws, PROJECT_UUID, token="bad-token")

        ws.close.assert_awaited_once_with(code=4001, reason="Invalid or expired token")

    @pytest.mark.asyncio
    async def test_project_not_found_closes_with_4004(self) -> None:
        """Closes with 4004 when project access is denied."""
        ws = AsyncMock(spec=WebSocket)
        mock_validate, mock_svc = _mock_auth_and_project(project_exists=False)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ontokit.core.auth.validate_token", mock_validate),
            patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.routes.projects.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.routes.projects.ProjectService", return_value=mock_svc),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        ws.close.assert_awaited_once_with(code=4004, reason="Project not found or access denied")

    @pytest.mark.asyncio
    async def test_connects_and_forwards_matching_message(self) -> None:
        """Forwards messages matching the project_id."""
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        index_msg = {"type": "index_complete", "project_id": PROJECT_ID, "entity_count": 100}
        mock_pool, mock_pubsub = _mock_pubsub([index_msg])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_mgr = AsyncMock()

        with (
            patch("ontokit.core.auth.validate_token", mock_validate),
            patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.routes.projects.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.routes.projects.ProjectService", return_value=mock_svc),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", mock_mgr),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        mock_mgr.connect.assert_awaited_once()
        ws.send_json.assert_awaited_once_with(index_msg)
        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_messages_for_other_projects(self) -> None:
        """Does not forward messages for other project IDs."""
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        other_msg = {"type": "index_complete", "project_id": "other-project"}
        mock_pool, _ = _mock_pubsub([other_msg])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ontokit.core.auth.validate_token", mock_validate),
            patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.routes.projects.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.routes.projects.ProjectService", return_value=mock_svc),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", AsyncMock()),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self) -> None:
        """Skips malformed JSON without crashing, delivers valid ones."""
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=[TimeoutError, WebSocketDisconnect(code=1000)])

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        valid_msg = {"type": "index_started", "project_id": PROJECT_ID}
        mock_pool, _ = _mock_pubsub(["not valid json{{{", valid_msg])

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("ontokit.core.auth.validate_token", mock_validate),
            patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.routes.projects.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.routes.projects.ProjectService", return_value=mock_svc),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", AsyncMock()),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        ws.send_json.assert_awaited_once_with(valid_msg)

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(self) -> None:
        """Disconnects and unsubscribes even on unexpected errors."""
        ws = AsyncMock(spec=WebSocket)

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=RuntimeError("Redis down"))
        mock_pool = AsyncMock()
        mock_pool.pubsub = Mock(return_value=mock_pubsub)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_mgr = AsyncMock()

        with (
            patch("ontokit.core.auth.validate_token", mock_validate),
            patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.routes.projects.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.routes.projects.ProjectService", return_value=mock_svc),
            patch("ontokit.api.routes.projects.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.projects.index_ws_manager", mock_mgr),
        ):
            await ontology_index_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()
