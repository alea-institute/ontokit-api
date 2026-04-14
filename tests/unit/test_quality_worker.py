"""Tests for quality-related ARQ worker tasks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from rdflib import Graph

from ontokit.worker import run_consistency_check_task, run_duplicate_detection_task

PROJECT_ID = "12345678-1234-5678-1234-567812345678"
PROJECT_UUID = UUID(PROJECT_ID)
JOB_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _InlineExecutor:
    """A fake executor that runs callables inline (no subprocess)."""

    def __enter__(self) -> _InlineExecutor:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def submit(self, fn: Any, *args: Any) -> Any:
        from concurrent.futures import Future

        f: Future[object] = Future()
        try:
            f.set_result(fn(*args))
        except Exception as e:
            f.set_exception(e)
        return f


def _make_ctx(
    project: MagicMock | None = None,
    project_exists: bool = True,
) -> dict[str, AsyncMock]:
    """Build a mock ARQ ctx with db and redis."""
    db = AsyncMock()
    redis = AsyncMock()

    if project_exists and project is None:
        project = MagicMock()
        project.source_file_path = "ontokit/test.ttl"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = project if project_exists else None
    db.execute = AsyncMock(return_value=mock_result)

    return {"db": db, "redis": redis}


class TestRunConsistencyCheckTask:
    """Tests for run_consistency_check_task."""

    @pytest.mark.asyncio
    @patch("ontokit.services.consistency_service.run_consistency_check")
    @patch("ontokit.worker._parse_rdf")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_success_with_git(
        self,
        mock_git_cls: MagicMock,
        mock_parse: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """Runs consistency check via git and caches result."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = True
        mock_git.get_file_from_branch.return_value = b"<turtle content>"
        mock_git_cls.return_value = mock_git

        mock_parse.return_value = MagicMock(spec=Graph)

        mock_result = MagicMock()
        mock_result.issues = [MagicMock(), MagicMock()]
        mock_result.model_dump_json.return_value = '{"issues": [{}, {}]}'
        mock_check.return_value = mock_result

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()):
            result = await run_consistency_check_task(ctx, PROJECT_ID, "main", JOB_ID)

        assert result["status"] == "completed"
        assert result["issues_found"] == 2
        assert result["job_id"] == JOB_ID

        # Verify Redis caching
        redis = ctx["redis"]
        assert redis.set.await_count == 2  # cache_key + job_key
        assert redis.publish.await_count == 2  # started + complete

    @pytest.mark.asyncio
    @patch("ontokit.worker.get_storage_service")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_success_with_storage_fallback(
        self,
        mock_git_cls: MagicMock,
        mock_storage_fn: MagicMock,
    ) -> None:
        """Falls back to storage when git repo doesn't exist."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = False
        mock_git_cls.return_value = mock_git

        mock_storage = AsyncMock()
        mock_storage.download_file.return_value = b"<turtle content>"
        mock_storage_fn.return_value = mock_storage

        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.model_dump_json.return_value = '{"issues": []}'

        with (
            patch("ontokit.worker._parse_rdf", return_value=MagicMock(spec=Graph)),
            patch(
                "ontokit.services.consistency_service.run_consistency_check",
                return_value=mock_result,
            ),
            patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()),
        ):
            result = await run_consistency_check_task(ctx, PROJECT_ID, "main", JOB_ID)

        assert result["status"] == "completed"
        mock_storage.download_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_project_not_found(self) -> None:
        """Raises ValueError when project doesn't exist."""
        ctx = _make_ctx(project_exists=False)

        with pytest.raises(ValueError, match="not found"):
            await run_consistency_check_task(ctx, PROJECT_ID, "main", JOB_ID)

        # Verify failure notification was published
        ctx["redis"].publish.assert_awaited()

    @pytest.mark.asyncio
    async def test_project_no_ontology(self) -> None:
        """Raises ValueError when project has no ontology file and no integration."""
        project = MagicMock()
        project.source_file_path = None
        project.github_integration = None
        ctx = _make_ctx(project=project)

        with pytest.raises(ValueError, match="no ontology file"):
            await run_consistency_check_task(ctx, PROJECT_ID, "main", JOB_ID)

    @pytest.mark.asyncio
    @patch("ontokit.services.consistency_service.run_consistency_check")
    @patch("ontokit.worker._parse_rdf")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_without_job_id(
        self,
        mock_git_cls: MagicMock,
        mock_parse: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """Caches only by branch key when no job_id is provided."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = True
        mock_git.get_file_from_branch.return_value = b"<turtle content>"
        mock_git_cls.return_value = mock_git

        mock_parse.return_value = MagicMock(spec=Graph)

        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.model_dump_json.return_value = '{"issues": []}'
        mock_check.return_value = mock_result

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()):
            result = await run_consistency_check_task(ctx, PROJECT_ID, "main", None)

        assert result["status"] == "completed"
        # Only cache_key set (no job_key)
        redis = ctx["redis"]
        assert redis.set.await_count == 1


class TestRunDuplicateDetectionTask:
    """Tests for run_duplicate_detection_task."""

    @pytest.mark.asyncio
    @patch("ontokit.services.duplicate_detection_service.find_duplicates")
    @patch("ontokit.worker._parse_rdf")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_success_with_git(
        self,
        mock_git_cls: MagicMock,
        mock_parse: MagicMock,
        mock_find: MagicMock,
    ) -> None:
        """Runs duplicate detection via git and caches result."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = True
        mock_git.get_file_from_branch.return_value = b"<turtle content>"
        mock_git_cls.return_value = mock_git

        mock_parse.return_value = MagicMock(spec=Graph)

        mock_result = MagicMock()
        mock_result.clusters = [MagicMock()]
        mock_result.model_dump_json.return_value = '{"clusters": [{}]}'
        mock_find.return_value = mock_result

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()):
            result = await run_duplicate_detection_task(ctx, PROJECT_ID, "main", 0.85, JOB_ID)

        assert result["status"] == "completed"
        assert result["clusters_found"] == 1
        assert result["job_id"] == JOB_ID

        redis = ctx["redis"]
        assert redis.set.await_count == 2
        assert redis.publish.await_count == 2

    @pytest.mark.asyncio
    @patch("ontokit.worker.get_storage_service")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_success_with_storage_fallback(
        self,
        mock_git_cls: MagicMock,
        mock_storage_fn: MagicMock,
    ) -> None:
        """Falls back to storage when git repo doesn't exist."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = False
        mock_git_cls.return_value = mock_git

        mock_storage = AsyncMock()
        mock_storage.download_file.return_value = b"<turtle content>"
        mock_storage_fn.return_value = mock_storage

        mock_result = MagicMock()
        mock_result.clusters = []
        mock_result.model_dump_json.return_value = '{"clusters": []}'

        with (
            patch("ontokit.worker._parse_rdf", return_value=MagicMock(spec=Graph)),
            patch(
                "ontokit.services.duplicate_detection_service.find_duplicates",
                return_value=mock_result,
            ),
            patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()),
        ):
            result = await run_duplicate_detection_task(ctx, PROJECT_ID, "main", 0.85, JOB_ID)

        assert result["status"] == "completed"
        mock_storage.download_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_project_not_found(self) -> None:
        """Raises ValueError when project doesn't exist."""
        ctx = _make_ctx(project_exists=False)

        with pytest.raises(ValueError, match="not found"):
            await run_duplicate_detection_task(ctx, PROJECT_ID, "main", 0.85, JOB_ID)

        ctx["redis"].publish.assert_awaited()

    @pytest.mark.asyncio
    async def test_project_no_ontology(self) -> None:
        """Raises ValueError when project has no ontology file and no integration."""
        project = MagicMock()
        project.source_file_path = None
        project.github_integration = None
        ctx = _make_ctx(project=project)

        with pytest.raises(ValueError, match="no ontology file"):
            await run_duplicate_detection_task(ctx, PROJECT_ID, "main", 0.85, JOB_ID)

    @pytest.mark.asyncio
    @patch("ontokit.services.duplicate_detection_service.find_duplicates")
    @patch("ontokit.worker._parse_rdf")
    @patch("ontokit.worker.BareGitRepositoryService")
    async def test_custom_threshold(
        self,
        mock_git_cls: MagicMock,
        mock_parse: MagicMock,
        mock_find: MagicMock,
    ) -> None:
        """Custom threshold is forwarded to find_duplicates."""
        ctx = _make_ctx()

        mock_git = MagicMock()
        mock_git.repository_exists.return_value = True
        mock_git.get_file_from_branch.return_value = b"<turtle content>"
        mock_git_cls.return_value = mock_git

        mock_parse.return_value = MagicMock(spec=Graph)

        mock_result = MagicMock()
        mock_result.clusters = []
        mock_result.model_dump_json.return_value = '{"clusters": []}'
        mock_find.return_value = mock_result

        with patch("concurrent.futures.ProcessPoolExecutor", return_value=_InlineExecutor()):
            await run_duplicate_detection_task(ctx, PROJECT_ID, "main", 0.95, JOB_ID)

        mock_find.assert_called_once()
        call_args = mock_find.call_args[0]
        assert call_args[1] == 0.95
