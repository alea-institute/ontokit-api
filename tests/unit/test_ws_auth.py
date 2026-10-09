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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        result = await authenticate_ws(ws, PROJECT_UUID, token=None)
        assert result is False
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once_with(code=4001, reason="Authentication required")

    @pytest.mark.asyncio
    async def test_invalid_token_http_exception_closes_4001(self) -> None:
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
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


def _ok_project_ctx() -> Mock:
    """A patched async_session_maker whose ProjectService.get succeeds."""
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestAuthenticateWsAuthModeParity:
    """WebSocket auth must honor settings.auth_mode exactly like core.auth HTTP deps.

    Without this parity a ``disabled``/``optional`` deployment admits anonymous
    callers on its HTTP API but rejects them at the WebSocket handshake.
    """

    @pytest.mark.asyncio
    async def test_disabled_mode_no_token_succeeds_anonymous(self, monkeypatch) -> None:
        """auth_mode=disabled → no token required; proceeds as ANONYMOUS_USER."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "disabled")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        mock_svc = AsyncMock()
        mock_svc.get.return_value = Mock()

        validate = AsyncMock()
        with (
            patch("ontokit.api.utils.ws_auth.validate_token", validate),
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token=None)

        assert result is True
        ws.close.assert_not_awaited()
        # No token was validated — the disabled branch never touches the JWT path.
        validate.assert_not_awaited()
        # The anonymous identity was the one passed to the access check.
        passed_user = mock_svc.get.await_args.args[1]
        assert passed_user.id == "anonymous"

    @pytest.mark.asyncio
    async def test_disabled_mode_still_enforces_project_access(self, monkeypatch) -> None:
        """Anonymous access to a private project is still denied (4003)."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "disabled")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        mock_svc = AsyncMock()
        mock_svc.get.side_effect = HTTPException(status_code=403, detail="Forbidden")

        with (
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token=None)

        assert result is False
        ws.close.assert_awaited_once_with(code=4003, reason="Access denied")

    @pytest.mark.asyncio
    async def test_optional_mode_no_token_downgrades_to_anonymous(self, monkeypatch) -> None:
        """auth_mode=optional → absent token proceeds as anonymous (public project)."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "optional")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        mock_svc = AsyncMock()
        mock_svc.get.return_value = Mock()

        with (
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token=None)

        assert result is True
        ws.close.assert_not_awaited()
        assert mock_svc.get.await_args.args[1].id == "anonymous"

    @pytest.mark.asyncio
    async def test_optional_mode_invalid_token_downgrades_to_anonymous(self, monkeypatch) -> None:
        """auth_mode=optional → invalid token silently downgrades (mirrors OptionalUser)."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "optional")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        mock_svc = AsyncMock()
        mock_svc.get.return_value = Mock()

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(side_effect=HTTPException(status_code=401)),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="bad")

        assert result is True
        ws.close.assert_not_awaited()
        assert mock_svc.get.await_args.args[1].id == "anonymous"

    @pytest.mark.asyncio
    async def test_optional_mode_valid_token_uses_real_identity(self, monkeypatch) -> None:
        """auth_mode=optional → a valid token yields the real authenticated user."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "optional")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        mock_svc = AsyncMock()
        mock_svc.get.return_value = Mock()

        with (
            patch(
                "ontokit.api.utils.ws_auth.validate_token",
                AsyncMock(return_value=_fake_token_payload()),
            ),
            patch("ontokit.api.utils.ws_auth.fetch_userinfo", AsyncMock(return_value=None)),
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=mock_svc),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is True
        ws.close.assert_not_awaited()
        assert mock_svc.get.await_args.args[1].id == "user-1"

    @pytest.mark.asyncio
    async def test_optional_mode_token_unexpected_error_closes_1011(self, monkeypatch) -> None:
        """auth_mode=optional → an infra error during validation still 1011s (not a downgrade)."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "optional")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        with patch(
            "ontokit.api.utils.ws_auth.validate_token",
            AsyncMock(side_effect=RuntimeError("network error")),
        ):
            result = await authenticate_ws(ws, PROJECT_UUID, token="tok")

        assert result is False
        ws.close.assert_awaited_once_with(code=1011, reason="Internal server error")

    @pytest.mark.asyncio
    async def test_required_mode_no_token_still_closes_4001(self, monkeypatch) -> None:
        """auth_mode=required (explicit) → no token still hard-closes 4001."""
        monkeypatch.setattr("ontokit.api.utils.ws_auth.settings.auth_mode", "required")
        ws = AsyncMock(spec=WebSocket)
        ws.scope = {"subprotocols": []}
        ws.state = Mock()
        result = await authenticate_ws(ws, PROJECT_UUID, token=None)
        assert result is False
        ws.close.assert_awaited_once_with(code=4001, reason="Authentication required")


class TestSubprotocolAuth:
    """Use a real ASGI scope to exercise handshake credential extraction."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query_token", [None, "legacy-token"])
    async def test_subprotocol_authenticates_without_echoing_token(self, query_token) -> None:
        import base64

        sent = []
        token = "header.jwt.token"
        encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
        ws = WebSocket(
            {
                "type": "websocket",
                "subprotocols": ["ontokit.bearer.v1", f"ontokit.token.{encoded}"],
            },
            AsyncMock(return_value={"type": "websocket.connect"}),
            AsyncMock(side_effect=sent.append),
        )
        validate = AsyncMock(return_value=_fake_token_payload())
        with (
            patch("ontokit.api.utils.ws_auth.validate_token", validate),
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=AsyncMock()),
        ):
            assert await authenticate_ws(ws, PROJECT_UUID, query_token)
        validate.assert_awaited_once_with(token)
        assert sent == [
            {"type": "websocket.accept", "subprotocol": "ontokit.bearer.v1", "headers": []}
        ]
        assert ws.state.auth_user.id == "user-1"
        assert ws.state.auth_token == token

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "protocols",
        [
            ["ontokit.bearer.v1"],
            ["ontokit.token.dG9r"],
            ["ontokit.bearer.v1", "ontokit.token.!bad"],
            ["ontokit.bearer.v1", "ontokit.token."],
            ["ontokit.bearer.v1", "ontokit.token.a"],
            ["ontokit.bearer.v1", "ontokit.token._w"],
            ["ontokit.bearer.v1", "ontokit.token.dG9r", "ontokit.token.dG9r"],
        ],
    )
    async def test_malformed_subprotocol_is_missing_token(self, protocols) -> None:
        sent = []
        ws = WebSocket(
            {"type": "websocket", "subprotocols": protocols},
            AsyncMock(return_value={"type": "websocket.connect"}),
            AsyncMock(side_effect=sent.append),
        )
        assert not await authenticate_ws(ws, PROJECT_UUID, None)
        assert sent[-1] == {
            "type": "websocket.close",
            "code": 4001,
            "reason": "Authentication required",
        }

    @pytest.mark.asyncio
    async def test_query_token_compatibility(self) -> None:
        sent = []
        ws = WebSocket(
            {"type": "websocket", "subprotocols": []},
            AsyncMock(return_value={"type": "websocket.connect"}),
            AsyncMock(side_effect=sent.append),
        )
        validate = AsyncMock(return_value=_fake_token_payload())
        with (
            patch("ontokit.api.utils.ws_auth.validate_token", validate),
            patch(
                "ontokit.api.utils.ws_auth.async_session_maker",
                Mock(return_value=_ok_project_ctx()),
            ),
            patch("ontokit.api.utils.ws_auth.ProjectService", return_value=AsyncMock()),
        ):
            assert await authenticate_ws(ws, PROJECT_UUID, "legacy-token")
        validate.assert_awaited_once_with("legacy-token")
        assert sent[0]["subprotocol"] is None
