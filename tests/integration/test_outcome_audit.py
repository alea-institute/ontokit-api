"""Audit-snapshot proofs with migrated Postgres and real domain services.

These tests exercise real ``TrustService`` and ``SuggestionService`` objects against the
migrated integration database. Acceptance paths that must prove a merge use real pygit2 bare
repositories and the real pull-request service; only the external ARQ and GitHub boundaries
are faked. Scenarios that need no suggestion lifecycle use database-backed sessions directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.trust import list_suggestion_outcomes
from ontokit.core.auth import CurrentUser
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import PRStatus, PullRequest
from ontokit.models.suggestion_outcome import SuggestionOutcome, SuggestionOutcomeType
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.suggestion import SuggestionRejectRequest
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.suggestion_service import SuggestionService
from ontokit.services.trust_service import SYSTEM_AUTO_ACCEPT_ACTOR, TrustService

pytestmark = pytest.mark.integration

FILE = "ontology.ttl"
BASE_CONTENT = b"@prefix ex: <https://example.test/> .\n"
MERGED_CONTENT = BASE_CONTENT + b"ex:Audited ex:status \"merged\" .\n"


@dataclass
class AuditProject:
    project: Project
    owner: CurrentUser
    submitter: CurrentUser
    submitter_member: ProjectMember
    git: BareGitRepositoryService


async def _seed_project(
    db: AsyncSession,
    tmp_path: Path,
    *,
    submitter_trusted: bool = True,
    submitter_role: str = "suggester",
    promotion_threshold: int = 5,
    auto_accept: bool = False,
) -> AuditProject:
    project_id = uuid4()
    owner = CurrentUser(
        id=f"owner-{project_id}", name="Project Owner", email="owner@example.test"
    )
    submitter = CurrentUser(
        id=f"submitter-{project_id}",
        name="Account Submitter",
        email="submitter@example.test",
    )
    project = Project(
        id=project_id,
        name="Outcome audit integration",
        owner_id=owner.id,
        source_file_path=FILE,
        trust_promotion_threshold=promotion_threshold,
        auto_accept_enabled=auto_accept,
        auto_accept_quiet_days=1,
    )
    project.members.append(ProjectMember(user_id=owner.id, role="owner"))
    submitter_member = ProjectMember(
        user_id=submitter.id,
        role=submitter_role,
        is_trusted=submitter_trusted,
        trust_override="none",
    )
    project.members.append(submitter_member)
    db.add(project)
    await db.commit()

    git = BareGitRepositoryService(base_path=str(tmp_path))
    git.initialize_repository(project_id, BASE_CONTENT, FILE)
    return AuditProject(project, owner, submitter, submitter_member, git)


async def _add_submitted_session(
    db: AsyncSession,
    seeded: AuditProject,
    *,
    user: CurrentUser | None = None,
    is_anonymous: bool = False,
    submitter_name: str | None = None,
    submitter_email: str | None = None,
    auto_accept_after: datetime | None = None,
) -> SuggestionSession:
    actor = user or seeded.submitter
    session_key = uuid4().hex[:12]
    session = SuggestionSession(
        project_id=seeded.project.id,
        user_id=actor.id,
        user_name=actor.name,
        user_email=actor.email,
        session_id=f"s_{session_key}",
        branch=f"suggest/{session_key}",
        beacon_token=f"integration-{session_key}",
        status=SuggestionSessionStatus.SUBMITTED.value,
        changes_count=1,
        is_anonymous=is_anonymous,
        submitter_name=submitter_name,
        submitter_email=submitter_email,
        auto_accept_after=auto_accept_after,
    )
    db.add(session)
    await db.commit()
    return session


async def _attach_real_pull_request(
    db: AsyncSession, seeded: AuditProject, session: SuggestionSession
) -> PullRequest:
    seeded.git.create_branch(seeded.project.id, session.branch, from_ref="main")
    seeded.git.commit_changes(
        seeded.project.id,
        MERGED_CONTENT,
        FILE,
        "Audited suggestion",
        seeded.submitter.name,
        seeded.submitter.email,
        session.branch,
    )
    pull_request = PullRequest(
        id=uuid4(),
        project_id=seeded.project.id,
        pr_number=1,
        title="Audited suggestion",
        source_branch=session.branch,
        target_branch="main",
        status=PRStatus.OPEN.value,
        author_id=session.user_id,
        author_name=session.user_name,
        author_email=session.user_email,
    )
    db.add(pull_request)
    session.pr_number = pull_request.pr_number
    session.pr_id = pull_request.id
    await db.commit()
    return pull_request


async def _approve_with_real_merge(
    db: AsyncSession,
    seeded: AuditProject,
    service: SuggestionService,
    session: SuggestionSession,
) -> None:
    pull_requests = PullRequestService(
        db,
        git_service=seeded.git,
        github_service=MagicMock(),
    )
    queue = AsyncMock()
    with (
        patch(
            "ontokit.services.suggestion_service.get_pull_request_service",
            return_value=pull_requests,
        ),
        patch(
            "ontokit.api.utils.redis.get_arq_pool",
            new=AsyncMock(return_value=queue),
        ),
    ):
        await service.approve(seeded.project.id, session.session_id, seeded.owner)


async def _auto_accept_with_real_merge(
    db: AsyncSession, seeded: AuditProject, service: SuggestionService
) -> int:
    pull_requests = PullRequestService(
        db,
        git_service=seeded.git,
        github_service=MagicMock(),
    )
    queue = AsyncMock()
    with (
        patch(
            "ontokit.services.suggestion_service.get_pull_request_service",
            return_value=pull_requests,
        ),
        patch(
            "ontokit.api.utils.redis.get_arq_pool",
            new=AsyncMock(return_value=queue),
        ),
    ):
        return await service.auto_accept_ripe_sessions()


async def _outcome_for_session(
    db: AsyncSession, session: SuggestionSession
) -> SuggestionOutcome:
    row = await db.scalar(
        select(SuggestionOutcome).where(SuggestionOutcome.session_id == session.id)
    )
    assert row is not None
    return row


async def _cleanup(db: AsyncSession, *project_ids: UUID) -> None:
    await db.rollback()
    await db.execute(delete(Project).where(Project.id.in_(project_ids)))
    await db.commit()


@pytest.mark.asyncio
async def test_ae1_live_reject_snapshots_trusted_suggester(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    session = await _add_submitted_session(real_db_session, seeded)
    try:
        await SuggestionService(real_db_session, seeded.git).reject(
            seeded.project.id,
            session.session_id,
            SuggestionRejectRequest(reason="Not aligned with the ontology"),
            seeded.owner,
        )

        outcome = await _outcome_for_session(real_db_session, session)
        assert outcome.outcome == SuggestionOutcomeType.REJECTED.value
        assert outcome.snapshot_tier == "trusted"
        assert outcome.snapshot_role == "suggester"
        assert outcome.submitter_name == seeded.submitter.name
        assert outcome.submitter_email == seeded.submitter.email
        assert outcome.decided_by == seeded.owner.id
        assert outcome.decided_by_name == seeded.owner.name
        assert outcome.snapshot_captured_at is not None
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae2_accepted_snapshot_survives_later_role_change(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    session = await _add_submitted_session(real_db_session, seeded)
    pull_request = await _attach_real_pull_request(real_db_session, seeded, session)
    try:
        service = SuggestionService(real_db_session, seeded.git)
        await _approve_with_real_merge(real_db_session, seeded, service, session)
        outcome = await _outcome_for_session(real_db_session, session)
        outcome_id = outcome.id

        seeded.submitter_member.role = "editor"
        await real_db_session.commit()
        reread = await real_db_session.scalar(
            select(SuggestionOutcome)
            .where(SuggestionOutcome.id == outcome_id)
            .execution_options(populate_existing=True)
        )
        changed_member = await real_db_session.scalar(
            select(ProjectMember)
            .where(ProjectMember.id == seeded.submitter_member.id)
            .execution_options(populate_existing=True)
        )

        assert reread is not None
        assert reread.snapshot_tier == "trusted"
        assert reread.snapshot_role == "suggester"
        assert changed_member is not None and changed_member.role == "editor"
        assert pull_request.status == PRStatus.MERGED.value
        assert seeded.git.get_file_from_branch(seeded.project.id, "main", FILE) == MERGED_CONTENT
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae3_ripe_auto_accept_snapshots_submitter_standing(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(
        real_db_session, tmp_path, submitter_trusted=True, auto_accept=True
    )
    session = await _add_submitted_session(
        real_db_session,
        seeded,
        auto_accept_after=datetime.now(UTC) - timedelta(minutes=1),
    )
    pull_request = await _attach_real_pull_request(real_db_session, seeded, session)
    try:
        service = SuggestionService(real_db_session, seeded.git)
        assert await _auto_accept_with_real_merge(real_db_session, seeded, service) == 1

        outcome = await _outcome_for_session(real_db_session, session)
        await real_db_session.refresh(session)
        await real_db_session.refresh(pull_request)
        assert session.status == SuggestionSessionStatus.MERGED.value
        assert pull_request.status == PRStatus.MERGED.value
        assert outcome.snapshot_tier == "trusted"
        assert outcome.snapshot_role == "suggester"
        assert outcome.decided_by == SYSTEM_AUTO_ACCEPT_ACTOR
        assert outcome.decided_by_name is None
        assert outcome.snapshot_captured_at is not None
        assert seeded.git.get_file_from_branch(seeded.project.id, "main", FILE) == MERGED_CONTENT
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae4_anonymous_decision_snapshots_self_reported_identity(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path)
    anonymous = CurrentUser(id=f"anonymous-{uuid4().hex}", name=None, email=None)
    session = await _add_submitted_session(
        real_db_session,
        seeded,
        user=anonymous,
        is_anonymous=True,
        submitter_name="Self-Reported Author",
        submitter_email="anonymous@example.test",
    )
    try:
        await SuggestionService(real_db_session, seeded.git).dismiss(
            seeded.project.id, session.session_id, seeded.owner, "triaged"
        )

        outcome = await _outcome_for_session(real_db_session, session)
        assert outcome.is_anonymous is True
        assert outcome.counts_toward_promotion is False
        assert outcome.submitter_name == "Self-Reported Author"
        assert outcome.submitter_email == "anonymous@example.test"
        assert outcome.snapshot_tier is None
        assert outcome.snapshot_role is None
        assert outcome.snapshot_captured_at is not None
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae5_endpoint_serves_legacy_nulls_alongside_snapshots(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    session = await _add_submitted_session(real_db_session, seeded)
    try:
        await SuggestionService(real_db_session, seeded.git).reject(
            seeded.project.id,
            session.session_id,
            SuggestionRejectRequest(reason="Create a snapshotted neighbor"),
            seeded.owner,
        )
        legacy_user_id = f"legacy-{uuid4()}"
        await real_db_session.execute(
            insert(SuggestionOutcome).values(
                id=uuid4(),
                project_id=seeded.project.id,
                user_id=legacy_user_id,
                session_id=None,
                outcome=SuggestionOutcomeType.REJECTED.value,
                counts_toward_promotion=True,
                is_anonymous=False,
                decided_by=seeded.owner.id,
                note="Inserted as a pre-feature row",
                snapshot_tier=None,
                snapshot_role=None,
                submitter_name=None,
                submitter_email=None,
                decided_by_name=None,
                snapshot_captured_at=None,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await real_db_session.commit()

        response = await list_suggestion_outcomes(
            seeded.project.id, real_db_session, seeded.owner, cursor=None, limit=25
        )
        by_user = {item.user_id: item for item in response.items}
        legacy = by_user[legacy_user_id]
        current = by_user[seeded.submitter.id]

        assert response.total == 2
        assert response.next_cursor is None
        assert legacy.snapshot_tier is None
        assert legacy.snapshot_role is None
        assert legacy.submitter_name is None
        assert legacy.submitter_email is None
        assert legacy.decided_by_name is None
        assert legacy.snapshot_captured_at is None
        assert current.snapshot_tier == "trusted"
        assert current.snapshot_role == "suggester"
        assert current.snapshot_captured_at is not None
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae6_promotion_acceptance_snapshots_pre_promotion_tier(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(
        real_db_session,
        tmp_path,
        submitter_trusted=False,
        promotion_threshold=1,
    )
    session = await _add_submitted_session(real_db_session, seeded)
    await _attach_real_pull_request(real_db_session, seeded, session)
    try:
        service = SuggestionService(real_db_session, seeded.git)
        await _approve_with_real_merge(real_db_session, seeded, service, session)

        outcome = await _outcome_for_session(real_db_session, session)
        await real_db_session.refresh(seeded.submitter_member)
        assert outcome.outcome == SuggestionOutcomeType.ACCEPTED.value
        assert outcome.snapshot_tier == "untrusted"
        assert outcome.snapshot_role == "suggester"
        assert seeded.submitter_member.is_trusted is True
        assert seeded.submitter_member.trust_granted_by == "system:auto-promotion"
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_ae7_snapshot_resolution_failure_degrades_and_merge_proceeds(
    real_db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    session = await _add_submitted_session(real_db_session, seeded)
    pull_request = await _attach_real_pull_request(real_db_session, seeded, session)
    service = SuggestionService(real_db_session, seeded.git)

    def fail_resolution(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced tier-resolution failure")

    monkeypatch.setattr(service.trust, "resolve_tier", fail_resolution)
    try:
        await _approve_with_real_merge(real_db_session, seeded, service, session)

        outcome = await _outcome_for_session(real_db_session, session)
        response = await list_suggestion_outcomes(
            seeded.project.id, real_db_session, seeded.owner, cursor=None, limit=25
        )
        served = response.items[0]
        await real_db_session.refresh(session)
        await real_db_session.refresh(pull_request)

        assert session.status == SuggestionSessionStatus.MERGED.value
        assert pull_request.status == PRStatus.MERGED.value
        assert outcome.snapshot_tier is None
        assert outcome.snapshot_role is None
        assert outcome.snapshot_captured_at is not None
        assert served.snapshot_tier is None
        assert served.snapshot_role is None
        assert served.snapshot_captured_at is not None
        assert served.is_anonymous is False
        assert "suggestion outcome snapshot resolution failed (RuntimeError)" in caplog.text
        assert response.total == 1
        assert response.next_cursor is None
        assert served.outcome == SuggestionOutcomeType.ACCEPTED
        assert seeded.git.get_file_from_branch(seeded.project.id, "main", FILE) == MERGED_CONTENT
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_outcome_cursor_walk_survives_newer_insert_and_equal_timestamps(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    newest_time = datetime(2026, 8, 10, 12, tzinfo=UTC)
    tied_time = newest_time - timedelta(minutes=1)
    rows = [
        ("00000000-0000-0000-0000-000000000005", "walk-5", newest_time),
        ("00000000-0000-0000-0000-000000000004", "walk-4", tied_time),
        ("00000000-0000-0000-0000-000000000003", "walk-3", tied_time),
        ("00000000-0000-0000-0000-000000000002", "walk-2", tied_time - timedelta(minutes=1)),
        ("00000000-0000-0000-0000-000000000001", "walk-1", tied_time - timedelta(minutes=2)),
    ]
    try:
        for outcome_id, user_id, created_at in rows:
            real_db_session.add(
                SuggestionOutcome(
                    id=UUID(outcome_id),
                    project_id=seeded.project.id,
                    user_id=user_id,
                    outcome=SuggestionOutcomeType.REJECTED.value,
                    is_anonymous=False,
                    created_at=created_at,
                )
            )
        await real_db_session.commit()

        first = await list_suggestion_outcomes(
            seeded.project.id, real_db_session, seeded.owner, cursor=None, limit=2
        )
        assert [item.user_id for item in first.items] == ["walk-5", "walk-4"]
        assert first.next_cursor is not None

        real_db_session.add(
            SuggestionOutcome(
                project_id=seeded.project.id,
                user_id="inserted-newer",
                outcome=SuggestionOutcomeType.ACCEPTED.value,
                is_anonymous=False,
                created_at=newest_time + timedelta(minutes=1),
            )
        )
        await real_db_session.commit()

        second = await list_suggestion_outcomes(
            seeded.project.id,
            real_db_session,
            seeded.owner,
            cursor=first.next_cursor,
            limit=2,
        )
        assert second.total == 6
        assert [item.user_id for item in second.items] == ["walk-3", "walk-2"]
        assert second.next_cursor is not None

        third = await list_suggestion_outcomes(
            seeded.project.id,
            real_db_session,
            seeded.owner,
            cursor=second.next_cursor,
            limit=2,
        )
        assert [item.user_id for item in third.items] == ["walk-1"]
        assert third.next_cursor is None

        walked_ids = [item.user_id for page in (first, second, third) for item in page.items]
        assert walked_ids == ["walk-5", "walk-4", "walk-3", "walk-2", "walk-1"]
        assert len(walked_ids) == len(set(walked_ids))
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_outcome_endpoint_rejects_non_admin_member(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    seeded = await _seed_project(real_db_session, tmp_path, submitter_trusted=True)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await list_suggestion_outcomes(
                seeded.project.id,
                real_db_session,
                seeded.submitter,
                cursor=None,
                limit=25,
            )
        assert exc_info.value.status_code == 403
    finally:
        await _cleanup(real_db_session, seeded.project.id)


@pytest.mark.asyncio
async def test_outcome_endpoint_isolates_projects(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    project_a = await _seed_project(real_db_session, tmp_path / "a", submitter_trusted=True)
    project_b = await _seed_project(real_db_session, tmp_path / "b", submitter_trusted=True)
    session_a = await _add_submitted_session(real_db_session, project_a)
    session_b = await _add_submitted_session(real_db_session, project_b)
    try:
        trust = TrustService(real_db_session)
        await trust.record_outcome(
            project_a.project.id,
            session_a,
            SuggestionOutcomeType.REJECTED,
            project_a.owner.id,
            project=project_a.project,
            decided_by_name=project_a.owner.name,
        )
        await trust.record_outcome(
            project_b.project.id,
            session_b,
            SuggestionOutcomeType.REJECTED,
            project_b.owner.id,
            project=project_b.project,
            decided_by_name=project_b.owner.name,
        )
        await real_db_session.commit()

        response = await list_suggestion_outcomes(
            project_a.project.id,
            real_db_session,
            project_a.owner,
            cursor=None,
            limit=25,
        )
        assert response.total == 1
        assert response.next_cursor is None
        assert [item.user_id for item in response.items] == [project_a.submitter.id]
        assert project_b.submitter.id not in {item.user_id for item in response.items}
    finally:
        await _cleanup(real_db_session, project_a.project.id, project_b.project.id)


@pytest.mark.asyncio
async def test_outcome_cursor_cannot_be_replayed_across_projects(
    real_db_session: AsyncSession, tmp_path: Path
) -> None:
    project_a = await _seed_project(real_db_session, tmp_path / "a", submitter_trusted=True)
    project_b = await _seed_project(real_db_session, tmp_path / "b", submitter_trusted=True)
    newest_time = datetime(2026, 8, 10, 12, tzinfo=UTC)
    try:
        real_db_session.add_all(
            [
                SuggestionOutcome(
                    project_id=project_a.project.id,
                    user_id="project-a-newest",
                    outcome=SuggestionOutcomeType.REJECTED.value,
                    is_anonymous=False,
                    created_at=newest_time,
                ),
                SuggestionOutcome(
                    project_id=project_a.project.id,
                    user_id="project-a-older",
                    outcome=SuggestionOutcomeType.REJECTED.value,
                    is_anonymous=False,
                    created_at=newest_time - timedelta(minutes=1),
                ),
                SuggestionOutcome(
                    project_id=project_b.project.id,
                    user_id="project-b-row",
                    outcome=SuggestionOutcomeType.REJECTED.value,
                    is_anonymous=False,
                    created_at=newest_time - timedelta(minutes=2),
                ),
            ]
        )
        await real_db_session.commit()

        project_a_page = await list_suggestion_outcomes(
            project_a.project.id,
            real_db_session,
            project_a.owner,
            cursor=None,
            limit=1,
        )
        assert project_a_page.next_cursor is not None

        with pytest.raises(HTTPException) as exc_info:
            await list_suggestion_outcomes(
                project_b.project.id,
                real_db_session,
                project_b.owner,
                cursor=project_a_page.next_cursor,
                limit=1,
            )

        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "Invalid outcome cursor"
    finally:
        await _cleanup(real_db_session, project_a.project.id, project_b.project.id)


@pytest.mark.asyncio
async def test_live_postgres_exposes_declared_audit_cursor_index(
    real_db_session: AsyncSession,
) -> None:
    declared = next(
        index
        for index in SuggestionOutcome.__table__.indexes
        if index.name == "ix_suggestion_outcomes_audit_cursor"
    )
    connection = await real_db_session.connection()
    live_indexes = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_indexes("suggestion_outcomes")
    )
    live = next(index for index in live_indexes if index["name"] == declared.name)

    assert live["column_names"] == list(declared.columns.keys())
