"""Tests for the auto-accept quiet-period clock (U7).

The clock is the mechanism by which unreviewed content can reach the canonical
ontology, so these tests are mostly about when it must NOT start.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, repeat
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from ontokit.core.auth import CurrentUser
from ontokit.models.suggestion_session import SuggestionSessionStatus
from ontokit.schemas.suggestion import SuggestionResubmitRequest
from ontokit.services.suggestion_service import SuggestionService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _user(user_id: str = "contributor-1") -> CurrentUser:
    return CurrentUser(id=user_id, email="c@example.com", name="C", username="c")


def _member(user_id: str, role: str = "suggester", *, is_trusted: bool = False) -> MagicMock:
    member = MagicMock()
    member.user_id = user_id
    member.role = role
    member.is_trusted = is_trusted
    member.trust_override = "none"
    return member


def _project(
    members: list[MagicMock] | None = None,
    *,
    auto_accept_enabled: bool = True,
    quiet_days: int = 7,
) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_ID
    project.name = "P"
    project.is_public = True
    project.members = members if members is not None else []
    project.auto_accept_enabled = auto_accept_enabled
    project.auto_accept_quiet_days = quiet_days
    project.trust_promotion_threshold = 5
    return project


def _session(
    *,
    user_id: str = "contributor-1",
    is_anonymous: bool = False,
    is_llm_generated: bool = False,
    status: str = SuggestionSessionStatus.CHANGES_REQUESTED.value,
) -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.project_id = PROJECT_ID
    session.session_id = "s_abc12345"
    session.user_id = user_id
    session.user_name = "C"
    session.user_email = "c@example.com"
    session.branch = "suggest/x/s_abc12345"
    session.status = status
    session.changes_count = 1
    session.revision = 1
    session.pr_number = 3
    session.is_anonymous = is_anonymous
    session.is_llm_generated = is_llm_generated
    session.auto_accept_after = None
    session.auto_accept_halted_at = None
    return session


def _results(*results: object, project: object) -> Iterator[object]:
    tail = MagicMock()
    tail.scalar_one_or_none.return_value = project
    tail.scalar.return_value = 0
    tail.scalars.return_value.all.return_value = []
    return chain(results, repeat(tail))


def _result_for(obj: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    return result


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    db.refresh = AsyncMock()
    db.add = Mock()
    return db


@pytest.fixture
def service(mock_db: AsyncMock) -> SuggestionService:
    return SuggestionService(db=mock_db, git_service=MagicMock())


class TestScheduleAutoAccept:
    async def test_trusted_human_on_enabled_project_gets_a_window(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE1's setup: a trusted submission starts a quiet-period clock."""
        project = _project([_member("contributor-1", is_trusted=True)], quiet_days=7)
        session = _session()
        mock_db.execute.side_effect = _results(_result_for(project), project=project)

        before = datetime.now(UTC)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())

        assert session.auto_accept_after is not None
        delta = session.auto_accept_after - before
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=1)
        assert session.auto_accept_halted_at is None

    async def test_quiet_days_is_read_from_the_project(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1", is_trusted=True)], quiet_days=2)
        session = _session()
        mock_db.execute.side_effect = _results(_result_for(project), project=project)

        before = datetime.now(UTC)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after - before < timedelta(days=2, minutes=1)

    async def test_disabled_project_never_schedules(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1", is_trusted=True)], auto_accept_enabled=False)
        session = _session()
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after is None

    async def test_llm_generated_never_schedules(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE5."""
        project = _project([_member("contributor-1", is_trusted=True)])
        session = _session(is_llm_generated=True)
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after is None

    async def test_anonymous_never_schedules(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE4."""
        project = _project([])
        session = _session(user_id="anonymous-abc", is_anonymous=True)
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user("anonymous-abc"))
        assert session.auto_accept_after is None

    async def test_untrusted_never_schedules(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1", is_trusted=False)])
        session = _session()
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after is None

    async def test_reviewer_never_schedules(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Staff can merge directly; auto-accept is for the trusted rung only."""
        project = _project([_member("contributor-1", "editor")])
        session = _session()
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after is None

    async def test_ineligible_scheduling_clears_a_stale_window(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Trust revoked between submit and resubmit must clear the clock."""
        project = _project([_member("contributor-1", is_trusted=False)])
        session = _session()
        session.auto_accept_after = datetime.now(UTC) + timedelta(days=3)
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        await service._schedule_auto_accept(PROJECT_ID, session, _user())
        assert session.auto_accept_after is None


class TestHaltAutoAccept:
    def test_halt_clears_the_window_and_stamps_the_time(self, service: SuggestionService) -> None:
        session = _session()
        session.auto_accept_after = datetime.now(UTC) + timedelta(days=4)
        service._halt_auto_accept(session)
        assert session.auto_accept_after is None
        assert session.auto_accept_halted_at is not None

    def test_halt_is_idempotent(self, service: SuggestionService) -> None:
        session = _session()
        service._halt_auto_accept(session)
        first = session.auto_accept_halted_at
        service._halt_auto_accept(session)
        assert session.auto_accept_after is None
        assert session.auto_accept_halted_at >= first


class TestResubmitRestartsTheClock:
    async def test_resolved_objection_restarts_from_zero(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE1 / KTD11 — a full fresh window, not the remainder."""
        project = _project([_member("contributor-1", is_trusted=True)], quiet_days=7)
        session = _session(status=SuggestionSessionStatus.CHANGES_REQUESTED.value)
        session.auto_accept_halted_at = datetime.now(UTC) - timedelta(days=3)
        mock_db.execute.side_effect = _results(
            _result_for(session), _result_for(project), _result_for(project), project=project
        )

        before = datetime.now(UTC)
        await service.resubmit(
            PROJECT_ID, session.session_id, SuggestionResubmitRequest(summary="fixed"), _user()
        )

        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        assert session.auto_accept_after - before > timedelta(days=6, hours=23)
        assert session.auto_accept_halted_at is None

    async def test_resubmit_by_untrusted_does_not_restart(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1", is_trusted=False)])
        session = _session(status=SuggestionSessionStatus.CHANGES_REQUESTED.value)
        mock_db.execute.side_effect = _results(
            _result_for(session), _result_for(project), _result_for(project), project=project
        )
        await service.resubmit(
            PROJECT_ID, session.session_id, SuggestionResubmitRequest(summary="fixed"), _user()
        )
        assert session.auto_accept_after is None
