"""Tests for the PR Party GitHub client (``ontokit/services/pr_party_github.py``).

Mock style follows ``tests/unit/test_github_service.py``: hand-rolled
``httpx`` mocks via ``unittest.mock``, no ``respx``.

**Fixture provenance.** Every canned payload below is shaped from GitHub's
published REST response schemas so unit-green cannot mean integration-dead.
Each constant names its endpoint and doc source in the comment above it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ontokit.services.pr_party_github import (
    ChecksRollup,
    GenerationModeError,
    GitHubAPIError,
    Mergeability,
    MergeNotAllowedError,
    PRPartyClientMode,
    PRPartyGitHubClient,
    RateLimitedError,
    ReviewEvent,
    ReviewNotSubmittedError,
    SelfApprovalError,
    StaleCardError,
    TokenExpiredError,
    actuation_client,
    generation_client,
)

GENERATION_TOKEN = "github_pat_generation_readonly"  # noqa: S105 - fixture value
ACTUATION_TOKEN = "github_pat_actuation_write"  # noqa: S105 - fixture value

HEAD_SHA = "6dcb09b5b57875f334f61aebed695e2e4193db5e"


# --- Canned GitHub payloads -------------------------------------------------

# GET /user
# docs: REST API endpoints for users -> "Get the authenticated user"
# https://docs.github.com/rest/users/users#get-the-authenticated-user
AUTHENTICATED_USER_RESPONSE: dict[str, Any] = {
    "login": "damienriehl",
    "id": 1234,
    "node_id": "MDQ6VXNlcjEyMzQ=",
    "type": "User",
}

# Header GitHub attaches to responses authenticated with a fine-grained PAT.
# docs: "Managing your personal access tokens" -> token expiration header.
# Documented shape is a space-separated UTC timestamp, e.g. "2026-08-01 15:30:00 UTC".
TOKEN_EXPIRATION_HEADERS = {"github-authentication-token-expiration": "2026-08-01 15:30:00 UTC"}

# GET /users/{login}
# docs: https://docs.github.com/rest/users/users#get-a-user
USER_BY_LOGIN_RESPONSE: dict[str, Any] = {
    "login": "frjohn",
    "id": 4321,
    "node_id": "MDQ6VXNlcjQzMjE=",
    "type": "User",
}

# GET /repos/{owner}/{repo}/pulls/{number}
# docs: https://docs.github.com/rest/pulls/pulls#get-a-pull-request
# `mergeable` is null while GitHub computes the merge commit; `mergeable_state`
# is "unknown" in that window. Both are documented behavior, not an edge case.
PR_DETAIL_RESPONSE: dict[str, Any] = {
    "number": 42,
    "node_id": "PR_kwDOAbcDef4AbcDeg",
    "state": "open",
    "title": "Add the Bl. Carlo Acutis feast",
    "body": "Adds the feast day and its rank.",
    "draft": False,
    "merged": False,
    "mergeable": True,
    "mergeable_state": "clean",
    "html_url": "https://github.com/catholicos/liturgy/pull/42",
    "created_at": "2026-07-20T10:00:00Z",
    "updated_at": "2026-07-25T18:30:00Z",
    "user": {"login": "frjohn", "node_id": "MDQ6VXNlcjQzMjE=", "type": "User"},
    "head": {"ref": "feat/acutis", "sha": HEAD_SHA},
    "base": {
        "ref": "dev",
        "repo": {"full_name": "catholicos/liturgy", "name": "liturgy"},
    },
}

# GET /repos/{owner}/{repo}/commits/{ref}/check-runs
# docs: https://docs.github.com/rest/checks/runs#list-check-runs-for-a-git-reference
# `status` in queued|in_progress|completed|waiting|requested|pending;
# `conclusion` in success|failure|neutral|cancelled|timed_out|action_required|skipped|stale
CHECK_RUNS_ALL_SUCCESS: dict[str, Any] = {
    "total_count": 2,
    "check_runs": [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "tests", "status": "completed", "conclusion": "success"},
    ],
}

CHECK_RUNS_WITH_FAILURE: dict[str, Any] = {
    "total_count": 3,
    "check_runs": [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "tests", "status": "completed", "conclusion": "failure"},
        {"id": 3, "name": "build", "status": "in_progress", "conclusion": None},
    ],
}

CHECK_RUNS_STILL_RUNNING: dict[str, Any] = {
    "total_count": 2,
    "check_runs": [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "tests", "status": "queued", "conclusion": None},
    ],
}

CHECK_RUNS_NEUTRAL_AND_SKIPPED: dict[str, Any] = {
    "total_count": 2,
    "check_runs": [
        {"id": 1, "name": "danger", "status": "completed", "conclusion": "neutral"},
        {"id": 2, "name": "codeql", "status": "completed", "conclusion": "skipped"},
    ],
}

CHECK_RUNS_EMPTY: dict[str, Any] = {"total_count": 0, "check_runs": []}

# GET /search/issues?q=org:{org}+is:pr+is:open
# docs: https://docs.github.com/rest/search/search#search-issues-and-pull-requests
# Items are *issue*-shaped: no `head`, so no head SHA — callers do detail fetches.
# `repository_url` is the only carrier of the repo identity.
SEARCH_RESPONSE: dict[str, Any] = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "number": 42,
            "node_id": "PR_kwDOAbcDef4AbcDeg",
            "title": "Add the Bl. Carlo Acutis feast",
            "state": "open",
            "draft": False,
            "updated_at": "2026-07-25T18:30:00Z",
            "created_at": "2026-07-20T10:00:00Z",
            "html_url": "https://github.com/catholicos/liturgy/pull/42",
            "repository_url": "https://api.github.com/repos/catholicos/liturgy",
            "user": {"login": "frjohn", "node_id": "MDQ6VXNlcjQzMjE=", "type": "User"},
            "pull_request": {"url": "https://api.github.com/repos/catholicos/liturgy/pulls/42"},
        },
        {
            "number": 7,
            "node_id": "PR_kwDOAbcDef4AbcDeh",
            "title": "Bump rdflib",
            "state": "open",
            "draft": True,
            "updated_at": "2026-07-24T09:15:00Z",
            "created_at": "2026-07-24T09:15:00Z",
            "html_url": "https://github.com/catholicos/canon/pull/7",
            "repository_url": "https://api.github.com/repos/catholicos/canon",
            "user": {"login": "dependabot[bot]", "node_id": "MDM6Qm90MQ==", "type": "Bot"},
            "pull_request": {"url": "https://api.github.com/repos/catholicos/canon/pulls/7"},
        },
    ],
}

# POST /repos/{owner}/{repo}/pulls/{number}/reviews
# docs: https://docs.github.com/rest/pulls/reviews#create-a-review-for-a-pull-request
# `state` in APPROVED|CHANGES_REQUESTED|COMMENTED|PENDING|DISMISSED. PENDING means
# the review was created as a draft and never submitted.
REVIEW_APPROVED_RESPONSE: dict[str, Any] = {
    "id": 80,
    "node_id": "MDE3OlB1bGxSZXF1ZXN0UmV2aWV3ODA=",
    "state": "APPROVED",
    "body": "Looks right.",
    "commit_id": HEAD_SHA,
    "submitted_at": "2026-07-25T19:00:00Z",
    "html_url": "https://github.com/catholicos/liturgy/pull/42#pullrequestreview-80",
    "user": {"login": "damienriehl", "node_id": "MDQ6VXNlcjEyMzQ=", "type": "User"},
}

REVIEW_PENDING_RESPONSE: dict[str, Any] = {
    **REVIEW_APPROVED_RESPONSE,
    "state": "PENDING",
    "submitted_at": None,
}

# PUT /repos/{owner}/{repo}/pulls/{number}/merge
# docs: https://docs.github.com/rest/pulls/pulls#merge-a-pull-request
# 409 = "Head branch was modified. Review and try the merge again."
# 405 = "Pull Request is not mergeable."
MERGE_SUCCESS_RESPONSE: dict[str, Any] = {
    "sha": "3a0f2b1c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a",
    "merged": True,
    "message": "Pull Request successfully merged",
}

MERGE_CONFLICT_RESPONSE: dict[str, Any] = {
    "message": "Head branch was modified. Review and try the merge again.",
    "documentation_url": "https://docs.github.com/rest/pulls/pulls#merge-a-pull-request",
}

# POST /repos/{owner}/{repo}/issues/{number}/comments
# docs: https://docs.github.com/rest/issues/comments#create-an-issue-comment
COMMENT_RESPONSE: dict[str, Any] = {
    "id": 991,
    "node_id": "IC_kwDOAbcDef5AbcDeg",
    "body": "Why does this change the rank?",
    "created_at": "2026-07-25T19:05:00Z",
    "updated_at": "2026-07-25T19:05:00Z",
    "html_url": "https://github.com/catholicos/liturgy/pull/42#issuecomment-991",
    "user": {"login": "damienriehl", "node_id": "MDQ6VXNlcjEyMzQ=", "type": "User"},
}

# Error envelopes. docs: https://docs.github.com/rest/using-the-rest-api/troubleshooting
UNAUTHORIZED_RESPONSE: dict[str, Any] = {
    "message": "Bad credentials",
    "documentation_url": "https://docs.github.com/rest",
}

SELF_APPROVAL_RESPONSE: dict[str, Any] = {
    "message": "Unprocessable Entity",
    "errors": ["Can not approve your own pull request"],
    "documentation_url": "https://docs.github.com/rest/pulls/reviews",
}

RATE_LIMIT_RESPONSE: dict[str, Any] = {
    "message": "API rate limit exceeded for user ID 1234.",
    "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",
}

# Reset is documented as a UTC epoch-seconds string.
RATE_LIMIT_RESET_EPOCH = 1785000000
RATE_LIMITED_HEADERS = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "0",
    "x-ratelimit-reset": str(RATE_LIMIT_RESET_EPOCH),
}


# --- Mock helpers (mirrors tests/unit/test_github_service.py) ---------------


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Create a mock ``httpx.Response``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.headers = headers or {}
    return resp


def _make_async_client(request_response: MagicMock | None = None) -> AsyncMock:
    """Create a mock ``httpx.AsyncClient`` usable as an async context manager."""
    client = AsyncMock()
    if request_response is not None:
        client.request = AsyncMock(return_value=request_response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _actuator() -> PRPartyGitHubClient:
    return actuation_client(ACTUATION_TOKEN)


def _generator() -> PRPartyGitHubClient:
    return generation_client(GENERATION_TOKEN)


def _called_url(client: AsyncMock) -> str:
    return str(client.request.call_args.kwargs["url"])


def _called_json(client: AsyncMock) -> dict[str, Any]:
    payload = client.request.call_args.kwargs["json"]
    assert isinstance(payload, dict)
    return payload


class TestClientConstruction:
    """Mode is explicit and factories pin it (KTD13)."""

    def test_factories_set_mode(self) -> None:
        assert _generator().mode is PRPartyClientMode.GENERATION
        assert _actuator().mode is PRPartyClientMode.ACTUATION

    def test_generation_client_cannot_write(self) -> None:
        assert _generator().can_write is False
        assert _actuator().can_write is True

    @pytest.mark.asyncio
    async def test_every_call_carries_an_explicit_timeout(self) -> None:
        """The existing github_service._request has none; this client must."""
        mock_client = _make_async_client(_mock_response(200, AUTHENTICATED_USER_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client) as ctor:
            await _generator().get_authenticated_user()

        timeout = ctor.call_args.kwargs["timeout"]
        assert timeout is not None
        assert timeout.read is not None


class TestGenerationModeRefusesWrites:
    """KTD13: the generation domain holds a read-only token and must not actuate."""

    @pytest.mark.asyncio
    async def test_refuses_create_review_without_http_call(self) -> None:
        with patch("httpx.AsyncClient") as ctor, pytest.raises(GenerationModeError) as exc:
            await _generator().create_review(
                "catholicos", "liturgy", 42, commit_id=HEAD_SHA, event=ReviewEvent.APPROVE
            )
        assert ctor.called is False
        assert "create_review" in str(exc.value)

    @pytest.mark.asyncio
    async def test_refuses_merge_without_http_call(self) -> None:
        with patch("httpx.AsyncClient") as ctor, pytest.raises(GenerationModeError):
            await _generator().merge_pull_request("catholicos", "liturgy", 42, sha=HEAD_SHA)
        assert ctor.called is False

    @pytest.mark.asyncio
    async def test_refuses_create_comment_without_http_call(self) -> None:
        with patch("httpx.AsyncClient") as ctor, pytest.raises(GenerationModeError):
            await _generator().create_comment("catholicos", "liturgy", 42, body="hi")
        assert ctor.called is False

    def test_refusal_error_is_a_dedicated_type(self) -> None:
        assert issubclass(GenerationModeError, PermissionError) or issubclass(
            GenerationModeError, Exception
        )
        assert not issubclass(GenerationModeError, TokenExpiredError)

    @pytest.mark.asyncio
    async def test_read_calls_are_allowed_in_generation_mode(self) -> None:
        mock_client = _make_async_client(_mock_response(200, PR_DETAIL_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            detail = await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert detail.number == 42


class TestCreateReview:
    """R8: a real GitHub review under the reviewer's identity."""

    @pytest.mark.asyncio
    async def test_pins_commit_id_and_sends_non_pending_event(self) -> None:
        mock_client = _make_async_client(_mock_response(200, REVIEW_APPROVED_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            review = await _actuator().create_review(
                "catholicos",
                "liturgy",
                42,
                commit_id=HEAD_SHA,
                event=ReviewEvent.APPROVE,
                body="Looks right.",
            )

        payload = _called_json(mock_client)
        assert payload["commit_id"] == HEAD_SHA
        assert payload["event"] == "APPROVE"
        assert payload["event"] != "PENDING"
        assert payload["body"] == "Looks right."
        assert mock_client.request.call_args.kwargs["method"] == "POST"
        assert _called_url(mock_client).endswith("/repos/catholicos/liturgy/pulls/42/reviews")
        assert review.id == 80
        assert review.state == "APPROVED"
        assert review.commit_id == HEAD_SHA

    @pytest.mark.asyncio
    async def test_pending_response_state_is_a_failure(self) -> None:
        """A PENDING review was never submitted — never report it as a verdict."""
        mock_client = _make_async_client(_mock_response(200, REVIEW_PENDING_RESPONSE))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(ReviewNotSubmittedError),
        ):
            await _actuator().create_review(
                "catholicos", "liturgy", 42, commit_id=HEAD_SHA, event=ReviewEvent.APPROVE
            )

    @pytest.mark.asyncio
    async def test_rejects_a_pending_event_argument(self) -> None:
        with patch("httpx.AsyncClient") as ctor, pytest.raises(ValueError, match="PENDING"):
            await _actuator().create_review(
                "catholicos", "liturgy", 42, commit_id=HEAD_SHA, event="PENDING"
            )
        assert ctor.called is False

    @pytest.mark.asyncio
    async def test_rejects_an_empty_commit_id(self) -> None:
        with (
            patch("httpx.AsyncClient") as ctor,
            pytest.raises(ValueError, match="commit_id"),
        ):
            await _actuator().create_review(
                "catholicos", "liturgy", 42, commit_id="", event=ReviewEvent.APPROVE
            )
        assert ctor.called is False

    @pytest.mark.asyncio
    async def test_self_review_422_maps_to_self_approval_error(self) -> None:
        mock_client = _make_async_client(_mock_response(422, SELF_APPROVAL_RESPONSE))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(SelfApprovalError) as exc,
        ):
            await _actuator().create_review(
                "catholicos", "liturgy", 42, commit_id=HEAD_SHA, event=ReviewEvent.APPROVE
            )

        assert exc.value.status_code == 422
        assert "own pull request" in str(exc.value)

    @pytest.mark.asyncio
    async def test_expired_token_401_maps_to_token_expired_error(self) -> None:
        """A revoked/expired PAT must degrade (R12), not surface as a generic 500."""
        mock_client = _make_async_client(_mock_response(401, UNAUTHORIZED_RESPONSE))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(TokenExpiredError) as exc,
        ):
            await _actuator().create_review(
                "catholicos", "liturgy", 42, commit_id=HEAD_SHA, event=ReviewEvent.APPROVE
            )

        assert exc.value.status_code == 401
        assert not isinstance(exc.value, SelfApprovalError)


class TestMergePullRequest:
    """R11: merge always pins the revision it was authorized against."""

    @pytest.mark.asyncio
    async def test_always_sends_sha(self) -> None:
        mock_client = _make_async_client(_mock_response(200, MERGE_SUCCESS_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _actuator().merge_pull_request(
                "catholicos", "liturgy", 42, sha=HEAD_SHA, merge_method="squash"
            )

        payload = _called_json(mock_client)
        assert payload["sha"] == HEAD_SHA
        assert payload["merge_method"] == "squash"
        assert mock_client.request.call_args.kwargs["method"] == "PUT"
        assert result.merged is True
        assert result.sha == MERGE_SUCCESS_RESPONSE["sha"]

    @pytest.mark.asyncio
    async def test_rejects_an_empty_sha(self) -> None:
        with patch("httpx.AsyncClient") as ctor, pytest.raises(ValueError, match="sha"):
            await _actuator().merge_pull_request("catholicos", "liturgy", 42, sha="")
        assert ctor.called is False

    @pytest.mark.asyncio
    async def test_head_moved_409_maps_to_stale_card_error(self) -> None:
        """Merging against a stale head SHA must fail loudly, never look successful."""
        mock_client = _make_async_client(_mock_response(409, MERGE_CONFLICT_RESPONSE))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(StaleCardError) as exc,
        ):
            await _actuator().merge_pull_request("catholicos", "liturgy", 42, sha=HEAD_SHA)

        assert exc.value.status_code == 409
        assert "Head branch was modified" in str(exc.value)

    @pytest.mark.asyncio
    async def test_not_mergeable_405_is_not_a_stale_card(self) -> None:
        mock_client = _make_async_client(
            _mock_response(405, {"message": "Pull Request is not mergeable"})
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(MergeNotAllowedError) as exc,
        ):
            await _actuator().merge_pull_request("catholicos", "liturgy", 42, sha=HEAD_SHA)

        assert not isinstance(exc.value, StaleCardError)

    @pytest.mark.asyncio
    async def test_409_outside_merge_is_not_a_stale_card(self) -> None:
        """The 409 -> StaleCardError mapping is merge-specific, not global."""
        mock_client = _make_async_client(_mock_response(409, {"message": "Conflict"}))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(GitHubAPIError) as exc,
        ):
            await _actuator().get_pull_request("catholicos", "liturgy", 42)

        assert not isinstance(exc.value, StaleCardError)


class TestCreateComment:
    """R13: questions ride PR comments."""

    @pytest.mark.asyncio
    async def test_posts_to_the_issue_comments_endpoint(self) -> None:
        mock_client = _make_async_client(_mock_response(201, COMMENT_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            comment = await _actuator().create_comment(
                "catholicos", "liturgy", 42, body="Why does this change the rank?"
            )

        assert _called_url(mock_client).endswith("/repos/catholicos/liturgy/issues/42/comments")
        assert _called_json(mock_client)["body"] == "Why does this change the rank?"
        assert comment.id == 991
        assert comment.user_login == "damienriehl"


class TestGetPullRequest:
    """R4: mergeability has three states, and null is not 'no'."""

    @pytest.mark.asyncio
    async def test_parses_detail_fields(self) -> None:
        mock_client = _make_async_client(_mock_response(200, PR_DETAIL_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            detail = await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert detail.repo_full_name == "catholicos/liturgy"
        assert detail.number == 42
        assert detail.head_sha == HEAD_SHA
        assert detail.base_ref == "dev"
        assert detail.author_login == "frjohn"
        assert detail.author_node_id == "MDQ6VXNlcjQzMjE="
        assert detail.node_id == "PR_kwDOAbcDef4AbcDeg"
        assert detail.state == "open"
        assert detail.draft is False
        assert detail.updated_at == datetime(2026, 7, 25, 18, 30, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_mergeable_true_is_mergeable(self) -> None:
        mock_client = _make_async_client(_mock_response(200, PR_DETAIL_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            detail = await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert detail.mergeability is Mergeability.MERGEABLE
        assert detail.is_computing is False

    @pytest.mark.asyncio
    async def test_mergeable_null_is_computing_not_unmergeable(self) -> None:
        """GitHub returns mergeable=null while it computes — retry later, never 'no'."""
        payload = {**PR_DETAIL_RESPONSE, "mergeable": None, "mergeable_state": "unknown"}
        mock_client = _make_async_client(_mock_response(200, payload))

        with patch("httpx.AsyncClient", return_value=mock_client):
            detail = await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert detail.mergeability is Mergeability.COMPUTING
        assert detail.mergeability is not Mergeability.NOT_MERGEABLE
        assert detail.is_computing is True

    @pytest.mark.asyncio
    async def test_mergeable_false_is_not_mergeable(self) -> None:
        payload = {**PR_DETAIL_RESPONSE, "mergeable": False, "mergeable_state": "dirty"}
        mock_client = _make_async_client(_mock_response(200, payload))

        with patch("httpx.AsyncClient", return_value=mock_client):
            detail = await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert detail.mergeability is Mergeability.NOT_MERGEABLE
        assert detail.mergeable_state == "dirty"

    @pytest.mark.asyncio
    async def test_encodes_user_controlled_path_segments(self) -> None:
        mock_client = _make_async_client(_mock_response(200, PR_DETAIL_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            await _generator().get_pull_request("cath/../evil", "liturgy", 42)

        assert "cath%2F..%2Fevil" in _called_url(mock_client)


class TestCheckRunsRollup:
    """R4: one summary status for a head SHA."""

    @pytest.mark.asyncio
    async def test_all_success(self) -> None:
        mock_client = _make_async_client(_mock_response(200, CHECK_RUNS_ALL_SUCCESS))

        with patch("httpx.AsyncClient", return_value=mock_client):
            rollup = await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)

        assert rollup is ChecksRollup.SUCCESS
        assert _called_url(mock_client).startswith(
            f"https://api.github.com/repos/catholicos/liturgy/commits/{HEAD_SHA}/check-runs"
        )

    @pytest.mark.asyncio
    async def test_any_failure_is_failure(self) -> None:
        mock_client = _make_async_client(_mock_response(200, CHECK_RUNS_WITH_FAILURE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            rollup = await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)

        assert rollup is ChecksRollup.FAILURE

    @pytest.mark.asyncio
    async def test_any_incomplete_is_pending(self) -> None:
        mock_client = _make_async_client(_mock_response(200, CHECK_RUNS_STILL_RUNNING))

        with patch("httpx.AsyncClient", return_value=mock_client):
            rollup = await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)

        assert rollup is ChecksRollup.PENDING

    @pytest.mark.asyncio
    async def test_neutral_and_skipped_do_not_fail_the_rollup(self) -> None:
        mock_client = _make_async_client(_mock_response(200, CHECK_RUNS_NEUTRAL_AND_SKIPPED))

        with patch("httpx.AsyncClient", return_value=mock_client):
            rollup = await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)

        assert rollup is ChecksRollup.SUCCESS

    @pytest.mark.asyncio
    async def test_no_check_runs_is_none_not_success(self) -> None:
        """Zero configured checks is a distinct fact from 'everything passed'."""
        mock_client = _make_async_client(_mock_response(200, CHECK_RUNS_EMPTY))

        with patch("httpx.AsyncClient", return_value=mock_client):
            rollup = await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)

        assert rollup is ChecksRollup.NONE


class TestOrgSearch:
    """R1: one org-scoped search feeds the sweep."""

    @pytest.mark.asyncio
    async def test_builds_the_org_scoped_query(self) -> None:
        mock_client = _make_async_client(_mock_response(200, SEARCH_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            await _generator().search_org_open_prs("catholicos")

        url = _called_url(mock_client)
        assert "/search/issues?q=" in url
        assert "org%3Acatholicos" in url
        assert "is%3Apr" in url
        assert "is%3Aopen" in url

    @pytest.mark.asyncio
    async def test_parses_issue_shaped_items(self) -> None:
        mock_client = _make_async_client(_mock_response(200, SEARCH_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            results = await _generator().search_org_open_prs("catholicos")

        assert len(results) == 2
        first = results[0]
        assert first.repo_full_name == "catholicos/liturgy"
        assert first.number == 42
        assert first.state == "open"
        assert first.title == "Add the Bl. Carlo Acutis feast"
        assert first.author_login == "frjohn"
        assert first.updated_at == datetime(2026, 7, 25, 18, 30, tzinfo=UTC)
        assert first.draft is False

        second = results[1]
        assert second.repo_full_name == "catholicos/canon"
        assert second.author_login == "dependabot[bot]"
        assert second.author_type == "Bot"
        assert second.draft is True

    @pytest.mark.asyncio
    async def test_search_items_carry_no_head_sha(self) -> None:
        """Documented shape: search returns issues, so callers must detail-fetch."""
        mock_client = _make_async_client(_mock_response(200, SEARCH_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            results = await _generator().search_org_open_prs("catholicos")

        assert not hasattr(results[0], "head_sha")

    @pytest.mark.asyncio
    async def test_encodes_the_org_segment(self) -> None:
        mock_client = _make_async_client(_mock_response(200, SEARCH_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            await _generator().search_org_open_prs("evil org&q=x")

        url = _called_url(mock_client)
        # The injected "&q=x" must survive as encoded text, never as a second param.
        assert "%26q%3Dx" in url
        assert "&q=x" not in url


class TestUserLookups:
    """KTD12: logins resolve to rename-proof node ids."""

    @pytest.mark.asyncio
    async def test_get_user_returns_login_and_node_id(self) -> None:
        mock_client = _make_async_client(_mock_response(200, USER_BY_LOGIN_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            user = await _generator().get_user("frjohn")

        assert user.login == "frjohn"
        assert user.node_id == "MDQ6VXNlcjQzMjE="
        assert _called_url(mock_client).endswith("/users/frjohn")

    @pytest.mark.asyncio
    async def test_authenticated_user_returns_login_and_node_id(self) -> None:
        mock_client = _make_async_client(_mock_response(200, AUTHENTICATED_USER_RESPONSE))

        with patch("httpx.AsyncClient", return_value=mock_client):
            identity = await _actuator().get_authenticated_user()

        assert identity.login == "damienriehl"
        assert identity.node_id == "MDQ6VXNlcjEyMzQ="
        assert _called_url(mock_client).endswith("/user")


class TestTokenExpirationHeader:
    """KTD13: expires_at comes from GitHub's header, not from guesswork."""

    @pytest.mark.asyncio
    async def test_parsed_when_present(self) -> None:
        mock_client = _make_async_client(
            _mock_response(200, AUTHENTICATED_USER_RESPONSE, TOKEN_EXPIRATION_HEADERS)
        )
        client = _actuator()

        with patch("httpx.AsyncClient", return_value=mock_client):
            identity = await client.get_authenticated_user()

        expected = datetime(2026, 8, 1, 15, 30, tzinfo=UTC)
        assert identity.token_expires_at == expected
        assert client.last_token_expires_at == expected

    @pytest.mark.asyncio
    async def test_none_when_absent(self) -> None:
        """Classic PATs with no expiry send no header — that is None, not an error."""
        mock_client = _make_async_client(_mock_response(200, AUTHENTICATED_USER_RESPONSE))
        client = _actuator()

        with patch("httpx.AsyncClient", return_value=mock_client):
            identity = await client.get_authenticated_user()

        assert identity.token_expires_at is None
        assert client.last_token_expires_at is None

    @pytest.mark.asyncio
    async def test_unparseable_header_does_not_raise(self) -> None:
        mock_client = _make_async_client(
            _mock_response(
                200,
                AUTHENTICATED_USER_RESPONSE,
                {"github-authentication-token-expiration": "not a date"},
            )
        )

        with patch("httpx.AsyncClient", return_value=mock_client):
            identity = await _actuator().get_authenticated_user()

        assert identity.token_expires_at is None


class TestRateLimiting:
    """403 + exhausted budget is a distinct, retry-at-a-known-time condition."""

    @pytest.mark.asyncio
    async def test_403_with_zero_remaining_maps_to_rate_limited(self) -> None:
        mock_client = _make_async_client(
            _mock_response(403, RATE_LIMIT_RESPONSE, RATE_LIMITED_HEADERS)
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RateLimitedError) as exc,
        ):
            await _generator().search_org_open_prs("catholicos")

        assert exc.value.reset_at == datetime.fromtimestamp(RATE_LIMIT_RESET_EPOCH, tz=UTC)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_429_with_zero_remaining_maps_to_rate_limited(self) -> None:
        mock_client = _make_async_client(
            _mock_response(429, RATE_LIMIT_RESPONSE, RATE_LIMITED_HEADERS)
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RateLimitedError),
        ):
            await _generator().search_org_open_prs("catholicos")

    @pytest.mark.asyncio
    async def test_403_without_exhausted_budget_is_a_plain_api_error(self) -> None:
        mock_client = _make_async_client(
            _mock_response(
                403, {"message": "Resource not accessible"}, {"x-ratelimit-remaining": "42"}
            )
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(GitHubAPIError) as exc,
        ):
            await _generator().search_org_open_prs("catholicos")

        assert not isinstance(exc.value, RateLimitedError)

    @pytest.mark.asyncio
    async def test_secondary_rate_limit_surfaces_retry_after(self) -> None:
        mock_client = _make_async_client(
            _mock_response(
                403,
                {"message": "You have exceeded a secondary rate limit"},
                {"x-ratelimit-remaining": "0", "retry-after": "60"},
            )
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RateLimitedError) as exc,
        ):
            await _generator().search_org_open_prs("catholicos")

        assert exc.value.retry_after_seconds == 60


class TestErrorTaxonomy:
    """All PR Party GitHub failures share one root so callers can catch broadly."""

    def test_all_errors_share_a_root(self) -> None:
        from ontokit.services.pr_party_github import PRPartyGitHubError

        for exc_type in (
            TokenExpiredError,
            RateLimitedError,
            SelfApprovalError,
            StaleCardError,
            MergeNotAllowedError,
            ReviewNotSubmittedError,
            GenerationModeError,
            GitHubAPIError,
        ):
            assert issubclass(exc_type, PRPartyGitHubError)

    @pytest.mark.asyncio
    async def test_unmapped_status_is_a_generic_api_error(self) -> None:
        mock_client = _make_async_client(_mock_response(500, {"message": "Server Error"}))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(GitHubAPIError) as exc,
        ):
            await _generator().get_pull_request("catholicos", "liturgy", 42)

        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_401_on_a_read_call_is_also_token_expired(self) -> None:
        """The mapping lives in one place, so the generation token gets it too."""
        mock_client = _make_async_client(_mock_response(401, UNAUTHORIZED_RESPONSE))

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(TokenExpiredError),
        ):
            await _generator().get_check_runs_rollup("catholicos", "liturgy", HEAD_SHA)
