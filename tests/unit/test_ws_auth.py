"""Tests for the shared WebSocket authentication helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, WebSocket

from ontokit.api.utils.ws_auth import authenticate_ws

PROJECT_UUID = UUID("12345678-1234-5678-1234-567812345678")


def _fake_token_payload() -> Mock:
    payload = Mock()
    payload.sub = "user-1"
    payload.name = "Test User"
    payload.email = "test@example.com"
    payload.preferred_username = "testuser"
    payload.roles = ["owner"]
    return payload


class TestAuthenticateWs:
    """Tests for authenticate_ws helper."""

    @pytest.mark.asyncio
    async def test_no_token_closes_4001(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        result = await authenticate_ws(ws, PROJECT_UUID, token=None)
        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4001, reason="Authentication required")

    @pytest.mark.asyncio
    async def test_invalid_token_http_exception_closes_4001(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        with patch(
            "ontokit.api.utils.ws_auth.validate_token",
            AsyncMock(side_effect=HTTPException(status_code=401)),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="bad")
        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4001, reason="Invalid or expired token")

    @pytest.mark.asyncio
    async def test_unexpected_auth_error_closes_1011(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        with patch(
            "ontokit.api.utils.ws_auth.validate_token",
            AsyncMock(side_effect=RuntimeError("network error")),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")
        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1011, reason="Internal server error")

    @pytest.mark.asyncio
    async def test_project_not_found_closes_4004(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        mock_svc = AsyncMock()
        mock_svc.get.side_effect = HTTPException(status_code=404, detail="Not found")

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(return_value=_fake_token_payload()),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.utils.ws_auth.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4004, reason="Project not found")

    @pytest.mark.asyncio
    async def test_access_denied_closes_4003(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        mock_svc = AsyncMock()
        mock_svc.get.side_effect = HTTPException(status_code=403, detail="Forbidden")

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(return_value=_fake_token_payload()),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.utils.ws_auth.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4003, reason="Access denied")

    @pytest.mark.asyncio
    async def test_unexpected_project_error_closes_1011(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        mock_svc = AsyncMock()
        mock_svc.get.side_effect = RuntimeError("db down")

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(return_value=_fake_token_payload()),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.utils.ws_auth.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=1011, reason="Internal server error")

    @pytest.mark.asyncio
    async def test_success_returns_true(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        mock_svc = AsyncMock()
        mock_svc.get.return_value = Mock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(return_value=_fake_token_payload()),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch("ontokit.api.utils.ws_auth.async_session_maker", Mock(return_value=mock_ctx)),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is True
        ws.accept.assert_awaited_once()
        ws.close.assert_not_awaited()
