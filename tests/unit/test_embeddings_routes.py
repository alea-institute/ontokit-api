"""Tests for embeddings routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

PROJECT_ID = "12345678-1234-5678-1234-567812345678"


def test_semantic_search_rejects_anonymous_at_route(client: TestClient) -> None:
    with patch("ontokit.api.routes.semantic_search.EmbeddingService") as service:
        response = client.get(
            f"/api/v1/projects/{PROJECT_ID}/search/semantic", params={"q": "contract"}
        )
    assert response.status_code == 401
    service.return_value.semantic_search.assert_not_called()


def _make_project_response(user_role: str = "owner") -> MagicMock:
    resp = MagicMock()
    resp.user_role = user_role
    resp.source_file_path = "ontology.ttl"
    return resp


class TestGetEmbeddingConfig:
    """Tests for GET /api/v1/projects/{id}/embeddings/config."""

    @patch("ontokit.api.routes.embeddings.EmbeddingService")
    @patch("ontokit.api.routes.embeddings.get_project_service")
    def test_get_config_returns_default(
        self,
        mock_get_ps: MagicMock,
        mock_embed_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:  # noqa: ARG002
        """Returns default config when none is set."""
        client, _ = authed_client

        mock_ps = MagicMock()
        mock_ps.get = AsyncMock(return_value=_make_project_response())
        mock_get_ps.return_value = mock_ps

        mock_embed = MagicMock()
        mock_embed.get_config = AsyncMock(return_value=None)
        mock_embed_cls.return_value = mock_embed

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/config")
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "local"
        assert data["model_name"] == "all-MiniLM-L6-v2"
        assert data["api_key_set"] is False

    @patch("ontokit.api.routes.embeddings.EmbeddingService")
    @patch("ontokit.api.routes.embeddings.get_project_service")
    def test_get_config_returns_custom(
        self,
        mock_get_ps: MagicMock,
        mock_embed_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:  # noqa: ARG002
        """Returns custom config when set."""
        client, _ = authed_client

        mock_ps = MagicMock()
        mock_ps.get = AsyncMock(return_value=_make_project_response())
        mock_get_ps.return_value = mock_ps

        from ontokit.schemas.embeddings import EmbeddingConfig

        custom_config = EmbeddingConfig(
            provider="openai",
            model_name="text-embedding-3-small",
            api_key_set=True,
            dimensions=1536,
            auto_embed_on_save=True,
        )
        mock_embed = MagicMock()
        mock_embed.get_config = AsyncMock(return_value=custom_config)
        mock_embed_cls.return_value = mock_embed

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/config")
        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "openai"
        assert data["api_key_set"] is True


class TestUpdateEmbeddingConfig:
    """Tests for PUT /api/v1/projects/{id}/embeddings/config."""

    @patch("ontokit.api.routes.embeddings.EmbeddingService")
    @patch("ontokit.api.routes.embeddings._verify_write_access", new_callable=AsyncMock)
    def test_update_config_success(
        self,
        mock_verify: AsyncMock,  # noqa: ARG002
        mock_embed_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Successfully updates embedding config."""
        client, _ = authed_client

        from ontokit.schemas.embeddings import EmbeddingConfig

        updated = EmbeddingConfig(
            provider="voyage",
            model_name="voyage-3",
            api_key_set=True,
            dimensions=1024,
            auto_embed_on_save=False,
        )
        mock_embed = MagicMock()
        mock_embed.update_config = AsyncMock(return_value=updated)
        mock_embed_cls.return_value = mock_embed

        response = client.put(
            f"/api/v1/projects/{PROJECT_ID}/embeddings/config",
            json={"provider": "voyage", "model_name": "voyage-3"},
        )
        assert response.status_code == 200
        assert response.json()["provider"] == "voyage"

    @pytest.mark.asyncio
    async def test_invalid_paid_provider_config_returns_typed_422(self) -> None:
        from ontokit.api.routes.embeddings import update_embedding_config
        from ontokit.schemas.embeddings import EmbeddingConfigUpdate

        embed_service = MagicMock()
        embed_service.update_config = AsyncMock(
            side_effect=ValueError("OpenAI API key is required")
        )

        with (
            patch(
                "ontokit.api.routes.embeddings._verify_write_access",
                new_callable=AsyncMock,
            ),
            pytest.raises(HTTPException) as raised,
        ):
            await update_embedding_config(
                project_id=uuid4(),
                data=EmbeddingConfigUpdate(
                    provider="openai",
                    model_name="text-embedding-3-small",
                ),
                db=AsyncMock(),
                embed_service=embed_service,
                user=MagicMock(),
            )

        assert raised.value.status_code == 422
        assert raised.value.detail == "OpenAI API key is required"


class TestGenerateEmbeddings:
    """Tests for POST /api/v1/projects/{id}/embeddings/generate."""

    @patch("ontokit.api.routes.embeddings.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.embeddings.get_git_service")
    @patch("ontokit.api.routes.embeddings._verify_write_access", new_callable=AsyncMock)
    def test_generate_success(
        self,
        mock_verify: AsyncMock,  # noqa: ARG002
        mock_git_fn: MagicMock,
        mock_pool_fn: AsyncMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Triggers embedding generation and returns 202 with job_id."""
        client, mock_session = authed_client

        mock_git = MagicMock()
        mock_git.get_default_branch.return_value = "main"
        mock_git_fn.return_value = mock_git

        # No active job
        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_active_result

        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = Mock(job_id="embed-job-1")
        mock_pool_fn.return_value = mock_pool

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/embeddings/generate")
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["job_id"] is not None
        mock_pool.enqueue_job.assert_awaited_once()

    @patch("ontokit.api.routes.embeddings.get_git_service")
    @patch("ontokit.api.routes.embeddings._verify_write_access", new_callable=AsyncMock)
    def test_generate_conflict_when_active_job(
        self,
        mock_verify: AsyncMock,  # noqa: ARG002
        mock_git_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 409 when an embedding job is already in progress."""
        client, mock_session = authed_client

        mock_git = MagicMock()
        mock_git.get_default_branch.return_value = "main"
        mock_git_fn.return_value = mock_git

        active_job = Mock()
        active_job.id = uuid4()
        mock_active_result = MagicMock()
        mock_active_result.scalar_one_or_none.return_value = active_job
        mock_session.execute.return_value = mock_active_result

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/embeddings/generate")
        assert response.status_code == 409
        assert "already in progress" in response.json()["detail"].lower()


class TestGetEmbeddingStatus:
    """Tests for GET /api/v1/projects/{id}/embeddings/status."""

    @patch("ontokit.api.routes.embeddings.EmbeddingService")
    @patch("ontokit.api.routes.embeddings.get_git_service")
    @patch("ontokit.api.routes.embeddings.get_project_service")
    def test_get_status(
        self,
        mock_get_ps: MagicMock,
        mock_git_fn: MagicMock,
        mock_embed_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:  # noqa: ARG002
        """Returns embedding status with coverage info."""
        client, _ = authed_client

        mock_ps = MagicMock()
        mock_ps.get = AsyncMock(return_value=_make_project_response())
        mock_get_ps.return_value = mock_ps

        mock_git = MagicMock()
        mock_git.get_default_branch.return_value = "main"
        mock_git_fn.return_value = mock_git

        from ontokit.schemas.embeddings import EmbeddingStatus

        status = EmbeddingStatus(
            total_entities=100,
            embedded_entities=80,
            coverage_percent=80.0,
            provider="local",
            model_name="all-MiniLM-L6-v2",
            job_in_progress=False,
        )
        mock_embed = MagicMock()
        mock_embed.get_status = AsyncMock(return_value=status)
        mock_embed_cls.return_value = mock_embed

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/status")
        assert response.status_code == 200
        data = response.json()
        assert data["total_entities"] == 100
        assert data["coverage_percent"] == 80.0


class TestGetEmbeddingJob:
    """Tests for GET /api/v1/projects/{id}/embeddings/jobs/{job_id}."""

    def test_job_status_requires_authentication(self, client: TestClient) -> None:
        job_id = uuid4()
        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/jobs/{job_id}")
        assert response.status_code in (401, 403)

    @patch("ontokit.api.routes.embeddings.get_project_service")
    def test_member_can_poll_job_without_raw_worker_error(
        self,
        mock_get_ps: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        from datetime import UTC, datetime

        client, session = authed_client
        job_id = uuid4()
        mock_get_ps.return_value.get = AsyncMock(
            return_value=_make_project_response(user_role="viewer")
        )
        job = MagicMock(
            id=job_id,
            project_id=PROJECT_ID,
            branch="main",
            status="failed",
            total_entities=12,
            embedded_entities=4,
            error_message="provider rejected secret sk-live-sensitive",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
            completed_at=datetime(2026, 8, 22, 0, 1, tzinfo=UTC),
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/jobs/{job_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == str(job_id)
        assert body["status"] == "failed"
        assert body["progress_percent"] == 33.3
        assert body["error_message"] == "Embedding generation failed"
        assert "sk-live-sensitive" not in response.text

    @patch("ontokit.api.routes.embeddings.get_project_service")
    def test_job_id_is_scoped_to_project(
        self,
        mock_get_ps: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        client, session = authed_client
        mock_get_ps.return_value.get = AsyncMock(
            return_value=_make_project_response(user_role="editor")
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/embeddings/jobs/{uuid4()}")

        assert response.status_code == 404


class TestClearEmbeddings:
    """Tests for DELETE /api/v1/projects/{id}/embeddings."""

    @patch("ontokit.api.routes.embeddings.EmbeddingService")
    @patch("ontokit.api.routes.embeddings._verify_write_access", new_callable=AsyncMock)
    def test_clear_embeddings_success(
        self,
        mock_verify: AsyncMock,  # noqa: ARG002
        mock_embed_cls: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Successfully clears all embeddings for a project."""
        client, _ = authed_client

        mock_embed = MagicMock()
        mock_embed.clear_embeddings = AsyncMock()
        mock_embed_cls.return_value = mock_embed

        response = client.delete(f"/api/v1/projects/{PROJECT_ID}/embeddings")
        assert response.status_code == 204
