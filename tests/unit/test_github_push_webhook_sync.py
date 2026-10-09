"""Real-repository tests for the GitHub push webhook sync (R5, R6).

The webhook used to call ``BareGitRepositoryService.pull_branch``, which never
existed, so every push raised ``AttributeError`` and nothing synced. These
tests drive ``handle_github_push_webhook`` against a real pair of bare
repositories: a local canonical repository cloned from a file-based "GitHub"
remote that then moves ahead.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pygit2
import pytest

from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.services.pull_request_service import PullRequestService

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


def _service(db: AsyncMock, git_service: BareGitRepositoryService) -> PullRequestService:
    return PullRequestService(
        db=db,
        git_service=git_service,
        github_service=MagicMock(),
        user_service=MagicMock(),
    )


async def _push(service: PullRequestService, *, outbound_only: bool, ref: str = REF) -> None:
    with (
        patch("ontokit.services.pull_request_service.settings") as mock_settings,
        patch(
            "ontokit.services.pull_request_service.resolve_mirror_credential",
            new=AsyncMock(return_value="tok"),
        ),
    ):
        mock_settings.github_mirror_outbound_only = outbound_only
        await service.handle_github_push_webhook(PROJECT_ID, ref, [])


class TestPushWebhookRealRepos:
    @pytest.mark.asyncio
    async def test_remote_ahead_fast_forwards_and_records_sync(self, repos) -> None:
        """AE2: remote two commits ahead -> local lands on remote head, last_sync_at set."""
        git_service, remote, local = repos
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")
        remote_head = _commit(remote, "@prefix : <urn:x#> .\n:a a :C .\n", "third")
        assert local.references[REF].target != remote_head

        integration = _integration()
        await _push(_service(_db(integration), git_service), outbound_only=False)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == remote_head
        assert integration.last_sync_at is not None
        assert integration.sync_status == "idle"
        assert integration.sync_error is None

    @pytest.mark.asyncio
    async def test_outbound_only_leaves_branch_untouched(self, repos) -> None:
        git_service, remote, local = repos
        before = local.references[REF].target
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")

        integration = _integration()
        db = _db(integration)
        await _push(_service(db, git_service), outbound_only=True)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == before
        assert integration.last_sync_at is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_default_branch_ref_ignored(self, repos) -> None:
        git_service, remote, local = repos
        before = local.references[REF].target
        _commit(remote, "@prefix : <urn:x#> .\n:a a :B .\n", "second")

        integration = _integration()
        db = _db(integration)
        await _push(_service(db, git_service), outbound_only=False, ref="refs/heads/feature")

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == before
        assert integration.last_sync_at is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_failure_logged_and_branch_unchanged(
        self, repos, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R6: an unreachable remote logs with project context; the branch is not moved."""
        git_service, _remote, local = repos
        before = local.references[REF].target
        local.remotes.set_url("origin", str(tmp_path / "does-not-exist.git"))

        integration = _integration()
        with caplog.at_level("WARNING", logger="ontokit.services.pull_request_service"):
            await _push(_service(_db(integration), git_service), outbound_only=False)

        local = pygit2.Repository(local.path)
        assert local.references[REF].target == before
        assert integration.last_sync_at is None
        assert integration.sync_status == "error"
        assert str(PROJECT_ID) in caplog.text
        assert f"branch {BRANCH}" in caplog.text
