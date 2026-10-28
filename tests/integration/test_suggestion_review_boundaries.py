"""Request-level authorization and effects on a disposable migrated PostgreSQL DB.

Service commits and per-item rollbacks stay inside savepoints. The outer
transaction owns every synthetic row and teardown checks that none escaped.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ontokit.api.routes import pull_requests, suggestions
from ontokit.core.auth import CurrentUser, get_current_user, get_current_user_optional
from ontokit.models.notification import Notification
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import PullRequest, PullRequestReview
from ontokit.models.suggestion_outcome import SuggestionOutcome
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.suggestion_service import SuggestionService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def boundary_db() -> AsyncIterator[SimpleNamespace]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set: requires disposable migrated PostgreSQL")
    engine = create_async_engine(database_url)
    project_ids = [uuid4(), uuid4()]
    pr_id = uuid4()
    clock = datetime.now(UTC) + timedelta(days=7)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as db:
                    for project_id in project_ids:
                        project = Project(
                            id=project_id,
                            name="Synthetic boundary project",
                            owner_id="owner",
                            is_public=False,
                        )
                        project.members = [
                            ProjectMember(user_id="owner", role="owner"),
                            ProjectMember(user_id="reviewer", role="editor"),
                            ProjectMember(user_id="alice", role="suggester", is_trusted=False),
                            ProjectMember(user_id="bob", role="suggester", is_trusted=True),
                        ]
                        db.add(project)
                    await db.flush()
                    db.add(
                        PullRequest(
                            id=pr_id,
                            project_id=project_ids[0],
                            pr_number=1,
                            title="Synthetic review",
                            source_branch="suggest/shared",
                            target_branch="main",
                            author_id="alice",
                            status="open",
                        )
                    )
                    await db.flush()
                    for project_id in project_ids:
                        for session_id, user_id, status, anonymous in [
                            ("shared", "alice", "submitted", False),
                            ("trusted", "bob", "submitted", False),
                            ("anonymous", "anonymous-synthetic", "auto-submitted", True),
                            ("draft", "alice", "active", False),
                        ]:
                            linked = project_id == project_ids[0] and session_id == "shared"
                            db.add(
                                SuggestionSession(
                                    project_id=project_id,
                                    session_id=session_id,
                                    branch=f"suggest/{session_id}",
                                    user_id=user_id,
                                    status=status,
                                    is_anonymous=anonymous,
                                    beacon_token="synthetic-test-token",
                                    changes_count=1,
                                    pr_id=pr_id if linked else None,
                                    pr_number=1 if linked else None,
                                    auto_accept_after=clock if linked else None,
                                )
                            )
                    db.add(
                        SuggestionSession(
                            project_id=project_ids[1],
                            session_id="foreign-only",
                            branch="suggest/foreign-only",
                            user_id="alice",
                            status="submitted",
                            beacon_token="synthetic-test-token",
                        )
                    )
                    # Persist seed data to the enclosing transaction so a refused
                    # bulk item can roll back its own savepoint without erasing it.
                    await db.commit()
                # Request state must not reuse the seeding session's identity map:
                # its relationship loader options differ from a fresh API lookup.
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as request_db:
                    yield SimpleNamespace(
                        db=request_db, project_ids=project_ids, pr_id=pr_id, clock=clock
                    )
            finally:
                await outer.rollback()
        async with engine.connect() as check:
            assert (
                await check.scalar(
                    select(func.count()).select_from(Project).where(Project.id.in_(project_ids))
                )
                == 0
            ), "Service commit escaped the test's outer transaction"
            assert (
                await check.scalar(
                    select(func.count()).select_from(PullRequest).where(PullRequest.id == pr_id)
                )
                == 0
            )
    finally:
        await engine.dispose()


@asynccontextmanager
async def _client(
    db: AsyncSession, user_id: str, git: MagicMock, github: MagicMock
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    app = FastAPI()
    app.include_router(suggestions.router)
    app.include_router(pull_requests.router)
    original_overrides = app.dependency_overrides
    user = CurrentUser(id=user_id, name=f"Synthetic {user_id}")
    app.dependency_overrides = {
        get_current_user: lambda: user,
        get_current_user_optional: lambda: user,
        suggestions.get_service: lambda: SuggestionService(db, git),
        pull_requests.get_service: lambda: PullRequestService(
            db, git_service=git, github_service=github, user_service=MagicMock()
        ),
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, app
    finally:
        app.dependency_overrides = original_overrides


async def _snapshot(db: AsyncSession, project_ids: list[UUID]) -> list[list[tuple]]:
    """Read columns directly, avoiding potentially stale ORM identity-map state."""
    statements = [
        select(model.__table__).where(model.project_id.in_(project_ids)).order_by(model.id)
        for model in (SuggestionSession, SuggestionOutcome, Notification)
    ]
    statements.append(
        select(PullRequestReview.__table__)
        .where(
            PullRequestReview.pull_request_id.in_(
                select(PullRequest.id).where(PullRequest.project_id.in_(project_ids))
            )
        )
        .order_by(PullRequestReview.id)
    )
    return [[tuple(row) for row in (await db.execute(query)).all()] for query in statements]


async def test_own_sessions_and_reviewer_queues_are_isolated(boundary_db: SimpleNamespace) -> None:
    db, project_ids = boundary_db.db, boundary_db.project_ids
    git, github = MagicMock(), MagicMock()
    for project_id in project_ids:
        for user_id, expected in [
            (
                "alice",
                {"shared", "draft"} | ({"foreign-only"} if project_id == project_ids[1] else set()),
            ),
            ("bob", {"trusted"}),
        ]:
            async with _client(db, user_id, git, github) as (client, _):
                response = await client.get(f"/{project_id}/suggestions/sessions")
            assert response.status_code == 200
            assert {row["session_id"] for row in response.json()["items"]} == expected
            assert {row["submitter"]["id"] for row in response.json()["items"]} == {user_id}

        triage = {"shared", "anonymous"} | (
            {"foreign-only"} if project_id == project_ids[1] else set()
        )
        async with _client(db, "reviewer", git, github) as (client, _):
            for queue, expected, tiers in [
                ("triage", triage, {"anonymous", "untrusted"}),
                ("review", {"trusted"}, {"trusted"}),
            ]:
                response = await client.get(
                    f"/{project_id}/suggestions/pending", params={"queue": queue}
                )
                assert response.status_code == 200
                rows = response.json()["items"]
                assert {row["session_id"] for row in rows} == expected
                assert {row["submitter_tier"] for row in rows} == tiers
    assert git.method_calls == []
    assert github.method_calls == []


@pytest.mark.parametrize(
    "user_id,session_id,expected_status,detail",
    [
        ("reviewer", "foreign-only", 404, "Suggestion session not found"),
        ("alice", "shared", 403, "Editor access or above required to review suggestions"),
    ],
)
async def test_denied_suggestion_write_preserves_state(
    boundary_db: SimpleNamespace,
    user_id: str,
    session_id: str,
    expected_status: int,
    detail: str,
) -> None:
    db, project_ids = boundary_db.db, boundary_db.project_ids
    before = await _snapshot(db, project_ids)
    git, github = MagicMock(), MagicMock()
    async with _client(db, user_id, git, github) as (client, _):
        response = await client.post(f"/{project_ids[0]}/suggestions/sessions/{session_id}/dismiss")
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert await _snapshot(db, project_ids) == before
    assert git.method_calls == []
    assert github.method_calls == []


async def test_private_review_denial_and_allowed_control_persist(
    boundary_db: SimpleNamespace,
) -> None:
    db, project_ids = boundary_db.db, boundary_db.project_ids
    before = await _snapshot(db, project_ids)
    git, github = MagicMock(), MagicMock()
    path = f"/{project_ids[0]}/pull-requests/1/reviews"
    body = {"status": "commented", "body": "Synthetic reviewer comment"}
    async with _client(db, "nonmember", git, github) as (client, _):
        denied = await client.post(path, json=body)
    assert denied.status_code == 403
    assert denied.json() == {"detail": "You don't have access to this project"}
    assert await _snapshot(db, project_ids) == before

    async with _client(db, "reviewer", git, github) as (client, _):
        allowed = await client.post(path, json=body)
        listed = await client.get(path)
        wrong_project = await client.get(f"/{project_ids[1]}/pull-requests/1/reviews")
    assert allowed.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"] == [allowed.json()]
    assert wrong_project.status_code == 404
    review = (
        await db.execute(
            select(
                PullRequestReview.reviewer_id, PullRequestReview.status, PullRequestReview.body
            ).where(PullRequestReview.pull_request_id == boundary_db.pr_id)
        )
    ).one()
    assert tuple(review) == ("reviewer", "commented", body["body"])
    notification = (
        await db.execute(
            select(Notification.user_id, Notification.type, Notification.body).where(
                Notification.project_id == project_ids[0]
            )
        )
    ).one()
    assert tuple(notification) == ("alice", "pr_review", body["body"])
    clock = (
        await db.execute(
            select(
                SuggestionSession.auto_accept_after, SuggestionSession.auto_accept_halted_at
            ).where(SuggestionSession.pr_id == boundary_db.pr_id)
        )
    ).one()
    assert clock.auto_accept_after is None
    assert clock.auto_accept_halted_at is not None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(SuggestionOutcome)
            .where(SuggestionOutcome.project_id.in_(project_ids))
        )
        == 0
    )
    assert git.method_calls == []
    assert github.method_calls == []


async def test_bulk_failure_does_not_rollback_following_dismiss(
    boundary_db: SimpleNamespace,
) -> None:
    db, project_ids = boundary_db.db, boundary_db.project_ids
    git, github = MagicMock(), MagicMock()
    async with _client(db, "reviewer", git, github) as (client, _):
        response = await client.post(
            f"/{project_ids[0]}/suggestions/bulk-review",
            json={
                "action": "dismiss",
                "session_ids": ["foreign-only", "shared"],
                "note": "Duplicate",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "action": "dismiss",
            "succeeded": ["shared"],
            "failed": [{"session_id": "foreign-only", "reason": "Suggestion session not found"}],
        }
        repeat = await client.post(f"/{project_ids[0]}/suggestions/sessions/shared/dismiss")
    assert repeat.status_code == 400
    outcome = (
        await db.execute(
            select(
                SuggestionOutcome.outcome, SuggestionOutcome.decided_by, SuggestionOutcome.note
            ).where(SuggestionOutcome.project_id == project_ids[0])
        )
    ).one()
    assert tuple(outcome) == ("dismissed", "reviewer", "Duplicate")
    rows = (
        await db.execute(
            select(
                SuggestionSession.project_id,
                SuggestionSession.status,
                SuggestionSession.reviewer_id,
            ).where(
                SuggestionSession.session_id == "shared",
                SuggestionSession.project_id.in_(project_ids),
            )
        )
    ).all()
    assert {tuple(row) for row in rows} == {
        (project_ids[0], "discarded", "reviewer"),
        (project_ids[1], "submitted", None),
    }
    assert (
        await db.scalar(
            select(func.count())
            .select_from(SuggestionOutcome)
            .where(SuggestionOutcome.project_id == project_ids[1])
        )
        == 0
    )
    assert (
        await db.scalar(
            select(SuggestionSession.status).where(
                SuggestionSession.project_id == project_ids[1],
                SuggestionSession.session_id == "foreign-only",
            )
        )
        == "submitted"
    )
    assert git.method_calls == []
    assert github.method_calls == []


async def test_request_exception_restores_overrides_and_contains_commits(
    boundary_db: SimpleNamespace,
) -> None:
    db, project_id = boundary_db.db, boundary_db.project_ids[0]
    with pytest.raises(RuntimeError, match="synthetic request failure"):
        async with _client(db, "reviewer", MagicMock(), MagicMock()) as (client, app):
            response = await client.post(f"/{project_id}/suggestions/sessions/shared/dismiss")
            assert response.status_code == 204
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(SuggestionOutcome)
                    .where(SuggestionOutcome.project_id == project_id)
                )
                == 1
            )

            @app.get("/synthetic-failure")
            async def fail() -> None:
                raise RuntimeError("synthetic request failure")

            await client.get("/synthetic-failure")
    assert app.dependency_overrides == {}
    # Fixture teardown independently reads after outer rollback, including this
    # case's successful service commit followed by an ASGI exception.
