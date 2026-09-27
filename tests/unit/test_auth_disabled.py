"""Tests for the three auth modes: required, optional, disabled."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from ontokit.core.auth import (
    ANONYMOUS_USER,
    CurrentUser,
    get_current_user,
    get_current_user_optional,
    get_current_user_with_token,
    require_authenticated_identity,
)

# ---------------------------------------------------------------------------
# ANONYMOUS_USER constant
# ---------------------------------------------------------------------------


class TestAnonymousUser:
    """Tests for the ANONYMOUS_USER constant."""

    def test_anonymous_user_id(self) -> None:
        """ANONYMOUS_USER has id='anonymous'."""
        assert ANONYMOUS_USER.id == "anonymous"

    def test_anonymous_user_roles(self) -> None:
        """ANONYMOUS_USER has roles=['viewer']."""
        assert ANONYMOUS_USER.roles == ["viewer"]

    @patch("ontokit.core.auth.settings")
    def test_anonymous_user_is_not_superadmin(self, mock_settings) -> None:  # noqa: ANN001
        """ANONYMOUS_USER is never a superadmin."""
        mock_settings.superadmin_ids = set()
        assert ANONYMOUS_USER.is_superadmin is False

    def test_anonymous_user_is_current_user_instance(self) -> None:
        """ANONYMOUS_USER is an instance of CurrentUser."""
        assert isinstance(ANONYMOUS_USER, CurrentUser)

    def test_anonymous_user_is_explicitly_marked(self) -> None:
        """Sensitive boundaries distinguish disabled-auth access without ID heuristics."""
        assert ANONYMOUS_USER.is_anonymous is True
        assert CurrentUser(id="real-user").is_anonymous is False

    def test_sensitive_boundary_rejects_only_anonymous_identity(self) -> None:
        """Browse-only disabled auth remains while sensitive features fail closed."""
        with pytest.raises(HTTPException) as exc_info:
            require_authenticated_identity(ANONYMOUS_USER)
        assert exc_info.value.status_code == 403

        require_authenticated_identity(CurrentUser(id="real-user"))


# ---------------------------------------------------------------------------
# AUTH_MODE=disabled
# ---------------------------------------------------------------------------


class TestAuthModeDisabled:
    """Tests for AUTH_MODE=disabled — read and suggest only."""

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    async def test_disabled_get_current_user_returns_anonymous(self, mock_settings, method) -> None:  # noqa: ANN001
        """In disabled mode, get_current_user returns ANONYMOUS_USER (no credentials needed)."""
        mock_settings.auth_mode = "disabled"
        result = await get_current_user(
            credentials=None, request=Request({"type": "http", "method": method})
        )
        assert result is ANONYMOUS_USER

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_disabled_get_current_user_optional_returns_anonymous(
        self, mock_settings
    ) -> None:  # noqa: ANN001
        """In disabled mode, get_current_user_optional returns ANONYMOUS_USER (not None)."""
        mock_settings.auth_mode = "disabled"
        result = await get_current_user_optional(credentials=None)
        assert result is ANONYMOUS_USER

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    @pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
    async def test_disabled_get_current_user_with_token_returns_anonymous(
        self, mock_settings, method
    ) -> None:  # noqa: ANN001
        """In disabled mode, get_current_user_with_token returns (ANONYMOUS_USER, 'anonymous')."""
        mock_settings.auth_mode = "disabled"
        user, token = await get_current_user_with_token(
            credentials=None, request=Request({"type": "http", "method": method})
        )
        assert user is ANONYMOUS_USER
        assert token == "anonymous"

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_disabled_ignores_valid_credentials(self, mock_settings) -> None:  # noqa: ANN001
        """In disabled mode, even a present/valid Bearer token is ignored — everyone is
        anonymous viewer, no privilege differentiation (the disabled early-return fires
        before any token validation). Documents the /ce:review LOW finding for PR-2."""
        mock_settings.auth_mode = "disabled"
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.valid.jwt")
        request = Request({"type": "http", "method": "GET"})
        assert await get_current_user(credentials=creds, request=request) is ANONYMOUS_USER
        assert await get_current_user_optional(credentials=creds) is ANONYMOUS_USER
        user, token = await get_current_user_with_token(credentials=creds, request=request)
        assert user is ANONYMOUS_USER
        assert token == "anonymous"


# ---------------------------------------------------------------------------
# AUTH_MODE=required (default)
# ---------------------------------------------------------------------------


class TestAuthModeRequired:
    """Tests for AUTH_MODE=required — existing behavior, 401 without credentials."""

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_required_get_current_user_raises_401_without_credentials(
        self, mock_settings
    ) -> None:  # noqa: ANN001
        """In required mode, get_current_user raises 401 when no credentials provided."""
        mock_settings.auth_mode = "required"
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_required_get_current_user_optional_returns_none_without_credentials(
        self, mock_settings
    ) -> None:  # noqa: ANN001
        """In required mode, get_current_user_optional returns None when no credentials provided."""
        mock_settings.auth_mode = "required"
        result = await get_current_user_optional(credentials=None)
        assert result is None


# ---------------------------------------------------------------------------
# AUTH_MODE=optional
# ---------------------------------------------------------------------------


class TestAuthModeOptional:
    """Tests for AUTH_MODE=optional — GET endpoints work anonymously, writes require auth."""

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_optional_get_current_user_raises_401_without_credentials(
        self, mock_settings
    ) -> None:  # noqa: ANN001
        """In optional mode, get_current_user (RequiredUser) raises 401 without credentials (write protection)."""
        mock_settings.auth_mode = "optional"
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @patch("ontokit.core.auth.settings")
    async def test_optional_get_current_user_optional_returns_none_without_credentials(
        self, mock_settings
    ) -> None:  # noqa: ANN001
        """In optional mode, get_current_user_optional returns None without credentials (browse works)."""
        mock_settings.auth_mode = "optional"
        result = await get_current_user_optional(credentials=None)
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_token", [False, True])
async def test_disabled_post_identity(with_token, monkeypatch) -> None:
    """POST must be refused before a RequiredUser handler receives the identity."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from ontokit.core.auth import RequiredUser, RequiredUserWithToken

    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", "disabled")
    probe = FastAPI()

    if with_token:

        @probe.post("/")
        async def token_identity(user: RequiredUserWithToken):
            assert user[0] is ANONYMOUS_USER
            return {"id": user[0].id}

    else:

        @probe.post("/")
        async def identity(user: RequiredUser):
            assert user is ANONYMOUS_USER
            return {"id": user.id}

    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://test") as client:
        response = await client.post("/")
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Authentication is disabled on this deployment, so it is read-only "
        "apart from anonymous suggestions"
    }


@pytest.mark.parametrize("dependency", [get_current_user, get_current_user_with_token])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "TRACE", "CUSTOM"])
@pytest.mark.parametrize("token", [None, "a.valid.jwt"])
@pytest.mark.asyncio
async def test_disabled_unsafe_methods(dependency, method, token, monkeypatch) -> None:
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", "disabled")
    credentials = (
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
    )
    with pytest.raises(HTTPException) as exc:
        await dependency(
            credentials=credentials, request=Request({"type": "http", "method": method})
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == (
        "Authentication is disabled on this deployment, so it is read-only "
        "apart from anonymous suggestions"
    )


@pytest.mark.parametrize("with_token", [False, True])
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_disabled_safe_methods_receive_injected_request(with_token, method, monkeypatch):
    """A missing Request fails closed, so success proves FastAPI injected it."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from ontokit.core.auth import RequiredUser, RequiredUserWithToken

    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", "disabled")
    probe = FastAPI()
    received = []

    if with_token:

        @probe.api_route("/", methods=[method])
        async def token_identity(user: RequiredUserWithToken):
            received.append(user[0])
            assert user[1] == "anonymous"

    else:

        @probe.api_route("/", methods=[method])
        async def identity(user: RequiredUser):
            received.append(user)

    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://test") as client:
        response = await client.request(method, "/")
    assert response.status_code == 200
    assert len(received) == 1
    assert received[0] is ANONYMOUS_USER


@pytest.mark.parametrize("dependency", [get_current_user, get_current_user_with_token])
@pytest.mark.parametrize("mode", ["required", "optional"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.asyncio
async def test_authenticated_modes_still_require_credentials(dependency, mode, method, monkeypatch):
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", mode)
    with pytest.raises(HTTPException) as exc:
        await dependency(credentials=None, request=Request({"type": "http", "method": method}))
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("dependency", [get_current_user, get_current_user_with_token])
@pytest.mark.asyncio
async def test_disabled_missing_request_fails_closed(dependency, monkeypatch):
    monkeypatch.setattr("ontokit.core.auth.settings.auth_mode", "disabled")
    with pytest.raises(HTTPException) as exc:
        await dependency(credentials=None)
    assert exc.value.status_code == 403
