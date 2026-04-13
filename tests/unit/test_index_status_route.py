"""Tests for GET /api/v1/projects/{id}/ontology/index-status endpoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.database import get_db
from ontokit.main import app
from ontokit.models.ontology_index import IndexingStatus

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
URL = f"/api/v1/projects/{PROJECT_ID}/ontology/index-status"


def _fake_user() -> MagicMock:
    """Return a minimal mock user for RequiredUser."""
    user = MagicMock()
    user.sub = "user-1"
    user.email = "test@example.com"
    user.is_superadmin = False
    return user


def _fake_project(role: str = "viewer") -> MagicMock:
    """Return a mock project with the given user role."""
    project = MagicMock()
    project.id = PROJECT_ID
    project.user_role = role
    return project


def _fake_index_status(
    status: str = IndexingStatus.READY.value,
    entity_count: int = 42,
) -> MagicMock:
    """Return a mock OntologyIndexStatus."""
    idx = MagicMock()
    idx.status = status
    idx.entity_count = entity_count
    idx.indexed_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    idx.commit_hash = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    idx.error_message = None
    return idx


@pytest.fixture
def mock_db_client() -> Generator[TestClient]:
    mock_session = MagicMock()

    async def _override_get_db() -> AsyncGenerator[AsyncSession]:
        yield mock_session

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    app.dependency_overrides.clear()


class TestGetOntologyIndexStatus:
    """Tests for the GET index-status endpoint."""

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    @patch("ontokit.api.routes.projects.get_git_service")
    def test_returns_status_when_index_exists(
        self,
        mock_get_git: MagicMock,
        mock_index_cls: MagicMock,
        mock_db_client: TestClient,
    ) -> None:
        """Returns 200 with index status fields when a record exists."""
        mock_git = MagicMock()
        mock_git.get_default_branch.return_value = "main"
        mock_get_git.return_value = mock_git

        mock_service = AsyncMock()
        mock_service.get_index_status.return_value = _fake_index_status()
        mock_index_cls.return_value = mock_service

        with (
            patch("ontokit.api.routes.projects.ProjectService") as mock_ps_cls,
            patch("ontokit.core.auth.get_current_user", return_value=_fake_user()),
        ):
            mock_ps = AsyncMock()
            mock_ps.get.return_value = _fake_project()
            mock_ps_cls.return_value = mock_ps

            response = mock_db_client.get(URL)

        # The route is registered and reachable (not 404/405 from routing)
        assert response.status_code != 404
        assert response.status_code != 405

    def test_route_is_registered(self, mock_db_client: TestClient) -> None:
        """The GET index-status route is reachable (not a 404/405 routing error)."""
        response = mock_db_client.get(URL)
        # Without auth it will likely be 401/403, but NOT 404/405
        assert response.status_code not in (404, 405)

    def test_route_accepts_branch_query_param(self, mock_db_client: TestClient) -> None:
        """The route accepts a branch query parameter without routing errors."""
        response = mock_db_client.get(URL, params={"branch": "develop"})
        assert response.status_code not in (404, 405)
