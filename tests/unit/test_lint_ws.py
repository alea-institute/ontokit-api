"""Tests for lint_websocket endpoint authentication and message forwarding."""

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from ontokit.api.routes.lint import lint_websocket

PROJECT_ID = "12345678-1234-5678-1234-567812345678"
PROJECT_UUID = UUID(PROJECT_ID)
FAKE_TOKEN = "fake-jwt-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_token_payload() -> Mock:
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
    mock_validate = AsyncMock(return_value=_fake_token_payload())
    mock_svc = AsyncMock()
    if project_exists:
        mock_svc.get.return_value = Mock()
    else:
        from fastapi import HTTPException

        mock_svc.get.side_effect = HTTPException(status_code=404, detail="Not found")
    return mock_validate, mock_svc


def _mock_pubsub(messages: list[Any]) -> tuple[AsyncMock, AsyncMock]:
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


def _enter_auth_patches(
    stack: contextlib.ExitStack,
    mock_validate: AsyncMock,
    mock_svc: AsyncMock,
) -> None:
    """Enter auth + project access patches into an ExitStack."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    stack.enter_context(patch("ontokit.core.auth.validate_token", mock_validate))
    stack.enter_context(patch("ontokit.core.auth.fetch_userinfo", AsyncMock(return_value=None)))
    stack.enter_context(
        patch("ontokit.api.routes.lint.async_session_maker", Mock(return_value=mock_ctx))
    )
    stack.enter_context(
        patch("ontokit.services.project_service.ProjectService", return_value=mock_svc)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLintWebSocketAuth:
    """Tests for lint_websocket authentication."""

    @pytest.mark.asyncio
    async def test_no_token_closes_with_4001(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        await lint_websocket(ws, PROJECT_UUID, token=None)
        ws.close.assert_awaited_once_with(code=4001, reason="Authentication required")

    @pytest.mark.asyncio
    async def test_invalid_token_closes_with_4001(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        with patch("ontokit.core.auth.validate_token", AsyncMock(side_effect=ValueError("bad"))):
            await lint_websocket(ws, PROJECT_UUID, token="bad-token")
        ws.close.assert_awaited_once_with(code=4001, reason="Invalid or expired token")

    @pytest.mark.asyncio
    async def test_project_not_found_closes_with_4004(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        mock_validate, mock_svc = _mock_auth_and_project(project_exists=False)
        with contextlib.ExitStack() as stack:
            _enter_auth_patches(stack, mock_validate, mock_svc)
            await lint_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)
        ws.close.assert_awaited_once_with(code=4004, reason="Project not found or access denied")


class TestLintWebSocketMessages:
    """Tests for lint_websocket message forwarding."""

    @pytest.mark.asyncio
    async def test_connects_and_forwards_matching_message(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        lint_msg = {"type": "lint_complete", "project_id": PROJECT_ID, "run_id": "r1"}
        mock_pool, mock_pubsub = _mock_pubsub([lint_msg])
        mock_mgr = AsyncMock()

        with contextlib.ExitStack() as stack:
            _enter_auth_patches(stack, mock_validate, mock_svc)
            stack.enter_context(
                patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool)
            )
            stack.enter_context(patch("ontokit.api.routes.lint.manager", mock_mgr))
            await lint_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        mock_mgr.connect.assert_awaited_once()
        ws.send_json.assert_awaited_once_with(lint_msg)
        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_messages_for_other_projects(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        other_msg = {"type": "lint_complete", "project_id": "other-project"}
        mock_pool, _ = _mock_pubsub([other_msg])

        with contextlib.ExitStack() as stack:
            _enter_auth_patches(stack, mock_validate, mock_svc)
            stack.enter_context(
                patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool)
            )
            stack.enter_context(patch("ontokit.api.routes.lint.manager", AsyncMock()))
            await lint_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=[TimeoutError, WebSocketDisconnect(code=1000)])

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        valid_msg = {"type": "lint_started", "project_id": PROJECT_ID}
        mock_pool, _ = _mock_pubsub(["not valid json{{{", valid_msg])

        with contextlib.ExitStack() as stack:
            _enter_auth_patches(stack, mock_validate, mock_svc)
            stack.enter_context(
                patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool)
            )
            stack.enter_context(patch("ontokit.api.routes.lint.manager", AsyncMock()))
            await lint_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        ws.send_json.assert_awaited_once_with(valid_msg)

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(self) -> None:
        ws = AsyncMock(spec=WebSocket)

        mock_validate, mock_svc = _mock_auth_and_project(project_exists=True)
        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=RuntimeError("Redis down"))
        mock_pool = AsyncMock()
        mock_pool.pubsub = Mock(return_value=mock_pubsub)
        mock_mgr = AsyncMock()

        with contextlib.ExitStack() as stack:
            _enter_auth_patches(stack, mock_validate, mock_svc)
            stack.enter_context(
                patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool)
            )
            stack.enter_context(patch("ontokit.api.routes.lint.manager", mock_mgr))
            await lint_websocket(ws, PROJECT_UUID, token=FAKE_TOKEN)

        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()
