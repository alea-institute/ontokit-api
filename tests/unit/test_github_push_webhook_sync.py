"""Real-repository tests for the GitHub push-webhook sync (R5, R6).

The webhook used to call ``BareGitRepositoryService.pull_branch``, which never
existed, so every push raised ``AttributeError`` and nothing synced. The
webhook now only decides and enqueues; ``sync_github_project_task`` in the arq
worker runs the sync with the same per-integration logic as the periodic cron.
These tests drive that task against a real pair of bare repositories: a local
canonical repository cloned from a file-based "GitHub" remote that then moves
ahead.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pygit2
import pytest
from sqlalchemy.exc import SQLAlchemyError

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.services.demo_target_authorizer import DemoTargetDenied
from ontokit.worker import sync_github_project_task

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
BRANCH = "main"
REF = f"refs/heads/{BRANCH}"


def _commit(repo: pygit2.Repository, content: str, message: str) -> pygit2.Oid:
    """Create a commit on ``main`` in a bare repository."""
    blob = repo.create_blob(content.encode("utf-8"))
    builder = repo.TreeBuilder()
    builder.insert("ontology.ttl", blob, pygit2.GIT_FILEMODE_BLOB)
    tree = builder.write()
    sig = pygit2.Signature("Test", "test@example.com")
    parents = [repo.references[REF].target] if REF in repo.references else []
    return repo.create_commit(REF, sig, sig, message, tree, parents)


@pytest.fixture
def repos(tmp_path: Path) -> tuple[BareGitRepositoryService, pygit2.Repository, pygit2.Repository]:
    """A remote bare repo with one commit and a local bare clone of it."""
    remote_path = tmp_path / "remote.git"
    remote = pygit2.init_repository(str(remote_path), bare=True)
    remote.set_head(REF)
    _commit(remote, "@prefix : <urn:x#> .\n", "initial")

    base = tmp_path / "repos"
    base.mkdir()
    local = pygit2.clone_repository(str(remote_path), str(base / f"{PROJECT_ID}.git"), bare=True)
    git_service = BareGitRepositoryService(base_path=str(base))
    return git_service, remote, local


def _integration() -> MagicMock:
    integration = MagicMock()
    integration.project_id = PROJECT_ID
    integration.sync_enabled = True
    integration.default_branch = BRANCH
    integration.sync_status = "idle"
    integration.sync_error = None
    integration.last_sync_at = None
    integration.repo_owner = "example-org"
    integration.repo_name = "example-ontology"
    return integration


def _db(integration: MagicMock) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = integration
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.add = Mock()
    return db


async def _run_task(
    integration: MagicMock | None,
    git_service: BareGitRepositoryService,
    *,
    outbound_only: bool = False,
    credential: Any = "tok",
) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
    """Run the worker task; return its result, the db mock and the resolver mock."""
    db = _db(integration)  # type: ignore[arg-type]
    db.rollback = AsyncMock()
    resolver = (
        AsyncMock(side_effect=credential)
        if isinstance(credential, BaseException)
        else AsyncMock(return_value=credential)
    )
    with (
        patch("ontokit.worker.BareGitRepositoryService", return_value=git_service),
        patch("ontokit.services.github_sync.settings") as mock_settings,
        patch("ontokit.services.mirror_credential.resolve_mirror_credential", new=resolver),
    ):
        mock_settings.github_mirror_outbound_only = outbound_only
        result = await sync_github_project_task({"db": db}, str(PROJECT_ID))
    return result, db, resolver


class TestSyncGithubProjectTaskRealRepos:
    @pytest.mark.asyncio
    async def test_remote_ahead_fast_forwards_and_records_sync(self, repos) -> None:
        """AE2: remote two commits ahead -> local lands on remote head, last_sync_at set."""
        git_service, remote, local = repos
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")
        remote_head = _commit(remote, "@prefix : <urn:x#> .\n:a a :C .\n", "third")
        assert local.references[REF].target != remote_head

        integration = _integration()
        result, _db_mock, _resolver = await _run_task(integration, git_service)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == remote_head
        assert result == {"project_id": str(PROJECT_ID), "status": "synced"}
        assert integration.last_sync_at is not None
        assert integration.sync_status == "idle"
        assert integration.sync_error is None

    @pytest.mark.asyncio
    async def test_conflict_status_integration_is_skipped(self, repos) -> None:
        git_service, remote, local = repos
        before = local.references[REF].target
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")

        integration = _integration()
        integration.sync_status = "conflict"
        result, db, resolver = await _run_task(integration, git_service)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == before
        assert result["status"] == "skipped"
        assert integration.sync_status == "conflict"
        assert integration.last_sync_at is None
        resolver.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_disabled_integration_is_skipped(self, repos) -> None:
        git_service, _remote, _local = repos
        integration = _integration()
        integration.sync_enabled = False
        result, db, resolver = await _run_task(integration, git_service)

        assert result["status"] == "skipped"
        resolver.assert_not_awaited()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_integration_is_skipped(self, repos) -> None:
        git_service, _remote, _local = repos
        result, _db_mock, resolver = await _run_task(None, git_service)

        assert result == {"project_id": str(PROJECT_ID), "status": "skipped"}
        resolver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_credential_resolution_failure_logged_and_not_raised(
        self, repos, caplog: pytest.LogCaptureFixture
    ) -> None:
        git_service, _remote, local = repos
        before = local.references[REF].target
        integration = _integration()

        with caplog.at_level("ERROR", logger="ontokit.worker"):
            result, db, _resolver = await _run_task(
                integration, git_service, credential=SQLAlchemyError("secret-dsn-marker")
            )

        assert result["status"] == "error"
        assert pygit2.Repository(local.path).references[REF].target == before
        db.rollback.assert_awaited_once()
        assert str(PROJECT_ID) in caplog.text
        assert f"branch {BRANCH}" in caplog.text
        assert "secret-dsn-marker" not in caplog.text

    @pytest.mark.asyncio
    async def test_target_refusal_is_persisted(self, repos) -> None:
        git_service, _remote, _local = repos
        integration = _integration()
        denial = DemoTargetDenied("mirror credential resolution refused: wrong demo target")

        result, db, _resolver = await _run_task(integration, git_service, credential=denial)

        assert result["status"] == "error"
        assert integration.sync_status == "error"
        assert integration.sync_error == str(denial)
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_credential_skips_without_touching_branch(self, repos) -> None:
        git_service, remote, local = repos
        before = local.references[REF].target
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")

        result, db, _resolver = await _run_task(_integration(), git_service, credential=None)

        assert result["status"] == "skipped"
        assert pygit2.Repository(local.path).references[REF].target == before
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_error_logged_and_branch_unchanged(
        self, repos, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R6: an unreachable remote logs with project context; the branch is not moved."""
        git_service, _remote, local = repos
        before = local.references[REF].target
        local.remotes.set_url("origin", str(tmp_path / "does-not-exist.git"))

        integration = _integration()
        with caplog.at_level("WARNING", logger="ontokit.worker"):
            result, _db_mock, _resolver = await _run_task(integration, git_service)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == before
        assert result["status"] == "synced"  # the sync ran; its outcome is on the integration
        assert integration.last_sync_at is None
        assert integration.sync_status == "error"
        assert str(PROJECT_ID) in caplog.text
        assert f"branch {BRANCH}" in caplog.text
        assert "did not complete" in caplog.text

    @pytest.mark.asyncio
    async def test_outbound_only_diverged_remote_logged_and_branch_unchanged(
        self, repos, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A remote that moved ahead is reported as diverged, never pulled in."""
        git_service, remote, local = repos
        before = local.references[REF].target
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")

        integration = _integration()
        with caplog.at_level("WARNING", logger="ontokit.worker"):
            await _run_task(integration, git_service, outbound_only=True)

        assert pygit2.Repository(local.path).references[REF].target == before
        assert integration.sync_status == "diverged"
        assert str(PROJECT_ID) in caplog.text
        assert f"branch {BRANCH}" in caplog.text
        assert "diverged" in caplog.text

    @pytest.mark.asyncio
    async def test_unexpected_sync_exception_logged_rolled_back_not_raised(
        self, repos, caplog: pytest.LogCaptureFixture
    ) -> None:
        git_service, _remote, _local = repos
        integration = _integration()
        with (
            patch(
                "ontokit.worker.sync_github_project",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            caplog.at_level("ERROR", logger="ontokit.worker"),
        ):
            result, db, _resolver = await _run_task(integration, git_service)

        assert result["status"] == "error"
        db.rollback.assert_awaited_once()
        assert str(PROJECT_ID) in caplog.text
        assert f"branch {BRANCH}" in caplog.text


class TestSyncGithubProjectTaskRegistration:
    def test_task_is_registered_without_result_or_retry(self) -> None:
        from ontokit.services.github_sync import GITHUB_PROJECT_SYNC_TASK
        from ontokit.worker import WorkerSettings

        by_name = {
            getattr(f, "name", getattr(f, "__name__", None)): f for f in WorkerSettings.functions
        }
        task = by_name[GITHUB_PROJECT_SYNC_TASK]
        assert task.coroutine is sync_github_project_task  # type: ignore[union-attr]
        assert task.keep_result_s == 0  # type: ignore[union-attr]
        assert task.max_tries == 1  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_job_id_collapses_a_burst_of_pushes_in_real_redis(self) -> None:
        """Two enqueues with the per-project job id yield one queued job."""
        import os

        from arq import create_pool
        from arq.connections import RedisSettings

        from ontokit.services.github_sync import (
            GITHUB_PROJECT_SYNC_TASK,
            github_project_sync_job_id,
        )

        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            pytest.skip("REDIS_URL not set")
        queue = f"test-github-sync-{uuid.uuid4().hex}"
        project_id = uuid.uuid4()
        job_id = github_project_sync_job_id(project_id)
        try:
            pool = await create_pool(RedisSettings.from_dsn(redis_url), default_queue_name=queue)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Redis unavailable: {exc}")
        try:
            first = await pool.enqueue_job(
                GITHUB_PROJECT_SYNC_TASK, str(project_id), _job_id=job_id
            )
            second = await pool.enqueue_job(
                GITHUB_PROJECT_SYNC_TASK, str(project_id), _job_id=job_id
            )
            assert first is not None
            assert second is None
            assert await pool.zcard(queue) == 1
        finally:
            await pool.delete(queue, f"arq:job:{job_id}")
            await pool.aclose()
