"""Tests for GET /api/v1/projects/{id}/ontology/index-status endpoint."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ontokit.api.routes.projects import get_git, get_service
from ontokit.main import app
from ontokit.models.ontology_index import IndexingStatus

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
URL = f"/api/v1/projects/{PROJECT_ID}/ontology/index-status"


def _fake_project(role: str = "viewer") -> MagicMock:
    """Return a mock project with the given user role."""
    project = MagicMock()
    project.id = PROJECT_ID
    project.user_role = role
    return project


def _fake_index_status(
    status: str = IndexingStatus.READY.value,
    entity_count: int = 42,
    error_message: str | None = None,
) -> MagicMock:
    """Return a mock OntologyIndexStatus."""
    idx = MagicMock()
    idx.status = status
    idx.entity_count = entity_count
    idx.indexed_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    idx.commit_hash = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    idx.error_message = error_message
    return idx


@pytest.fixture
def mock_project_service() -> Generator[AsyncMock, None, None]:
    mock_svc = AsyncMock()
    app.dependency_overrides[get_service] = lambda: mock_svc
    try:
        yield mock_svc
    finally:
        app.dependency_overrides.pop(get_service, None)


@pytest.fixture
def mock_git_service() -> Generator[MagicMock, None, None]:
    mock_git = MagicMock()
    mock_git.get_default_branch.return_value = "main"
    app.dependency_overrides[get_git] = lambda: mock_git
    try:
        yield mock_git
    finally:
        app.dependency_overrides.pop(get_git, None)


class TestGetOntologyIndexStatus:
    """Tests for the GET index-status endpoint."""

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    def test_returns_status_when_index_exists(
        self,
        mock_index_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
        mock_project_service: AsyncMock,
        mock_git_service: MagicMock,  # noqa: ARG002
    ) -> None:
        """Returns 200 with index status fields when a record exists."""
        client, _ = authed_client
        mock_project_service.get.return_value = _fake_project()

        mock_index_svc = AsyncMock()
        mock_index_svc.get_index_status.return_value = _fake_index_status()
        mock_index_cls.return_value = mock_index_svc

        response = client.get(URL)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == IndexingStatus.READY.value
        assert data["entity_count"] == 42
        assert data["commit_hash"] == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert data["indexed_at"] == "2026-01-15T12:00:00+00:00"
        assert data["error_message"] is None

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    def test_returns_404_when_no_index_record(
        self,
        mock_index_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
        mock_project_service: AsyncMock,
        mock_git_service: MagicMock,  # noqa: ARG002
    ) -> None:
        """Returns 404 when no index status record exists for the branch."""
        client, _ = authed_client
        mock_project_service.get.return_value = _fake_project()

        mock_index_svc = AsyncMock()
        mock_index_svc.get_index_status.return_value = None
        mock_index_cls.return_value = mock_index_svc

        response = client.get(URL)

        assert response.status_code == 404
        assert "No index status found" in response.json()["detail"]

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    def test_uses_explicit_branch_param(
        self,
        mock_index_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
        mock_project_service: AsyncMock,
        mock_git_service: MagicMock,
    ) -> None:
        """Passes the explicit branch query param to the service."""
        client, _ = authed_client
        mock_project_service.get.return_value = _fake_project()

        mock_index_svc = AsyncMock()
        mock_index_svc.get_index_status.return_value = _fake_index_status()
        mock_index_cls.return_value = mock_index_svc

        response = client.get(URL, params={"branch": "develop"})

        assert response.status_code == 200
        mock_index_svc.get_index_status.assert_awaited_once_with(PROJECT_ID, "develop")
        mock_git_service.get_default_branch.assert_not_called()

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    def test_defaults_to_project_default_branch(
        self,
        mock_index_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
        mock_project_service: AsyncMock,
        mock_git_service: MagicMock,
    ) -> None:
        """Falls back to the project default branch when no branch param is given."""
        client, _ = authed_client
        mock_project_service.get.return_value = _fake_project()

        mock_index_svc = AsyncMock()
        mock_index_svc.get_index_status.return_value = _fake_index_status()
        mock_index_cls.return_value = mock_index_svc

        response = client.get(URL)

        assert response.status_code == 200
        mock_git_service.get_default_branch.assert_called_once_with(PROJECT_ID)
        mock_index_svc.get_index_status.assert_awaited_once_with(PROJECT_ID, "main")

    @patch("ontokit.api.routes.projects.OntologyIndexService")
    def test_returns_failed_status_with_error_message(
        self,
        mock_index_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
        mock_project_service: AsyncMock,
        mock_git_service: MagicMock,  # noqa: ARG002
    ) -> None:
        """Returns error_message when index status is failed."""
        client, _ = authed_client
        mock_project_service.get.return_value = _fake_project()

        mock_index_svc = AsyncMock()
        mock_index_svc.get_index_status.return_value = _fake_index_status(
            status=IndexingStatus.FAILED.value,
            entity_count=0,
            error_message="Parse error in ontology.ttl",
        )
        mock_index_cls.return_value = mock_index_svc

        response = client.get(URL)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_message"] == "Parse error in ontology.ttl"
        assert data["entity_count"] == 0
