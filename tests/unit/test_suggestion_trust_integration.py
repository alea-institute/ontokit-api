"""Tests for the trust ladder wired into the review paths (U3, U4, U5).

These assert on the persisted outcome rows and the scheduled clock, not on mock
call counts: the point of U3 is that a resolved suggestion can never exist
without its outcome row, and that is a data claim.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain, repeat
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from ontokit.core.auth import CurrentUser
from ontokit.models.suggestion_outcome import SuggestionOutcome, SuggestionOutcomeType
from ontokit.models.suggestion_session import SuggestionSessionStatus
from ontokit.schemas.suggestion import (
    BulkReviewAction,
    BulkReviewRequest,
    SuggestionRejectRequest,
    SuggestionRequestChangesRequest,
    SuggestionSaveRequest,
)
from ontokit.schemas.trust import TrustTier
from ontokit.services.suggestion_service import SuggestionService

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(user_id: str = "contributor-1", name: str = "Contributor") -> CurrentUser:
    return CurrentUser(id=user_id, email="c@example.com", name=name, username="c")


def _member(
    user_id: str,
    role: str = "suggester",
    *,
    is_trusted: bool = False,
    trust_override: str = "none",
) -> MagicMock:
    member = MagicMock()
    member.user_id = user_id
    member.role = role
    member.is_trusted = is_trusted
    member.trust_override = trust_override
    member.trust_granted_at = None
    member.trust_granted_by = None
    return member


def _project(
    members: list[MagicMock] | None = None,
    *,
    threshold: int = 5,
    auto_accept_enabled: bool = False,
    quiet_days: int = 7,
) -> MagicMock:
    project = MagicMock()
    project.id = PROJECT_ID
    project.name = "Test Project"
    project.is_public = True
    project.source_file_path = None
    project.members = members if members is not None else []
    project.trust_promotion_threshold = threshold
    project.auto_accept_enabled = auto_accept_enabled
    project.auto_accept_quiet_days = quiet_days
    return project


def _session(
    *,
    session_id: str = "s_abc12345",
    user_id: str = "contributor-1",
    status: str = SuggestionSessionStatus.SUBMITTED.value,
    is_anonymous: bool = False,
    is_llm_generated: bool = False,
    changes_count: int = 1,
    pr_number: int | None = 7,
) -> MagicMock:
    session = MagicMock()
    session.id = uuid.uuid4()
    session.project_id = PROJECT_ID
    session.session_id = session_id
    session.user_id = user_id
    session.user_name = "Contributor"
    session.user_email = "c@example.com"
    session.branch = f"suggest/x/{session_id}"
    session.status = status
    session.changes_count = changes_count
    session.entities_modified = None
    session.pr_number = pr_number
    session.pr_id = None
    session.revision = 1
    session.summary = None
    session.is_anonymous = is_anonymous
    session.submitter_name = None
    session.submitter_email = None
    session.client_ip = None
    session.reviewer_id = None
    session.reviewer_name = None
    session.reviewer_email = None
    session.reviewer_feedback = None
    session.reviewed_at = None
    session.is_llm_generated = is_llm_generated
    session.auto_accept_after = None
    session.auto_accept_halted_at = None
    session.verification_passed = False
    session.created_at = datetime.now(UTC)
    session.last_activity = datetime.now(UTC)
    return session


def _results(*results: object, project: object, accepted: int = 0) -> Iterator[object]:
    """Ordered query results, then a tail answering the trust follow-on queries."""
    tail = MagicMock()
    tail.scalar_one_or_none.return_value = project
    tail.scalar.return_value = accepted
    tail.scalars.return_value.all.return_value = []
    return chain(results, repeat(tail))


def _result_for(obj: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = obj
    return result


def _sessions_result(sessions: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
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
    git = MagicMock()
    git.get_file_from_branch.return_value = (
        b"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    )
    return SuggestionService(db=mock_db, git_service=git)


def _added_outcomes(mock_db: AsyncMock) -> list[SuggestionOutcome]:
    return [
        call.args[0]
        for call in mock_db.add.call_args_list
        if isinstance(call.args[0], SuggestionOutcome)
    ]


# ---------------------------------------------------------------------------
# U3 — outcome recording on the review paths
# ---------------------------------------------------------------------------


class TestApproveRecordsOutcome:
    async def test_approve_appends_one_accepted_outcome(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        member = _member("contributor-1")
        project = _project([member, _member("reviewer-1", "admin")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project, accepted=1
        )

        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            await service.approve(PROJECT_ID, session.session_id, _user("reviewer-1"))

        outcomes = _added_outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == SuggestionOutcomeType.ACCEPTED.value
        assert outcomes[0].project_id == PROJECT_ID
        assert outcomes[0].session_id == session.id
        assert outcomes[0].counts_toward_promotion is True
        assert outcomes[0].decided_by == "reviewer-1"

    async def test_decided_by_override_attributes_system_merges(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1"), _member("reviewer-1", "admin")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )
        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            await service.approve(
                PROJECT_ID,
                session.session_id,
                _user("reviewer-1"),
                decided_by="system:auto-accept",
            )
        assert _added_outcomes(mock_db)[0].decided_by == "system:auto-accept"

    async def test_anonymous_outcome_is_credited_but_not_counted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """R7."""
        project = _project([_member("reviewer-1", "admin")])
        session = _session(user_id="anonymous-abc123", is_anonymous=True)
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )
        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            await service.approve(PROJECT_ID, session.session_id, _user("reviewer-1"))

        outcome = _added_outcomes(mock_db)[0]
        assert outcome.is_anonymous is True
        assert outcome.counts_toward_promotion is False

    async def test_promotes_and_notifies_once_on_threshold(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Covers AE3."""
        member = _member("contributor-1")
        project = _project([member, _member("reviewer-1", "admin")], threshold=5)
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project, accepted=5
        )

        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            await service.approve(PROJECT_ID, session.session_id, _user("reviewer-1"))

        assert member.is_trusted is True
        notifications = [
            call.args[0]
            for call in mock_db.add.call_args_list
            if getattr(call.args[0], "type", None) == "trust_promoted"
        ]
        assert len(notifications) == 1
        assert notifications[0].user_id == "contributor-1"

    async def test_no_duplicate_notification_for_already_trusted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        member = _member("contributor-1", is_trusted=True)
        project = _project([member, _member("reviewer-1", "admin")], threshold=5)
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project, accepted=50
        )
        with patch("ontokit.services.suggestion_service.get_pull_request_service") as factory:
            factory.return_value = AsyncMock()
            await service.approve(PROJECT_ID, session.session_id, _user("reviewer-1"))

        assert not [
            c
            for c in mock_db.add.call_args_list
            if getattr(c.args[0], "type", None) == "trust_promoted"
        ]


class TestRejectRecordsOutcome:
    async def test_reject_appends_rejected_outcome_and_no_promotion(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        member = _member("contributor-1")
        project = _project([member, _member("reviewer-1", "admin")], threshold=1)
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project, accepted=99
        )

        await service.reject(
            PROJECT_ID,
            session.session_id,
            SuggestionRejectRequest(reason="out of scope"),
            _user("reviewer-1"),
        )

        outcomes = _added_outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == SuggestionOutcomeType.REJECTED.value
        assert outcomes[0].note == "out of scope"
        assert member.is_trusted is False

    async def test_reject_halts_the_auto_accept_clock(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """R12."""
        project = _project([_member("reviewer-1", "admin")])
        session = _session()
        session.auto_accept_after = datetime.now(UTC) + timedelta(days=4)
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )

        await service.reject(
            PROJECT_ID,
            session.session_id,
            SuggestionRejectRequest(reason="no"),
            _user("reviewer-1"),
        )

        assert session.auto_accept_after is None
        assert session.auto_accept_halted_at is not None


class TestRequestChangesHaltsClock:
    async def test_request_changes_halts_without_terminal_outcome(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """A revision request halts the clock but is not a terminal outcome."""
        project = _project([_member("reviewer-1", "admin")])
        session = _session()
        session.auto_accept_after = datetime.now(UTC) + timedelta(days=3)
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )

        await service.request_changes(
            PROJECT_ID,
            session.session_id,
            SuggestionRequestChangesRequest(feedback="please narrow the label"),
            _user("reviewer-1"),
        )

        assert session.auto_accept_after is None
        assert session.auto_accept_halted_at is not None
        assert _added_outcomes(mock_db) == []


class TestDismiss:
    async def test_dismiss_appends_dismissed_outcome(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("reviewer-1", "admin")])
        session = _session()
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )

        await service.dismiss(PROJECT_ID, session.session_id, _user("reviewer-1"), "spam")

        assert session.status == SuggestionSessionStatus.DISCARDED.value
        outcomes = _added_outcomes(mock_db)
        assert len(outcomes) == 1
        assert outcomes[0].outcome == SuggestionOutcomeType.DISMISSED.value
        assert outcomes[0].note == "spam"

    async def test_dismiss_requires_reviewer(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("outsider", "suggester")])
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        with pytest.raises(HTTPException) as exc:
            await service.dismiss(PROJECT_ID, "s_abc12345", _user("outsider"))
        assert exc.value.status_code == 403

    async def test_dismiss_wrong_status_raises_400(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("reviewer-1", "admin")])
        session = _session(status=SuggestionSessionStatus.MERGED.value)
        mock_db.execute.side_effect = _results(
            _result_for(project), _result_for(session), project=project
        )
        with pytest.raises(HTTPException) as exc:
            await service.dismiss(PROJECT_ID, session.session_id, _user("reviewer-1"))
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# U4 — entity minting gate
# ---------------------------------------------------------------------------


class TestMintingGate:
    def _save(self, mints: bool) -> SuggestionSaveRequest:
        declaration = ":A a owl:Class ." if mints else ""
        return SuggestionSaveRequest(
            content=(
                "@prefix : <http://x#> .\n"
                "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                f"{declaration}"
            ),
            entity_iri="http://x#A",
            entity_label="A",
            mints_entity=mints,
        )

    def test_untrusted_cannot_mint(self, service: SuggestionService) -> None:
        """Covers AE2."""
        project = _project([_member("contributor-1")])
        with pytest.raises(HTTPException) as exc:
            service._assert_can_mint(project, _user("contributor-1"))
        assert exc.value.status_code == 403
        assert exc.value.detail["reason"] == "trust_required_to_mint"

    def test_trusted_can_mint(self, service: SuggestionService) -> None:
        project = _project([_member("contributor-1", is_trusted=True)])
        service._assert_can_mint(project, _user("contributor-1"))

    def test_reviewer_can_mint(self, service: SuggestionService) -> None:
        """KTD4 again: staff are never blocked by the ladder."""
        project = _project([_member("contributor-1", "editor")])
        service._assert_can_mint(project, _user("contributor-1"))

    def test_anonymous_cannot_mint(self, service: SuggestionService) -> None:
        with pytest.raises(HTTPException) as exc:
            service._assert_can_mint(_project([]), None)
        assert exc.value.status_code == 403

    async def test_save_without_minting_is_allowed_for_untrusted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Propose-edit stays available at every rung (AE2's second half)."""
        project = _project([_member("contributor-1")])
        session = _session(status=SuggestionSessionStatus.ACTIVE.value, changes_count=0)
        mock_db.execute.side_effect = _results(
            _result_for(session), _result_for(project), _result_for(project), project=project
        )
        commit_info = MagicMock()
        commit_info.hash = "abc123"
        service.git_service.commit_changes = MagicMock(return_value=commit_info)

        response = await service.save(
            PROJECT_ID, session.session_id, self._save(False), _user("contributor-1")
        )
        assert response.commit_hash == "abc123"

    async def test_save_with_minting_is_refused_for_untrusted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1")])
        session = _session(status=SuggestionSessionStatus.ACTIVE.value, changes_count=0)
        mock_db.execute.side_effect = _results(
            _result_for(session), _result_for(project), _result_for(project), project=project
        )
        service.git_service.commit_changes = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await service.save(
                PROJECT_ID, session.session_id, self._save(True), _user("contributor-1")
            )
        assert exc.value.status_code == 403
        service.git_service.commit_changes.assert_not_called()

    async def test_client_flag_false_cannot_hide_real_mint(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1")])
        session = _session(status=SuggestionSessionStatus.ACTIVE.value, changes_count=0)
        mock_db.execute.side_effect = _results(
            _result_for(session), _result_for(project), _result_for(project), project=project
        )
        request = self._save(True).model_copy(update={"mints_entity": False})

        with pytest.raises(HTTPException) as exc:
            await service.save(PROJECT_ID, session.session_id, request, _user("contributor-1"))

        assert exc.value.status_code == 403


class TestCapabilities:
    async def test_untrusted_sees_threshold_and_no_minting(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1")], threshold=5)
        mock_db.execute.side_effect = _results(_result_for(project), project=project, accepted=2)
        caps = await service.get_capabilities(PROJECT_ID, _user("contributor-1"))
        assert caps.tier is TrustTier.UNTRUSTED
        assert caps.can_mint_entities is False
        assert caps.can_suggest is True
        assert caps.promotion_threshold == 5
        assert caps.accepted_count == 2

    async def test_anonymous_on_public_project(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([])
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        caps = await service.get_capabilities(PROJECT_ID, None)
        assert caps.tier is TrustTier.ANONYMOUS
        assert caps.can_mint_entities is False

    async def test_anonymous_on_private_project_is_refused(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """A tier readout is itself information about a private project."""
        project = _project([])
        project.is_public = False
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        with pytest.raises(HTTPException) as exc:
            await service.get_capabilities(PROJECT_ID, None)
        assert exc.value.status_code == 403

    async def test_first_suggestion_flags_verification_required(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("contributor-1")])
        mock_db.execute.side_effect = _results(_result_for(project), project=project, accepted=0)
        caps = await service.get_capabilities(PROJECT_ID, _user("contributor-1"))
        assert caps.verification_required is True

    async def test_trusted_reports_minting_allowed(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project(
            [_member("contributor-1", is_trusted=True)], auto_accept_enabled=True, quiet_days=7
        )
        mock_db.execute.side_effect = _results(_result_for(project), project=project, accepted=9)
        caps = await service.get_capabilities(PROJECT_ID, _user("contributor-1"))
        assert caps.tier is TrustTier.TRUSTED
        assert caps.can_mint_entities is True
        assert caps.auto_accept_enabled is True
        assert caps.auto_accept_quiet_days == 7
        assert caps.verification_required is False


# ---------------------------------------------------------------------------
# U5 — triage queue
# ---------------------------------------------------------------------------


class TestTriageQueue:
    def _fixture(self) -> tuple[MagicMock, list[MagicMock]]:
        project = _project(
            [
                _member("reviewer-1", "admin"),
                _member("trusted-1", is_trusted=True),
                _member("untrusted-1"),
            ]
        )
        sessions = [
            _session(session_id="s_anon", user_id="anonymous-1", is_anonymous=True),
            _session(session_id="s_untrusted", user_id="untrusted-1"),
            _session(session_id="s_trusted", user_id="trusted-1"),
        ]
        return project, sessions

    async def test_triage_queue_excludes_trusted(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project, sessions = self._fixture()
        mock_db.execute.side_effect = _results(
            _result_for(project), _sessions_result(sessions), project=project
        )
        result = await service.list_pending(PROJECT_ID, _user("reviewer-1"), "triage")
        assert {i.session_id for i in result.items} == {"s_anon", "s_untrusted"}

    async def test_review_queue_is_trusted_only(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project, sessions = self._fixture()
        mock_db.execute.side_effect = _results(
            _result_for(project), _sessions_result(sessions), project=project
        )
        result = await service.list_pending(PROJECT_ID, _user("reviewer-1"), "review")
        assert {i.session_id for i in result.items} == {"s_trusted"}

    async def test_omitting_queue_returns_all(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        """Backward compatible with the existing client."""
        project, sessions = self._fixture()
        mock_db.execute.side_effect = _results(
            _result_for(project), _sessions_result(sessions), project=project
        )
        result = await service.list_pending(PROJECT_ID, _user("reviewer-1"))
        assert len(result.items) == 3

    async def test_summaries_carry_the_submitter_tier(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project, sessions = self._fixture()
        mock_db.execute.side_effect = _results(
            _result_for(project), _sessions_result(sessions), project=project
        )
        result = await service.list_pending(PROJECT_ID, _user("reviewer-1"))
        by_id = {i.session_id: i.submitter_tier for i in result.items}
        assert by_id["s_anon"] is TrustTier.ANONYMOUS
        assert by_id["s_untrusted"] is TrustTier.UNTRUSTED
        assert by_id["s_trusted"] is TrustTier.TRUSTED


class TestBulkReview:
    async def test_bulk_dismiss_processes_every_id(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("reviewer-1", "admin")])
        sessions = {sid: _session(session_id=sid) for sid in ("s_1", "s_2", "s_3")}
        results: list[object] = [_result_for(project)]
        for sid in ("s_1", "s_2", "s_3"):
            results.extend([_result_for(project), _result_for(sessions[sid])])
        mock_db.execute.side_effect = _results(*results, project=project)

        response = await service.bulk_review(
            PROJECT_ID,
            BulkReviewRequest(session_ids=["s_1", "s_2", "s_3"], action=BulkReviewAction.DISMISS),
            _user("reviewer-1"),
        )

        assert response.succeeded == ["s_1", "s_2", "s_3"]
        assert response.failed == []
        assert len(_added_outcomes(mock_db)) == 3

    async def test_partial_success_reports_failures_without_aborting(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("reviewer-1", "admin")])
        good = _session(session_id="s_ok")
        stale = _session(session_id="s_stale", status=SuggestionSessionStatus.MERGED.value)
        mock_db.execute.side_effect = _results(
            _result_for(project),
            _result_for(project),
            _result_for(good),
            _result_for(project),
            _result_for(stale),
            project=project,
        )

        response = await service.bulk_review(
            PROJECT_ID,
            BulkReviewRequest(session_ids=["s_ok", "s_stale"], action=BulkReviewAction.DISMISS),
            _user("reviewer-1"),
        )

        assert response.succeeded == ["s_ok"]
        assert [f.session_id for f in response.failed] == ["s_stale"]
        assert "cannot dismiss" in response.failed[0].reason

    async def test_non_reviewer_is_refused(
        self, service: SuggestionService, mock_db: AsyncMock
    ) -> None:
        project = _project([_member("outsider", "suggester")])
        mock_db.execute.side_effect = _results(_result_for(project), project=project)
        with pytest.raises(HTTPException) as exc:
            await service.bulk_review(
                PROJECT_ID,
                BulkReviewRequest(session_ids=["s_1"], action=BulkReviewAction.DISMISS),
                _user("outsider"),
            )
        assert exc.value.status_code == 403

    def test_batch_size_is_capped_at_100(self) -> None:
        with pytest.raises(ValueError, match="at most 100"):
            BulkReviewRequest(
                session_ids=[f"s_{i}" for i in range(101)], action=BulkReviewAction.DISMISS
            )

    def test_empty_batch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            BulkReviewRequest(session_ids=[], action=BulkReviewAction.ACCEPT)
