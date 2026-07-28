"""Tests for the PR Party verdict endpoint (U6).

The contracts these pin:

- **The PR identity comes from the server, never the client.** The request body
  carries no repo, no PR number, and no reviewer — only a card id, the head SHA
  the reviewer was looking at, and an idempotency key. There is structurally no
  way to aim a verdict at a different PR or cast one as someone else (R23).
- **KTD16 idempotency is durable, not best-effort.** The action row is inserted
  ``pending`` and committed *before* the GitHub call, so a crash mid-flight
  leaves evidence rather than a silent double review. A replayed key returns the
  stored receipt; a different key at the same head is refused; a ``failed`` row
  is re-opened rather than duplicated.
- **Every refusal is decided server-side (KTD19).** Readiness, drift, own-PR,
  merge authorization, and PR lifecycle are all re-checked from the row at
  actuation time — the card the client rendered is an input, not an authority.
- **Degradation is a first-class outcome (R12).** No credential, or a PAT that
  died mid-verdict, records the reviewer's intent and hands back a deep link
  instead of failing the tap.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from ontokit.api.routes.pr_party import (
    get_action_service,
    get_actions_redis,
    get_queue_reader,
)
from ontokit.api.routes.pr_party_settings import get_credential_service
from ontokit.main import app
from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyCredential,
    PRPartyMergeDefault,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.pr_party import (
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_COMMENT,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PR_PARTY_VERDICT_REQUEST_CHANGES,
    PRPartyActionRequest,
)
from ontokit.services.pr_party_actions import PRPartyActionService
from ontokit.services.pr_party_github import (
    GitHubAPIError,
    MergeNotAllowedError,
    MergeResult,
    PRPartyReview,
    ReviewNotSubmittedError,
    SelfApprovalError,
    StaleCardError,
    TokenExpiredError,
    actuation_client,
)
from ontokit.services.pr_party_rate_limiter import LimiterOutcome, action_key, check_and_consume

BASE = "/api/v1/pr-party"
USER_ID = "test-user-id"

REPO = "catholicos/ontokit-api"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
KEY = "idem-key-0001"
OTHER_KEY = "idem-key-0002"


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------


def _reviewer(
    login: str = "damienriehl",
    node_id: str | None = "MDQ6VXNlcjE=",
    zitadel_user_id: str = USER_ID,
) -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=zitadel_user_id,
        github_login=login,
        github_node_id=node_id,
        merge_default=PRPartyMergeDefault.MANUAL,
    )
    row.id = uuid.uuid4()
    return row


def _pr(
    *,
    state: str = "open",
    head_sha: str = HEAD,
    author_kind: PRPartyAuthorKind = PRPartyAuthorKind.COUNTERPART,
    author_github_login: str | None = "someone-else",
    author_node_id: str | None = "MDQ6VXNlcjk=",
    mergeable_state: str | None = "clean",
    checks_rollup: str | None = "success",
    brief_status: PRPartyBriefStatus = PRPartyBriefStatus.READY,
) -> PRPartyPR:
    row = PRPartyPR(
        repo_full_name=REPO,
        pr_number=42,
        title="Add the PR Party verdict endpoint",
        state=state,
        head_sha=head_sha,
        author_kind=author_kind,
        author_github_login=author_github_login,
        author_node_id=author_node_id,
        mergeable_state=mergeable_state,
        checks_rollup=checks_rollup,
        brief_status=brief_status,
        brief_what="Adds the verdict endpoint.",
        brief_why="A tap has to become a real review.",
        brief_truncated=False,
        ready_at=datetime.now(UTC),
    )
    row.id = uuid.uuid4()
    return row


def _action(
    reviewer: PRPartyReviewer,
    pr: PRPartyPR,
    *,
    kind: PRPartyActionKind = PRPartyActionKind.REVIEW,
    verdict: str | None = PR_PARTY_VERDICT_APPROVE,
    status: PRPartyActionStatus = PRPartyActionStatus.SUCCEEDED,
    head_sha: str | None = None,
    idempotency_key: str = KEY,
    github_review_id: int | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> PRPartyAction:
    row = PRPartyAction(
        reviewer_id=reviewer.id,
        pr_id=pr.id,
        head_sha=head_sha or pr.head_sha,
        action_kind=kind,
        verdict=verdict,
        override=False,
        status=status,
        idempotency_key=idempotency_key,
        github_review_id=github_review_id,
        error=error,
    )
    row.id = uuid.uuid4()
    row.created_at = created_at or datetime.now(UTC)
    row.updated_at = updated_at
    return row


# ---------------------------------------------------------------------------
# Fakes at the dependency boundary
# ---------------------------------------------------------------------------


class _FakeCredentialServiceForAuth:
    """The slice of PRPartyCredentialService the reviewer guard uses."""

    def __init__(self, reviewer: PRPartyReviewer | None) -> None:
        self.reviewer = reviewer

    async def get_reviewer(self, zitadel_user_id: str) -> PRPartyReviewer | None:
        if self.reviewer is not None and self.reviewer.zitadel_user_id == zitadel_user_id:
            return self.reviewer
        return None


class _FakeCredentials:
    """Token resolution plus the credential row the actuation path marks."""

    def __init__(self, token: str | None = "ghp_token", reviewer: PRPartyReviewer | None = None):
        self.token = token
        self.credential = (
            PRPartyCredential(
                reviewer_id=reviewer.id if reviewer else uuid.uuid4(),
                encrypted_token="x",
                last_validated_at=datetime.now(UTC),
            )
            if reviewer
            else None
        )

    async def resolve_token(self, _reviewer: PRPartyReviewer) -> str | None:
        return self.token

    async def get_credential(self, _reviewer_id: uuid.UUID) -> PRPartyCredential | None:
        return self.credential


class _FakeStore:
    """In-memory stand-in for the action table with an explicit commit count."""

    def __init__(self, actions: list[PRPartyAction] | None = None) -> None:
        self.rows: list[PRPartyAction] = list(actions or [])
        self.commits = 0
        self.deleted: list[PRPartyAction] = []
        #: Raise on the Nth ``save`` call (1-based) to simulate a crash.
        self.fail_save_on_call: int | None = None
        self._saves = 0
        self._committed: dict[int, tuple[Any, Any, Any, Any]] = {}

    async def find_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None:
        matches = [
            a
            for a in self.rows
            if a.reviewer_id == reviewer_id
            and a.pr_id == pr_id
            and a.head_sha == head_sha
            and a.action_kind == action_kind
        ]
        live = [a for a in matches if a.status != PRPartyActionStatus.FAILED]
        pool = live or matches
        return pool[-1] if pool else None

    async def claim_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None:
        return await self.find_action(
            reviewer_id=reviewer_id,
            pr_id=pr_id,
            head_sha=head_sha,
            action_kind=action_kind,
        )

    async def actions_at_head(self, *, pr_id: uuid.UUID, head_sha: str) -> list[PRPartyAction]:
        return [a for a in self.rows if a.pr_id == pr_id and a.head_sha == head_sha]

    async def persist(self, action: PRPartyAction) -> None:
        if action not in self.rows:
            self.rows.append(action)
        self._commit()

    async def save(self) -> None:
        self._saves += 1
        if self.fail_save_on_call is not None and self._saves == self.fail_save_on_call:
            self._rollback()
            raise RuntimeError("simulated crash between the GitHub call and finalize")
        self._commit()

    async def delete(self, action: PRPartyAction) -> None:
        # In place: the reader shares this list, exactly as it shares a session.
        self.rows.remove(action)
        self.deleted.append(action)
        self._commit()

    async def rollback(self) -> None:
        self._rollback()

    def _commit(self) -> None:
        self.commits += 1
        self._committed = {
            id(a): (a.status, a.error, a.github_review_id, a.idempotency_key) for a in self.rows
        }

    def _rollback(self) -> None:
        """A failed commit leaves the DATABASE at its last committed state."""
        for action in self.rows:
            snapshot = self._committed.get(id(action))
            if snapshot is not None:
                (
                    action.status,
                    action.error,
                    action.github_review_id,
                    action.idempotency_key,
                ) = snapshot


class _LockingStore(_FakeStore):
    """Unit-level model of PostgreSQL's ``SELECT ... FOR UPDATE`` claim."""

    def __init__(self, actions: list[PRPartyAction]) -> None:
        super().__init__(actions)
        self._claim_lock = asyncio.Lock()

    async def claim_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None:
        await self._claim_lock.acquire()
        return await super().claim_action(
            reviewer_id=reviewer_id,
            pr_id=pr_id,
            head_sha=head_sha,
            action_kind=action_kind,
        )

    async def persist(self, action: PRPartyAction) -> None:
        action.updated_at = datetime.now(UTC)
        try:
            await super().persist(action)
        finally:
            self._claim_lock.release()


class _FakeGitHub:
    """Records calls; every method can be armed with an exception instead."""

    def __init__(
        self,
        *,
        review: PRPartyReview | None = None,
        merge: MergeResult | None = None,
        review_error: Exception | None = None,
        merge_error: Exception | None = None,
        reviews: list[PRPartyReview] | None = None,
        reviews_error: Exception | None = None,
    ) -> None:
        self.review = review or PRPartyReview(
            id=9_000_000_001,
            state="APPROVED",
            body=None,
            commit_id=HEAD,
            user_login="damienriehl",
            submitted_at=datetime.now(UTC),
            html_url=f"https://github.com/{REPO}/pull/42#pullrequestreview-1",
        )
        self.merge = merge or MergeResult(sha="c" * 40, merged=True, message="Pull Request merged")
        self.review_error = review_error
        self.merge_error = merge_error
        #: What ``GET .../pulls/{n}/reviews`` already holds — the reclaim path's
        #: duplicate check reads this.
        self.reviews = list(reviews or [])
        self.reviews_error = reviews_error
        self.review_calls: list[dict[str, Any]] = []
        self.merge_calls: list[dict[str, Any]] = []
        self.list_reviews_calls: list[tuple[str, str, int]] = []

    async def get_pr_reviews(self, owner: str, repo: str, number: int) -> list[PRPartyReview]:
        self.list_reviews_calls.append((owner, repo, number))
        if self.reviews_error is not None:
            raise self.reviews_error
        return list(self.reviews)

    async def create_review(
        self, owner: str, repo: str, number: int, **kwargs: Any
    ) -> PRPartyReview:
        self.review_calls.append({"owner": owner, "repo": repo, "number": number, **kwargs})
        if self.review_error is not None:
            raise self.review_error
        return self.review

    async def merge_pull_request(
        self, owner: str, repo: str, number: int, **kwargs: Any
    ) -> MergeResult:
        self.merge_calls.append({"owner": owner, "repo": repo, "number": number, **kwargs})
        if self.merge_error is not None:
            raise self.merge_error
        return self.merge


class _FakeReader:
    """Rows returned unfiltered so the route's own visibility gate is exercised."""

    def __init__(
        self, prs: list[PRPartyPR] | None = None, actions: list[PRPartyAction] | None = None
    ) -> None:
        self.prs = prs if prs is not None else []
        self.actions = actions if actions is not None else []

    async def list_open_prs(self) -> list[PRPartyPR]:
        return list(self.prs)

    async def get_pr(self, card_id: uuid.UUID) -> PRPartyPR | None:
        return next((p for p in self.prs if p.id == card_id), None)

    async def list_actions(self, pr_ids: list[uuid.UUID]) -> list[PRPartyAction]:
        wanted = set(pr_ids)
        return [a for a in self.actions if a.pr_id in wanted]


class _FakeRedis:
    """Counter-only Redis. ``error`` arms the fail-closed path."""

    def __init__(self, *, start: int = 0, error: Exception | None = None) -> None:
        self.counts: dict[str, int] = {}
        self.start = start
        self.error = error
        self.expired: list[str] = []

    async def incr(self, name: str) -> int:
        if self.error is not None:
            raise self.error
        self.counts[name] = self.counts.get(name, self.start) + 1
        return self.counts[name]

    async def expire(self, name: str, time: int) -> bool:  # noqa: ARG002
        self.expired.append(name)
        return True

    async def get(self, name: str) -> bytes | None:
        if self.error is not None:
            raise self.error
        value = self.counts.get(name)
        return None if value is None else str(value).encode()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(authed_client: tuple[TestClient, AsyncMock]) -> Any:
    """(client, install) — ``install(...)`` binds every fake in one call."""
    client, _db = authed_client

    def install(
        reviewer: PRPartyReviewer | None,
        *,
        prs: list[PRPartyPR] | None = None,
        store: _FakeStore | None = None,
        github: _FakeGitHub | None = None,
        token: str | None = "ghp_token",
        redis: _FakeRedis | None = None,
    ) -> dict[str, Any]:
        store = store if store is not None else _FakeStore()
        github = github or _FakeGitHub()
        credentials = _FakeCredentials(token, reviewer)
        redis = redis if redis is not None else _FakeRedis()

        service = PRPartyActionService(
            store=store,
            credentials=credentials,  # type: ignore[arg-type]
            actuation_factory=lambda _token: github,  # type: ignore[arg-type,return-value]
        )
        reader = _FakeReader(prs, store.rows)

        app.dependency_overrides[get_credential_service] = lambda: _FakeCredentialServiceForAuth(
            reviewer
        )
        app.dependency_overrides[get_queue_reader] = lambda: reader
        app.dependency_overrides[get_action_service] = lambda: service
        app.dependency_overrides[get_actions_redis] = lambda: redis
        return {
            "store": store,
            "github": github,
            "credentials": credentials,
            "redis": redis,
            "reader": reader,
        }

    return client, install


def _gh_review(
    *,
    review_id: int = 9_000_000_777,
    state: str = "APPROVED",
    commit_id: str | None = HEAD,
    login: str | None = "damienriehl",
    node_id: str | None = "MDQ6VXNlcjE=",
) -> PRPartyReview:
    """A review GitHub already holds, as ``get_pr_reviews`` would return it."""
    return PRPartyReview(
        id=review_id,
        state=state,
        body=None,
        commit_id=commit_id,
        user_login=login,
        submitted_at=datetime.now(UTC),
        html_url=f"https://github.com/{REPO}/pull/42#pullrequestreview-{review_id}",
        user_node_id=node_id,
    )


def _body(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_kind": "review",
        "verdict": PR_PARTY_VERDICT_APPROVE,
        "head_sha": HEAD,
        "idempotency_key": KEY,
    }
    payload.update(overrides)
    return payload


def _post(client: TestClient, pr: PRPartyPR, **overrides: Any) -> Any:
    return client.post(f"{BASE}/cards/{pr.id}/actions", json=_body(**overrides))


# ---------------------------------------------------------------------------
# Access control and identity
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_non_reviewer_is_403(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(None, prs=[pr])
        assert _post(client, pr).status_code == 403

    def test_unknown_card_is_404(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), prs=[])
        response = client.post(f"{BASE}/cards/{uuid.uuid4()}/actions", json=_body())
        assert response.status_code == 404

    def test_body_cannot_name_another_reviewer(self, wired: Any) -> None:
        """R23: there is no reviewer field, so extra keys are simply ignored."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])

        response = _post(client, pr, reviewer_id=str(uuid.uuid4()), repo_full_name="evil/repo")
        assert response.status_code == 200, response.text

        # The GitHub call used the repo off the SERVER's row, not the body's.
        call = fakes["github"].review_calls[0]
        assert (call["owner"], call["repo"], call["number"]) == ("catholicos", "ontokit-api", 42)
        assert fakes["store"].rows[0].reviewer_id == reviewer.id

    def test_reviewing_own_pr_is_403_by_node_id(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(node_id="MDQ6VXNlcjE=")
        pr = _pr(author_github_login="damienriehl", author_node_id="MDQ6VXNlcjE=")
        fakes = install(reviewer, prs=[pr])

        response = _post(client, pr)
        assert response.status_code == 403
        assert fakes["github"].review_calls == []

    def test_renamed_login_does_not_make_a_pr_own(self, wired: Any) -> None:
        """A node-id mismatch ends the question — matching logins do not rescue it."""
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id="MDQ6VXNlcjE=")
        pr = _pr(author_github_login="damienriehl", author_node_id="MDQ6VXNlcjk=")
        install(reviewer, prs=[pr])
        assert _post(client, pr).status_code == 200


# ---------------------------------------------------------------------------
# AE1 — the happy path
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_with_body_posts_one_review_and_returns_the_receipt(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])

        response = _post(client, pr, body="Two suggestions inline; both optional.")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert len(fakes["github"].review_calls) == 1
        call = fakes["github"].review_calls[0]
        assert call["event"] == "APPROVE"
        assert call["commit_id"] == HEAD
        assert call["body"] == "Two suggestions inline; both optional."

        assert payload["action"]["status"] == "succeeded"
        assert payload["action"]["github_review_id"] == 9_000_000_001
        assert payload["action"]["head_sha"] == HEAD
        assert payload["degraded"] is False
        assert payload["replayed"] is False
        assert payload["card"]["card_id"] == str(pr.id)

    def test_request_changes_and_comment_map_to_github_events(self, wired: Any) -> None:
        for verdict, event in (
            (PR_PARTY_VERDICT_REQUEST_CHANGES, "REQUEST_CHANGES"),
            (PR_PARTY_VERDICT_COMMENT, "COMMENT"),
        ):
            client, install = wired
            pr = _pr()
            fakes = install(_reviewer(), prs=[pr])
            response = _post(client, pr, verdict=verdict, body="notes")
            assert response.status_code == 200, response.text
            assert fakes["github"].review_calls[0]["event"] == event

    def test_pending_review_response_fails_the_row(self, wired: Any) -> None:
        """A PENDING review is a draft nobody sees — never a recorded verdict."""
        client, install = wired
        pr = _pr()
        fakes = install(
            _reviewer(),
            prs=[pr],
            github=_FakeGitHub(review_error=ReviewNotSubmittedError(7, "PENDING")),
        )
        response = _post(client, pr)
        assert response.status_code == 502
        assert fakes["store"].rows[0].status == PRPartyActionStatus.FAILED


# ---------------------------------------------------------------------------
# AE6 — readiness and override
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_brewing_without_override_is_409_with_the_reason(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_status=PRPartyBriefStatus.BREWING)
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr)
        assert response.status_code == 409
        assert "AI review still running" in response.text
        assert fakes["github"].review_calls == []

    def test_brewing_with_override_proceeds_and_records_the_override(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_status=PRPartyBriefStatus.BREWING, checks_rollup="pending")
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr, override=True)
        assert response.status_code == 200, response.text
        assert response.json()["action"]["override"] is True
        assert fakes["store"].rows[0].override is True
        assert len(fakes["github"].review_calls) == 1

    def test_discuss_live_bypasses_readiness(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_status=PRPartyBriefStatus.BREWING)
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)
        assert response.status_code == 200, response.text
        assert fakes["github"].review_calls == []


# ---------------------------------------------------------------------------
# Drift and lifecycle
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_head_moved_is_409_with_a_fresh_card_and_no_github_call(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(head_sha=HEAD)
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr, head_sha=OLD_HEAD)
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["card"]["head_sha"] == HEAD
        assert fakes["github"].review_calls == []

    def test_externally_merged_returns_a_retire_signal(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(state="merged")
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr)
        assert response.status_code == 409
        assert response.json()["detail"]["retire"] is True
        assert fakes["github"].review_calls == []

    def test_dirty_mergeability_blocks_a_merge(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr(mergeable_state="dirty")
        approval = _action(_reviewer(zitadel_user_id="other"), pr)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([approval]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409
        assert "conflict" in response.text.lower() or "dirty" in response.text.lower()
        assert fakes["github"].merge_calls == []


# ---------------------------------------------------------------------------
# Merge authorization (R11/R18)
# ---------------------------------------------------------------------------


class TestMergeAuthorization:
    def test_merge_without_an_approval_at_head_is_409(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(author_github_login="damienriehl", author_node_id="MDQ6VXNlcjE=")
        fakes = install(_reviewer(), prs=[pr])

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409
        assert fakes["github"].merge_calls == []

    def test_author_may_merge_once_a_counterpart_approval_exists_at_head(self, wired: Any) -> None:
        client, install = wired
        author = _reviewer(login="damienriehl", node_id="MDQ6VXNlcjE=")
        pr = _pr(author_github_login="damienriehl", author_node_id="MDQ6VXNlcjE=")
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        fakes = install(author, prs=[pr], store=_FakeStore([approval]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 200, response.text
        assert response.json()["action"]["merged"] is True
        assert fakes["github"].merge_calls[0]["sha"] == HEAD

    def test_approval_at_an_older_head_does_not_authorize_a_merge(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        stale_approval = _action(_reviewer(zitadel_user_id="counterpart"), pr, head_sha=OLD_HEAD)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([stale_approval]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409
        assert fakes["github"].merge_calls == []

    def test_merge_method_defaults_to_squash_and_is_overridable(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        fakes = install(_reviewer(), prs=[pr], store=_FakeStore([approval]))
        assert _post(client, pr, action_kind="merge", verdict=None).status_code == 200
        assert fakes["github"].merge_calls[0]["merge_method"] == "squash"

        client2, install2 = wired
        pr2 = _pr()
        approval2 = _action(_reviewer(zitadel_user_id="counterpart"), pr2)
        fakes2 = install2(_reviewer(), prs=[pr2], store=_FakeStore([approval2]))
        assert (
            _post(
                client2, pr2, action_kind="merge", verdict=None, merge_method="rebase"
            ).status_code
            == 200
        )
        assert fakes2["github"].merge_calls[0]["merge_method"] == "rebase"


# ---------------------------------------------------------------------------
# KTD16 idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_double_tap_same_key_replays_one_github_call(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])

        first = _post(client, pr)
        second = _post(client, pr)

        assert first.status_code == 200
        assert second.status_code == 200, second.text
        assert second.json()["replayed"] is True
        assert second.json()["action"]["github_review_id"] == 9_000_000_001
        assert len(fakes["github"].review_calls) == 1
        assert len(fakes["store"].rows) == 1

    def test_different_key_at_the_same_head_is_409(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])

        assert _post(client, pr).status_code == 200
        second = _post(client, pr, idempotency_key=OTHER_KEY)
        assert second.status_code == 409
        assert "already" in second.text.lower()
        assert len(fakes["github"].review_calls) == 1

    def test_pending_inside_the_reclaim_window_is_409_in_flight(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        in_flight = _action(reviewer, pr, status=PRPartyActionStatus.PENDING)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([in_flight]))

        response = _post(client, pr, idempotency_key=OTHER_KEY)
        assert response.status_code == 409
        assert "in flight" in response.text.lower()
        assert fakes["github"].review_calls == []

    def test_pending_older_than_the_window_is_reclaimed(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        abandoned = _action(
            reviewer,
            pr,
            status=PRPartyActionStatus.PENDING,
            created_at=datetime.now(UTC) - timedelta(hours=3),
        )
        fakes = install(reviewer, prs=[pr], store=_FakeStore([abandoned]))

        response = _post(client, pr, idempotency_key=OTHER_KEY)
        assert response.status_code == 200, response.text
        assert len(fakes["store"].rows) == 1
        assert fakes["store"].rows[0].idempotency_key == OTHER_KEY
        assert len(fakes["github"].review_calls) == 1

    def test_failed_row_is_reopened_not_duplicated(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        dead = _action(
            reviewer,
            pr,
            kind=PRPartyActionKind.MERGE,
            verdict=None,
            status=PRPartyActionStatus.FAILED,
            error="GitHubAPIError (HTTP 500)",
        )
        fakes = install(reviewer, prs=[pr], store=_FakeStore([approval, dead]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 200, response.text
        rows = [r for r in fakes["store"].rows if r.reviewer_id == reviewer.id]
        assert len(rows) == 1
        assert rows[0] is dead
        assert rows[0].status == PRPartyActionStatus.SUCCEEDED
        assert rows[0].error is None
        assert len(fakes["github"].merge_calls) == 1
        assert fakes["github"].review_calls == []

    def test_pending_row_is_committed_before_the_github_call(self, wired: Any) -> None:
        """A crash after GitHub answers must leave evidence, not silence."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        store = _FakeStore()
        store.fail_save_on_call = 1
        fakes = install(reviewer, prs=[pr], store=store)

        response = _post(client, pr)
        assert response.status_code == 500
        assert len(fakes["store"].rows) == 1
        assert fakes["store"].rows[0].status == PRPartyActionStatus.PENDING
        assert len(fakes["github"].review_calls) == 1

        # And the abandoned row is reclaimable once the window passes.
        fakes["store"].fail_save_on_call = None
        fakes["store"].rows[0].created_at = datetime.now(UTC) - timedelta(hours=3)
        retry = _post(client, pr, idempotency_key=OTHER_KEY)
        assert retry.status_code == 200, retry.text
        assert len(fakes["store"].rows) == 1

    def test_settled_replay_renders_the_stored_row_not_the_new_body(self, wired: Any) -> None:
        """A same-key replay is the STORED action, whatever the new body says.

        The receipt is projected from the row, and the settled/same-key branch
        returns before any write — so a client that retries a key with edited
        prose gets back what actually happened, flagged ``replayed``, and
        GitHub is not asked a second time.
        """
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])

        first = _post(client, pr, body="Two suggestions inline; both optional.")
        assert first.status_code == 200, first.text

        second = _post(client, pr, body="Actually, ship it.")
        assert second.status_code == 200, second.text
        payload = second.json()

        assert payload["replayed"] is True
        assert payload["action"] == first.json()["action"]
        assert payload["action"]["github_review_id"] == 9_000_000_001
        assert fakes["store"].rows[0].body == "Two suggestions inline; both optional."
        assert len(fakes["github"].review_calls) == 1


# ---------------------------------------------------------------------------
# Reclaim: a dead attempt may already have reached GitHub (KTD16)
# ---------------------------------------------------------------------------


class TestReclaimAdoption:
    """A reclaimed review row asks GitHub before it posts a second one.

    The row is committed *before* the call that may have killed the process, so
    its existence proves an attempt happened — not that it failed. Re-actuating
    on that evidence alone is how one crash becomes two reviews on the PR.
    """

    @staticmethod
    def _abandoned(reviewer: PRPartyReviewer, pr: PRPartyPR, **kw: Any) -> PRPartyAction:
        return _action(
            reviewer,
            pr,
            status=PRPartyActionStatus.PENDING,
            created_at=datetime.now(UTC) - timedelta(hours=3),
            **kw,
        )

    def test_reclaim_adopts_an_existing_review_instead_of_reposting(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(
            reviewer,
            prs=[pr],
            store=_FakeStore([self._abandoned(reviewer, pr)]),
            github=_FakeGitHub(reviews=[_gh_review(review_id=9_000_000_777)]),
        )

        response = _post(client, pr, idempotency_key=OTHER_KEY)
        assert response.status_code == 200, response.text
        payload = response.json()

        # Nothing was posted; the review GitHub already holds became this row's.
        assert fakes["github"].review_calls == []
        assert fakes["github"].list_reviews_calls == [("catholicos", "ontokit-api", 42)]

        row = fakes["store"].rows[0]
        assert row.status == PRPartyActionStatus.SUCCEEDED
        assert row.github_review_id == 9_000_000_777
        assert payload["action"]["status"] == "succeeded"
        assert payload["action"]["github_review_id"] == 9_000_000_777
        assert payload["replayed"] is False

    def test_reclaim_still_actuates_when_the_existing_review_is_another_verdict(
        self, wired: Any
    ) -> None:
        """A ``COMMENTED`` review at this head is not the approval this row cast."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(
            reviewer,
            prs=[pr],
            store=_FakeStore([self._abandoned(reviewer, pr)]),
            github=_FakeGitHub(reviews=[_gh_review(state="COMMENTED")]),
        )

        response = _post(client, pr, idempotency_key=OTHER_KEY)
        assert response.status_code == 200, response.text
        assert len(fakes["github"].review_calls) == 1
        assert fakes["store"].rows[0].github_review_id == 9_000_000_001

    def test_reclaim_ignores_a_review_of_an_older_revision(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(
            reviewer,
            prs=[pr],
            store=_FakeStore([self._abandoned(reviewer, pr)]),
            github=_FakeGitHub(reviews=[_gh_review(commit_id=OLD_HEAD)]),
        )

        assert _post(client, pr, idempotency_key=OTHER_KEY).status_code == 200
        assert len(fakes["github"].review_calls) == 1

    def test_reclaim_falls_back_to_actuating_when_the_listing_fails(self, wired: Any) -> None:
        """A read that is down must not make a verdict impossible to cast."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(
            reviewer,
            prs=[pr],
            store=_FakeStore([self._abandoned(reviewer, pr)]),
            github=_FakeGitHub(
                reviews_error=GitHubAPIError("reviews unavailable", status_code=503)
            ),
        )

        response = _post(client, pr, idempotency_key=OTHER_KEY)
        assert response.status_code == 200, response.text
        assert len(fakes["github"].review_calls) == 1
        assert fakes["store"].rows[0].status == PRPartyActionStatus.SUCCEEDED

    def test_reopened_failed_review_row_also_checks_first(self, wired: Any) -> None:
        """``failed`` is written *after* the call — it can still have landed."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        dead = _action(
            reviewer,
            pr,
            status=PRPartyActionStatus.FAILED,
            error="GitHubAPIError (HTTP 500)",
        )
        fakes = install(
            reviewer,
            prs=[pr],
            store=_FakeStore([dead]),
            github=_FakeGitHub(reviews=[_gh_review(review_id=9_000_000_778)]),
        )

        assert _post(client, pr, idempotency_key=OTHER_KEY).status_code == 200
        assert fakes["github"].review_calls == []
        assert dead.status == PRPartyActionStatus.SUCCEEDED
        assert dead.github_review_id == 9_000_000_778

    def test_a_fresh_row_never_pays_for_the_duplicate_check(self, wired: Any) -> None:
        """The check is the reclaim's cost, not every verdict's."""
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])
        assert _post(client, pr).status_code == 200
        assert fakes["github"].list_reviews_calls == []


@pytest.mark.parametrize(
    "row_status,stale",
    [
        (PRPartyActionStatus.PENDING, True),
        (PRPartyActionStatus.FAILED, False),
        (PRPartyActionStatus.DEGRADED_INTENT, False),
    ],
)
async def test_concurrent_existing_row_retry_posts_only_one_review(
    row_status: PRPartyActionStatus, stale: bool
) -> None:
    """N1: row-lock claiming admits one stale/failed/repair delivery."""
    reviewer = _reviewer()
    pr = _pr()
    row = _action(
        reviewer,
        pr,
        status=row_status,
        created_at=datetime.now(UTC) - (timedelta(hours=3) if stale else timedelta(minutes=1)),
    )
    store = _LockingStore([row])
    github = _FakeGitHub()
    service = PRPartyActionService(
        store=store,
        credentials=_FakeCredentials(token="ghp_repaired", reviewer=reviewer),
        actuation_factory=lambda _token: github,
        reclaim_minutes=30,
    )
    request = PRPartyActionRequest.model_validate(_body())

    outcomes = await asyncio.gather(
        service.actuate(
            reviewer=reviewer,
            pr=pr,
            request=request,
            pr_url=f"https://github.com/{REPO}/pull/42",
        ),
        service.actuate(
            reviewer=reviewer,
            pr=pr,
            request=request,
            pr_url=f"https://github.com/{REPO}/pull/42",
        ),
        return_exceptions=True,
    )

    assert len(github.review_calls) == 1
    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) >= 1


# ---------------------------------------------------------------------------
# Degradation (R12)
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_no_credential_records_intent_with_a_deep_link(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr], token=None)

        response = _post(client, pr)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["degraded"] is True
        assert payload["deep_link"] == f"https://github.com/{REPO}/pull/42/files"
        assert payload["action"]["status"] == "degraded_intent"
        assert fakes["github"].review_calls == []

    def test_token_expired_mid_verdict_degrades_and_marks_the_credential(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(
            reviewer,
            prs=[pr],
            github=_FakeGitHub(review_error=TokenExpiredError("Bad credentials", status_code=401)),
        )

        response = _post(client, pr)
        assert response.status_code == 200, response.text
        assert response.json()["degraded"] is True
        assert fakes["store"].rows[0].status == PRPartyActionStatus.DEGRADED_INTENT

        credential = fakes["credentials"].credential
        assert credential is not None
        assert credential.last_error is not None
        assert credential.last_validated_at is None

    def test_merge_degradation_deep_links_to_the_conversation_tab(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        install(_reviewer(), prs=[pr], store=_FakeStore([approval]), token=None)

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 200, response.text
        assert response.json()["deep_link"] == f"https://github.com/{REPO}/pull/42"


class TestDegradedReplay:
    """A ``degraded_intent`` row is a *pending* delivery, not a settled one.

    Replaying it unconditionally would strand the reviewer: once the PAT is
    repaired, the same key — the only key their client has for this tap — would
    keep echoing the old intent, and there would be no way to actually cast the
    verdict from the app.
    """

    @staticmethod
    def _degraded(reviewer: PRPartyReviewer, pr: PRPartyPR) -> PRPartyAction:
        return _action(reviewer, pr, status=PRPartyActionStatus.DEGRADED_INTENT)

    def test_replay_with_a_repaired_credential_actuates_for_real(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        row = self._degraded(reviewer, pr)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([row]), token="ghp_repaired")

        response = _post(client, pr)  # same idempotency key as the degraded row
        assert response.status_code == 200, response.text
        payload = response.json()

        assert len(fakes["github"].review_calls) == 1
        assert fakes["github"].list_reviews_calls == []
        assert payload["degraded"] is False
        assert payload["replayed"] is False
        assert payload["action"]["status"] == "succeeded"
        assert payload["action"]["github_review_id"] == 9_000_000_001
        assert len(fakes["store"].rows) == 1
        assert row.status == PRPartyActionStatus.SUCCEEDED

    def test_replay_with_a_still_broken_credential_replays_the_intent(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        row = self._degraded(reviewer, pr)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([row]), token=None)

        response = _post(client, pr)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["replayed"] is True
        assert payload["degraded"] is True
        # The replay has to carry the same "finish it here" link the first
        # degraded response did, or a retry is strictly less useful than it.
        assert payload["deep_link"] == f"https://github.com/{REPO}/pull/42/files"
        assert payload["action"]["status"] == "degraded_intent"
        assert fakes["github"].review_calls == []
        assert row.status == PRPartyActionStatus.DEGRADED_INTENT

    def test_a_degraded_merge_replay_deep_links_to_the_conversation_tab(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        stalled = _action(
            reviewer,
            pr,
            kind=PRPartyActionKind.MERGE,
            verdict=None,
            status=PRPartyActionStatus.DEGRADED_INTENT,
        )
        install(reviewer, prs=[pr], store=_FakeStore([approval, stalled]), token=None)

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 200, response.text
        assert response.json()["replayed"] is True
        assert response.json()["deep_link"] == f"https://github.com/{REPO}/pull/42"


class TestDegradedConfirmed:
    """A confirmed degraded verdict is a real verdict (R12 + U8).

    The reviewer approved in the app, the PAT was missing, they posted the
    review on GitHub by hand, and the reconciler matched it back to the row as
    ``degraded_confirmed``. Anything that treats that as less than an approval
    would punish the reviewer for our credential outage.
    """

    def test_it_authorizes_a_merge_and_shows_as_approved_to_the_counterpart(
        self, wired: Any
    ) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        confirmed = _action(
            _reviewer(zitadel_user_id="counterpart", node_id="MDQ6VXNlcjc="),
            pr,
            status=PRPartyActionStatus.DEGRADED_CONFIRMED,
            github_review_id=9_000_000_500,
        )
        fakes = install(reviewer, prs=[pr], store=_FakeStore([confirmed]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["action"]["merged"] is True
        assert len(fakes["github"].merge_calls) == 1
        assert payload["card"]["other_reviewer"]["has_approved"] is True

    def test_a_degraded_intent_approval_does_not_authorize_a_merge(self, wired: Any) -> None:
        """The contrast that makes the case above mean something."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        unconfirmed = _action(
            _reviewer(zitadel_user_id="counterpart", node_id="MDQ6VXNlcjc="),
            pr,
            status=PRPartyActionStatus.DEGRADED_INTENT,
        )
        fakes = install(reviewer, prs=[pr], store=_FakeStore([unconfirmed]))

        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409
        assert fakes["github"].merge_calls == []
        assert response.json()["detail"]["card"]["other_reviewer"]["has_approved"] is False


# ---------------------------------------------------------------------------
# GitHub failures
# ---------------------------------------------------------------------------


class TestGitHubFailures:
    def test_server_error_fails_the_row_and_keeps_the_card(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(
            _reviewer(),
            prs=[pr],
            github=_FakeGitHub(
                review_error=GitHubAPIError("boom: <html>secret body</html>", status_code=500)
            ),
        )

        response = _post(client, pr)
        assert response.status_code == 502
        assert fakes["store"].rows[0].status == PRPartyActionStatus.FAILED

    def test_error_text_is_scrubbed_of_response_bodies(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        secret = "token ghp_LEAKED in repo private/thing"
        fakes = install(
            _reviewer(),
            prs=[pr],
            github=_FakeGitHub(review_error=GitHubAPIError(secret, status_code=500)),
        )

        response = _post(client, pr)
        stored = fakes["store"].rows[0].error or ""
        assert "ghp_LEAKED" not in stored
        assert "ghp_LEAKED" not in response.text
        assert "GitHubAPIError" in stored
        assert "500" in stored

    def test_self_approval_is_403(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(
            _reviewer(),
            prs=[pr],
            github=_FakeGitHub(review_error=SelfApprovalError("nope", status_code=422)),
        )
        assert _post(client, pr).status_code == 403
        assert fakes["store"].rows[0].status == PRPartyActionStatus.FAILED

    def test_merge_409_is_a_stale_card(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        fakes = install(
            _reviewer(),
            prs=[pr],
            store=_FakeStore([approval]),
            github=_FakeGitHub(merge_error=StaleCardError("head moved", status_code=409)),
        )
        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409
        mine = [r for r in fakes["store"].rows if r.action_kind == PRPartyActionKind.MERGE]
        assert mine[0].status == PRPartyActionStatus.FAILED

    def test_merge_405_is_a_reasoned_409(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        install(
            _reviewer(),
            prs=[pr],
            store=_FakeStore([approval]),
            github=_FakeGitHub(
                merge_error=MergeNotAllowedError("branch protected", status_code=405)
            ),
        )
        response = _post(client, pr, action_kind="merge", verdict=None)
        assert response.status_code == 409


class TestTransportFailureThroughActuation:
    """GitHub being unreachable takes the 502 path, not an unhandled 500.

    Every other test here arms ``_FakeGitHub``, which can only prove the
    service handles errors it is *handed*. This one wires the **real**
    :class:`PRPartyGitHubClient` in as the actuation client and breaks httpx
    underneath it, so the assertion covers the whole chain: httpx raises a
    transport error, the client folds it into the taxonomy, and the service's
    ``except GitHubAPIError`` is the thing that catches it. Before the client
    wrapped transport errors, this request raised out of the route.
    """

    @staticmethod
    def _dead_transport() -> AsyncMock:
        transport = AsyncMock()
        transport.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        transport.__aenter__ = AsyncMock(return_value=transport)
        transport.__aexit__ = AsyncMock(return_value=False)
        return transport

    def test_connect_error_during_a_review_is_a_502_and_a_failed_row(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(
            _reviewer(),
            prs=[pr],
            github=actuation_client("ghp_actuation_token"),  # the real client
        )

        with patch("httpx.AsyncClient", return_value=self._dead_transport()):
            response = _post(client, pr)

        assert response.status_code == 502
        row = fakes["store"].rows[0]
        assert row.status == PRPartyActionStatus.FAILED
        # No HTTP status exists for a transport failure; scrub_error must not
        # render one, and httpx's prose must not be persisted.
        assert row.error == "GitHubAPIError"
        assert "connection refused" not in response.text

    def test_connect_error_during_a_merge_is_a_502_and_a_failed_row(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        approval = _action(_reviewer(zitadel_user_id="counterpart"), pr)
        fakes = install(
            _reviewer(),
            prs=[pr],
            store=_FakeStore([approval]),
            github=actuation_client("ghp_actuation_token"),
        )

        with patch("httpx.AsyncClient", return_value=self._dead_transport()):
            response = _post(client, pr, action_kind="merge", verdict=None)

        assert response.status_code == 502
        mine = [r for r in fakes["store"].rows if r.action_kind == PRPartyActionKind.MERGE]
        assert mine[0].status == PRPartyActionStatus.FAILED


# ---------------------------------------------------------------------------
# Discuss-live and unpark (R9)
# ---------------------------------------------------------------------------


class TestDiscussLiveAndUnpark:
    def test_discuss_live_parks_the_card_without_a_github_call(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])

        response = _post(client, pr, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["action"]["status"] == "succeeded"
        assert payload["action"]["verdict"] == PR_PARTY_VERDICT_DISCUSS_LIVE
        assert payload["card"]["parked"] is True
        assert fakes["github"].review_calls == []

        row = fakes["store"].rows[0]
        assert row.action_kind == PRPartyActionKind.REVIEW
        assert row.head_sha == HEAD

    def test_discuss_live_works_while_degraded(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(_reviewer(), prs=[pr], token=None)
        response = _post(client, pr, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)
        assert response.status_code == 200, response.text
        assert response.json()["degraded"] is False

    def test_unpark_deletes_the_discuss_live_row(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        park = _action(reviewer, pr, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([park]))

        response = client.post(f"{BASE}/cards/{pr.id}/unpark")
        assert response.status_code == 200, response.text
        assert response.json()["parked"] is False
        assert fakes["store"].deleted == [park]

    def test_unpark_is_idempotent(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])
        response = client.post(f"{BASE}/cards/{pr.id}/unpark")
        assert response.status_code == 200, response.text
        assert fakes["store"].deleted == []

    def test_unpark_never_removes_a_real_verdict(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        approval = _action(reviewer, pr, verdict=PR_PARTY_VERDICT_APPROVE)
        fakes = install(reviewer, prs=[pr], store=_FakeStore([approval]))

        assert client.post(f"{BASE}/cards/{pr.id}/unpark").status_code == 200
        assert fakes["store"].deleted == []


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_over_the_daily_cap_is_429(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr], redis=_FakeRedis(start=10_000))

        response = _post(client, pr)
        assert response.status_code == 429
        assert fakes["github"].review_calls == []

    def test_redis_down_fails_actuation_closed_with_503(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(
            _reviewer(), prs=[pr], redis=_FakeRedis(error=RedisConnectionError("no redis"))
        )

        response = _post(client, pr)
        assert response.status_code == 503
        assert fakes["github"].review_calls == []
        assert fakes["store"].rows == []

    def test_reads_are_unaffected_by_a_dead_limiter(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(_reviewer(), prs=[pr], redis=_FakeRedis(error=RedisConnectionError("no redis")))
        assert client.get(f"{BASE}/queue").status_code == 200
        assert client.get(f"{BASE}/cards/{pr.id}").status_code == 200


class TestLimiterUnit:
    @pytest.mark.asyncio
    async def test_key_is_per_user_per_utc_day(self) -> None:
        key = action_key("user-1", today="2026-07-26")
        assert key == "pr_party:actions:user-1:2026-07-26"

    @pytest.mark.asyncio
    async def test_missing_client_is_unavailable(self) -> None:
        outcome, remaining = await check_and_consume(None, "user-1")
        assert outcome is LimiterOutcome.UNAVAILABLE
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_allowed_reports_the_remaining_budget(self) -> None:
        outcome, remaining = await check_and_consume(_FakeRedis(), "user-1", limit=5)
        assert outcome is LimiterOutcome.ALLOWED
        assert remaining == 4

    @pytest.mark.asyncio
    async def test_over_limit_is_distinct_from_unavailable(self) -> None:
        redis = _FakeRedis(start=5)
        outcome, _ = await check_and_consume(redis, "user-1", limit=5)
        assert outcome is LimiterOutcome.OVER_LIMIT


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"idempotency_key": "short"},
            {"idempotency_key": "has spaces in it"},
            {"idempotency_key": "x" * 65},
            {"verdict": "lgtm"},
            {"action_kind": "question"},
            {"action_kind": "review", "verdict": None},
            {"action_kind": "merge", "verdict": PR_PARTY_VERDICT_APPROVE},
            {"merge_method": "fast-forward"},
            {"head_sha": ""},
        ],
    )
    def test_bad_requests_are_422(self, wired: Any, overrides: dict[str, Any]) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])
        assert _post(client, pr, **overrides).status_code == 422
        assert fakes["github"].review_calls == []

    def test_verdict_matching_is_casefolded(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])
        assert _post(client, pr, verdict="APPROVE").status_code == 200
        assert fakes["github"].review_calls[0]["event"] == "APPROVE"
