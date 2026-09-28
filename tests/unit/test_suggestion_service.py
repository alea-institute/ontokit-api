"""Tests for SuggestionService (ontokit/services/suggestion_service.py)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from itertools import chain, repeat
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from ontokit.core.auth import CurrentUser
from ontokit.models.suggestion_outcome import SuggestionOutcome
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.services.embedding_service import EmbeddingBudgetExceeded, EmbeddingPricingUnavailable
from ontokit.services.suggestion_service import SuggestionService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _no_commit_identity_row() -> MagicMock:
    """Result for the U9 commit-identity preference lookup: no opt-in row.

    Every suggestion commit now resolves its author identity through
    CommitIdentityService, which reads this table before writing. Without a row
    the contributor gets the default noreply alias, which is what these tests
    exercise.
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    return result


def _padded(*results: object, project: object) -> Iterator[object]:
    """Ordered query results, then a trust-aware tail forever.

    U3 added follow-on queries AFTER the state change these tests assert on —
    the project reload for promotion evaluation and the outcome-log count. The
    tail answers both (project row, zero accepted outcomes) so each test stays
    pinned to the behavior it is about rather than to an exact query count.
    """
    tail = MagicMock()
    tail.scalar_one_or_none.return_value = project
    tail.scalar.return_value = 0
    tail.scalars.return_value.all.return_value = []
    return chain(results, repeat(tail))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    user_id: str = "test-user-id",
    name: str = "Test User",
    email: str = "test@example.com",
) -> CurrentUser:
    return CurrentUser(id=user_id, email=email, name=name, username="testuser")


def _make_project(project_id: uuid.UUID = PROJECT_ID, is_public: bool = True) -> MagicMock:
    project = MagicMock()
    project.id = project_id
    project.name = "Test Project"
    project.is_public = is_public
    project.source_file_path = None
    project.github_integration = None

    member = MagicMock()
    member.user_id = "test-user-id"
    member.role = "editor"
    member.is_trusted = False
    member.trust_override = "none"
    project.members = [member]
    project.trust_promotion_threshold = 5
    return project


def _configure_editor_pr_claim(
    pr_service: AsyncMock,
    response: MagicMock,
    project: MagicMock,
) -> MagicMock:
    """Configure the locked claim and post-lock finalization seams."""
    db_pr = MagicMock()
    db_pr.id = response.id
    db_pr.pr_number = response.pr_number
    db_pr.title = response.title
    db_pr.github_pr_url = response.github_pr_url
    pr_service._claim_pull_request_already_locked = AsyncMock(return_value=(db_pr, project))
    pr_service._finalize_created_pull_request = AsyncMock(return_value=response)
    return db_pr


def _make_session(
    *,
    session_id: str = "s_abc12345",
    user_id: str = "test-user-id",
    status: str = SuggestionSessionStatus.ACTIVE.value,
    changes_count: int = 0,
    branch: str = "suggest/test-use/s_abc12345",
    entities_modified: str | None = None,
    pr_number: int | None = None,
    pr_id: uuid.UUID | None = None,
    last_activity: datetime | None = None,
    is_anonymous: bool = False,
    anonymous_content_bytes: int = 0,
) -> MagicMock:
    session = MagicMock(spec=SuggestionSession)
    session.id = uuid.uuid4()
    session.project_id = PROJECT_ID
    session.session_id = session_id
    session.user_id = user_id
    session.user_name = "Test User"
    session.user_email = "test@example.com"
    session.branch = branch
    session.status = status
    session.changes_count = changes_count
    session.entities_modified = entities_modified
    session.beacon_token = "tok_test"
    session.pr_number = pr_number
    session.pr_id = pr_id
    session.reviewer_id = None
    session.reviewer_name = None
    session.reviewer_email = None
    session.reviewer_feedback = None
    session.reviewed_at = None
    session.revision = 1
    session.summary = None
    # Anonymous-suggestion columns (PR-7): authenticated session defaults
    session.is_anonymous = is_anonymous
    session.anonymous_content_bytes = anonymous_content_bytes
    session.submitter_name = None
    session.submitter_email = None
    session.client_ip = None
    session.created_at = datetime.now(UTC)
    session.last_activity = last_activity or datetime.now(UTC)
    return session


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create an async mock of AsyncSession."""

    @asynccontextmanager
    async def savepoint() -> AsyncIterator[None]:
        yield

    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    session.refresh = AsyncMock()
    session.add = Mock()
    session.begin_nested = Mock(side_effect=savepoint)
    return session


@pytest.fixture
def mock_git() -> MagicMock:
    """Create a mock git service."""
    git = MagicMock()
    git.create_branch = MagicMock()
    git.delete_branch = MagicMock()
    git.get_default_branch = MagicMock(return_value="main")
    git.get_file_from_branch = MagicMock(return_value=b"")
    return git


@pytest.fixture
def service(
    mock_db: AsyncMock,
    mock_git: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> SuggestionService:
    @asynccontextmanager
    async def unlocked(*_args: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr("ontokit.services.suggestion_service.pull_request_write_locks", unlocked)
    monkeypatch.setattr("ontokit.services.suggestion_service.branch_write_lock", unlocked)
    suggestion_service = SuggestionService(db=mock_db, git_service=mock_git)
    suggestion_service._enqueue_branch_refresh = AsyncMock()  # type: ignore[method-assign]
    return suggestion_service


def _outcomes(mock_db: AsyncMock) -> list[SuggestionOutcome]:
    return [
        call.args[0]
        for call in mock_db.add.call_args_list
        if isinstance(call.args[0], SuggestionOutcome)
    ]


# ---------------------------------------------------------------------------
# _parse_entities_modified / _update_entities_modified
# ---------------------------------------------------------------------------


class TestParseEntitiesModified:
    def test_returns_empty_list_when_none(self, service: SuggestionService) -> None:
        """Returns empty list when entities_modified is None."""
        session = _make_session(entities_modified=None)
        assert service._parse_entities_modified(session) == []

    def test_returns_parsed_list(self, service: SuggestionService) -> None:
        """Returns parsed list from valid JSON."""
        session = _make_session(entities_modified=json.dumps(["Person", "Organization"]))
        assert service._parse_entities_modified(session) == ["Person", "Organization"]

    def test_returns_empty_list_on_invalid_json(self, service: SuggestionService) -> None:
        """Returns empty list for invalid JSON."""
        session = _make_session(entities_modified="not-json")
        assert service._parse_entities_modified(session) == []


class TestUpdateEntitiesModified:
    def test_adds_new_label(self, service: SuggestionService) -> None:
        """Adds a new label to the entities_modified list."""
        session = _make_session(entities_modified=json.dumps(["Person"]))
        service._update_entities_modified(session, "Organization")
        result = json.loads(session.entities_modified)
        assert "Organization" in result
        assert "Person" in result

    def test_does_not_duplicate(self, service: SuggestionService) -> None:
        """Does not add a duplicate label."""
        session = _make_session(entities_modified=json.dumps(["Person"]))
        service._update_entities_modified(session, "Person")
        result = json.loads(session.entities_modified)
        assert result == ["Person"]


# ---------------------------------------------------------------------------
# _get_git_ontology_path
# ---------------------------------------------------------------------------


class TestGetGitOntologyPath:
    def test_missing_github_path_does_not_fall_back_to_root(
        self, service: SuggestionService, mock_git: MagicMock
    ) -> None:
        project = _make_project()
        project.github_integration = MagicMock(
            turtle_file_path="src/domain.ttl", ontology_file_path="src/domain.owl"
        )
        mock_git.get_file_from_branch.side_effect = KeyError("src/domain.ttl")

        assert service._get_git_ontology_path(project) == "src/domain.ttl"
        mock_git.get_file_from_branch.assert_not_called()

    def test_default_path(self, service: SuggestionService) -> None:
        """Returns 'ontology.ttl' when project has no source_file_path."""
        project = _make_project()
        project.source_file_path = None
        assert service._get_git_ontology_path(project) == "ontology.ttl"

    def test_custom_path(self, service: SuggestionService) -> None:
        """Returns normalized path from project settings."""
        project = _make_project()
        project.source_file_path = "src/ontology.owl"
        assert service._get_git_ontology_path(project) == "src/ontology.owl"

    def test_rejects_path_traversal(self, service: SuggestionService) -> None:
        """Raises HTTPException for path traversal attempt."""
        project = _make_project()
        project.source_file_path = "../../etc/passwd"
        with pytest.raises(HTTPException) as exc_info:
            service._get_git_ontology_path(project)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _can_suggest / _get_user_role
# ---------------------------------------------------------------------------


class TestValidateSubmissionBaseline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "existing_paths",
        [["ontology.ttl"], ["domain/second.owl", "domain/first.ttl"]],
        ids=["one_file", "two_files"],
    )
    async def test_missing_baseline_reports_path_mismatch(
        self,
        service: SuggestionService,
        mock_git: MagicMock,
        caplog: pytest.LogCaptureFixture,
        existing_paths: list[str],
    ) -> None:
        mock_git.get_file_from_branch.side_effect = KeyError("missing.ttl")
        mock_git.get_repository.return_value.list_files.return_value = existing_paths

        with pytest.raises(HTTPException) as exc_info:
            await service._validate_submission_content(
                PROJECT_ID, "suggest/test", "missing.ttl", "", "test-user-id"
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {
            "message": "Ontology path is misconfigured for this project",
            "code": "ONTOLOGY_PATH_MISMATCH",
        }
        assert "resolved_path=missing.ttl" in caplog.text
        for path in existing_paths:
            assert path in caplog.text
        mock_git.get_repository.return_value.list_files.assert_called_once_with("main")

    @pytest.mark.asyncio
    async def test_no_ontology_files_allows_empty_baseline(
        self, service: SuggestionService, mock_git: MagicMock
    ) -> None:
        mock_git.get_file_from_branch.side_effect = KeyError("ontology.ttl")
        mock_git.get_repository.return_value.list_files.return_value = ["README.md"]

        await service._validate_submission_content(
            PROJECT_ID, "suggest/test", "ontology.ttl", "", "test-user-id"
        )


class TestCanSuggest:
    def test_editor_can_suggest(self, service: SuggestionService) -> None:
        """Editor role can suggest."""
        user = _make_user()
        assert service._can_suggest("editor", user) is True

    def test_viewer_cannot_suggest(self, service: SuggestionService) -> None:
        """Viewer role cannot suggest."""
        user = _make_user()
        assert service._can_suggest("viewer", user) is False

    def test_none_role_cannot_suggest(self, service: SuggestionService) -> None:
        """None role (non-member) cannot suggest."""
        user = _make_user()
        assert service._can_suggest(None, user) is False

    def test_superadmin_can_always_suggest(self, service: SuggestionService) -> None:
        """Superadmin bypasses role check."""
        user = _make_user()
        with patch.object(
            type(user), "is_superadmin", new_callable=lambda: property(lambda _s: True)
        ):
            assert service._can_suggest(None, user) is True

    @pytest.mark.parametrize("role", [None, "viewer"])
    @pytest.mark.parametrize("is_public", [False, True])
    def test_public_exception_only_for_nonmembers(
        self, service: SuggestionService, role: str | None, is_public: bool
    ) -> None:
        assert service._can_suggest(role, _make_user(), is_public=is_public) is (
            is_public and role is None
        )

    @pytest.mark.parametrize(
        "user", [CurrentUser(id="anonymous-123"), CurrentUser(id="guest", is_anonymous=True)]
    )
    def test_public_exception_requires_signed_in_user(
        self, service: SuggestionService, user: CurrentUser
    ) -> None:
        assert service._can_suggest(None, user, is_public=True) is False


class TestGetUserRole:
    def test_returns_role_for_member(self, service: SuggestionService) -> None:
        """Returns the role for a project member."""
        project = _make_project()
        user = _make_user()
        assert service._get_user_role(project, user) == "editor"

    def test_returns_none_for_non_member(self, service: SuggestionService) -> None:
        """Returns None for a non-member."""
        project = _make_project()
        user = _make_user(user_id="other-user")
        assert service._get_user_role(project, user) is None


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_member", [True, False], ids=["member", "nonmember"])
    async def test_creates_new_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        is_member: bool,
    ) -> None:
        """Members and signed-in public non-members can create a session."""
        project = _make_project()
        if not is_member:
            project.members = []

        # First execute: _get_project
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        # Second execute: check existing active session
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_project_result, mock_existing_result]

        def _simulate_refresh(obj: object, _attrs: list[str] | None = None) -> None:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()  # type: ignore[attr-defined]
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)  # type: ignore[attr-defined]

        mock_db.refresh.side_effect = _simulate_refresh

        user = _make_user()
        with patch("ontokit.services.suggestion_service.create_beacon_token", return_value="tok"):
            result = await service.create_session(PROJECT_ID, user)

        assert result.session_id is not None
        assert result.branch.startswith("suggest/")
        mock_git.create_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_existing_active_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Returns existing active session without creating a new one."""
        project = _make_project()
        existing = _make_session()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = existing

        mock_db.execute.side_effect = [mock_project_result, mock_existing_result]

        user = _make_user()
        result = await service.create_session(PROJECT_ID, user)
        assert result.session_id == existing.session_id

    @pytest.mark.asyncio
    async def test_forbidden_for_private_nonmember(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Private projects still require membership."""
        project = _make_project(is_public=False)
        # Make user not a member
        project.members = []

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_project_result

        user = _make_user(user_id="other-user")
        with pytest.raises(HTTPException) as exc_info:
            await service.create_session(PROJECT_ID, user)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    @pytest.mark.asyncio
    async def test_returns_user_sessions(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Lists sessions for the current user."""
        session = _make_session()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [session]
        mock_db.execute.return_value = mock_result

        user = _make_user()
        result = await service.list_sessions(PROJECT_ID, user)
        assert len(result.items) == 1
        assert result.items[0].session_id == session.session_id

    @pytest.mark.asyncio
    async def test_returns_empty_list(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Returns empty list when no sessions exist."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        user = _make_user()
        result = await service.list_sessions(PROJECT_ID, user)
        assert result.items == []


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------


class TestDiscard:
    @pytest.mark.asyncio
    async def test_discards_active_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Discards an active session and deletes the branch."""
        session = _make_session(status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()

        # _get_session, _verify_ownership (inline), _verify_project_access -> _get_project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]

        user = _make_user()
        await service.discard(PROJECT_ID, session.session_id, user)

        assert session.status == SuggestionSessionStatus.DISCARDED.value
        mock_git.delete_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_cannot_discard_submitted_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when trying to discard a submitted session."""
        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.discard(PROJECT_ID, session.session_id, user)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# auto_submit_stale_sessions
# ---------------------------------------------------------------------------


class TestAutoSubmitStaleSessions:
    @pytest.mark.asyncio
    async def test_no_stale_sessions(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Returns 0 when no stale sessions are found."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        count = await service.auto_submit_stale_sessions()
        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_already_claimed_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Skips sessions claimed by another worker (rowcount=0)."""
        stale_session = _make_session(
            changes_count=3,
            last_activity=datetime.now(UTC) - timedelta(hours=1),
        )

        mock_stale_result = MagicMock()
        mock_stale_result.scalars.return_value.all.return_value = [stale_session]

        mock_claim_result = MagicMock()
        mock_claim_result.rowcount = 0

        project = _make_project()
        project.members[0].user_id = stale_session.user_id
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [mock_stale_result, project_result, mock_claim_result]

        count = await service.auto_submit_stale_sessions()
        assert count == 0


# ---------------------------------------------------------------------------
# _verify_ownership
# ---------------------------------------------------------------------------


class TestVerifyOwnership:
    def test_owner_passes(self, service: SuggestionService) -> None:
        """No exception when user owns the session."""
        session = _make_session(user_id="test-user-id")
        user = _make_user(user_id="test-user-id")
        service._verify_ownership(session, user)  # should not raise

    def test_non_owner_raises(self, service: SuggestionService) -> None:
        """Raises 403 when user does not own the session."""
        session = _make_session(user_id="other-user")
        user = _make_user(user_id="test-user-id")
        with pytest.raises(HTTPException) as exc_info:
            service._verify_ownership(session, user)
        assert exc_info.value.status_code == 403

    def test_superadmin_bypasses(self, service: SuggestionService) -> None:
        """Superadmin can access any session."""
        session = _make_session(user_id="other-user")
        user = _make_user(user_id="admin-id")
        with patch.object(
            type(user), "is_superadmin", new_callable=lambda: property(lambda _s: True)
        ):
            service._verify_ownership(session, user)  # should not raise


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    @pytest.mark.asyncio
    async def test_builds_summary_without_pr(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """Builds a summary for a session without a linked PR."""
        session = _make_session(
            entities_modified=json.dumps(["Person"]),
            changes_count=2,
        )
        session.pr_id = None
        session.reviewer_id = None

        result = await service._build_summary(session)
        assert result.session_id == session.session_id
        assert result.entities_modified == ["Person"]
        assert result.changes_count == 2
        assert result.pr_url is None

    @pytest.mark.asyncio
    async def test_builds_summary_with_pr(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Builds a summary for a session with a linked PR."""
        pr_id = uuid.uuid4()
        session = _make_session(
            entities_modified=json.dumps(["Person"]),
            changes_count=1,
            pr_number=1,
            pr_id=pr_id,
        )
        session.reviewer_id = None

        mock_pr = MagicMock()
        mock_pr.github_pr_url = "https://github.com/org/repo/pull/1"

        mock_pr_result = MagicMock()
        mock_pr_result.scalar_one_or_none.return_value = mock_pr
        mock_db.execute.return_value = mock_pr_result

        result = await service._build_summary(session)
        assert result.pr_url == "https://github.com/org/repo/pull/1"

    @pytest.mark.asyncio
    async def test_builds_summary_with_reviewer(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,  # noqa: ARG002
    ) -> None:
        """Builds a summary that includes reviewer info."""
        session = _make_session(entities_modified=json.dumps(["Person"]))
        session.pr_id = None
        session.reviewer_id = "reviewer-id"
        session.reviewer_name = "Reviewer"
        session.reviewer_email = "reviewer@example.com"

        result = await service._build_summary(session)
        assert result.reviewer is not None
        assert result.reviewer.id == "reviewer-id"


# ---------------------------------------------------------------------------
# _get_project (line 71 – 404 branch)
# ---------------------------------------------------------------------------


class TestGetProject:
    @pytest.mark.asyncio
    async def test_project_not_found(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 404 when project does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await service._get_project(PROJECT_ID)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _get_session (line 122 – 404 branch)
# ---------------------------------------------------------------------------


class TestGetSession:
    @pytest.mark.asyncio
    async def test_session_not_found(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 404 when session does not exist."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await service._get_session(PROJECT_ID, "nonexistent")
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _verify_project_access (line 95 – 403 branch)
# ---------------------------------------------------------------------------


class TestVerifyProjectAccess:
    @pytest.mark.asyncio
    async def test_raises_403_when_no_permission(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 403 when user cannot suggest."""
        project = _make_project(is_public=False)
        project.members = []  # private projects require membership

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        user = _make_user(user_id="unknown-user")
        with pytest.raises(HTTPException) as exc_info:
            await service._verify_project_access(PROJECT_ID, user)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# _can_review / _verify_reviewer_access
# ---------------------------------------------------------------------------


class TestCanReview:
    def test_editor_can_review(self, service: SuggestionService) -> None:
        user = _make_user()
        assert service._can_review("editor", user) is True

    def test_viewer_cannot_review(self, service: SuggestionService) -> None:
        user = _make_user()
        assert service._can_review("viewer", user) is False

    def test_superadmin_can_review(self, service: SuggestionService) -> None:
        user = _make_user()
        with patch.object(
            type(user), "is_superadmin", new_callable=lambda: property(lambda _s: True)
        ):
            assert service._can_review(None, user) is True

    def test_suggester_cannot_review(self, service: SuggestionService) -> None:
        user = _make_user()
        assert service._can_review("suggester", user) is False


class TestVerifyReviewerAccess:
    @pytest.mark.asyncio
    async def test_raises_403_for_non_reviewer(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 403 when user lacks review permissions."""
        project = _make_project()
        project.members = []

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_result

        user = _make_user(user_id="unknown-user")
        with pytest.raises(HTTPException) as exc_info:
            await service._verify_reviewer_access(PROJECT_ID, user)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


class TestSave:
    @pytest.mark.asyncio
    async def test_save_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Saves content to the suggestion branch."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=0,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        # _get_session, _verify_project_access -> _get_project, save -> _get_project
        mock_db.execute.side_effect = [
            mock_session_result,
            mock_project_result,
            mock_project_result,
            _no_commit_identity_row(),  # U9 commit-identity preference lookup
        ]

        commit_info = MagicMock()
        commit_info.hash = "abc123"
        mock_git.commit_changes = MagicMock(return_value=commit_info)

        from ontokit.schemas.suggestion import SuggestionSaveRequest

        data = SuggestionSaveRequest(
            content="@prefix : <http://example.org/> .",
            entity_iri="http://example.org/Person",
            entity_label="Person",
        )

        user = _make_user()
        result = await service.save(PROJECT_ID, session.session_id, data, user)

        assert result.commit_hash == "abc123"
        assert result.branch == session.branch
        assert result.changes_count == 1
        mock_git.commit_changes.assert_called_once()
        service._enqueue_branch_refresh.assert_awaited_once_with(
            PROJECT_ID, session.branch, entity_iri=data.entity_iri
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turtle_path", ["src/domain.ttl", None])
    async def test_save_github_integration_path_preserves_unrelated_root(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        turtle_path: str | None,
    ) -> None:
        from ontokit.schemas.suggestion import SuggestionSaveRequest

        project = _make_project()
        project.source_file_path = "storage/project/ontology.ttl"
        project.github_integration = MagicMock(
            turtle_file_path=turtle_path, ontology_file_path="src/source.ttl"
        )
        filename = turtle_path or "src/source.ttl"
        session = _make_session()
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [
            session_result,
            project_result,
            project_result,
            _no_commit_identity_row(),
        ]
        files = {"ontology.ttl": b"# unrelated root ontology\n", filename: b""}
        mock_git.get_file_from_branch.side_effect = lambda _id, _branch, path: files[path]

        def commit_changes(**kwargs: object) -> MagicMock:
            content = kwargs["ontology_content"]
            assert isinstance(content, bytes)
            files[str(kwargs["filename"])] = content
            return MagicMock(hash="abc123")

        mock_git.commit_changes.side_effect = commit_changes
        data = SuggestionSaveRequest(
            content="@prefix : <http://example.org/> .",
            entity_iri="http://example.org/Person",
            entity_label="Person",
        )

        await service.save(PROJECT_ID, session.session_id, data, _make_user())

        assert mock_git.commit_changes.call_args.kwargs["filename"] == filename
        assert files[filename] == data.content.encode()
        assert files["ontology.ttl"] == b"# unrelated root ontology\n"

    @pytest.mark.asyncio
    async def test_save_non_active_session_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session is not active."""
        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]

        from ontokit.schemas.suggestion import SuggestionSaveRequest

        data = SuggestionSaveRequest(
            content="content",
            entity_iri="http://example.org/X",
            entity_label="X",
        )

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.save(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_save_git_failure_raises_500(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Raises 500 when git commit fails."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=0,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [
            mock_session_result,
            mock_project_result,
            mock_project_result,
            _no_commit_identity_row(),  # U9 commit-identity preference lookup
        ]

        mock_git.commit_changes = MagicMock(side_effect=RuntimeError("git error"))

        from ontokit.schemas.suggestion import SuggestionSaveRequest

        data = SuggestionSaveRequest(
            content="@prefix : <http://example.org/> .",
            entity_iri="http://example.org/X",
            entity_label="X",
        )

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.save(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_save_metadata_commit_failure_raises_500(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Raises 500 when DB metadata commit fails after git success."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=0,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [
            mock_session_result,
            mock_project_result,
            mock_project_result,
            _no_commit_identity_row(),  # U9 commit-identity preference lookup
        ]

        commit_info = MagicMock()
        commit_info.hash = "abc123"
        mock_git.commit_changes = MagicMock(return_value=commit_info)

        # Make db.commit fail
        mock_db.commit.side_effect = RuntimeError("DB error")

        from ontokit.schemas.suggestion import SuggestionSaveRequest

        data = SuggestionSaveRequest(
            content="@prefix : <http://example.org/> .",
            entity_iri="http://example.org/X",
            entity_label="X",
        )

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.save(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 500
        assert "metadata" in exc_info.value.detail


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


class TestSubmit:
    @pytest.mark.asyncio
    async def test_submit_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Submits a session by creating a PR."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=3,
            entities_modified=json.dumps(["Person", "Organization"]),
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        # For existing PR check (none found)
        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = _padded(
            mock_session_result,  # _get_session
            mock_project_result,  # _verify_project_access -> _get_project
            mock_no_pr_result,  # existing PR check
            mock_project_result,  # _get_project for notification
            project=project,
        )
        mock_git.get_default_branch = MagicMock(return_value="main")

        mock_pr_response = MagicMock()
        mock_pr_response.pr_number = 42
        mock_pr_response.id = uuid.uuid4()
        mock_pr_response.github_pr_url = "https://github.com/org/repo/pull/42"
        mock_pr_response.title = "Suggestion: Update Person, Organization"

        from ontokit.schemas.suggestion import SuggestionSubmitRequest

        data = SuggestionSubmitRequest(summary="My changes")
        user = _make_user()

        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
            patch("ontokit.services.suggestion_service.NotificationService") as mock_notif_cls,
        ):
            mock_pr_svc = AsyncMock()
            _configure_editor_pr_claim(mock_pr_svc, mock_pr_response, project)
            mock_pr_svc_factory.return_value = mock_pr_svc
            mock_notif = AsyncMock()
            mock_notif_cls.return_value = mock_notif

            result = await service.submit(PROJECT_ID, session.session_id, data, user)

        assert result.pr_number == 42
        assert result.status == "submitted"
        service._enqueue_branch_refresh.assert_awaited_once_with(
            PROJECT_ID, session.branch, full_embedding=True
        )

    @pytest.mark.asyncio
    async def test_submit_no_changes_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session has no changes."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=0,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]

        from ontokit.schemas.suggestion import SuggestionSubmitRequest

        data = SuggestionSubmitRequest(summary=None)
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            await service.submit(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400
        assert "No changes" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_submit_non_active_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session is not active."""
        session = _make_session(
            status=SuggestionSessionStatus.SUBMITTED.value,
            changes_count=5,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]

        from ontokit.schemas.suggestion import SuggestionSubmitRequest

        data = SuggestionSubmitRequest()
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            await service.submit(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_submit_existing_pr_idempotent(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Returns existing PR if branch already has one (idempotency)."""
        pr_id = uuid.uuid4()
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=2,
            entities_modified=json.dumps(["Person"]),
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        existing_pr = MagicMock()
        existing_pr.pr_number = 10
        existing_pr.id = pr_id
        existing_pr.github_pr_url = "https://github.com/org/repo/pull/10"

        mock_existing_pr_result = MagicMock()
        mock_existing_pr_result.scalar_one_or_none.return_value = existing_pr

        mock_db.execute.side_effect = _padded(
            mock_session_result,  # _get_session
            mock_project_result,  # _verify_project_access
            mock_existing_pr_result,  # existing PR check
            project=project,
        )
        from ontokit.schemas.suggestion import SuggestionSubmitRequest

        data = SuggestionSubmitRequest(summary="test")
        user = _make_user()

        result = await service.submit(PROJECT_ID, session.session_id, data, user)
        assert result.pr_number == 10
        assert result.status == "submitted"

    @pytest.mark.asyncio
    async def test_submit_reconciles_open_pr_created_after_precheck(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """A concurrent winner is returned instead of surfacing a spurious 409."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=2,
            entities_modified=json.dumps(["Person"]),
        )
        no_pr_result = MagicMock()
        no_pr_result.scalar_one_or_none.return_value = None
        raced_pr = MagicMock(
            id=uuid.uuid4(),
            pr_number=11,
            github_pr_url="https://github.com/org/repo/pull/11",
        )
        raced_pr_result = MagicMock()
        raced_pr_result.scalar_one_or_none.return_value = raced_pr
        project = _make_project()
        project.members[0].role = "suggester"
        project.members[0].is_trusted = True
        project.auto_accept_enabled = True
        project.auto_accept_quiet_days = 3
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [no_pr_result, raced_pr_result, project_result]
        mock_git.get_default_branch.return_value = "main"

        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service"
        ) as mock_pr_svc_factory:
            mock_pr_svc_factory.return_value._claim_pull_request_already_locked = AsyncMock(
                side_effect=HTTPException(
                    status_code=409,
                    detail="An open pull request already exists for this source branch",
                )
            )
            result = await service._create_pr_for_session(
                PROJECT_ID,
                session,
                _make_user(),
                "summary",
                SuggestionSessionStatus.SUBMITTED.value,
            )

        assert result.pr_number == 11
        assert result.pr_url == raced_pr.github_pr_url
        assert session.pr_id == raced_pr.id
        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("is_member", [True, False], ids=["member", "nonmember"])
    async def test_submit_fallback_to_direct_pr_on_403(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        is_member: bool,
    ) -> None:
        """Falls back to _create_pr_directly when PR service returns 403."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=2,
            entities_modified=json.dumps(["Person"]),
        )
        project = _make_project()
        if not is_member:
            project.members = []
        session.verification_passed = False

        from ontokit.schemas.trust import TrustTier
        from ontokit.services.trust_rate_limiter import TrustLimitDecision, TrustLimitStatus

        provider = MagicMock(enabled=False)

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None
        mock_no_direct_pr_result = MagicMock()
        mock_no_direct_pr_result.scalar_one_or_none.return_value = None
        # For _create_pr_directly: max pr_number query
        mock_max_result = MagicMock()
        mock_max_result.scalar.return_value = 5

        mock_db.execute.side_effect = _padded(
            mock_session_result,  # _get_session
            mock_project_result,  # _verify_project_access
            mock_no_pr_result,  # existing PR check
            mock_no_direct_pr_result,  # locked direct-creation check
            mock_max_result,  # max pr_number
            mock_project_result,  # _get_project for notification
            project=project,
        )
        mock_git.get_default_branch = MagicMock(return_value="main")

        # Make the PR service raise 403
        mock_direct_pr = MagicMock()
        mock_direct_pr.pr_number = 6
        mock_direct_pr.id = uuid.uuid4()
        mock_direct_pr.github_pr_url = None
        mock_direct_pr.title = "Suggestion: Update Person"

        from ontokit.schemas.suggestion import SuggestionSubmitRequest

        data = SuggestionSubmitRequest(summary="changes")
        user = _make_user()

        with (
            patch(
                "ontokit.services.suggestion_service.get_verification_provider",
                return_value=provider,
            ),
            patch(
                "ontokit.services.suggestion_service.check_and_consume",
                new=AsyncMock(return_value=TrustLimitDecision(TrustLimitStatus.ALLOWED, 2)),
            ) as limiter,
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
            patch("ontokit.services.suggestion_service.NotificationService") as mock_notif_cls,
        ):
            mock_pr_svc = AsyncMock()
            mock_pr_svc._claim_pull_request_already_locked = AsyncMock(
                side_effect=HTTPException(status_code=403, detail="Forbidden")
            )
            mock_pr_svc_factory.return_value = mock_pr_svc
            mock_notif = AsyncMock()
            mock_notif_cls.return_value = mock_notif

            # Mock _create_pr_directly to return a PR
            mock_db.flush = AsyncMock()
            mock_db.refresh = AsyncMock(
                side_effect=lambda obj: setattr(obj, "id", mock_direct_pr.id)
            )

            result = await service.submit(PROJECT_ID, session.session_id, data, user)

        assert result.pr_number == 6
        assert result.status == "submitted"

        if not is_member:
            assert service.trust.resolve_tier(project, user) is TrustTier.UNTRUSTED
            limiter.assert_awaited_once_with(None, str(PROJECT_ID), user.id)
            assert session.verification_passed is True
        else:
            limiter.assert_not_awaited()


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


class TestApprove:
    @pytest.mark.asyncio
    async def test_approve_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Approves a submitted session and merges the PR."""
        session = _make_session(
            user_id="contributor",
            status=SuggestionSessionStatus.SUBMITTED.value,
            pr_number=5,
        )
        project = _make_project()
        # Editor role for reviewer
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = _padded(
            mock_project_result, mock_session_result, project=project
        )

        user = _make_user()

        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service"
        ) as mock_pr_svc_factory:
            mock_pr_svc = AsyncMock()
            mock_pr_svc._merge_pull_request_for_suggestion = AsyncMock()
            mock_pr_svc._merge_pull_request_for_suggestion.return_value.merge_commit_hash = None
            mock_pr_svc_factory.return_value = mock_pr_svc

            await service.approve(PROJECT_ID, session.session_id, user)

        assert session.status == SuggestionSessionStatus.MERGED.value
        assert session.reviewer_id == user.id
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_approve_wrong_status_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session is not submitted."""
        session = _make_session(user_id="contributor", status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.approve(PROJECT_ID, session.session_id, user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_approve_auto_submitted_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Can approve an auto-submitted session."""
        session = _make_session(
            user_id="contributor",
            status=SuggestionSessionStatus.AUTO_SUBMITTED.value,
            pr_number=7,
        )
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = _padded(
            mock_project_result, mock_session_result, project=project
        )

        user = _make_user()

        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service"
        ) as mock_pr_svc_factory:
            mock_pr_svc = AsyncMock()
            mock_pr_svc._merge_pull_request_for_suggestion = AsyncMock()
            mock_pr_svc._merge_pull_request_for_suggestion.return_value.merge_commit_hash = None
            mock_pr_svc_factory.return_value = mock_pr_svc

            await service.approve(PROJECT_ID, session.session_id, user)

        assert session.status == SuggestionSessionStatus.MERGED.value

    @pytest.mark.asyncio
    async def test_approve_without_pr(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Approves a session that has no PR number (skips merge)."""
        session = _make_session(
            user_id="contributor",
            status=SuggestionSessionStatus.SUBMITTED.value,
            pr_number=None,
        )
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = _padded(
            mock_project_result, mock_session_result, project=project
        )

        user = _make_user()
        await service.approve(PROJECT_ID, session.session_id, user)

        assert session.status == SuggestionSessionStatus.MERGED.value

    @pytest.mark.asyncio
    async def test_approve_resumes_finalization_when_linked_pr_already_merged(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        from ontokit.models.pull_request import PRStatus

        pr_id = uuid.uuid4()
        session = _make_session(
            user_id="contributor",
            status=SuggestionSessionStatus.SUBMITTED.value,
            pr_number=5,
            pr_id=pr_id,
        )
        project = _make_project()
        project.members[0].role = "admin"
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        linked_pr = MagicMock()
        linked_pr.status = PRStatus.MERGED.value
        linked_pr.merge_commit_hash = None
        pr_result = MagicMock()
        pr_result.scalar_one_or_none.return_value = linked_pr
        mock_db.execute.side_effect = _padded(
            project_result, session_result, pr_result, project=project
        )

        with patch("ontokit.services.suggestion_service.get_pull_request_service") as pr_factory:
            await service.approve(PROJECT_ID, session.session_id, _make_user())

        pr_factory.assert_not_called()
        assert session.status == SuggestionSessionStatus.MERGED.value
        assert len(_outcomes(mock_db)) == 1

    @pytest.mark.asyncio
    async def test_approve_merge_failure_preserves_submitted_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Propagates a PR conflict without recording a terminal outcome."""
        session = _make_session(
            user_id="contributor",
            status=SuggestionSessionStatus.SUBMITTED.value,
            pr_number=5,
        )
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = _padded(
            mock_project_result, mock_session_result, project=project
        )

        user = _make_user()

        with patch(
            "ontokit.services.suggestion_service.get_pull_request_service"
        ) as mock_pr_svc_factory:
            mock_pr_svc = AsyncMock()
            mock_pr_svc._merge_pull_request_for_suggestion = AsyncMock(
                side_effect=HTTPException(status_code=409, detail="conflict")
            )
            mock_pr_svc_factory.return_value = mock_pr_svc

            with pytest.raises(HTTPException) as exc_info:
                await service.approve(PROJECT_ID, session.session_id, user)

        assert exc_info.value.status_code == 409
        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


class TestReject:
    @pytest.mark.asyncio
    async def test_reject_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Rejects a submitted session with a reason."""
        session = _make_session(
            user_id="submitter-1", status=SuggestionSessionStatus.SUBMITTED.value
        )
        session.user_name = "Submitter Account"
        session.user_email = "submitter@example.com"
        project = _make_project()
        project.members[0].role = "admin"
        submitter = MagicMock()
        submitter.user_id = "submitter-1"
        submitter.role = "suggester"
        submitter.is_trusted = True
        submitter.trust_override = "none"
        project.members.append(submitter)

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        from ontokit.schemas.suggestion import SuggestionRejectRequest

        data = SuggestionRejectRequest(reason="Not aligned with ontology design")
        user = _make_user()

        await service.reject(PROJECT_ID, session.session_id, data, user)

        assert session.status == SuggestionSessionStatus.REJECTED.value
        assert session.reviewer_feedback == "Not aligned with ontology design"
        assert session.reviewer_id == user.id
        outcomes = _outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].snapshot_tier == "trusted"
        assert outcomes[0].snapshot_role == "suggester"
        assert outcomes[0].submitter_name == "Submitter Account"
        assert outcomes[0].submitter_email == "submitter@example.com"
        assert outcomes[0].decided_by_name == "Test User"
        assert outcomes[0].snapshot_captured_at is not None

        # Covers AE2: later membership changes cannot rewrite the snapshot.
        submitter.role = "editor"
        assert outcomes[0].snapshot_role == "suggester"

    @pytest.mark.asyncio
    async def test_reject_wrong_status_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session is not submitted."""
        session = _make_session(status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        from ontokit.schemas.suggestion import SuggestionRejectRequest

        data = SuggestionRejectRequest(reason="Bad")
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            await service.reject(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_reject_auto_submitted_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Can reject an auto-submitted session."""
        session = _make_session(status=SuggestionSessionStatus.AUTO_SUBMITTED.value)
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        from ontokit.schemas.suggestion import SuggestionRejectRequest

        data = SuggestionRejectRequest(reason="Not needed")
        user = _make_user()

        await service.reject(PROJECT_ID, session.session_id, data, user)
        assert session.status == SuggestionSessionStatus.REJECTED.value


# ---------------------------------------------------------------------------
# dismiss / bulk review
# ---------------------------------------------------------------------------


class TestDismissAndBulkReview:
    async def test_dismiss_snapshots_anonymous_attribution(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Covers AE4 and the dismiss terminal path."""
        session = _make_session(
            user_id="anonymous-abc", status=SuggestionSessionStatus.SUBMITTED.value
        )
        session.is_anonymous = True
        session.submitter_name = "Anonymous Author"
        session.submitter_email = "author@example.com"
        project = _make_project()
        project.members[0].role = "admin"
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        mock_db.execute.side_effect = [project_result, session_result]

        await service.dismiss(PROJECT_ID, session.session_id, _make_user(), "spam")

        outcomes = _outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == "dismissed"
        assert outcomes[0].is_anonymous is True
        assert outcomes[0].snapshot_tier is None
        assert outcomes[0].snapshot_role is None
        assert outcomes[0].submitter_name == "Anonymous Author"
        assert outcomes[0].submitter_email == "author@example.com"
        assert outcomes[0].snapshot_captured_at is not None

    async def test_bulk_dismiss_funnels_through_snapshot_seam(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        from ontokit.schemas.suggestion import BulkReviewAction, BulkReviewRequest

        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)
        project = _make_project()
        project.members[0].role = "admin"
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        # bulk_review verifies once and passes the loaded project through the
        # unchecked dismiss path so snapshot capture does not add an N+1 query.
        mock_db.execute.side_effect = [project_result, session_result]
        data = BulkReviewRequest(
            session_ids=[session.session_id],
            action=BulkReviewAction.DISMISS,
            note="bulk triage",
        )

        response = await service.bulk_review(PROJECT_ID, data, _make_user())

        assert response.succeeded == [session.session_id]
        assert response.failed == []
        outcomes = _outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].snapshot_tier == "reviewer"
        assert outcomes[0].snapshot_role == "admin"
        assert outcomes[0].snapshot_captured_at is not None

    async def test_bulk_accept_reuses_the_authorized_project(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        from ontokit.schemas.suggestion import BulkReviewAction, BulkReviewRequest

        project = _make_project()
        project.members[0].role = "admin"
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = project_result
        user = _make_user()
        data = BulkReviewRequest(
            session_ids=["session-1"],
            action=BulkReviewAction.ACCEPT,
        )

        with patch.object(
            service, "_approve_unchecked", new_callable=AsyncMock
        ) as approve_unchecked:
            response = await service.bulk_review(PROJECT_ID, data, user)

        assert response.succeeded == ["session-1"]
        assert response.failed == []
        approve_unchecked.assert_awaited_once_with("session-1", user, project)


# ---------------------------------------------------------------------------
# request_changes
# ---------------------------------------------------------------------------


class TestRequestChanges:
    @pytest.mark.asyncio
    async def test_request_changes_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Requests changes on a submitted session."""
        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        from ontokit.schemas.suggestion import SuggestionRequestChangesRequest

        data = SuggestionRequestChangesRequest(feedback="Please fix the label")
        user = _make_user()

        await service.request_changes(PROJECT_ID, session.session_id, data, user)

        assert session.status == SuggestionSessionStatus.CHANGES_REQUESTED.value
        assert session.reviewer_feedback == "Please fix the label"
        assert session.reviewer_id == user.id
        assert _outcomes(mock_db) == []

    @pytest.mark.asyncio
    async def test_request_changes_wrong_status_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 400 when session is not in submitted state."""
        session = _make_session(status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session

        mock_db.execute.side_effect = [mock_project_result, mock_session_result]

        from ontokit.schemas.suggestion import SuggestionRequestChangesRequest

        data = SuggestionRequestChangesRequest(feedback="Fix it")
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            await service.request_changes(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# resubmit
# ---------------------------------------------------------------------------


class TestResubmit:
    @pytest.mark.asyncio
    async def test_resubmit_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Resubmits a session after changes were requested."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=1,
            pr_id=uuid.uuid4(),
            pr_number=10,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        pr = MagicMock(status="open", github_pr_url=None)
        pr_result = MagicMock()
        pr_result.scalar_one_or_none.return_value = pr
        mock_db.execute.side_effect = _padded(
            mock_session_result,
            mock_session_result,
            mock_project_result,
            pr_result,
            project=project,
        )

        from ontokit.schemas.suggestion import SuggestionResubmitRequest

        data = SuggestionResubmitRequest(summary="Fixed the labels")
        user = _make_user()

        session.reviewer_feedback = "Please fix the labels"
        session.reviewed_at = datetime.now(UTC)
        with (
            patch.object(service, "_validate_submission_content", new=AsyncMock()) as validate,
            patch(
                "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
                new=AsyncMock(),
            ) as duplicate_check,
        ):
            result = await service.resubmit(PROJECT_ID, session.session_id, data, user)
        validate.assert_awaited_once()
        duplicate_check.assert_not_awaited()

        assert result.pr_number == 10
        assert result.status == "submitted"
        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        assert session.revision == 2
        assert session.summary == "Fixed the labels"
        assert session.reviewer_feedback is None
        assert session.reviewed_at is None

    @pytest.mark.asyncio
    async def test_resubmit_wrong_status_raises_400(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """A changes-requested session must be reopened before resubmission."""
        session = _make_session(
            status=SuggestionSessionStatus.CHANGES_REQUESTED.value,
            changes_count=1,
            pr_id=uuid.uuid4(),
            pr_number=10,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        pr_result = MagicMock()
        pr_result.scalar_one_or_none.return_value = MagicMock(status="open")
        mock_db.execute.side_effect = [
            mock_session_result,
            mock_session_result,
            mock_project_result,
            pr_result,
        ]

        from ontokit.schemas.suggestion import SuggestionResubmitRequest

        data = SuggestionResubmitRequest(summary="try again")
        user = _make_user()

        with pytest.raises(HTTPException) as exc_info:
            await service.resubmit(PROJECT_ID, session.session_id, data, user)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Session is changes-requested, cannot submit"
        mock_db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# beacon_save
# ---------------------------------------------------------------------------


class TestBeaconSave:
    @pytest.mark.asyncio
    async def test_beacon_save_success(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Beacon save commits content to the suggestion branch."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=1,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        # _get_session, _verify_project_access -> _get_project, _get_project for filename
        mock_db.execute.side_effect = [
            mock_session_result,
            mock_project_result,
            mock_project_result,
            _no_commit_identity_row(),  # U9 commit-identity preference lookup
        ]

        mock_git.commit_changes = MagicMock()

        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(
            session_id=session.session_id,
            content="@prefix : <http://example.org/> .",
        )

        with patch(
            "ontokit.services.suggestion_service.verify_beacon_token",
            return_value=session.session_id,
        ):
            await service.beacon_save(PROJECT_ID, data, "valid-token")

        assert session.changes_count == 2
        mock_git.commit_changes.assert_called_once()

    @pytest.mark.asyncio
    async def test_beacon_save_derives_and_blocks_untrusted_minting(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        session = _make_session(status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()
        project.members[0].role = "suggester"
        project.members[0].is_trusted = False
        project.members[0].trust_override = "none"
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [
            session_result,
            project_result,
            project_result,
            _no_commit_identity_row(),
        ]
        mock_git.get_file_from_branch.return_value = b"@prefix : <http://example.org/> ."

        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(
            session_id=session.session_id,
            content=(
                "@prefix : <http://example.org/> .\n"
                "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                ":NewClass a owl:Class ."
            ),
        )
        with (
            patch(
                "ontokit.services.suggestion_service.verify_beacon_token",
                return_value=session.session_id,
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await service.beacon_save(PROJECT_ID, data, "valid-token")

        assert exc.value.status_code == 403
        mock_git.commit_changes.assert_not_called()

    @pytest.mark.asyncio
    async def test_beacon_save_invalid_token_raises_401(
        self,
        service: SuggestionService,
    ) -> None:
        """Raises 401 when beacon token is invalid."""
        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(session_id="s_abc12345", content="data")

        with patch(
            "ontokit.services.suggestion_service.verify_beacon_token",
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await service.beacon_save(PROJECT_ID, data, "bad-token")
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_beacon_save_token_mismatch_raises_403(
        self,
        service: SuggestionService,
    ) -> None:
        """Raises 403 when token session_id does not match data."""
        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(session_id="s_abc12345", content="data")

        with patch(
            "ontokit.services.suggestion_service.verify_beacon_token",
            return_value="s_other_session",
        ):
            with pytest.raises(HTTPException) as exc_info:
                await service.beacon_save(PROJECT_ID, data, "token")
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_beacon_save_non_active_silently_returns(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Silently returns when session is not active."""
        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_db.execute.return_value = mock_session_result

        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(session_id=session.session_id, content="data")

        with patch(
            "ontokit.services.suggestion_service.verify_beacon_token",
            return_value=session.session_id,
        ):
            await service.beacon_save(PROJECT_ID, data, "token")

        mock_git.commit_changes.assert_not_called()

    @pytest.mark.asyncio
    async def test_beacon_save_git_failure_silently_returns(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Beacon save is fire-and-forget: git failures are swallowed."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=1,
        )
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [
            mock_session_result,
            mock_project_result,
            mock_project_result,
            _no_commit_identity_row(),  # U9 commit-identity preference lookup
        ]

        mock_git.commit_changes = MagicMock(side_effect=RuntimeError("disk full"))

        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        data = SuggestionBeaconRequest(
            session_id=session.session_id, content="@prefix : <http://example.org/> ."
        )

        with patch(
            "ontokit.services.suggestion_service.verify_beacon_token",
            return_value=session.session_id,
        ):
            # Should not raise
            await service.beacon_save(PROJECT_ID, data, "token")

        # changes_count should NOT have been incremented
        assert session.changes_count == 1


# ---------------------------------------------------------------------------
# anonymous write budgets
# ---------------------------------------------------------------------------


class TestAnonymousWriteBudgets:
    @pytest.mark.asyncio
    async def test_beacon_silently_refuses_an_exhausted_anonymous_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _make_session(is_anonymous=True, changes_count=2)
        project = _make_project()
        service._get_project = AsyncMock(return_value=project)  # type: ignore[method-assign]
        service.commit_identity.resolve = AsyncMock(return_value=("Anonymous", "anon@example"))
        monkeypatch.setattr("ontokit.services.suggestion_service.MAX_ANONYMOUS_SESSION_COMMITS", 2)

        from ontokit.schemas.suggestion import SuggestionBeaconRequest

        await service._beacon_flush(
            PROJECT_ID,
            session,
            SuggestionBeaconRequest(
                session_id=session.session_id,
                content="@prefix : <http://example.org/> .",
            ),
        )

        mock_git.commit_changes.assert_not_called()
        mock_db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_rejects_the_commit_limit_before_git(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _make_session(is_anonymous=True, changes_count=2)
        project = _make_project()
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [session_result, project_result]
        monkeypatch.setattr("ontokit.services.suggestion_service.MAX_ANONYMOUS_SESSION_COMMITS", 2)

        from ontokit.schemas.suggestion import SuggestionSaveRequest

        data = SuggestionSaveRequest(
            content="@prefix : <http://example.org/> .",
            entity_iri="http://example.org/Foo",
            entity_label="Foo",
        )
        with pytest.raises(HTTPException) as exc:
            await service.save_anonymous(PROJECT_ID, session.session_id, data, session.session_id)

        assert exc.value.status_code == 429
        mock_git.commit_changes.assert_not_called()

    @pytest.mark.asyncio
    async def test_repeated_saves_account_for_committed_utf8_bytes(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = _make_session(is_anonymous=True)
        project = _make_project()
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = session
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [
            session_result,
            project_result,
            session_result,
            project_result,
            session_result,
            project_result,
        ]
        commit = MagicMock(hash="abc123")
        mock_git.commit_changes.return_value = commit
        from ontokit.schemas.suggestion import SuggestionSaveRequest

        first_content = '@prefix : <http://example.org/> .\n:s :p "éé" .'
        second_content = '@prefix : <http://example.org/> .\n:s :p "ééé" .'
        first_size = len(first_content.encode("utf-8"))
        second_size = len(second_content.encode("utf-8"))
        monkeypatch.setattr(
            "ontokit.services.suggestion_service.MAX_ANONYMOUS_SESSION_BYTES",
            first_size + second_size - 1,
        )

        first = SuggestionSaveRequest(
            content=first_content,
            entity_iri="http://example.org/Foo",
            entity_label="Foo",
        )
        second = SuggestionSaveRequest(
            content=second_content,
            entity_iri="http://example.org/Foo",
            entity_label="Foo",
        )

        await service.save_anonymous(PROJECT_ID, session.session_id, first, session.session_id)
        with pytest.raises(HTTPException) as exc:
            await service.save_anonymous(PROJECT_ID, session.session_id, second, session.session_id)

        assert exc.value.status_code == 413
        assert session.anonymous_content_bytes == first_size
        assert session.changes_count == 1
        assert mock_git.commit_changes.call_count == 1


# ---------------------------------------------------------------------------
# auto_submit_stale_sessions (extended)
# ---------------------------------------------------------------------------


class TestAutoSubmitStaleSessionsExtended:
    @pytest.mark.asyncio
    async def test_auto_submits_stale_session(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Auto-submits a stale session by creating a PR."""
        stale_session = _make_session(
            changes_count=3,
            last_activity=datetime.now(UTC) - timedelta(hours=1),
            entities_modified=json.dumps(["Person"]),
        )
        project = _make_project()
        # Need user_id to match project member for access check
        project.members[0].user_id = stale_session.user_id

        mock_stale_result = MagicMock()
        mock_stale_result.scalars.return_value.all.return_value = [stale_session]

        mock_claim_result = MagicMock()
        mock_claim_result.rowcount = 1

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = _padded(
            mock_stale_result,  # select stale sessions
            mock_project_result,  # _verify_project_access -> _get_project
            mock_claim_result,  # claim session UPDATE
            mock_no_pr_result,  # existing PR check
            mock_project_result,  # _get_project for notification
            project=project,
        )
        mock_git.get_default_branch = MagicMock(return_value="main")

        mock_pr_response = MagicMock()
        mock_pr_response.pr_number = 99
        mock_pr_response.id = uuid.uuid4()
        mock_pr_response.github_pr_url = None
        mock_pr_response.title = "Suggestion: Update Person"

        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
            patch("ontokit.services.suggestion_service.NotificationService") as mock_notif_cls,
        ):
            mock_pr_svc = AsyncMock()
            _configure_editor_pr_claim(mock_pr_svc, mock_pr_response, project)
            mock_pr_svc_factory.return_value = mock_pr_svc
            mock_notif = AsyncMock()
            mock_notif_cls.return_value = mock_notif

            count = await service.auto_submit_stale_sessions()

        assert count == 1

    @pytest.mark.asyncio
    async def test_auto_submit_discards_session_on_access_loss(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Discards session when user lost project access."""
        stale_session = _make_session(
            changes_count=2,
            last_activity=datetime.now(UTC) - timedelta(hours=1),
        )

        mock_stale_result = MagicMock()
        mock_stale_result.scalars.return_value.all.return_value = [stale_session]

        mock_claim_result = MagicMock()
        mock_claim_result.rowcount = 1

        # _verify_project_access -> private project with no matching member
        project = _make_project(is_public=False)
        project.members = []
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_stale_result, mock_project_result]

        count = await service.auto_submit_stale_sessions()
        assert count == 0
        assert stale_session.status == SuggestionSessionStatus.DISCARDED.value

    @pytest.mark.asyncio
    async def test_auto_submit_reverts_on_pr_failure(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Reverts session to ACTIVE when PR creation fails."""
        stale_session = _make_session(
            changes_count=2,
            last_activity=datetime.now(UTC) - timedelta(hours=1),
            entities_modified=json.dumps(["Person"]),
        )
        project = _make_project()
        project.members[0].user_id = stale_session.user_id

        mock_stale_result = MagicMock()
        mock_stale_result.scalars.return_value.all.return_value = [stale_session]

        mock_claim_result = MagicMock()
        mock_claim_result.rowcount = 1

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [
            mock_stale_result,
            mock_project_result,  # _verify_project_access
            mock_claim_result,
            mock_no_pr_result,  # existing PR check
        ]

        mock_git.get_default_branch = MagicMock(return_value="main")

        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
        ):
            mock_pr_svc = AsyncMock()
            mock_pr_svc._claim_pull_request_already_locked = AsyncMock(
                side_effect=RuntimeError("PR creation failed")
            )
            mock_pr_svc_factory.return_value = mock_pr_svc

            count = await service.auto_submit_stale_sessions()

        assert count == 0
        assert stale_session.status == SuggestionSessionStatus.ACTIVE.value

    @pytest.mark.asyncio
    async def test_stale_untrusted_session_waits_for_interactive_submit(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        stale_session = _make_session(
            changes_count=2,
            last_activity=datetime.now(UTC) - timedelta(hours=1),
        )
        project = _make_project()
        project.members[0].user_id = stale_session.user_id
        project.members[0].role = "suggester"
        project.members[0].is_trusted = False
        project.members[0].trust_override = "none"
        stale_result = MagicMock()
        stale_result.scalars.return_value.all.return_value = [stale_session]
        project_result = MagicMock()
        project_result.scalar_one_or_none.return_value = project
        mock_db.execute.side_effect = [stale_result, project_result]

        assert await service.auto_submit_stale_sessions() == 0
        assert stale_session.status == SuggestionSessionStatus.ACTIVE.value
        assert mock_db.execute.await_count == 2


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


class TestListPending:
    @pytest.mark.asyncio
    async def test_list_pending_sessions(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Lists pending sessions for reviewers."""
        session = _make_session(status=SuggestionSessionStatus.SUBMITTED.value)
        session.reviewer_id = None
        project = _make_project()
        project.members[0].role = "admin"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_sessions_result = MagicMock()
        mock_sessions_result.scalars.return_value.all.return_value = [session]

        mock_db.execute.side_effect = _padded(
            mock_project_result, mock_sessions_result, project=project
        )

        user = _make_user()
        result = await service.list_pending(PROJECT_ID, user)
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_list_pending_forbidden_for_viewer(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
    ) -> None:
        """Raises 403 when viewer tries to list pending sessions."""
        project = _make_project()
        project.members[0].role = "viewer"

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_db.execute.return_value = mock_project_result

        user = _make_user()
        with pytest.raises(HTTPException) as exc_info:
            await service.list_pending(PROJECT_ID, user)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# create_session – additional edge cases (lines 193-261)
# ---------------------------------------------------------------------------


class TestCreateSessionEdgeCases:
    @pytest.mark.asyncio
    async def test_create_branch_failure_raises_500(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Raises 500 when git branch creation fails."""
        project = _make_project()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_project_result, mock_existing_result]
        mock_git.create_branch.side_effect = RuntimeError("git error")

        user = _make_user()
        with (
            patch(
                "ontokit.services.suggestion_service.create_beacon_token",
                return_value="tok",
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await service.create_session(PROJECT_ID, user)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_integrity_error_returns_existing(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Returns existing session after IntegrityError (race condition)."""
        from sqlalchemy.exc import IntegrityError

        project = _make_project()
        existing = _make_session()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        # After rollback, re-query finds the existing session
        mock_refetch_result = MagicMock()
        mock_refetch_result.scalar_one_or_none.return_value = existing

        mock_db.execute.side_effect = [
            mock_project_result,
            mock_existing_result,
            mock_refetch_result,
        ]
        mock_db.commit.side_effect = IntegrityError("dup", {}, Exception())

        user = _make_user()
        with patch(
            "ontokit.services.suggestion_service.create_beacon_token",
            return_value="tok",
        ):
            result = await service.create_session(PROJECT_ID, user)

        assert result.session_id == existing.session_id
        mock_git.delete_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_integrity_error_no_existing_raises_500(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,  # noqa: ARG002
    ) -> None:
        """Raises 500 after IntegrityError when no existing session found."""
        from sqlalchemy.exc import IntegrityError

        project = _make_project()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        # After rollback, re-query finds nothing
        mock_refetch_result = MagicMock()
        mock_refetch_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [
            mock_project_result,
            mock_existing_result,
            mock_refetch_result,
        ]
        mock_db.commit.side_effect = IntegrityError("dup", {}, Exception())

        user = _make_user()
        with (
            patch(
                "ontokit.services.suggestion_service.create_beacon_token",
                return_value="tok",
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await service.create_session(PROJECT_ID, user)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_generic_exception_cleans_up_branch(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Cleans up branch and re-raises on generic commit exception."""
        project = _make_project()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        mock_db.execute.side_effect = [mock_project_result, mock_existing_result]
        mock_db.commit.side_effect = RuntimeError("unexpected")

        user = _make_user()
        with (
            patch(
                "ontokit.services.suggestion_service.create_beacon_token",
                return_value="tok",
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            await service.create_session(PROJECT_ID, user)

        mock_git.delete_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_refetches(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,  # noqa: ARG002
    ) -> None:
        """Re-fetches session from DB when refresh fails after commit."""
        project = _make_project()
        db_session_obj = _make_session()

        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project
        mock_existing_result = MagicMock()
        mock_existing_result.scalar_one_or_none.return_value = None

        # After refresh failure, re-fetch returns the session
        mock_refetch_result = MagicMock()
        mock_refetch_result.scalar_one.return_value = db_session_obj

        mock_db.execute.side_effect = [
            mock_project_result,
            mock_existing_result,
            mock_refetch_result,
        ]

        # commit succeeds, refresh fails
        commit_call_count = 0
        original_commit = AsyncMock()

        async def commit_side_effect() -> None:
            nonlocal commit_call_count
            commit_call_count += 1
            await original_commit()

        mock_db.commit.side_effect = commit_side_effect
        mock_db.refresh.side_effect = RuntimeError("refresh failed")

        user = _make_user()
        with patch(
            "ontokit.services.suggestion_service.create_beacon_token",
            return_value="tok",
        ):
            result = await service.create_session(PROJECT_ID, user)

        assert result.session_id == db_session_obj.session_id


# ---------------------------------------------------------------------------
# _create_pr_for_session – title truncation / many entities
# ---------------------------------------------------------------------------


class TestCreatePrForSession:
    @pytest.mark.asyncio
    async def test_title_with_more_than_5_entities(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Title shows first 5 entities and a '+N more' suffix."""
        entities = [f"Entity{i}" for i in range(8)]
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=8,
            entities_modified=json.dumps(entities),
        )
        project = _make_project()

        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = _padded(
            mock_no_pr_result, mock_project_result, project=project
        )

        mock_git.get_default_branch = MagicMock(return_value="main")

        mock_pr_response = MagicMock()
        mock_pr_response.pr_number = 1
        mock_pr_response.id = uuid.uuid4()
        mock_pr_response.github_pr_url = None
        mock_pr_response.title = "Suggestion"

        user = _make_user()

        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
            patch("ontokit.services.suggestion_service.NotificationService") as mock_notif_cls,
        ):
            mock_pr_svc = AsyncMock()
            _configure_editor_pr_claim(mock_pr_svc, mock_pr_response, project)
            mock_pr_svc_factory.return_value = mock_pr_svc
            mock_notif = AsyncMock()
            mock_notif_cls.return_value = mock_notif

            await service._create_pr_for_session(PROJECT_ID, session, user, "summary", "submitted")

        # Verify the PR was created with the right title structure
        service._enqueue_branch_refresh.assert_awaited_once_with(
            PROJECT_ID, session.branch, full_embedding=True
        )

        call_args = mock_pr_svc._claim_pull_request_already_locked.call_args
        pr_create_arg = call_args[0][1]  # second positional arg
        assert "(+3 more)" in pr_create_arg.title

    @pytest.mark.asyncio
    async def test_empty_entities_title(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Title is just 'Suggestion' when no entities are modified."""
        session = _make_session(
            status=SuggestionSessionStatus.ACTIVE.value,
            changes_count=1,
            entities_modified=json.dumps([]),
        )
        project = _make_project()

        mock_no_pr_result = MagicMock()
        mock_no_pr_result.scalar_one_or_none.return_value = None
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = _padded(
            mock_no_pr_result, mock_project_result, project=project
        )

        mock_git.get_default_branch = MagicMock(return_value="main")

        mock_pr_response = MagicMock()
        mock_pr_response.pr_number = 1
        mock_pr_response.id = uuid.uuid4()
        mock_pr_response.github_pr_url = None
        mock_pr_response.title = "Suggestion"

        user = _make_user()

        with (
            patch(
                "ontokit.services.suggestion_service.get_pull_request_service"
            ) as mock_pr_svc_factory,
            patch("ontokit.services.suggestion_service.NotificationService") as mock_notif_cls,
        ):
            mock_pr_svc = AsyncMock()
            _configure_editor_pr_claim(mock_pr_svc, mock_pr_response, project)
            mock_pr_svc_factory.return_value = mock_pr_svc
            mock_notif = AsyncMock()
            mock_notif_cls.return_value = mock_notif

            await service._create_pr_for_session(PROJECT_ID, session, user, None, "submitted")

        service._enqueue_branch_refresh.assert_awaited_once_with(
            PROJECT_ID, session.branch, full_embedding=True
        )

        call_args = mock_pr_svc._claim_pull_request_already_locked.call_args
        pr_create_arg = call_args[0][1]
        assert pr_create_arg.title == "Suggestion"


# ---------------------------------------------------------------------------
# discard – branch deletion failure (line 752-753)
# ---------------------------------------------------------------------------


class TestDiscardEdgeCases:
    @pytest.mark.asyncio
    async def test_discard_continues_on_branch_delete_failure(
        self,
        service: SuggestionService,
        mock_db: AsyncMock,
        mock_git: MagicMock,
    ) -> None:
        """Still marks session discarded even if branch deletion fails."""
        session = _make_session(status=SuggestionSessionStatus.ACTIVE.value)
        project = _make_project()

        mock_session_result = MagicMock()
        mock_session_result.scalar_one_or_none.return_value = session
        mock_project_result = MagicMock()
        mock_project_result.scalar_one_or_none.return_value = project

        mock_db.execute.side_effect = [mock_session_result, mock_project_result]
        mock_git.delete_branch.side_effect = RuntimeError("branch not found")

        user = _make_user()
        await service.discard(PROJECT_ID, session.session_id, user)

        assert session.status == SuggestionSessionStatus.DISCARDED.value


# ---------------------------------------------------------------------------
# get_suggestion_service factory (line 900)
# ---------------------------------------------------------------------------


class TestGetSuggestionServiceFactory:
    def test_returns_service_instance(self) -> None:
        """Factory returns a SuggestionService instance."""
        from ontokit.services.suggestion_service import get_suggestion_service

        mock_db = AsyncMock()
        svc = get_suggestion_service(mock_db)
        assert isinstance(svc, SuggestionService)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_type", "status_code"),
    [(EmbeddingBudgetExceeded, 402), (EmbeddingPricingUnavailable, 503)],
)
async def test_submit_embedding_refusal_precedes_submission_effects(
    service: SuggestionService,
    mock_db: AsyncMock,
    mock_git: MagicMock,
    failure_type: type[RuntimeError],
    status_code: int,
) -> None:
    from ontokit.schemas.suggestion import SuggestionSubmitRequest

    session = _make_session(changes_count=1)
    project = _make_project()
    baseline = b"@prefix ex: <http://example.org/> ."
    proposed = baseline + (
        b" ex:New a <http://www.w3.org/2002/07/owl#Class>; "
        b'<http://www.w3.org/2000/01/rdf-schema#label> "New" .'
    )
    mock_git.get_file_from_branch.side_effect = lambda _project_id, branch, _filename: (
        baseline if branch == "main" else proposed
    )
    failure = failure_type("refused")
    with (
        patch.object(service, "_get_session", new=AsyncMock(return_value=session)),
        patch.object(service, "_verify_project_access", new=AsyncMock(return_value=project)),
        patch.object(service, "_consume_untrusted_submission", new=AsyncMock()) as allowance,
        patch.object(service, "_create_pr_for_session_already_locked", new=AsyncMock()) as claim,
        patch.object(service, "_bind_session_to_pr", new=AsyncMock()) as bind,
        patch.object(service, "_finalize_pr_for_session", new=AsyncMock()) as finalize,
        patch("ontokit.services.suggestion_service.NotificationService") as notifications,
        patch(
            "ontokit.services.duplicate_check_service.DuplicateCheckService.check",
            new=AsyncMock(side_effect=failure),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await service.submit(
            PROJECT_ID,
            session.session_id,
            SuggestionSubmitRequest(summary="Saved change"),
            _make_user(),
        )
    assert exc.value.status_code == status_code
    assert exc.value.__cause__ is failure
    for effect in (
        allowance,
        claim,
        bind,
        finalize,
        service._enqueue_branch_refresh,
        mock_db.commit,
        mock_db.rollback,
    ):
        effect.assert_not_awaited()
    notifications.assert_not_called()
    assert session.status == SuggestionSessionStatus.ACTIVE.value
    assert session.pr_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [EmbeddingBudgetExceeded, EmbeddingPricingUnavailable])
async def test_stale_submit_embedding_refusal_restores_active(
    service: SuggestionService,
    mock_db: AsyncMock,
    mock_git: MagicMock,
    failure_type: type[RuntimeError],
) -> None:
    session = _make_session(changes_count=1, last_activity=datetime.now(UTC) - timedelta(hours=1))
    project = _make_project()
    stale = MagicMock()
    stale.scalars.return_value.all.return_value = [session]
    claimed = MagicMock(rowcount=1)
    no_pr = MagicMock()
    no_pr.scalar_one_or_none.return_value = None
    mock_db.execute.side_effect = [stale, claimed, no_pr]
    baseline = b"@prefix ex: <http://example.org/> ."
    proposed = baseline + (
        b" ex:New a <http://www.w3.org/2002/07/owl#Class>; "
        b'<http://www.w3.org/2000/01/rdf-schema#label> "New" .'
    )
    mock_git.get_file_from_branch.side_effect = lambda _project_id, branch, _filename: (
        baseline if branch == "main" else proposed
    )

    async def reflect_claim(_session: object) -> None:
        session.status = SuggestionSessionStatus.AUTO_SUBMITTED.value

    mock_db.refresh.side_effect = reflect_claim
    check = AsyncMock(side_effect=failure_type("refused"))
    with (
        patch.object(service, "_verify_project_access", new=AsyncMock(return_value=project)),
        patch.object(service, "_get_project", new=AsyncMock(return_value=project)),
        patch("ontokit.services.duplicate_check_service.DuplicateCheckService.check", new=check),
        patch("ontokit.services.suggestion_service.get_pull_request_service") as pr_factory,
        patch.object(service, "_finalize_pr_for_session", new=AsyncMock()) as finalize,
    ):
        assert await service.auto_submit_stale_sessions() == 0
    check.assert_awaited_once()
    assert session.status == SuggestionSessionStatus.ACTIVE.value
    mock_db.rollback.assert_awaited_once()
    assert mock_db.commit.await_count == 2  # Durable claim, then durable restoration.
    pr_factory.assert_not_called()
    finalize.assert_not_awaited()
    service._enqueue_branch_refresh.assert_not_awaited()


@pytest.fixture
def lifecycle(service: SuggestionService, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock infrastructure while exercising the lifecycle transitions themselves."""
    ctx = MagicMock()
    ctx.session = _make_session(
        status="submitted", changes_count=1, pr_number=12, pr_id=uuid.uuid4()
    )
    ctx.project = _make_project()
    ctx.user = _make_user()
    ctx.reviewer = _make_user(user_id="reviewer")
    reviewer_member = MagicMock(user_id="reviewer", role="owner")
    ctx.project.members.append(reviewer_member)
    ctx.pr = MagicMock(id=ctx.session.pr_id, pr_number=12, status="open", github_pr_url=None)
    ctx.pr_service = AsyncMock()
    ctx.pr_service._merge_pull_request_for_suggestion.return_value.merge_commit_hash = None
    ctx.notifications = AsyncMock()
    monkeypatch.setattr(
        "ontokit.services.suggestion_service.create_beacon_token", lambda _: "fresh-token"
    )
    monkeypatch.setattr(service, "_get_session", AsyncMock(return_value=ctx.session))
    monkeypatch.setattr(service, "_get_project", AsyncMock(return_value=ctx.project))
    monkeypatch.setattr(service, "_record_terminal_outcome", AsyncMock())
    monkeypatch.setattr(service, "_schedule_auto_accept", AsyncMock())
    monkeypatch.setattr(
        service.commit_identity, "resolve", AsyncMock(return_value=("User", "alias"))
    )
    monkeypatch.setattr(
        "ontokit.services.suggestion_service.get_pull_request_service", lambda _: ctx.pr_service
    )
    monkeypatch.setattr(
        "ontokit.services.suggestion_service.NotificationService", lambda _: ctx.notifications
    )
    service.db.execute.return_value = MagicMock()  # type: ignore[attr-defined]
    service.db.execute.return_value.scalar_one_or_none.return_value = ctx.pr  # type: ignore[attr-defined]
    return ctx


@pytest.mark.asyncio
async def test_lifecycle_save_requires_reopen(
    service: SuggestionService, lifecycle: MagicMock
) -> None:
    from ontokit.schemas.suggestion import SuggestionSaveRequest

    session = lifecycle.session
    session.status = "changes-requested"
    data = SuggestionSaveRequest(content="", entity_iri="urn:class", entity_label="Class")
    with pytest.raises(HTTPException) as exc:
        await service.save(PROJECT_ID, session.session_id, data, lifecycle.user)
    assert exc.value.status_code == 400
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = lifecycle.pr
    service.db.execute.side_effect = chain([no_active], repeat(linked))
    await service.reopen(PROJECT_ID, session.session_id, lifecycle.user)
    service.git_service.commit_changes.return_value.hash = "saved"
    result = await service.save(PROJECT_ID, session.session_id, data, lifecycle.user)
    assert result.branch == session.branch
    assert result.commit_hash == "saved"


@pytest.mark.asyncio
async def test_lifecycle_resubmit_active(service: SuggestionService, lifecycle: MagicMock) -> None:
    from ontokit.schemas.suggestion import SuggestionResubmitRequest

    session = lifecycle.session
    session.status = "active"
    result = await service.resubmit(
        PROJECT_ID, session.session_id, SuggestionResubmitRequest(summary="Revised"), lifecycle.user
    )
    assert result.pr_number == 12
    assert session.revision == 2
    lifecycle.notifications.notify_project_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifecycle_reject_closes_pr(service: SuggestionService, lifecycle: MagicMock) -> None:
    from ontokit.schemas.suggestion import SuggestionRejectRequest

    await service.reject(
        PROJECT_ID,
        lifecycle.session.session_id,
        SuggestionRejectRequest(reason="No"),
        lifecycle.reviewer,
    )
    lifecycle.pr_service._close_pull_request_for_suggestion.assert_awaited_once_with(
        PROJECT_ID, 12, lifecycle.reviewer
    )
    assert lifecycle.session.status == "rejected"


@pytest.mark.asyncio
async def test_lifecycle_self_approval_refused(
    service: SuggestionService, lifecycle: MagicMock
) -> None:
    with pytest.raises(HTTPException) as exc:
        await service.approve(PROJECT_ID, lifecycle.session.session_id, lifecycle.user)
    assert exc.value.status_code == 403
    assert "own suggestion" in exc.value.detail
    assert lifecycle.project.members[0].role == "editor"
    assert lifecycle.session.status == "submitted"
    lifecycle.pr_service.merge_pull_request.assert_not_awaited()
    lifecycle.pr_service._merge_pull_request_for_suggestion.assert_not_awaited()
    service.git_service.merge_branch.assert_not_called()
    lifecycle.notifications.create_notification.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.fixture
def reviewer_merge(
    service: SuggestionService, lifecycle: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> MagicMock:
    """Exercise both real services, including their authorization and merge gates."""
    from ontokit.services.pull_request_service import PullRequestService

    lifecycle.project.members[-1].role = "editor"
    lifecycle.project.pr_approval_required = 0
    lifecycle.pr.author_id = lifecycle.session.user_id
    lifecycle.pr.source_branch = lifecycle.session.branch
    lifecycle.pr.target_branch = "main"
    lifecycle.pr.reviews = []
    lifecycle.pr.github_pr_number = None
    lifecycle.pr_service = PullRequestService(service.db, service.git_service)
    monkeypatch.setattr(
        SuggestionService, "_get_project", AsyncMock(return_value=lifecycle.project)
    )
    monkeypatch.setattr(
        lifecycle.pr_service, "_get_project", AsyncMock(return_value=lifecycle.project)
    )
    monkeypatch.setattr(lifecycle.pr_service, "_get_pr", AsyncMock(return_value=lifecycle.pr))
    monkeypatch.setattr(lifecycle.pr_service, "_sync_pull_request_to_github", AsyncMock())
    monkeypatch.setattr(
        "ontokit.services.pull_request_service.NotificationService",
        lambda _: lifecycle.notifications,
    )
    service.git_service.list_branches.return_value = []
    service.git_service.merge_branch.return_value = MagicMock(success=True, merge_commit_hash=None)
    return lifecycle


@pytest.mark.asyncio
async def test_editor_approve_merges_deletes_branch_and_notifies(
    service: SuggestionService, reviewer_merge: MagicMock
) -> None:
    ctx = reviewer_merge
    await service.approve(PROJECT_ID, ctx.session.session_id, ctx.reviewer)

    assert ctx.session.status == "merged"
    assert ctx.session.reviewer_id == ctx.reviewer.id
    assert ctx.pr.status == "merged"
    assert ctx.pr.merged_by == ctx.reviewer.id
    service.git_service.merge_branch.assert_called_once_with(
        project_id=PROJECT_ID,
        source=ctx.session.branch,
        target="main",
        message=f"Merge suggestion: {ctx.session.session_id}",
        author_name=ctx.reviewer.name,
        author_email=ctx.reviewer.email,
    )
    service.git_service.delete_branch.assert_called_once_with(PROJECT_ID, ctx.session.branch)
    decisions = [
        call.kwargs
        for call in ctx.notifications.create_notification.await_args_list
        if call.kwargs["notification_type"] == "suggestion_approved"
    ]
    assert len(decisions) == 1
    assert decisions[0]["user_id"] == ctx.session.user_id
    service._record_terminal_outcome.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("required_approvals", [1, 2])
async def test_editor_approve_requires_recorded_pr_approvals(
    service: SuggestionService, reviewer_merge: MagicMock, required_approvals: int
) -> None:
    ctx = reviewer_merge
    ctx.project.pr_approval_required = required_approvals
    with pytest.raises(HTTPException) as exc:
        await service.approve(PROJECT_ID, ctx.session.session_id, ctx.reviewer)

    assert exc.value.status_code == 400
    assert exc.value.detail == (f"Pull request requires {required_approvals} approvals, but has 0")
    assert ctx.session.status == "submitted"
    assert ctx.session.reviewer_id is None
    assert ctx.pr.status == "open"
    service.git_service.merge_branch.assert_not_called()
    service.git_service.delete_branch.assert_not_called()
    service._record_terminal_outcome.assert_not_awaited()
    ctx.notifications.create_notification.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "request_changes"])
async def test_lifecycle_decision_notification(
    service: SuggestionService,
    lifecycle: MagicMock,
    action: str,
) -> None:
    from ontokit.schemas.suggestion import SuggestionRejectRequest, SuggestionRequestChangesRequest

    session = lifecycle.session
    if action == "approve":
        await service.approve(PROJECT_ID, session.session_id, lifecycle.reviewer)
        expected = "suggestion_approved"
    elif action == "reject":
        await service.reject(
            PROJECT_ID, session.session_id, SuggestionRejectRequest(reason="No"), lifecycle.reviewer
        )
        expected = "suggestion_rejected"
    else:
        await service.request_changes(
            PROJECT_ID,
            session.session_id,
            SuggestionRequestChangesRequest(feedback="Revise"),
            lifecycle.reviewer,
        )
        expected = "suggestion_changes_requested"
    lifecycle.notifications.create_notification.assert_awaited_once()
    notification = lifecycle.notifications.create_notification.call_args.kwargs
    assert notification["user_id"] == session.user_id
    assert notification["notification_type"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "submitted", "merged", "rejected", "discarded"])
async def test_lifecycle_reopen_wrong_state(
    service: SuggestionService,
    lifecycle: MagicMock,
    state: str,
) -> None:
    lifecycle.session.status = state
    with pytest.raises(HTTPException) as exc:
        await service.reopen(PROJECT_ID, lifecycle.session.session_id, lifecycle.user)
    assert exc.value.status_code == 400
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_reopen_owner_only(
    service: SuggestionService, lifecycle: MagicMock
) -> None:
    lifecycle.session.status = "changes-requested"
    for user in (lifecycle.reviewer, CurrentUser(id="superadmin", roles=["superadmin"])):
        with pytest.raises(HTTPException) as exc:
            await service.reopen(PROJECT_ID, lifecycle.session.session_id, user)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_lifecycle_reopen_conflict(service: SuggestionService, lifecycle: MagicMock) -> None:
    session = lifecycle.session
    session.status = "changes-requested"
    active = _make_session(session_id="other-active")
    service.db.execute.return_value.scalar_one_or_none.return_value = active
    with pytest.raises(HTTPException) as exc:
        await service.reopen(PROJECT_ID, session.session_id, lifecycle.user)
    assert exc.value.status_code == 409
    assert session.status == "changes-requested"
    result = await service.create_session(PROJECT_ID, lifecycle.user)
    assert result.session_id == active.session_id
    service.git_service.create_branch.assert_not_called()


@pytest.mark.asyncio
async def test_lifecycle_reopen_fresh_token_and_activity(
    service: SuggestionService,
    lifecycle: MagicMock,
) -> None:
    session = lifecycle.session
    session.status = "changes-requested"
    previous = session.last_activity = datetime.now(UTC) - timedelta(hours=3)
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = lifecycle.pr
    service.db.execute.side_effect = chain([no_active], repeat(linked))
    result = await service.reopen(PROJECT_ID, session.session_id, lifecycle.user)
    assert result.beacon_token == "fresh-token"
    assert session.beacon_token == result.beacon_token
    assert session.last_activity > previous
    assert session.status == "active"


@pytest.mark.asyncio
async def test_lifecycle_reopen_concurrent_conflict(
    service: SuggestionService,
    lifecycle: MagicMock,
) -> None:
    from sqlalchemy.exc import IntegrityError

    lifecycle.session.status = "changes-requested"
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = lifecycle.pr
    service.db.execute.side_effect = chain([no_active], repeat(linked))
    service.db.commit.side_effect = IntegrityError("duplicate active", {}, Exception())
    with pytest.raises(HTTPException) as exc:
        await service.reopen(PROJECT_ID, lifecycle.session.session_id, lifecycle.user)
    assert exc.value.status_code == 409
    service.db.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit", "resubmit"])
@pytest.mark.parametrize("valid", [True, False])
async def test_lifecycle_revision_submission_gates(
    service: SuggestionService,
    lifecycle: MagicMock,
    method: str,
    valid: bool,
) -> None:
    from ontokit.schemas.suggestion import SuggestionResubmitRequest, SuggestionSubmitRequest

    session = lifecycle.session
    session.status = "active"
    service.git_service.get_file_from_branch.return_value = b"" if valid else b"invalid turtle !!!"
    data = (SuggestionSubmitRequest if method == "submit" else SuggestionResubmitRequest)(
        summary="Fix"
    )
    call = getattr(service, method)(PROJECT_ID, session.session_id, data, lifecycle.user)
    if valid:
        result = await call
        assert result.pr_number == 12
        assert session.pr_id == lifecycle.pr.id
        assert session.revision == 2
        lifecycle.notifications.notify_project_roles.assert_awaited_once()
        assert (
            "resubmitted" in lifecycle.notifications.notify_project_roles.call_args.kwargs["title"]
        )
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 422
        assert session.status == "active"
        assert session.revision == 1
        lifecycle.notifications.notify_project_roles.assert_not_awaited()
        service.db.commit.assert_not_awaited()
    lifecycle.pr_service._claim_pull_request_already_locked.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_resubmit_without_pr(
    service: SuggestionService, lifecycle: MagicMock
) -> None:
    from ontokit.schemas.suggestion import SuggestionResubmitRequest

    lifecycle.session.status = "active"
    lifecycle.session.pr_id = lifecycle.session.pr_number = None
    with pytest.raises(HTTPException) as exc:
        await service.resubmit(
            PROJECT_ID, lifecycle.session.session_id, SuggestionResubmitRequest(), lifecycle.user
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_lifecycle_stale_revision(service: SuggestionService, lifecycle: MagicMock) -> None:
    session = lifecycle.session
    session.status = "active"
    session.last_activity = datetime.now(UTC) - timedelta(hours=1)
    stale = MagicMock()
    stale.scalars.return_value.all.return_value = [session]
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = lifecycle.pr
    service.db.execute.side_effect = [stale, linked]
    assert await service.auto_submit_stale_sessions() == 1
    assert session.revision == 2
    assert session.pr_number == 12
    assert session.status == "auto-submitted"
    lifecycle.notifications.notify_project_roles.assert_awaited_once()
    lifecycle.pr_service._claim_pull_request_already_locked.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_discard_revision(service: SuggestionService, lifecycle: MagicMock) -> None:
    lifecycle.session.status = "active"
    await service.discard(PROJECT_ID, lifecycle.session.session_id, lifecycle.user)
    lifecycle.pr_service._close_pull_request_for_discard_already_locked.assert_awaited_once_with(
        PROJECT_ID, 12, lifecycle.session.branch
    )
    service.git_service.delete_branch.assert_called_once_with(
        PROJECT_ID, lifecycle.session.branch, force=True
    )
    assert lifecycle.session.status == "discarded"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "editor"])
async def test_lifecycle_reject_refused_close(
    service: SuggestionService,
    lifecycle: MagicMock,
    role: str,
) -> None:
    from ontokit.schemas.suggestion import SuggestionRejectRequest

    lifecycle.project.members[-1].role = role
    lifecycle.pr_service._close_pull_request_for_suggestion.side_effect = HTTPException(
        400, "Merged"
    )
    with pytest.raises(HTTPException) as exc:
        await service.reject(
            PROJECT_ID,
            lifecycle.session.session_id,
            SuggestionRejectRequest(reason="No"),
            lifecycle.reviewer,
        )
    assert exc.value.status_code == 400
    assert lifecycle.session.status == "submitted"
    service._record_terminal_outcome.assert_not_awaited()
    lifecycle.notifications.create_notification.assert_not_awaited()
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifecycle_bulk_self_approval(
    service: SuggestionService, lifecycle: MagicMock
) -> None:
    from ontokit.schemas.suggestion import BulkReviewRequest

    result = await service.bulk_review(
        PROJECT_ID,
        BulkReviewRequest(session_ids=[lifecycle.session.session_id], action="accept"),
        lifecycle.user,
    )
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert "own suggestion" in result.failed[0].reason
    lifecycle.pr_service._merge_pull_request_for_suggestion.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "request_changes", "dismiss"])
@pytest.mark.parametrize("anonymous", [True, False])
async def test_lifecycle_silent_decisions(
    service: SuggestionService,
    lifecycle: MagicMock,
    action: str,
    anonymous: bool,
) -> None:
    from ontokit.schemas.suggestion import SuggestionRejectRequest, SuggestionRequestChangesRequest

    lifecycle.session.is_anonymous = anonymous
    args = [PROJECT_ID, lifecycle.session.session_id]
    if action == "reject":
        args.append(SuggestionRejectRequest(reason="No"))
    if action == "request_changes":
        args.append(SuggestionRequestChangesRequest(feedback="Revise"))
    args.append(lifecycle.reviewer)
    await getattr(service, action)(*args)
    if anonymous or action == "dismiss":
        lifecycle.notifications.create_notification.assert_not_awaited()
    else:
        lifecycle.notifications.create_notification.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "request_changes"])
@pytest.mark.parametrize("state", ["active", "merged"])
async def test_lifecycle_review_wrong_state(
    service: SuggestionService,
    lifecycle: MagicMock,
    action: str,
    state: str,
) -> None:
    from ontokit.schemas.suggestion import SuggestionRejectRequest, SuggestionRequestChangesRequest

    lifecycle.session.status = state
    args = [PROJECT_ID, lifecycle.session.session_id]
    if action == "reject":
        args.append(SuggestionRejectRequest(reason="No"))
    if action == "request_changes":
        args.append(SuggestionRequestChangesRequest(feedback="Revise"))
    args.append(lifecycle.reviewer)
    with pytest.raises(HTTPException) as exc:
        await getattr(service, action)(*args)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("changed_field", ["status", "last_activity"])
async def test_stale_revision_skip_releases_database_locks(
    service: SuggestionService,
    lifecycle: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    """A revision edited/submitted while waiting must release its transaction locks."""
    session = lifecycle.session
    session.status = "active"
    session.last_activity = datetime.now(UTC) - timedelta(hours=1)
    stale = MagicMock()
    stale.scalars.return_value.all.return_value = [session]
    service.db.execute.return_value = stale

    async def refresh(_session: object) -> None:
        if changed_field == "status":
            session.status = "submitted"
        else:
            session.last_activity = datetime.now(UTC)

    service.db.refresh.side_effect = refresh

    unfinished_transactions = []

    @asynccontextmanager
    async def locked(*_args: object) -> AsyncIterator[None]:
        yield
        if service.db.commit.await_count + service.db.rollback.await_count == 0:
            unfinished_transactions.append(session.session_id)

    monkeypatch.setattr("ontokit.services.suggestion_service.pull_request_write_locks", locked)
    assert await service.auto_submit_stale_sessions() == 0
    assert unfinished_transactions == []
    lifecycle.notifications.notify_project_roles.assert_not_awaited()
    assert session.revision == 1


async def test_bulk_self_approval_does_not_poison_following_item(
    service: SuggestionService,
    lifecycle: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback expires the shared project; later items must still be reviewable."""
    from unittest.mock import PropertyMock

    from ontokit.schemas.suggestion import BulkReviewRequest

    other = _make_session(
        session_id="s_other", user_id="other-contributor", status="submitted", pr_number=13
    )
    service._get_session.side_effect = [lifecycle.session, other]
    expired = False

    def project_id() -> uuid.UUID:
        if expired:
            raise RuntimeError("Expired ORM attribute requires an asynchronous refresh")
        return PROJECT_ID

    async def rollback() -> None:
        nonlocal expired
        expired = True

    async def refresh(project: object) -> None:
        nonlocal expired
        assert project is lifecycle.project
        expired = False

    monkeypatch.setattr(
        type(lifecycle.project), "id", PropertyMock(side_effect=project_id), raising=False
    )
    service.db.rollback.side_effect = rollback
    service.db.refresh.side_effect = refresh
    result = await service.bulk_review(
        PROJECT_ID,
        BulkReviewRequest(
            session_ids=[lifecycle.session.session_id, other.session_id], action="accept"
        ),
        lifecycle.user,
    )
    assert result.succeeded == [other.session_id]
    assert [failure.session_id for failure in result.failed] == [lifecycle.session.session_id]
    assert "own suggestion" in result.failed[0].reason
    lifecycle.pr_service._merge_pull_request_for_suggestion.assert_awaited_once()


@pytest.mark.parametrize(
    ("limit_status", "status_code", "reason"),
    [
        ("exhausted", 429, "daily_limit_reached"),
        ("unavailable", 503, "submission_limiter_unavailable"),
    ],
)
async def test_public_nonmember_submit_enforces_limiter(
    service: SuggestionService,
    mock_db: AsyncMock,
    limit_status: str,
    status_code: int,
    reason: str,
) -> None:
    from ontokit.schemas.suggestion import SuggestionSubmitRequest
    from ontokit.services.trust_rate_limiter import TrustLimitDecision, TrustLimitStatus

    project = _make_project()
    project.members = []
    session = _make_session(changes_count=1)
    session.verification_passed = True
    service._get_project = AsyncMock(return_value=project)
    service._get_session = AsyncMock(return_value=session)
    redis = AsyncMock()
    with (
        patch(
            "ontokit.services.suggestion_service.check_and_consume",
            new=AsyncMock(return_value=TrustLimitDecision(TrustLimitStatus(limit_status), 0)),
        ) as limiter,
        pytest.raises(HTTPException) as exc,
    ):
        await service.submit(
            PROJECT_ID, session.session_id, SuggestionSubmitRequest(), _make_user(), redis=redis
        )
    assert exc.value.status_code == status_code
    assert exc.value.detail["reason"] == reason
    limiter.assert_awaited_once_with(redis, str(PROJECT_ID), session.user_id)
    assert session.status == "active"
    assert session.pr_id is None
    mock_db.add.assert_not_called()
    mock_db.commit.assert_not_awaited()


@pytest.mark.parametrize("action", ["save", "submit"])
async def test_private_nonmember_cannot_continue_session(
    service: SuggestionService, mock_db: AsyncMock, action: str
) -> None:
    from ontokit.schemas.suggestion import SuggestionSaveRequest, SuggestionSubmitRequest

    project = _make_project(is_public=False)
    project.members = []
    session = _make_session(changes_count=1)
    service._get_project = AsyncMock(return_value=project)
    service._get_session = AsyncMock(return_value=session)
    with pytest.raises(HTTPException) as exc:
        if action == "save":
            await service.save(
                PROJECT_ID,
                session.session_id,
                SuggestionSaveRequest(content="", entity_iri="http://x#A", entity_label="A"),
                _make_user(),
            )
        else:
            await service.submit(
                PROJECT_ID, session.session_id, SuggestionSubmitRequest(), _make_user()
            )
    assert exc.value.status_code == 403
    service.git_service.commit_changes.assert_not_called()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_status", ["closed", "merged", None])
@pytest.mark.parametrize("automatic", [False, True])
async def test_revision_refuses_settled_or_missing_pr(
    service: SuggestionService, lifecycle: MagicMock, pr_status: str | None, automatic: bool
) -> None:
    from ontokit.schemas.suggestion import SuggestionResubmitRequest

    ctx = lifecycle
    ctx.session.status = "active"
    ctx.pr.status = pr_status
    service.db.execute.return_value.scalar_one_or_none.return_value = ctx.pr if pr_status else None
    with pytest.raises(HTTPException) as exc:
        if automatic:
            await service._resubmit_already_locked(
                ctx.project, ctx.session, ctx.user, None, "auto-submitted"
            )
        else:
            await service.resubmit(
                PROJECT_ID, ctx.session.session_id, SuggestionResubmitRequest(), ctx.user
            )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Suggestion pull request is not open"
    assert ctx.session.revision == 1
    assert ctx.session.status == "active"
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_status", ["closed", "merged", None])
async def test_reopen_refuses_settled_or_missing_pr(
    service: SuggestionService, lifecycle: MagicMock, pr_status: str | None
) -> None:
    ctx = lifecycle
    ctx.session.status = "changes-requested"
    ctx.pr.status = pr_status
    active = MagicMock()
    active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = ctx.pr if pr_status else None
    service.db.execute.side_effect = [active, linked]
    with pytest.raises(HTTPException) as exc:
        await service.reopen(PROJECT_ID, ctx.session.session_id, ctx.user)
    assert exc.value.status_code == 409
    assert exc.value.detail == "Suggestion pull request is not open"
    assert ctx.session.status == "changes-requested"
    service.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_status", ["open", "closed", "merged"])
@pytest.mark.parametrize("lost_access", [False, True])
async def test_discard_revision_with_settled_pr_or_lost_access(
    service: SuggestionService,
    reviewer_merge: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    pr_status: str,
    lost_access: bool,
) -> None:
    ctx = reviewer_merge
    ctx.session.status = "active"
    ctx.session.last_activity = datetime.now(UTC) - timedelta(hours=1)
    ctx.pr.status = pr_status
    locked = False

    @asynccontextmanager
    async def lock(*_args: object) -> AsyncIterator[None]:
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    async def commit() -> None:
        assert locked
        assert ctx.pr.status == ("closed" if pr_status == "open" else pr_status)
        assert ctx.session.status == "discarded"

    monkeypatch.setattr("ontokit.services.suggestion_service.branch_write_lock", lock)
    service.db.commit.side_effect = commit
    if lost_access:
        monkeypatch.setattr(
            service,
            "_verify_project_access",
            AsyncMock(side_effect=HTTPException(status_code=403, detail="Access lost")),
        )
        service.db.execute.return_value.scalars.return_value.all.return_value = [ctx.session]
        assert await service.auto_submit_stale_sessions() == 0
    else:
        await service.discard(PROJECT_ID, ctx.session.session_id, ctx.user)
    assert ctx.session.status == "discarded"
    assert ctx.pr.status == ("closed" if pr_status == "open" else pr_status)
    service.db.commit.assert_awaited_once()
    service.git_service.delete_branch.assert_called_once_with(
        PROJECT_ID, ctx.session.branch, force=True
    )


@pytest.mark.asyncio
async def test_revision_requires_fresh_owner_approval(
    service: SuggestionService, reviewer_merge: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontokit.models.pull_request import PullRequestReview
    from ontokit.schemas.pull_request import ReviewCreate
    from ontokit.schemas.suggestion import (
        SuggestionRequestChangesRequest,
        SuggestionResubmitRequest,
        SuggestionSaveRequest,
    )

    ctx = reviewer_merge
    ctx.project.pr_approval_required = 1
    owner = _make_user(user_id="owner")
    ctx.project.members.append(MagicMock(user_id=owner.id, role="owner"))
    monkeypatch.setattr(ctx.pr_service, "_to_review_response", MagicMock())
    await ctx.pr_service.create_review(
        PROJECT_ID, 12, ReviewCreate(status="approved", body="Reviewed revision 1"), owner
    )
    old_approval = next(
        call.args[0]
        for call in service.db.add.call_args_list
        if isinstance(call.args[0], PullRequestReview)
    )
    objection = MagicMock(reviewer_id="reviewer", status="changes_requested")
    ctx.pr.reviews = [old_approval, objection]
    await service.request_changes(
        PROJECT_ID,
        ctx.session.session_id,
        SuggestionRequestChangesRequest(feedback="Fix label"),
        ctx.reviewer,
    )
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = ctx.pr
    service.db.execute.side_effect = [no_active, linked]
    await service.reopen(PROJECT_ID, ctx.session.session_id, ctx.user)
    assert ctx.session.changes_count == 0
    service.db.execute.side_effect = None
    service.db.execute.return_value = linked
    service.git_service.commit_changes.return_value.hash = "revised"
    await service.save(
        PROJECT_ID,
        ctx.session.session_id,
        SuggestionSaveRequest(content="", entity_iri="urn:class", entity_label="Fixed"),
        ctx.user,
    )
    assert ctx.session.changes_count == 1
    await service.resubmit(
        PROJECT_ID, ctx.session.session_id, SuggestionResubmitRequest(), ctx.user
    )
    service.db.commit.reset_mock()
    with pytest.raises(HTTPException) as exc:
        await service.approve(PROJECT_ID, ctx.session.session_id, ctx.reviewer)
    assert exc.value.detail == "Pull request requires 1 approvals, but has 0"
    assert old_approval.status == "commented"
    assert old_approval.body == "Reviewed revision 1"
    assert objection.status == "changes_requested"
    assert len(ctx.pr.reviews) == 2
    service.db.commit.assert_not_awaited()
    service.git_service.merge_branch.assert_not_called()
    service.db.add.reset_mock()
    await ctx.pr_service.create_review(
        PROJECT_ID, 12, ReviewCreate(status="approved", body="Reviewed revision 2"), owner
    )
    fresh_approval = next(
        call.args[0]
        for call in service.db.add.call_args_list
        if isinstance(call.args[0], PullRequestReview)
    )
    ctx.pr.reviews.append(fresh_approval)
    await service.approve(PROJECT_ID, ctx.session.session_id, ctx.reviewer)
    assert ctx.session.status == "merged"
    service.git_service.merge_branch.assert_called_once()


@pytest.mark.asyncio
async def test_direct_pr_close_refuses_revision_but_allows_discard(
    service: SuggestionService, reviewer_merge: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ontokit.schemas.suggestion import SuggestionResubmitRequest

    ctx = reviewer_merge
    ctx.session.status = "changes-requested"
    owner = _make_user(user_id="owner")
    ctx.project.members.append(MagicMock(user_id=owner.id, role="owner"))
    monkeypatch.setattr(ctx.pr_service, "_to_pr_response", AsyncMock())
    await ctx.pr_service.close_pull_request(PROJECT_ID, 12, owner)
    assert ctx.pr.status == "closed"
    no_active = MagicMock()
    no_active.scalar_one_or_none.return_value = None
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = ctx.pr
    service.db.execute.side_effect = [no_active, linked]
    service.db.commit.reset_mock()
    with pytest.raises(HTTPException) as exc:
        await service.reopen(PROJECT_ID, ctx.session.session_id, ctx.user)
    assert exc.value.status_code == 409
    assert exc.value.detail == "Suggestion pull request is not open"
    service.db.commit.assert_not_awaited()

    # The direct close can also happen after reopen: that active draft has an exit.
    ctx.session.status = "active"
    service.db.execute.side_effect = None
    service.db.execute.return_value = linked
    with pytest.raises(HTTPException) as exc:
        await service.resubmit(
            PROJECT_ID, ctx.session.session_id, SuggestionResubmitRequest(), ctx.user
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Suggestion pull request is not open"
    assert ctx.session.revision == 1
    service.db.commit.assert_not_awaited()
    await service.discard(PROJECT_ID, ctx.session.session_id, ctx.user)
    assert ctx.session.status == "discarded"
    assert ctx.pr.status == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_status", ["closed", "merged", None])
async def test_stale_revision_rolls_back_when_pr_not_open(
    service: SuggestionService, lifecycle: MagicMock, pr_status: str | None
) -> None:
    ctx = lifecycle
    ctx.session.status = "active"
    ctx.session.last_activity = datetime.now(UTC) - timedelta(hours=1)
    ctx.pr.status = pr_status
    stale = MagicMock()
    stale.scalars.return_value.all.return_value = [ctx.session]
    linked = MagicMock()
    linked.scalar_one_or_none.return_value = ctx.pr if pr_status else None
    service.db.execute.side_effect = [stale, linked]
    assert await service.auto_submit_stale_sessions() == 0
    assert ctx.session.revision == 1
    assert ctx.session.status == "active"
    service.db.commit.assert_not_awaited()
    service.db.rollback.assert_awaited_once()
    ctx.notifications.notify_project_roles.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["submitted", "auto-submitted"])
async def test_revision_dismisses_prior_approval_history(
    service: SuggestionService, lifecycle: MagicMock, new_status: str
) -> None:
    ctx = lifecycle
    ctx.session.status = "active"
    approval = MagicMock(status="approved", body="Original review")
    objection = MagicMock(status="changes_requested")
    ctx.pr.reviews = [approval, objection]
    await service._resubmit_already_locked(ctx.project, ctx.session, ctx.user, None, new_status)
    assert ctx.pr.reviews == [approval, objection]
    assert approval.status == "commented"
    assert approval.body == "Original review"
    assert objection.status == "changes_requested"
    assert ctx.session.revision == 2
