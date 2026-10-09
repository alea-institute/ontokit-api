"""Tests for lint_websocket endpoint message forwarding."""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_periodic_authorization():
    """Route tests isolate Redis forwarding; authorization is tested in ws_forward."""
    with patch(
        "ontokit.api.utils.ws_forward.project_reauthorizer",
        return_value=AsyncMock(),
    ):
        yield


class TestLintWebSocketAuth:
    """Tests for lint_websocket authentication delegation."""

    @pytest.mark.asyncio
    async def test_auth_failure_returns_early(self) -> None:
        """Returns without connecting when authenticate_ws returns False."""
        ws = AsyncMock(spec=WebSocket)
        mock_mgr = Mock(connect=AsyncMock(), disconnect=Mock())

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=False)),
            patch("ontokit.api.routes.lint.manager", mock_mgr),
        ):
            await lint_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.connect.assert_not_awaited()


class TestLintWebSocketMessages:
    """Tests for lint_websocket message forwarding."""

    @pytest.mark.asyncio
    async def test_connects_and_forwards_matching_message(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        lint_msg = {"type": "lint_complete", "project_id": PROJECT_ID, "run_id": "r1"}
        mock_pool, mock_pubsub = _mock_pubsub([lint_msg])
        mock_mgr = Mock(connect=AsyncMock(), disconnect=Mock())

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.lint.manager", mock_mgr),
        ):
            await lint_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.connect.assert_awaited_once()
        ws.send_json.assert_awaited_once_with(lint_msg)
        mock_mgr.disconnect.assert_called_once()
        mock_pubsub.unsubscribe.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_messages_for_other_projects(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))

        other_msg = {"type": "lint_complete", "project_id": "other-project"}
        mock_pool, _ = _mock_pubsub([other_msg])

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.lint.manager", Mock(connect=AsyncMock(), disconnect=Mock())),
        ):
            await lint_websocket(ws, PROJECT_UUID, token="t")

        ws.send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_malformed_json(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.receive_text = AsyncMock(side_effect=[TimeoutError, WebSocketDisconnect(code=1000)])

        valid_msg = {"type": "lint_started", "project_id": PROJECT_ID}
        mock_pool, _ = _mock_pubsub(["not valid json{{{", valid_msg])

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.lint.manager", Mock(connect=AsyncMock(), disconnect=Mock())),
        ):
            await lint_websocket(ws, PROJECT_UUID, token="t")

        ws.send_json.assert_awaited_once_with(valid_msg)

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(self) -> None:
        ws = AsyncMock(spec=WebSocket)

        mock_pubsub = AsyncMock()
        mock_pubsub.get_message = AsyncMock(side_effect=RuntimeError("Redis down"))
        mock_pool = AsyncMock()
        mock_pool.pubsub = Mock(return_value=mock_pubsub)
        mock_mgr = Mock(connect=AsyncMock(), disconnect=Mock())

        with (
            patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
            patch("ontokit.api.routes.lint.get_arq_pool", return_value=mock_pool),
            patch("ontokit.api.routes.lint.manager", mock_mgr),
        ):
            await lint_websocket(ws, PROJECT_UUID, token="t")

        mock_mgr.disconnect.assert_called_once()
        ws.close.assert_awaited_once_with(code=1011, reason="Internal server error")
        mock_pubsub.unsubscribe.assert_awaited()


async def test_manager_setup_error_closes_and_logs_traceback(caplog):
    ws = AsyncMock(spec=WebSocket)
    mock_mgr = Mock(connect=AsyncMock(side_effect=RuntimeError("setup failed")), disconnect=Mock())
    with (
        patch("ontokit.api.utils.ws_auth.authenticate_ws", AsyncMock(return_value=True)),
        patch("ontokit.api.routes.lint.manager", mock_mgr),
    ):
        await lint_websocket(ws, PROJECT_UUID, token="t")
    ws.close.assert_awaited_once_with(code=1011, reason="Internal server error")
    assert any(record.exc_info is not None for record in caplog.records)
    mock_mgr.disconnect.assert_called_once()
