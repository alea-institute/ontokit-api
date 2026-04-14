"""Tests for quality routes (cross-references, consistency, duplicates)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from rdflib import Graph

PROJECT_ID = "12345678-1234-5678-1234-567812345678"
JOB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class TestGetEntityReferences:
    """Tests for GET /api/v1/projects/{id}/entities/{iri}/references."""

    @patch("ontokit.api.routes.quality.get_cross_references")
    @patch("ontokit.api.routes.quality.load_project_graph", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_references_success(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_load: AsyncMock,
        mock_xrefs: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns cross-references for an entity IRI."""
        client, _ = authed_client

        mock_graph = MagicMock(spec=Graph)
        mock_load.return_value = (mock_graph, "main")
        mock_xrefs.return_value = {
            "target_iri": "http://example.org/Person",
            "total": 0,
            "groups": [],
        }

        iri = "http://example.org/Person"
        response = client.get(f"/api/v1/projects/{PROJECT_ID}/entities/{iri}/references")
        assert response.status_code == 200
        data = response.json()
        assert data["target_iri"] == "http://example.org/Person"
        assert data["total"] == 0

    @patch("ontokit.api.routes.quality.get_cross_references")
    @patch("ontokit.api.routes.quality.load_project_graph", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_references_with_branch(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_load: AsyncMock,
        mock_xrefs: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Branch query param is forwarded to load_project_graph."""
        client, _ = authed_client

        mock_graph = MagicMock(spec=Graph)
        mock_load.return_value = (mock_graph, "dev")
        mock_xrefs.return_value = {
            "target_iri": "http://example.org/Foo",
            "total": 0,
            "groups": [],
        }

        response = client.get(
            f"/api/v1/projects/{PROJECT_ID}/entities/http://example.org/Foo/references",
            params={"branch": "dev"},
        )
        assert response.status_code == 200
        # Verify branch was passed through
        mock_load.assert_called_once()
        call_args = mock_load.call_args
        assert call_args[0][1] == "dev"


class TestTriggerConsistencyCheck:
    """Tests for POST /api/v1/projects/{id}/quality/check."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_trigger_check_success(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Consistency check enqueues a job, sets pending key, and returns job_id."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_job = MagicMock()
        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = mock_job
        mock_pool_fn.return_value = mock_pool

        mock_redis = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/check")
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert len(data["job_id"]) > 0
        mock_pool.enqueue_job.assert_called_once()
        call_args = mock_pool.enqueue_job.call_args[0]
        assert call_args[0] == "run_consistency_check_task"
        # The job_id returned to the client must match what was enqueued
        assert data["job_id"] == call_args[3]
        # Pending status key must be set in Redis
        mock_redis.set.assert_called_once()
        set_args = mock_redis.set.call_args
        assert "quality_job_status" in set_args[0][0]
        assert set_args[0][1] == "pending"

    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_trigger_check_enqueue_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when ARQ job enqueue returns None."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = None
        mock_pool_fn.return_value = mock_pool

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/check")
        assert response.status_code == 500

    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_trigger_check_pool_exception(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when ARQ pool raises an exception."""
        client, _ = authed_client

        mock_resolve.return_value = "main"
        mock_pool_fn.side_effect = RuntimeError("Redis unavailable")

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/check")
        assert response.status_code == 500


class TestGetQualityJobResult:
    """Tests for GET /api/v1/projects/{id}/quality/jobs/{job_id}."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_cached(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns cached result when available in Redis."""
        client, _ = authed_client

        cached_data = json.dumps(
            {
                "project_id": PROJECT_ID,
                "branch": "main",
                "issues": [],
                "checked_at": datetime.now(UTC).isoformat(),
                "duration_ms": 42.5,
            }
        )

        mock_redis = AsyncMock()
        mock_redis.get.return_value = cached_data
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/jobs/{JOB_ID}")
        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == PROJECT_ID
        assert data["issues"] == []

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_pending(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 202 when job result is not ready but status key exists."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        # First call: result key → None; second call: status key → "pending"
        mock_redis.get.side_effect = [None, b"pending"]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/jobs/{JOB_ID}")
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "pending"
        assert data["job_id"] == JOB_ID

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_failed(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when the job failed and status key contains error."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        failed_status = json.dumps({"state": "failed", "error": "Out of memory"})
        mock_redis.get.side_effect = [None, failed_status.encode()]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/jobs/{JOB_ID}")
        assert response.status_code == 500
        assert "Out of memory" in response.json()["detail"]

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_not_found(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 404 when both result and status keys are absent."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        # Both result key and status key return None
        mock_redis.get.side_effect = [None, None]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/jobs/{JOB_ID}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_redis_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when Redis raises an operational exception."""
        client, _ = authed_client

        mock_redis_fn.side_effect = RuntimeError("Redis down")

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/jobs/{JOB_ID}")
        assert response.status_code == 500


class TestGetConsistencyIssues:
    """Tests for GET /api/v1/projects/{id}/quality/issues."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_issues_cached(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns cached consistency issues from Redis."""
        client, _ = authed_client

        mock_resolve.return_value = "main"
        cached_data = json.dumps(
            {
                "project_id": PROJECT_ID,
                "branch": "main",
                "issues": [
                    {
                        "rule_id": "missing_label",
                        "severity": "warning",
                        "entity_iri": "http://ex.org/A",
                        "entity_type": "class",
                        "message": "Missing label",
                    }
                ],
                "checked_at": datetime.now(UTC).isoformat(),
                "duration_ms": 10.0,
            }
        )

        mock_redis = AsyncMock()
        mock_redis.get.return_value = cached_data
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/issues")
        assert response.status_code == 200
        data = response.json()
        assert len(data["issues"]) == 1

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_issues_empty_when_no_cache(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns empty result when no cached data exists."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/issues")
        assert response.status_code == 200
        data = response.json()
        assert data["issues"] == []
        assert data["branch"] == "main"

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_issues_redis_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when Redis raises an operational exception."""
        client, _ = authed_client

        mock_resolve.return_value = "main"
        mock_redis_fn.side_effect = RuntimeError("Redis down")

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/issues")
        assert response.status_code == 500


class TestDetectDuplicates:
    """Tests for POST /api/v1/projects/{id}/quality/duplicates."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_detect_duplicates_success(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns job_id when duplicate detection is enqueued."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_job = MagicMock()
        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = mock_job
        mock_pool_fn.return_value = mock_pool

        mock_redis = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates")
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert len(data["job_id"]) > 0
        mock_pool.enqueue_job.assert_called_once()
        call_args = mock_pool.enqueue_job.call_args[0]
        assert call_args[0] == "run_duplicate_detection_task"
        # The job_id returned to the client must match what was enqueued
        assert data["job_id"] == call_args[4]
        # Pending status key must be set in Redis
        mock_redis.set.assert_called_once()
        set_args = mock_redis.set.call_args
        assert "duplicates_job_status" in set_args[0][0]
        assert set_args[0][1] == "pending"

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_detect_duplicates_custom_threshold(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Custom threshold parameter is forwarded to the enqueued job."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_job = MagicMock()
        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = mock_job
        mock_pool_fn.return_value = mock_pool

        mock_redis_fn.return_value = AsyncMock()

        response = client.post(
            f"/api/v1/projects/{PROJECT_ID}/quality/duplicates",
            params={"threshold": 0.9},
        )
        assert response.status_code == 200
        data = response.json()
        # Verify threshold was passed to enqueue_job
        call_args = mock_pool.enqueue_job.call_args[0]
        assert call_args[3] == 0.9  # threshold is the 4th positional arg
        # The job_id returned to the client must match what was enqueued
        assert data["job_id"] == call_args[4]

    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_detect_duplicates_enqueue_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when ARQ job enqueue returns None."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_pool = AsyncMock()
        mock_pool.enqueue_job.return_value = None
        mock_pool_fn.return_value = mock_pool

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates")
        assert response.status_code == 500

    @patch("ontokit.api.routes.quality.get_arq_pool", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_detect_duplicates_pool_exception(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_pool_fn: AsyncMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when ARQ pool raises an exception."""
        client, _ = authed_client

        mock_resolve.return_value = "main"
        mock_pool_fn.side_effect = RuntimeError("Redis unavailable")

        response = client.post(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates")
        assert response.status_code == 500


class TestGetDuplicateJobResult:
    """Tests for GET /api/v1/projects/{id}/quality/duplicates/jobs/{job_id}."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_cached(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns cached duplicate result when available in Redis."""
        client, _ = authed_client

        cached_data = json.dumps(
            {
                "clusters": [],
                "threshold": 0.85,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        )

        mock_redis = AsyncMock()
        mock_redis.get.return_value = cached_data
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/jobs/{JOB_ID}")
        assert response.status_code == 200
        data = response.json()
        assert data["clusters"] == []
        assert data["threshold"] == 0.85

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_pending(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 202 when job result is not ready but status key exists."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        mock_redis.get.side_effect = [None, b"pending"]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/jobs/{JOB_ID}")
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "pending"
        assert data["job_id"] == JOB_ID

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_failed(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when the job failed and status key contains error."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        failed_status = json.dumps({"state": "failed", "error": "Timeout exceeded"})
        mock_redis.get.side_effect = [None, failed_status.encode()]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/jobs/{JOB_ID}")
        assert response.status_code == 500
        assert "Timeout exceeded" in response.json()["detail"]

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_not_found(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 404 when both result and status keys are absent."""
        client, _ = authed_client

        mock_redis = AsyncMock()
        mock_redis.get.side_effect = [None, None]
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/jobs/{JOB_ID}")
        assert response.status_code == 404

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_job_result_redis_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when Redis raises an operational exception."""
        client, _ = authed_client

        mock_redis_fn.side_effect = RuntimeError("Redis down")

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/jobs/{JOB_ID}")
        assert response.status_code == 500


class TestGetLatestDuplicates:
    """Tests for GET /api/v1/projects/{id}/quality/duplicates/latest."""

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_latest_cached(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns cached duplicate results."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        cached_data = json.dumps(
            {
                "clusters": [
                    {
                        "entities": [
                            {"iri": "http://ex.org/A", "label": "Thing A", "entity_type": "class"},
                            {"iri": "http://ex.org/B", "label": "Thing B", "entity_type": "class"},
                        ],
                        "similarity": 0.92,
                    }
                ],
                "threshold": 0.85,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        )

        mock_redis = AsyncMock()
        mock_redis.get.return_value = cached_data
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/latest")
        assert response.status_code == 200
        data = response.json()
        assert len(data["clusters"]) == 1
        assert data["clusters"][0]["similarity"] == 0.92

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_latest_empty(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns empty result when no cached data exists."""
        client, _ = authed_client

        mock_resolve.return_value = "main"

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis_fn.return_value = mock_redis

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/latest")
        assert response.status_code == 200
        data = response.json()
        assert data["clusters"] == []

    @patch("ontokit.api.routes.quality._get_redis")
    @patch("ontokit.api.routes.quality.resolve_branch", new_callable=AsyncMock)
    @patch("ontokit.api.routes.quality.verify_project_access", new_callable=AsyncMock)
    def test_get_latest_redis_failure(
        self,
        mock_access: AsyncMock,  # noqa: ARG002
        mock_resolve: AsyncMock,
        mock_redis_fn: MagicMock,
        authed_client: tuple[TestClient, AsyncMock],
    ) -> None:
        """Returns 500 when Redis raises an operational exception."""
        client, _ = authed_client

        mock_resolve.return_value = "main"
        mock_redis_fn.side_effect = RuntimeError("Redis down")

        response = client.get(f"/api/v1/projects/{PROJECT_ID}/quality/duplicates/latest")
        assert response.status_code == 500
