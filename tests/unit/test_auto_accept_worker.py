"""Tests for the auto-accept sweep and its cron registration (U8).

This is the only path by which content reaches the canonical ontology without a
human pressing a button, so the exclusions are tested from both directions: the
query predicate and the merge-time re-check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, repeat
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from ontokit import worker
from ontokit.models.suggestion_outcome import SuggestionOutcome
from ontokit.models.suggestion_session import SuggestionSessionStatus
from ontokit.services.suggestion_service import SuggestionService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _member(user_id: str, role: str = "suggester", *, is_trusted: bool = True) -> MagicMock:
    member = MagicMock()
    member.user_id = user_id
    member.role = role
    member.is_trusted = is_trusted
    member.trust_override = "none"
    return member


def _project(members: list[MagicMock] | None = None) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_ID
    project.name = "P"
    project.is_public = True
    project.members = members if members is not None else []
    project.auto_accept_enabled = True
    project.auto_accept_quiet_days = 7
    project.trust_promotion_threshold = 5
    return project


def _session(user_id: str = "trusted-1") -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.project_id = PROJECT_ID
    session.session_id = "s_ripe0001"
    session.user_id = user_id
    session.user_name = "T"
    session.user_email = "t@example.com"
    session.branch = "suggest/x/s_ripe0001"
    session.status = SuggestionSessionStatus.SUBMITTED.value
    session.changes_count = 1
    session.pr_number = 4
    session.pr_id = None
    session.entities_modified = None
    session.is_anonymous = False
    session.is_llm_generated = False
    session.auto_accept_after = datetime.now(UTC) - timedelta(hours=1)
    session.auto_accept_halted_at = None
    session.auto_accept_claimed_until = None
    return session


def _results(*results: object, project: object) -> Iterator[object]:
    tail = MagicMock()
    tail.scalar_one_or_none.return_value = project
    tail.scalar.return_value = 0
    tail.scalars.return_value.all.return_value = []
    tail.rowcount = 1
    return chain(results, repeat(tail))


def _result_for(obj: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    return result


def _ripe_result(sessions: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
    return result


def _claim(rowcount: int) -> MagicMock:
    result = MagicMock()
    result.rowcount = rowcount
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


def _outcomes(mock_db: AsyncMock) -> list[SuggestionOutcome]:
    return [
        c.args[0] for c in mock_db.add.call_args_list if isinstance(c.args[0], SuggestionOutcome)
    ]


class TestAutoAcceptSweep:
    async def test_merges_a_ripe_trusted_session(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE3: the sweep snapshots the submitter, not its synthetic actor."""
        project = _project([_member("trusted-1")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]),
            _claim(1),
            _result_for(project),  # project reload for the tier re-check
            _result_for(session),  # _get_session inside the approve path
            project=project,
        )

        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            count = await service.auto_accept_ripe_sessions()

        assert count == 1
        assert session.status == SuggestionSessionStatus.MERGED.value
        recorded = _outcomes(mock_db)
        assert len(recorded) == 1
        assert recorded[0].decided_by == "system:auto-accept"
        merge_call = factory.return_value.merge_pull_request.await_args
        assert merge_call is not None
        assert merge_call.kwargs["system_auto_accept"] is True
        assert recorded[0].decided_by_name is None
        assert recorded[0].snapshot_tier == "trusted"
        assert recorded[0].snapshot_role == "suggester"
        assert recorded[0].submitter_name == "T"
        assert recorded[0].submitter_email == "t@example.com"
        assert recorded[0].snapshot_captured_at is not None

    async def test_skips_when_another_worker_claimed_it(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Two concurrent sweeps must merge a session exactly once (R17)."""
        project = _project([_member("trusted-1")])
        session = _session()
        mock_db.execute.side_effect = _results(_ripe_result([session]), _claim(0), project=project)
        count = await service.auto_accept_ripe_sessions()
        assert count == 0
        assert _outcomes(mock_db) == []

    async def test_skips_when_trust_was_revoked_after_scheduling(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """The merge-time re-check is the belt to the clock's braces."""
        project = _project([_member("trusted-1", is_trusted=False)])
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]), _claim(1), _result_for(project), project=project
        )
        count = await service.auto_accept_ripe_sessions()
        assert count == 0
        assert session.status == SuggestionSessionStatus.SUBMITTED.value

    async def test_skips_when_submitter_is_now_a_reviewer(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """A promotion to editor takes the session off the auto-accept path."""
        project = _project([_member("trusted-1", "editor")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]), _claim(1), _result_for(project), project=project
        )
        assert await service.auto_accept_ripe_sessions() == 0

    async def test_cancels_when_project_disabled_after_scheduling(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("trusted-1")])
        project.auto_accept_enabled = False
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]), _claim(1), _result_for(project), project=project
        )
        assert await service.auto_accept_ripe_sessions() == 0
        assert session.auto_accept_after is None
        assert session.auto_accept_claimed_until is None

    async def test_no_ripe_sessions_is_a_no_op(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project()
        mock_db.execute.side_effect = _results(_ripe_result([]), project=project)
        assert await service.auto_accept_ripe_sessions() == 0

    async def test_merge_failure_reverts_the_claim_for_the_next_sweep(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("trusted-1")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]),
            _claim(1),
            _result_for(project),
            _result_for(session),
            project=project,
        )
        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.side_effect = RuntimeError("merge exploded")
            count = await service.auto_accept_ripe_sessions()

        assert count == 0
        mock_db.rollback.assert_awaited()
        assert session.auto_accept_after is not None

    async def test_authorization_failure_does_not_mark_the_session_merged(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """A denied PR merge is retryable, never a successful auto-accept."""
        project = _project([_member("trusted-1")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _ripe_result([session]),
            _claim(1),
            _result_for(project),
            _result_for(session),
            project=project,
        )
        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value.merge_pull_request = AsyncMock(
                side_effect=HTTPException(status_code=403, detail="denied")
            )
            count = await service.auto_accept_ripe_sessions()

        assert count == 0
        assert session.status == SuggestionSessionStatus.SUBMITTED.value
        assert session.auto_accept_after is not None
        mock_db.rollback.assert_awaited()
        assert _outcomes(mock_db) == []

    async def test_query_predicate_excludes_anonymous_llm_and_halted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE4 and AE5 at the query level.

        Compiling the WHERE clause pins the exclusions, so a future edit that
        drops one is a visible test failure rather than a silent auto-merge of
        anonymous or machine-written content.
        """
        project = _project()
        mock_db.execute.side_effect = _results(_ripe_result([]), project=project)
        await service.auto_accept_ripe_sessions()

        stmt = str(mock_db.execute.await_args_list[0].args[0])
        assert "is_anonymous IS false" in stmt
        assert "is_llm_generated IS false" in stmt
        assert "auto_accept_halted_at IS NULL" in stmt
        assert "auto_accept_after IS NOT NULL" in stmt
        assert "auto_accept_claimed_until IS NULL" in stmt
        assert "ORDER BY suggestion_sessions.auto_accept_after ASC" in stmt
        assert "LIMIT" in stmt


class TestWorkerRegistration:
    async def test_task_returns_the_count(self) -> None:
        with patch("ontokit.services.suggestion_service.SuggestionService") as svc_cls:
            svc_cls.return_value.auto_accept_ripe_sessions = AsyncMock(return_value=3)
            result = await worker.auto_accept_suggestions({"db": AsyncMock()})
        assert result == {"auto_accepted": 3}

    async def test_task_reraises_failures_so_arq_records_them(self) -> None:
        with patch("ontokit.services.suggestion_service.SuggestionService") as svc_cls:
            svc_cls.return_value.auto_accept_ripe_sessions = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            with pytest.raises(RuntimeError, match="boom"):
                await worker.auto_accept_suggestions({"db": AsyncMock()})

    def test_registered_in_worker_functions(self) -> None:
        assert worker.auto_accept_suggestions in worker.WorkerSettings.functions

    def test_registered_as_a_cron_job(self) -> None:
        names = {getattr(job, "name", None) for job in worker.WorkerSettings.cron_jobs}
        assert "cron:auto_accept_suggestions" in names
