"""PostgreSQL proofs for pull-request lifecycle serialization."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ontokit.core.auth import CurrentUser
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration, PRStatus, PullRequest
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.schemas.pull_request import PRCreate
from ontokit.schemas.suggestion import SuggestionSubmitRequest
from ontokit.services.branch_lock import pull_request_write_locks
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.suggestion_service import SuggestionService


async def _delete_project(db: AsyncSession, project_id: UUID) -> None:
    await db.rollback()
    await db.execute(delete(Project).where(Project.id == project_id))
    await db.commit()


@pytest.mark.asyncio
async def test_project_lock_serializes_pr_numbers_for_disjoint_branches(
    real_db_session: AsyncSession,
) -> None:
    """Disjoint branch pairs still receive distinct project-scoped numbers."""
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="PR allocation", owner_id="owner"))
    await real_db_session.commit()
    session_factory = async_sessionmaker(real_db_session.bind, expire_on_commit=False)

    async def allocate(source: str, target: str) -> int:
        async with (
            session_factory() as db,
            pull_request_write_locks(db, project_id, {source, target}),
        ):
            max_number = await db.scalar(
                select(func.max(PullRequest.pr_number)).where(PullRequest.project_id == project_id)
            )
            await asyncio.sleep(0.05)
            number = (max_number or 0) + 1
            db.add(
                PullRequest(
                    project_id=project_id,
                    pr_number=number,
                    title=source,
                    source_branch=source,
                    target_branch=target,
                    author_id="owner",
                )
            )
            await db.commit()
            return number

    try:
        numbers = await asyncio.gather(
            allocate("source-a", "target-a"),
            allocate("source-b", "target-b"),
        )
        assert sorted(numbers) == [1, 2]
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_retroactive_sync_shares_project_pr_number_lock(
    real_db_session: AsyncSession,
) -> None:
    """History imports cannot race an interactive project PR allocation."""
    project_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Retro allocation", owner_id="owner"))
    await real_db_session.commit()
    session_factory = async_sessionmaker(real_db_session.bind, expire_on_commit=False)
    allocation_started = asyncio.Event()

    async def allocate_interactive() -> None:
        async with (
            session_factory() as db,
            pull_request_write_locks(db, project_id, {"live", "main"}),
        ):
            max_number = await db.scalar(
                select(func.max(PullRequest.pr_number)).where(
                    PullRequest.project_id == project_id
                )
            )
            allocation_started.set()
            await asyncio.sleep(0.1)
            db.add(
                PullRequest(
                    project_id=project_id,
                    pr_number=(max_number or 0) + 1,
                    title="live",
                    source_branch="live",
                    target_branch="main",
                    author_id="owner",
                )
            )
            await db.commit()

    async def import_history() -> None:
        await allocation_started.wait()
        commit = MagicMock(
            is_merge=True,
            merged_branch="retro",
            parent_hashes=["base", "head"],
            timestamp="2026-08-23T12:00:00+00:00",
            hash="abc123",
            short_hash="abc123",
            author_name="Owner",
            author_email="owner@example.test",
            message="merge retro",
        )
        git = MagicMock()
        git.get_history.return_value = [commit]
        async with session_factory() as db:
            await PullRequestService(db, git_service=git)._sync_merge_commits_to_prs(
                project_id
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(allocate_interactive(), import_history()),
            timeout=2,
        )
        rows = (
            await real_db_session.execute(
                select(PullRequest)
                .where(PullRequest.project_id == project_id)
                .order_by(PullRequest.pr_number)
            )
        ).scalars().all()
        assert [(row.pr_number, row.source_branch) for row in rows] == [
            (1, "live"),
            (2, "retro"),
        ]
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_configured_github_sync_uses_precommit_intent_snapshot(
    real_db_session: AsyncSession,
) -> None:
    """Pending-receipt commits cannot expire data needed by remote sync."""
    project_id = uuid4()
    project = Project(id=project_id, name="Sync snapshot", owner_id="owner")
    integration = GitHubIntegration(
        project_id=project_id,
        repo_owner="org",
        repo_name="repo",
        sync_enabled=True,
    )
    pull_request = PullRequest(
        project_id=project_id,
        pr_number=1,
        title="Snapshot me",
        description="Durable body",
        source_branch="feature",
        target_branch="main",
        author_id="owner",
    )
    real_db_session.add_all([project, integration, pull_request])
    await real_db_session.commit()

    service = PullRequestService(real_db_session)
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(integration, "token")
    )
    service.github_reconciler.reconcile = AsyncMock(
        return_value=MagicMock(
            number=42,
            html_url="https://github.example/org/repo/pull/42",
        )
    )

    try:
        await service._sync_pull_request_to_github(project_id, pull_request)

        intent = service.github_reconciler.reconcile.await_args.kwargs["intent"]
        assert intent.title == "Snapshot me"
        assert intent.body == "Durable body"
        assert intent.head == "feature"
        assert intent.base == "main"
        assert pull_request.github_sync_status == "synced"
        assert pull_request.github_pr_number == 42
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_suggestion_submit_acquires_pr_lock_set_once(
    real_db_session: AsyncSession,
) -> None:
    """The real suggestion and PR services compose without lock re-entry."""
    project_id = uuid4()
    user = CurrentUser(id="editor", name="Editor", email="editor@example.test")
    project = Project(id=project_id, name="Suggestion lock", owner_id="owner")
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    suggestion = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        user_name=user.name,
        user_email=user.email,
        session_id="lock-once",
        branch="suggest/editor/lock-once",
        beacon_token="lock-token",
        changes_count=1,
    )
    real_db_session.add_all([project, suggestion])
    await real_db_session.commit()

    git = MagicMock()
    git.get_default_branch.return_value = "main"
    git.get_file_from_branch.return_value = b"@prefix ex: <https://example.test/> .\n"
    source_branch = MagicMock()
    source_branch.name = suggestion.branch
    target_branch = MagicMock()
    target_branch.name = "main"
    git.list_branches.return_value = [source_branch, target_branch]
    service = SuggestionService(real_db_session, git)
    service._validate_submission_content = AsyncMock()  # type: ignore[method-assign]
    service._verify_untrusted_human = AsyncMock()  # type: ignore[method-assign]
    service._consume_untrusted_submission = AsyncMock()  # type: ignore[method-assign]

    try:
        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service",
            return_value=PullRequestService(real_db_session, git_service=git),
        ):
            result = await asyncio.wait_for(
                service.submit(
                    project_id,
                    suggestion.session_id,
                    SuggestionSubmitRequest(summary="lock proof"),
                    user,
                ),
                timeout=2,
            )
        assert result.pr_number == 1
        await real_db_session.refresh(suggestion)
        assert suggestion.status == "submitted"
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_expected_pr_conflict_preserves_shared_session_state(
    real_db_session: AsyncSession,
) -> None:
    """A pre-write 409 does not roll back its caller's pending session update."""
    project_id = uuid4()
    user = CurrentUser(id="editor", name="Editor", email="editor@example.test")
    project = Project(id=project_id, name="Shared session", owner_id="owner")
    project.members.append(ProjectMember(user_id=user.id, role="editor"))
    suggestion = SuggestionSession(
        project_id=project_id,
        user_id=user.id,
        session_id="shared-state",
        branch="feature",
        beacon_token="shared-token",
    )
    existing = PullRequest(
        project_id=project_id,
        pr_number=1,
        title="existing",
        source_branch="feature",
        target_branch="main",
        author_id=user.id,
        status=PRStatus.OPEN.value,
    )
    real_db_session.add_all([project, suggestion, existing])
    await real_db_session.commit()

    git = MagicMock()
    source_branch = MagicMock()
    source_branch.name = "feature"
    target_branch = MagicMock()
    target_branch.name = "main"
    git.list_branches.return_value = [source_branch, target_branch]
    service = PullRequestService(real_db_session, git_service=git)

    try:
        suggestion.summary = "pending caller update"
        with pytest.raises(HTTPException) as error:
            await service.create_pull_request(
                project_id,
                PRCreate(title="duplicate", source_branch="feature", target_branch="main"),
                user,
            )
        assert error.value.status_code == 409
        assert suggestion.summary == "pending caller update"

        await real_db_session.commit()
        await real_db_session.refresh(suggestion)
        assert suggestion.summary == "pending caller update"
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_application_reopen_rejects_open_successor_before_github(
    real_db_session: AsyncSession,
) -> None:
    """A closed predecessor cannot reopen over its open branch successor."""
    project_id = uuid4()
    project = Project(id=project_id, name="Reopen", owner_id="owner")
    predecessor = PullRequest(
        project_id=project_id,
        pr_number=1,
        title="predecessor",
        source_branch="feature",
        target_branch="main",
        author_id="owner",
        status=PRStatus.CLOSED.value,
        github_pr_number=101,
    )
    successor = PullRequest(
        project_id=project_id,
        pr_number=2,
        title="successor",
        source_branch="feature",
        target_branch="main",
        author_id="owner",
        status=PRStatus.OPEN.value,
    )
    real_db_session.add_all([project, predecessor, successor])
    await real_db_session.commit()

    github = MagicMock()
    github.reopen_pull_request = AsyncMock()
    service = PullRequestService(real_db_session, github_service=github)
    try:
        with pytest.raises(HTTPException) as error:
            await service.reopen_pull_request(
                project_id,
                predecessor.pr_number,
                CurrentUser(id="owner", name="Owner"),
            )
        assert error.value.status_code == 409
        github.reopen_pull_request.assert_not_awaited()
        await real_db_session.refresh(predecessor)
        assert predecessor.status == PRStatus.CLOSED.value
    finally:
        await _delete_project(real_db_session, project_id)


@pytest.mark.asyncio
async def test_webhook_reopen_conflict_is_visible_and_preserves_local_state(
    real_db_session: AsyncSession,
) -> None:
    """An impossible webhook reopen returns 409 instead of retrying a bad commit."""
    project_id = uuid4()
    project = Project(id=project_id, name="Webhook reopen", owner_id="owner")
    integration = GitHubIntegration(
        project_id=project_id,
        repo_owner="org",
        repo_name="repo",
        sync_enabled=True,
    )
    predecessor = PullRequest(
        project_id=project_id,
        pr_number=1,
        title="predecessor",
        source_branch="feature",
        target_branch="main",
        author_id="owner",
        status=PRStatus.CLOSED.value,
        github_pr_number=101,
    )
    successor = PullRequest(
        project_id=project_id,
        pr_number=2,
        title="successor",
        source_branch="feature",
        target_branch="main",
        author_id="owner",
        status=PRStatus.OPEN.value,
    )
    real_db_session.add_all([project, integration, predecessor, successor])
    await real_db_session.commit()

    service = PullRequestService(real_db_session)
    try:
        with pytest.raises(HTTPException) as error:
            await service.handle_github_pr_webhook(
                project_id, "reopened", {"number": predecessor.github_pr_number}
            )
        assert error.value.status_code == 409
        await real_db_session.refresh(predecessor)
        assert predecessor.status == PRStatus.CLOSED.value
    finally:
        await _delete_project(real_db_session, project_id)
