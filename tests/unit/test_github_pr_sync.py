"""Focused contract tests for pull-request GitHub mirror receipts and retry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import HTTPException

from ontokit.core.auth import CurrentUser
from ontokit.models.pull_request import GitHubSyncStatus, PRStatus
from ontokit.services.github_service import GitHubPR, GitHubService
from ontokit.services.pull_request_github_sync import (
    GitHubPRSyncIntent,
    GitHubPullRequestReconciler,
)
from ontokit.services.pull_request_service import PullRequestService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _user(user_id: str = "author") -> CurrentUser:
    return CurrentUser(
        id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.title(),
        username=user_id,
        roles=[],
    )


def _project(role: str = "editor") -> MagicMock:
    member = MagicMock(user_id="author", role=role)
    return MagicMock(id=PROJECT_ID, members=[member], pr_approval_required=0, is_public=True)


def _pr(*, status: str = PRStatus.OPEN.value, number: int | None = None) -> MagicMock:
    now = datetime.now(UTC)
    return MagicMock(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        pr_number=1,
        title="Mirror me",
        description="Body",
        source_branch="feature/exact",
        target_branch="main",
        status=status,
        author_id="author",
        github_pr_number=number,
        github_pr_url=None,
        github_sync_status=GitHubSyncStatus.FAILED.value,
        github_sync_last_attempted_at=now,
        github_sync_message="GitHub synchronization failed. Retry when the integration is available.",
        github_sync_attempt_id=None,
        github_integration_id=None,
        github_repo_owner=None,
        github_repo_name=None,
        github_sync_generation=0,
        github_sync_merge_title=None,
        reviews=[],
        comments=[],
        created_at=now,
        updated_at=None,
        merged_by=None,
        merged_at=None,
        merge_commit_hash=None,
        base_commit_hash=None,
        head_commit_hash=None,
        author_name="Author",
        author_email="author@example.com",
    )


def _github_pr(
    *,
    number: int = 42,
    head: str = "feature/exact",
    base: str = "main",
    state: str = "open",
) -> GitHubPR:
    now = datetime.now(UTC)
    return GitHubPR(
        number=number,
        title="Mirror me",
        body="Body",
        state=state,
        html_url=f"https://github.example/pr/{number}",
        head_ref=head,
        base_ref=base,
        user_login="author",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_list_pull_requests_encodes_head_and_base_exactly() -> None:
    github = GitHubService()
    github._request = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await github.list_pull_requests(
        token="token",
        owner="org/name",
        repo="repo",
        state="all",
        head="org/name:feature/a&state=closed",
        base="release/1?draft=true",
    )

    endpoint = github._request.await_args.args[1]
    assert "head=org%2Fname%3Afeature%2Fa%26state%3Dclosed" in endpoint
    assert "base=release%2F1%3Fdraft%3Dtrue" in endpoint


@pytest.mark.asyncio
async def test_create_sync_reconciles_exact_open_match_without_creating() -> None:
    db = AsyncMock()
    github = MagicMock()
    github.list_pull_requests = AsyncMock(
        return_value=[
            _github_pr(number=10, head="feature/exact-old"),
            _github_pr(number=11, base="develop"),
            _github_pr(number=12),
        ]
    )
    github.create_pull_request = AsyncMock()
    github.update_pull_request = AsyncMock(return_value=_github_pr(number=12))
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)
    pr = _pr()

    result = await service._mirror_pull_request_to_github(
        pr,
        MagicMock(repo_owner="org", repo_name="repo"),
        "token",
        desired_state="open",
    )

    assert result.number == 12
    github.create_pull_request.assert_not_awaited()
    github.update_pull_request.assert_awaited_once_with(
        token="token",
        owner="org",
        repo="repo",
        pr_number=12,
        title="Mirror me",
        body="Body",
        state="open",
    )


@pytest.mark.asyncio
async def test_create_sync_reuses_and_reopens_exact_closed_match() -> None:
    db = AsyncMock()
    github = MagicMock()
    github.list_pull_requests = AsyncMock(return_value=[_github_pr(number=10, state="closed")])
    github.update_pull_request = AsyncMock(return_value=_github_pr(number=10))
    github.create_pull_request = AsyncMock()
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)

    result = await service._mirror_pull_request_to_github(
        _pr(),
        MagicMock(repo_owner="org", repo_name="repo"),
        "token",
        desired_state="open",
    )

    assert result.number == 10
    github.update_pull_request.assert_awaited_once()
    github.create_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_best_effort_sync_records_safe_failure_without_leaking_exception() -> None:
    db = AsyncMock()
    github = MagicMock()
    github.list_pull_requests = AsyncMock(side_effect=RuntimeError("token secret-123 rejected"))
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(repo_owner="org", repo_name="repo"), "token")
    )
    pr = _pr()
    claimed = MagicMock()
    claimed.scalar_one_or_none.return_value = pr
    db.execute.side_effect = [claimed, MagicMock(rowcount=1)]

    await service._sync_pull_request_to_github(PROJECT_ID, pr, desired_state="open")

    assert pr.github_sync_status == GitHubSyncStatus.FAILED.value
    assert "secret-123" not in pr.github_sync_message
    assert pr.github_sync_message == service.GITHUB_SYNC_FAILED_MESSAGE
    assert db.commit.await_count >= 2  # pending is durable before I/O, then failure


@pytest.mark.asyncio
async def test_best_effort_sync_records_not_configured_without_network() -> None:
    db = AsyncMock()
    github = MagicMock()
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)
    service._get_github_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    pr = _pr()
    claimed = MagicMock()
    claimed.scalar_one_or_none.return_value = pr
    db.execute.return_value = claimed

    await service._sync_pull_request_to_github(PROJECT_ID, pr, desired_state="open")

    assert pr.github_sync_status == GitHubSyncStatus.NOT_CONFIGURED.value
    assert pr.github_sync_message is None
    github.create_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_retry_rejects_non_author_editor_but_allows_admin() -> None:
    db = AsyncMock()
    service = PullRequestService(db, git_service=MagicMock(), github_service=MagicMock())
    pr = _pr()
    service._get_project = AsyncMock(return_value=_project("editor"))  # type: ignore[method-assign]
    service._get_pr = AsyncMock(return_value=pr)  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc:
        await service.retry_github_sync(PROJECT_ID, 1, _user("someone-else"))
    assert exc.value.status_code == 403

    admin_project = _project("admin")
    admin_project.members = [MagicMock(user_id="admin", role="admin")]
    service._get_project = AsyncMock(return_value=admin_project)  # type: ignore[method-assign]
    service._get_github_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._to_pr_response = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    await service.retry_github_sync(PROJECT_ID, 1, _user("admin"))


@pytest.mark.asyncio
async def test_retry_allows_merged_pull_request() -> None:
    service = PullRequestService(AsyncMock(), git_service=MagicMock(), github_service=MagicMock())
    service._get_project = AsyncMock(return_value=_project())  # type: ignore[method-assign]
    service._get_pr = AsyncMock(return_value=_pr(status=PRStatus.MERGED.value))  # type: ignore[method-assign]
    service._get_github_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._to_pr_response = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    await service.retry_github_sync(PROJECT_ID, 1, _user())

    service._to_pr_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_retry_while_pending_does_not_start_second_network_call() -> None:
    db = AsyncMock()
    service = PullRequestService(db, git_service=MagicMock(), github_service=MagicMock())
    pr = _pr()
    pr.github_sync_status = GitHubSyncStatus.PENDING.value
    pr.github_sync_last_attempted_at = datetime.now(UTC)
    service._get_project = AsyncMock(return_value=_project())  # type: ignore[method-assign]
    service._get_pr = AsyncMock(return_value=pr)  # type: ignore[method-assign]
    service._sync_pull_request_to_github = AsyncMock()  # type: ignore[method-assign]
    service._to_pr_response = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    await service.retry_github_sync(PROJECT_ID, 1, _user())

    service._sync_pull_request_to_github.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_pending_retry_is_reclaimed_and_runs_network_sync() -> None:
    db = AsyncMock()
    service = PullRequestService(db, git_service=MagicMock(), github_service=MagicMock())
    pr = _pr()
    pr.github_sync_status = GitHubSyncStatus.PENDING.value
    pr.github_sync_last_attempted_at = datetime.now(UTC) - timedelta(minutes=6)
    integration = MagicMock(repo_owner="org", repo_name="repo")
    service._get_project = AsyncMock(return_value=_project())  # type: ignore[method-assign]
    service._get_pr = AsyncMock(return_value=pr)  # type: ignore[method-assign]
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(integration, "token")
    )
    service._perform_github_sync = AsyncMock()  # type: ignore[method-assign]
    service._to_pr_response = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]

    await service.retry_github_sync(PROJECT_ID, 1, _user())

    service._perform_github_sync.assert_awaited_once()
    assert pr.github_sync_status == GitHubSyncStatus.PENDING.value
    assert pr.github_sync_attempt_id is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_network_completion_cannot_overwrite_newer_attempt() -> None:
    db = AsyncMock()
    update_result = MagicMock(rowcount=0)
    db.execute.return_value = update_result
    service = PullRequestService(db, git_service=MagicMock(), github_service=MagicMock())
    pr = _pr()
    newer_attempt = uuid.uuid4()
    pr.github_sync_status = GitHubSyncStatus.PENDING.value
    pr.github_sync_attempt_id = newer_attempt

    await service._finish_github_sync(
        pr,
        uuid.uuid4(),
        sync_status=GitHubSyncStatus.SYNCED,
        github_pr_number=42,
        github_pr_url="https://github.example/pr/42",
    )

    assert pr.github_sync_status == GitHubSyncStatus.PENDING.value
    assert pr.github_sync_attempt_id == newer_attempt
    assert pr.github_pr_number is None
    assert db.refresh.await_args_list == [call(pr), call(pr, ["reviews", "comments"])]


@pytest.mark.parametrize(
    ("local_state", "title", "remote_state"),
    [
        (PRStatus.OPEN.value, "Updated title", "open"),
        (PRStatus.CLOSED.value, "Mirror me", "closed"),
    ],
)
@pytest.mark.asyncio
async def test_update_and_close_failures_are_durable_and_replay_latest_state(
    local_state: str,
    title: str,
    remote_state: str,
) -> None:
    db = AsyncMock()
    pr = _pr(status=local_state, number=42)
    pr.title = title
    integration = MagicMock(repo_owner="org", repo_name="repo")
    claimed_first = MagicMock()
    claimed_first.scalar_one_or_none.return_value = pr
    claimed_retry = MagicMock()
    claimed_retry.scalar_one_or_none.return_value = pr
    db.execute.side_effect = [
        claimed_first,
        MagicMock(rowcount=1),
        claimed_retry,
        MagicMock(rowcount=1),
    ]
    github = MagicMock()
    github.get_pull_request_or_none = AsyncMock(return_value=_github_pr(number=42))
    github.update_pull_request = AsyncMock(
        side_effect=[RuntimeError("GitHub unavailable"), _github_pr(number=42, state=remote_state)]
    )
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(integration, "token")
    )

    await service._sync_pull_request_to_github(PROJECT_ID, pr)
    assert pr.github_sync_status == GitHubSyncStatus.FAILED.value

    await service._sync_pull_request_to_github(PROJECT_ID, pr)

    assert pr.github_sync_status == GitHubSyncStatus.SYNCED.value
    assert github.update_pull_request.await_count == 2
    last_call = github.update_pull_request.await_args
    assert last_call.kwargs["title"] == title
    assert last_call.kwargs["body"] == "Body"
    assert last_call.kwargs["state"] == remote_state
    assert db.commit.await_count == 4


@pytest.mark.asyncio
async def test_merge_failure_is_durable_and_retry_replays_merge_intent() -> None:
    db = AsyncMock()
    pr = _pr(status=PRStatus.MERGED.value, number=42)
    pr.github_sync_merge_title = "Durable merge title"
    integration = MagicMock(repo_owner="org", repo_name="repo")
    claimed_first = MagicMock()
    claimed_first.scalar_one_or_none.return_value = pr
    claimed_retry = MagicMock()
    claimed_retry.scalar_one_or_none.return_value = pr
    db.execute.side_effect = [
        claimed_first,
        MagicMock(rowcount=1),
        claimed_retry,
        MagicMock(rowcount=1),
    ]
    merged = _github_pr(number=42, state="closed")
    merged.merged = True
    github = MagicMock()
    github.get_pull_request_or_none = AsyncMock(return_value=_github_pr(number=42))
    github.update_pull_request = AsyncMock(return_value=_github_pr(number=42))
    github.merge_pull_request = AsyncMock(
        side_effect=[RuntimeError("merge unavailable"), {"merged": True}]
    )
    github.get_pull_request = AsyncMock(return_value=merged)
    service = PullRequestService(db, git_service=MagicMock(), github_service=github)
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(integration, "token")
    )

    await service._sync_pull_request_to_github(PROJECT_ID, pr)
    assert pr.github_sync_status == GitHubSyncStatus.FAILED.value

    await service._sync_pull_request_to_github(PROJECT_ID, pr)

    assert pr.github_sync_status == GitHubSyncStatus.SYNCED.value
    assert github.merge_pull_request.await_count == 2
    assert github.merge_pull_request.await_args.kwargs["commit_title"] == "Durable merge title"


@pytest.mark.asyncio
async def test_superseded_open_side_effect_reconciles_latest_closed_generation() -> None:
    db = AsyncMock()
    stale_open = _pr(status=PRStatus.OPEN.value, number=42)
    stale_open.github_sync_generation = 1
    stale_attempt = uuid.uuid4()
    stale_open.github_sync_attempt_id = stale_attempt
    latest_closed = _pr(status=PRStatus.CLOSED.value, number=42)
    latest_closed.id = stale_open.id
    latest_closed.github_sync_generation = 2
    latest_closed.github_sync_attempt_id = uuid.uuid4()
    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = latest_closed
    db.execute.side_effect = [
        MagicMock(rowcount=0),
        latest_result,
        MagicMock(rowcount=1),
    ]
    integration = MagicMock(repo_owner="org", repo_name="repo")
    service = PullRequestService(db, git_service=MagicMock(), github_service=MagicMock())
    service.github_reconciler.reconcile = AsyncMock(
        side_effect=[_github_pr(number=42), _github_pr(number=42, state="closed")]
    )
    service._get_github_token = AsyncMock(  # type: ignore[method-assign]
        return_value=(integration, "token")
    )

    await service._perform_github_sync(
        stale_open,
        (integration, "token"),
        attempt_id=stale_attempt,
        generation=1,
    )

    intents = [call.kwargs["intent"] for call in service.github_reconciler.reconcile.await_args_list]
    assert [intent.state for intent in intents] == ["open", "closed"]
    assert latest_closed.github_sync_generation == 3
    assert latest_closed.github_sync_status == GitHubSyncStatus.SYNCED.value


@pytest.mark.asyncio
async def test_repo_reconfiguration_ignores_stored_number_and_discovers_exact_current_mirror() -> None:
    github = MagicMock()
    github.get_pull_request_or_none = AsyncMock()
    github.list_pull_requests = AsyncMock(return_value=[_github_pr(number=77)])
    github.update_pull_request = AsyncMock(return_value=_github_pr(number=77))
    reconciler = GitHubPullRequestReconciler(github)
    intent = GitHubPRSyncIntent(
        title="Mirror me",
        body="Body",
        head="feature/exact",
        base="main",
        state="open",
        merge_title=None,
        stored_number=42,
        stored_repo_owner="old-org",
        stored_repo_name="old-repo",
    )

    result = await reconciler.reconcile(
        token="token", owner="new-org", repo="new-repo", intent=intent
    )

    assert result.number == 77
    github.get_pull_request_or_none.assert_not_awaited()
    assert github.update_pull_request.await_args.kwargs["pr_number"] == 77


def test_new_integration_identity_clears_stored_number_even_when_repo_path_is_reused() -> None:
    service = PullRequestService(AsyncMock(), git_service=MagicMock(), github_service=MagicMock())
    pr = _pr(number=42)
    pr.github_pr_url = "https://github.example/pr/42"
    pr.github_integration_id = uuid.uuid4()
    pr.github_repo_owner = "org"
    pr.github_repo_name = "repo"
    replacement = MagicMock(id=uuid.uuid4(), repo_owner="org", repo_name="repo")

    service._mark_github_sync_pending(pr, replacement)

    assert pr.github_pr_number is None
    assert pr.github_pr_url is None
    assert pr.github_integration_id == replacement.id


@pytest.mark.asyncio
async def test_wrong_head_or_base_is_never_mutated_even_in_same_repository() -> None:
    github = MagicMock()
    github.get_pull_request_or_none = AsyncMock(
        return_value=_github_pr(number=42, head="unrelated")
    )
    github.list_pull_requests = AsyncMock(return_value=[_github_pr(number=77)])
    github.update_pull_request = AsyncMock(return_value=_github_pr(number=77))
    reconciler = GitHubPullRequestReconciler(github)
    intent = GitHubPRSyncIntent(
        title="Mirror me",
        body="Body",
        head="feature/exact",
        base="main",
        state="closed",
        merge_title=None,
        stored_number=42,
        stored_repo_owner="org",
        stored_repo_name="repo",
    )

    await reconciler.reconcile(token="token", owner="org", repo="repo", intent=intent)

    assert github.update_pull_request.await_args.kwargs["pr_number"] == 77
    assert github.update_pull_request.await_args.kwargs["state"] == "closed"


@pytest.mark.asyncio
async def test_transient_stored_mirror_verification_failure_never_falls_through_to_mutation() -> None:
    github = MagicMock()
    github.get_pull_request_or_none = AsyncMock(side_effect=RuntimeError("temporary failure"))
    github.list_pull_requests = AsyncMock()
    github.create_pull_request = AsyncMock()
    github.update_pull_request = AsyncMock()
    reconciler = GitHubPullRequestReconciler(github)
    intent = GitHubPRSyncIntent(
        title="Mirror me",
        body="Body",
        head="feature/exact",
        base="main",
        state="open",
        merge_title=None,
        stored_number=42,
        stored_repo_owner="org",
        stored_repo_name="repo",
    )

    with pytest.raises(RuntimeError, match="temporary failure"):
        await reconciler.reconcile(token="token", owner="org", repo="repo", intent=intent)

    github.list_pull_requests.assert_not_awaited()
    github.create_pull_request.assert_not_awaited()
    github.update_pull_request.assert_not_awaited()
