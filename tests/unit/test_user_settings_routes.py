# ruff: noqa: ARG001, ARG002
"""Tests for user settings routes (GitHub token, repos, user search)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi.testclient import TestClient

from ontokit.main import app
from ontokit.services.github_service import GitHubService, get_github_service
from ontokit.services.user_service import UserService, get_user_service


class TestGetGitHubTokenStatus:
    """Tests for GET /api/v1/users/me/github-token."""

    def test_no_token_stored(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        """Returns has_token=false when user has no stored token."""
        client, mock_session = authed_client

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        response = client.get("/api/v1/users/me/github-token")
        assert response.status_code == 200
        data = response.json()
        assert data["has_token"] is False
        assert data["github_username"] is None

    def test_token_exists(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        """Returns has_token=true with github_username when token exists."""
        client, mock_session = authed_client

        mock_row = Mock()
        mock_row.github_username = "octocat"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result

        response = client.get("/api/v1/users/me/github-token")
        assert response.status_code == 200
        data = response.json()
        assert data["has_token"] is True
        assert data["github_username"] == "octocat"


class TestRetiredGitHubTokenWriteEndpoints:
    """The PAT write surface is gone (U11 / R3).

    Per-user PATs are retired: all mirror operations authenticate as the system
    mirror identity, and a lay contributor should never be asked for a GitHub
    credential. These pin the removal so the endpoints cannot quietly return.
    """

    def test_save_token_route_is_gone(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = authed_client
        response = client.post("/api/v1/users/me/github-token", json={"token": "ghp_x"})
        assert response.status_code in (404, 405)

    def test_delete_token_route_is_gone(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        client, _ = authed_client
        response = client.delete("/api/v1/users/me/github-token")
        assert response.status_code in (404, 405)

    def test_openapi_no_longer_advertises_a_write_surface(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, _ = authed_client
        paths = client.get("/openapi.json").json()["paths"]
        methods = set(paths.get("/api/v1/users/me/github-token", {}))
        assert "post" not in methods
        assert "delete" not in methods
        # The read path stays for one release (KTD15).
        assert "get" in methods


class TestListGitHubRepos:
    """Tests for GET /api/v1/users/me/github-repos."""

    @patch("ontokit.api.routes.user_settings.decrypt_token", return_value="ghp_plaintoken")
    def test_list_repos_success(
        self,
        mock_decrypt: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns repo list when token exists."""
        client, mock_session = authed_client

        mock_row = Mock()
        mock_row.encrypted_token = "encrypted-val"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_row
        mock_session.execute.return_value = mock_result

        mock_github = AsyncMock(spec=GitHubService)
        mock_github.list_user_repos.return_value = [
            {
                "full_name": "octocat/hello-world",
                "owner": {"login": "octocat"},
                "name": "hello-world",
                "description": "A test repo",
                "private": False,
                "default_branch": "main",
                "html_url": "https://github.com/octocat/hello-world",
            }
        ]
        app.dependency_overrides[get_github_service] = lambda: mock_github

        try:
            response = client.get("/api/v1/users/me/github-repos")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
            assert data["items"][0]["full_name"] == "octocat/hello-world"
        finally:
            app.dependency_overrides.pop(get_github_service, None)

    def test_list_repos_no_token(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        """Returns 400 when user has no stored token."""
        client, mock_session = authed_client

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        response = client.get("/api/v1/users/me/github-repos")
        assert response.status_code == 400
        assert "No GitHub token found" in response.json()["detail"]


class TestSearchUsers:
    """Tests for GET /api/v1/users/search."""

    def test_search_users_success(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        """Returns matching users."""
        client, _ = authed_client

        mock_user_svc = AsyncMock(spec=UserService)
        mock_user_svc.search_users.return_value = (
            [{"id": "u1", "username": "alice", "display_name": "Alice", "email": "a@b.com"}],
            1,
        )
        app.dependency_overrides[get_user_service] = lambda: mock_user_svc

        try:
            response = client.get("/api/v1/users/search", params={"q": "alice"})
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 1
            assert data["items"][0]["username"] == "alice"
        finally:
            app.dependency_overrides.pop(get_user_service, None)

    def test_search_users_query_too_short(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        """Returns 422 when query is less than 2 characters."""
        client, _ = authed_client

        response = client.get("/api/v1/users/search", params={"q": "a"})
        assert response.status_code == 422

    def test_search_users_missing_query(self, authed_client: tuple[TestClient, AsyncMock]) -> None:
        """Returns 422 when query param is missing."""
        client, _ = authed_client

        response = client.get("/api/v1/users/search")
        assert response.status_code == 422


class TestCommitIdentityEndpoints:
    """Commit-authoring identity (R14, R15).

    The contributor-facing half of U9: read the alias that will appear in
    public git history, and opt in to a verified address instead.
    """

    def test_get_returns_the_alias_by_default(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        body = client.get("/api/v1/users/me/commit-identity").json()
        assert body["noreply_alias"].endswith("@users.noreply.ontokit.local")
        assert body["effective_email"] == body["noreply_alias"]
        assert body["use_verified_email"] is False
        assert "test@example.com" not in body["effective_email"]

    def test_get_returns_the_opted_in_verified_address(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        preference = MagicMock()
        preference.commit_email = "1234+t@users.noreply.github.com"
        preference.commit_email_verified = True
        preference.use_verified_email = True
        result = MagicMock()
        result.scalar_one_or_none.return_value = preference
        session.execute = AsyncMock(return_value=result)

        body = client.get("/api/v1/users/me/commit-identity").json()
        assert body["effective_email"] == "1234+t@users.noreply.github.com"

    def test_unverified_address_is_not_used(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        preference = MagicMock()
        preference.commit_email = "unverified@example.com"
        preference.commit_email_verified = False
        preference.use_verified_email = True
        result = MagicMock()
        result.scalar_one_or_none.return_value = preference
        session.execute = AsyncMock(return_value=result)

        body = client.get("/api/v1/users/me/commit-identity").json()
        assert body["effective_email"] != "unverified@example.com"
        assert body["commit_email_verified"] is False

    def test_patch_updates_the_preference(
        self, authed_client: tuple[TestClient, AsyncMock]
    ) -> None:
        client, session = authed_client
        preference = MagicMock()
        preference.commit_email = None
        preference.commit_email_verified = False
        preference.use_verified_email = False
        result = MagicMock()
        result.scalar_one_or_none.return_value = preference
        session.execute = AsyncMock(return_value=result)

        response = client.patch(
            "/api/v1/users/me/commit-identity",
            json={"commit_email": "1234+t@users.noreply.github.com"},
        )
        assert response.status_code == 200
        assert preference.commit_email == "1234+t@users.noreply.github.com"
        assert preference.commit_email_verified is False
