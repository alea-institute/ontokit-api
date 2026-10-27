"""Selected refusal guards preserve legacy contracts through the HTTP stack."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response

from ontokit.api.routes import anonymous_suggestions, pull_requests, suggestions
from ontokit.core.auth import CurrentUser, get_current_user
from ontokit.core.exceptions import GuidedHTTPException
from ontokit.main import app
from ontokit.schemas.trust import TrustTier
from ontokit.services.pull_request_service import PullRequestService
from ontokit.services.suggestion_service import SuggestionService
from ontokit.services.trust_rate_limiter import TrustLimitDecision, TrustLimitStatus

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")
BASE = f"/api/v1/projects/{PROJECT_ID}"


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    db = AsyncMock()
    git = Mock()
    git.get_default_branch.return_value = "main"
    git.get_file_from_branch.return_value = b"# synthetic ontology"
    git.list_branches.return_value = []
    session = SimpleNamespace(
        session_id="s_test",
        user_id="contributor",
        status="active",
        changes_count=1,
        is_anonymous=False,
        branch="suggest/contributor/s_test",
        verification_passed=True,
        pr_id=None,
        pr_number=None,
    )
    project = SimpleNamespace(id=PROJECT_ID, pr_approval_required=1)
    suggestion = SuggestionService(db, git)
    monkeypatch.setattr(suggestion, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(suggestion, "_verify_project_access", AsyncMock(return_value=project))
    monkeypatch.setattr(suggestion, "_verify_untrusted_human", AsyncMock())
    monkeypatch.setattr(suggestion, "_get_git_ontology_path", Mock(return_value="ontology.ttl"))
    monkeypatch.setattr(suggestion, "_validate_submission_content", AsyncMock())
    monkeypatch.setattr(suggestion.trust, "resolve_tier", Mock(return_value=TrustTier.UNTRUSTED))
    monkeypatch.setattr(suggestions, "get_arq_pool", AsyncMock(return_value=None))

    @asynccontextmanager
    async def no_lock(*_args: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr("ontokit.services.suggestion_service.pull_request_write_locks", no_lock)
    pr = SimpleNamespace(
        id=PROJECT_ID,
        pr_number=1,
        status="open",
        reviews=[],
        source_branch="feature",
        target_branch="main",
        title="Synthetic change",
    )
    review = PullRequestService(db, git_service=git)
    monkeypatch.setattr(review, "_get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(review, "_get_pr", AsyncMock(return_value=pr))
    monkeypatch.setattr(review, "get_pull_request", AsyncMock(return_value=pr))
    monkeypatch.setattr(review, "_get_user_role", Mock(return_value="owner"))

    async def suggestion_dependency() -> SuggestionService:
        return suggestion

    async def review_dependency() -> PullRequestService:
        return review

    async def user_dependency() -> CurrentUser:
        return CurrentUser(id="contributor")

    monkeypatch.setattr(
        app,
        "dependency_overrides",
        {
            suggestions.get_service: suggestion_dependency,
            anonymous_suggestions.get_service: suggestion_dependency,
            pull_requests.get_service: review_dependency,
            get_current_user: user_dependency,
        },
    )
    return SimpleNamespace(
        suggestion=suggestion,
        review=review,
        session=session,
        project=project,
        pr=pr,
        db=db,
        git=git,
    )


async def post(path: str, body: dict[str, object] | None = None, **kwargs: object) -> Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body or {}, **kwargs)


def assert_guidance(response: Response, status: int, detail: object, code: str) -> None:
    assert response.status_code == status
    payload = response.json()
    assert payload["detail"] == detail
    assert set(payload["error"]) == {"code", "message", "recovery", "request_id"}
    assert payload["error"]["code"] == code
    assert payload["error"]["message"]
    assert payload["error"]["recovery"]
    assert payload["error"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.parametrize("guard", ["initial_state", "locked_state", "no_changes"])
async def test_submit_guard_guidance(services: SimpleNamespace, guard: str) -> None:
    if guard == "initial_state":
        services.session.status = "submitted"
    elif guard == "locked_state":

        async def refresh(session: SimpleNamespace) -> None:
            session.status = "submitted"

        services.db.refresh.side_effect = refresh
    else:
        services.session.changes_count = 0

    response = await post(f"{BASE}/suggestions/sessions/s_test/submit")
    if guard == "no_changes":
        assert_guidance(response, 400, "No changes to submit", "suggestion_no_changes")
    else:
        assert_guidance(
            response, 400, "Session is submitted, cannot submit", "suggestion_not_submittable"
        )
    services.suggestion._validate_submission_content.assert_not_awaited()
    services.db.commit.assert_not_awaited()
    services.git.merge_branch.assert_not_called()


@pytest.mark.parametrize(
    ("limit_status", "status", "reason", "message", "code"),
    [
        (
            TrustLimitStatus.UNAVAILABLE,
            503,
            "submission_limiter_unavailable",
            "Suggestion submissions are temporarily unavailable. Try again.",
            "submission_limiter_unavailable",
        ),
        (
            TrustLimitStatus.EXHAUSTED,
            429,
            "daily_limit_reached",
            "You have reached today's suggestion limit. It resets at midnight UTC.",
            "submission_limit_reached",
        ),
    ],
)
async def test_limiter_guidance_preserves_detail_and_validation_order(
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    limit_status: TrustLimitStatus,
    status: int,
    reason: str,
    message: str,
    code: str,
) -> None:
    effects: list[str] = []

    async def validate(*_args: object) -> None:
        effects.append("duplicate-validation")

    async def consume(*_args: object) -> TrustLimitDecision:
        assert effects == ["duplicate-validation"]
        effects.append("allowance-check")
        return TrustLimitDecision(limit_status, 0)

    services.suggestion._validate_submission_content.side_effect = validate
    limiter = AsyncMock(side_effect=consume)
    monkeypatch.setattr("ontokit.services.suggestion_service.check_and_consume", limiter)
    response = await post(f"{BASE}/suggestions/sessions/s_test/submit")
    assert_guidance(response, status, {"reason": reason, "message": message}, code)
    assert effects == ["duplicate-validation", "allowance-check"]
    services.suggestion._validate_submission_content.assert_awaited_once()
    limiter.assert_awaited_once()
    services.db.commit.assert_not_awaited()
    services.git.merge_branch.assert_not_called()


async def test_anonymous_no_changes_guidance(
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services.session.is_anonymous = True
    services.session.changes_count = 0
    monkeypatch.setattr(anonymous_suggestions, "settings", SimpleNamespace(auth_mode="optional"))
    monkeypatch.setattr(anonymous_suggestions, "_verify_anon_token", Mock(return_value="s_test"))
    response = await post(
        f"{BASE}/suggestions/anonymous/sessions/s_test/submit",
        headers={"X-Anonymous-Token": "synthetic-token"},
    )
    assert_guidance(response, 400, "No changes to submit", "suggestion_no_changes")
    services.db.commit.assert_not_awaited()


@pytest.mark.parametrize("state", ["closed", "merged"])
async def test_closed_review_guidance(services: SimpleNamespace, state: str) -> None:
    services.pr.status = state
    response = await post(
        f"{BASE}/pull-requests/1/reviews", {"status": "commented", "body": "note"}
    )
    assert_guidance(
        response, 400, "Cannot review a closed or merged pull request", "pull_request_not_open"
    )
    services.db.commit.assert_not_awaited()


async def test_approval_shortfall_guidance(services: SimpleNamespace) -> None:
    services.project.pr_approval_required = 2
    response = await post(f"{BASE}/pull-requests/1/merge")
    assert_guidance(
        response, 400, "Pull request requires 2 approvals, but has 0", "approvals_required"
    )
    services.git.merge_branch.assert_not_called()


async def test_git_merge_failure_guidance(services: SimpleNamespace) -> None:
    services.project.pr_approval_required = 0
    services.git.merge_branch.return_value = SimpleNamespace(
        success=False, message="Conflicts detected"
    )
    response = await post(f"{BASE}/pull-requests/1/merge")
    assert_guidance(response, 409, "Merge failed: Conflicts detected", "merge_failed")
    services.git.merge_branch.assert_called_once()
    services.db.commit.assert_not_awaited()
    assert services.pr.status == "open"


async def test_guided_refusal_keeps_bulk_review_partial_success(
    services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        services.suggestion, "_verify_reviewer_access", AsyncMock(return_value=services.project)
    )
    refusal = GuidedHTTPException(
        400,
        "Pull request requires 1 approvals, but has 0",
        code="approvals_required",
        message="More reviews are needed.",
        recovery="Obtain the required reviews before merging.",
    )
    approve = AsyncMock(side_effect=[refusal, None])
    monkeypatch.setattr(services.suggestion, "_approve_unchecked", approve)
    response = await post(
        f"{BASE}/suggestions/bulk-review",
        {
            "session_ids": ["s_refused", "s_ok"],
            "action": "accept",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "action": "accept",
        "succeeded": ["s_ok"],
        "failed": [
            {"session_id": "s_refused", "reason": "Pull request requires 1 approvals, but has 0"}
        ],
    }
    assert approve.await_count == 2
    services.db.rollback.assert_awaited_once()


@pytest.mark.parametrize(
    ("path", "statuses"),
    [
        ("/suggestions/sessions/{session_id}/submit", [400, 429, 503]),
        ("/suggestions/sessions/{session_id}/resubmit", [400, 429, 503]),
        ("/suggestions/anonymous/sessions/{session_id}/submit", [400]),
        ("/suggestions/sessions/{session_id}/approve", [400, 409]),
        ("/pull-requests/{pr_number}/merge", [400, 409]),
        ("/pull-requests/{pr_number}/reviews", [400]),
    ],
)
def test_selected_openapi_responses_document_optional_guidance(
    path: str, statuses: list[int]
) -> None:
    schema = app.openapi()
    responses = schema["paths"]["/api/v1/projects/{project_id}" + path]["post"]["responses"]
    for status in statuses:
        assert responses[str(status)]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/HTTPErrorResponse",
        }
    error_schema = schema["components"]["schemas"]["HTTPErrorResponse"]
    assert "error" not in error_schema["required"]
    assert "error" in error_schema["properties"]
