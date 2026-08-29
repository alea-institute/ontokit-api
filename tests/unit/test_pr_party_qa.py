"""Tests for PR Party Q&A — asking on a card, and binding the answer (U7).

The contracts these pin:

- **A question is posted as the asking reviewer** (KTD18). It rides the
  *actuation* client, because a comment authored by a PAT triggers Actions
  workflows and one authored by ``GITHUB_TOKEN`` does not. The body carries the
  ``@claude`` mention the org workflow triggers on and an attribution line
  naming the human who asked.
- **Degraded is compose-for-copy, not failure** (R12). With no usable PAT the
  reviewer gets back the exact comment text and a deep link, and *nothing*
  reaches GitHub — no client is even constructed.
- **An answer binds by linkage, never by author identity** (C4). A CodeRabbit
  walkthrough that happens to land after a question is not an answer to it. A
  comment that quotes the question, references its comment id, or @-mentions the
  asker is.
- **The thread is served live from GitHub** (KD5/R13). There is no Q&A table:
  ``qa_thread`` is filtered out of the PR's issue comments at read time, so
  GitHub stays the system of record and nothing here can go stale.
- **The org answerer is authorization-gated and containment-bounded** (A2,
  KTD18). The shipped workflow asset is parsed here and asserted: least-privilege
  ``permissions``, an ``author_association`` gate, no checkout of the PR head,
  and an allowed-tools restriction that leaves the agent able to do one thing —
  post its comment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import ontokit
from ontokit.api.routes import include_pr_party_routes
from ontokit.api.routes.pr_party import (
    get_actions_redis,
    get_qa_service,
    get_queue_reader,
)
from ontokit.api.routes.pr_party_settings import get_credential_service
from ontokit.main import app
from ontokit.models.pr_party import (
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyCredential,
    PRPartyMergeDefault,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.services.pr_party_github import (
    GitHubAPIError,
    IssueComment,
    TokenExpiredError,
)
from ontokit.services.pr_party_qa import (
    CLAUDE_MENTION,
    CODERABBIT_REVIEW_COMMAND,
    ORG_WORKFLOW_ASSET,
    PRPartyQAService,
    QARefused,
    answer_binds,
    build_qa_thread,
    classify_issue_comment,
    compose_note,
    compose_question,
    ingest_issue_comment,
    parse_attribution,
    register_qa_hook,
)

BASE = "/api/v1/pr-party"
USER_ID = "test-user-id"
REPO = "catholicos/ontokit-api"
HEAD = "a" * 40
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Row and payload factories
# ---------------------------------------------------------------------------


def _reviewer(login: str = "damienriehl") -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=USER_ID,
        github_login=login,
        github_node_id="MDQ6VXNlcjE=",
        merge_default=PRPartyMergeDefault.MANUAL,
    )
    row.id = uuid.uuid4()
    return row


def _pr(
    *, state: str = "open", brief_status: PRPartyBriefStatus = PRPartyBriefStatus.READY
) -> PRPartyPR:
    row = PRPartyPR(
        repo_full_name=REPO,
        pr_number=42,
        title="Add the Q&A service",
        state=state,
        head_sha=HEAD,
        author_kind=PRPartyAuthorKind.COUNTERPART,
        author_github_login="someone-else",
        author_node_id="MDQ6VXNlcjk=",
        mergeable_state="clean",
        checks_rollup="success",
        brief_status=brief_status,
        brief_what="Adds Q&A.",
        brief_why="Questions have to reach GitHub.",
        brief_truncated=False,
    )
    row.id = uuid.uuid4()
    return row


def _comment(
    comment_id: int,
    body: str,
    *,
    login: str | None = "damienriehl",
    minutes: int = 0,
) -> IssueComment:
    stamp = NOW + timedelta(minutes=minutes)
    return IssueComment(
        id=comment_id,
        body=body,
        user_login=login,
        html_url=f"https://github.com/{REPO}/pull/42#issuecomment-{comment_id}",
        created_at=stamp,
        updated_at=stamp,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCredentialServiceForAuth:
    def __init__(self, reviewer: PRPartyReviewer | None) -> None:
        self.reviewer = reviewer

    async def get_reviewer(self, zitadel_user_id: str) -> PRPartyReviewer | None:
        if self.reviewer is not None and self.reviewer.zitadel_user_id == zitadel_user_id:
            return self.reviewer
        return None


class _FakeCredentials:
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
        self.saved = 0

    async def resolve_token(self, _reviewer: PRPartyReviewer) -> str | None:
        return self.token

    async def get_credential(self, _reviewer_id: uuid.UUID) -> PRPartyCredential | None:
        return self.credential

    async def save(self) -> None:
        self.saved += 1


class _FakeGitHub:
    """Records comment posts and comment reads; either can be armed to raise."""

    def __init__(
        self,
        *,
        comments: list[IssueComment] | None = None,
        post_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.comments = list(comments or [])
        self.post_error = post_error
        self.read_error = read_error
        self.posted: list[dict[str, Any]] = []
        self.reads: list[tuple[str, str, int]] = []

    async def create_comment(
        self, owner: str, repo: str, number: int, *, body: str
    ) -> IssueComment:
        self.posted.append({"owner": owner, "repo": repo, "number": number, "body": body})
        if self.post_error is not None:
            raise self.post_error
        return _comment(9_000_001, body)

    async def get_issue_comments(self, owner: str, repo: str, number: int) -> list[IssueComment]:
        self.reads.append((owner, repo, number))
        if self.read_error is not None:
            raise self.read_error
        return list(self.comments)


class _FakeReader:
    def __init__(self, prs: list[PRPartyPR] | None = None) -> None:
        self.prs = prs if prs is not None else []

    async def list_open_prs(self) -> list[PRPartyPR]:
        return list(self.prs)

    async def get_pr(self, card_id: uuid.UUID) -> PRPartyPR | None:
        return next((p for p in self.prs if p.id == card_id), None)

    async def list_actions(self, pr_ids: list[uuid.UUID]) -> list[Any]:  # noqa: ARG002
        return []


class _FakeRedis:
    def __init__(self, *, start: int = 0, error: Exception | None = None) -> None:
        self.counts: dict[str, int] = {}
        self.start = start
        self.error = error

    async def incr(self, name: str) -> int:
        if self.error is not None:
            raise self.error
        self.counts[name] = self.counts.get(name, self.start) + 1
        return self.counts[name]

    async def expire(self, name: str, time: int) -> bool:  # noqa: ARG002
        return True


def _service(
    reviewer: PRPartyReviewer | None,
    *,
    token: str | None = "ghp_token",
    github: _FakeGitHub | None = None,
    generation: _FakeGitHub | None | str = "same",
) -> tuple[PRPartyQAService, _FakeGitHub, _FakeCredentials, list[str]]:
    """A service wired to fakes, plus the factory-call log that proves no HTTP."""
    actuation = github or _FakeGitHub()
    credentials = _FakeCredentials(token, reviewer)
    factory_calls: list[str] = []

    def _actuation_factory(tok: str) -> Any:
        factory_calls.append(tok)
        return actuation

    read_client = actuation if generation == "same" else generation

    service = PRPartyQAService(
        credentials=credentials,  # type: ignore[arg-type]
        actuation_factory=_actuation_factory,  # type: ignore[arg-type]
        generation_factory=lambda: read_client,  # type: ignore[arg-type,return-value]
    )
    return service, actuation, credentials, factory_calls


@pytest.fixture
def wired(authed_client: tuple[TestClient, AsyncMock]) -> Any:
    """(client, install) — ``install(...)`` binds every fake in one call."""
    _global_client, _db = authed_client
    target = APIRouter()
    include_pr_party_routes(target, auth_mode="required", reviewers="zit-1:octocat")
    probe_app = FastAPI()
    probe_app.include_router(target, prefix="/api/v1")
    probe_app.dependency_overrides.update(app.dependency_overrides)
    client = TestClient(probe_app, raise_server_exceptions=False)

    def install(
        reviewer: PRPartyReviewer | None,
        *,
        prs: list[PRPartyPR] | None = None,
        token: str | None = "ghp_token",
        github: _FakeGitHub | None = None,
        redis: _FakeRedis | None = None,
    ) -> dict[str, Any]:
        service, actuation, credentials, factory_calls = _service(
            reviewer, token=token, github=github
        )
        reader = _FakeReader(prs)
        redis = redis if redis is not None else _FakeRedis()

        probe_app.dependency_overrides[get_credential_service] = lambda: _FakeCredentialServiceForAuth(
            reviewer
        )
        probe_app.dependency_overrides[get_queue_reader] = lambda: reader
        probe_app.dependency_overrides[get_qa_service] = lambda: service
        probe_app.dependency_overrides[get_actions_redis] = lambda: redis
        return {
            "service": service,
            "github": actuation,
            "credentials": credentials,
            "factory_calls": factory_calls,
            "redis": redis,
            "reader": reader,
        }

    try:
        yield client, install
    finally:
        probe_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_question_carries_the_mention_and_the_attribution(self) -> None:
        body = compose_question("Why is the index partial?", github_login="damienriehl")

        assert body.startswith(f"{CLAUDE_MENTION} Why is the index partial?")
        assert "(asked via PR Party by damienriehl)" in body

    def test_a_reviewer_who_types_the_mention_does_not_get_it_twice(self) -> None:
        body = compose_question("@claude why is the index partial?", github_login="damienriehl")

        assert body.count(CLAUDE_MENTION) == 1

    def test_an_empty_question_is_refused_before_any_call(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compose_question("   ", github_login="damienriehl")

    def test_a_deliberation_note_never_summons_the_answerer(self) -> None:
        body = compose_note("We agreed to split the migration.", github_login="damienriehl")

        assert CLAUDE_MENTION not in body.casefold()
        assert "We agreed to split the migration." in body
        assert "damienriehl" in body

    def test_attribution_round_trips(self) -> None:
        body = compose_question("Anything?", github_login="Fr-John")

        assert parse_attribution(body) == "Fr-John"

    def test_attribution_of_an_unrelated_comment_is_none(self) -> None:
        assert parse_attribution("Just a regular comment.") is None


# ---------------------------------------------------------------------------
# Answer binding (C4)
# ---------------------------------------------------------------------------


class TestAnswerBinding:
    def _question(self) -> IssueComment:
        return _comment(
            100,
            f"{CLAUDE_MENTION} Does the partial index cover question rows?\n\n"
            "(asked via PR Party by damienriehl)",
        )

    def test_an_unrelated_bot_comment_does_not_bind(self) -> None:
        question = self._question()
        walkthrough = _comment(
            101,
            "## Walkthrough\n\nThis pull request adds a Q&A service.",
            login="coderabbitai[bot]",
            minutes=5,
        )

        assert not answer_binds(walkthrough, question=question, asker_login="damienriehl")

    def test_author_identity_alone_never_binds(self) -> None:
        """The answerer bot posting *something* is not the answerer answering."""
        question = self._question()
        bot_noise = _comment(102, "Claude finished its run.", login="claude[bot]", minutes=5)

        assert not answer_binds(bot_noise, question=question, asker_login="damienriehl")

    def test_a_reference_to_the_question_comment_id_binds(self) -> None:
        question = self._question()
        answer = _comment(
            103,
            f"> Replying to https://github.com/{REPO}/pull/42#issuecomment-100\n\n"
            "No — the index covers every action kind.",
            login="claude[bot]",
            minutes=5,
        )

        assert answer_binds(answer, question=question, asker_login="damienriehl")

    def test_an_at_mention_of_the_asker_binds(self) -> None:
        question = self._question()
        answer = _comment(
            104,
            "@damienriehl it covers every action kind, including questions.",
            login="claude[bot]",
            minutes=5,
        )

        assert answer_binds(answer, question=question, asker_login="damienriehl")

    def test_a_quote_of_the_question_binds(self) -> None:
        question = self._question()
        answer = _comment(
            105,
            "> Does the partial index cover question rows?\n\nIt does.",
            login="claude[bot]",
            minutes=5,
        )

        assert answer_binds(answer, question=question, asker_login="damienriehl")

    def test_a_comment_that_predates_the_question_never_binds(self) -> None:
        question = self._question()
        earlier = _comment(99, "@damienriehl good morning", login="claude[bot]", minutes=-30)

        assert not answer_binds(earlier, question=question, asker_login="damienriehl")

    def test_another_question_is_not_an_answer(self) -> None:
        question = self._question()
        second = _comment(
            106,
            f"{CLAUDE_MENTION} @damienriehl and what about merges?",
            minutes=5,
        )

        assert not answer_binds(second, question=question, asker_login="damienriehl")


# ---------------------------------------------------------------------------
# Thread projection
# ---------------------------------------------------------------------------


class TestThread:
    def test_the_thread_is_the_filtered_exchange(self) -> None:
        comments = [
            _comment(1, "Nice work!", login="someone-else", minutes=-10),
            _comment(
                2,
                f"{CLAUDE_MENTION} Why a partial index?\n\n(asked via PR Party by damienriehl)",
                minutes=0,
            ),
            _comment(3, "## Walkthrough\n\nunrelated", login="coderabbitai[bot]", minutes=2),
            _comment(
                4,
                "@damienriehl because failed rows sit outside the predicate.",
                login="claude[bot]",
                minutes=3,
            ),
        ]

        thread = build_qa_thread(comments)

        assert len(thread) == 1
        entry = thread[0]
        assert entry.question_comment_id == 2
        assert entry.question_author == "damienriehl"
        assert entry.answer_comment_id == 4
        assert entry.answer_author == "claude[bot]"
        assert "outside the predicate" in (entry.answer_body or "")

    def test_a_question_with_no_answer_yet_is_still_an_entry(self) -> None:
        comments = [
            _comment(2, f"{CLAUDE_MENTION} Anything?\n\n(asked via PR Party by damienriehl)"),
            _comment(3, "## Walkthrough", login="coderabbitai[bot]", minutes=2),
        ]

        thread = build_qa_thread(comments)

        assert len(thread) == 1
        assert thread[0].answer_comment_id is None
        assert thread[0].answered_at is None

    def test_one_answer_is_never_claimed_by_two_questions(self) -> None:
        comments = [
            _comment(2, f"{CLAUDE_MENTION} First?\n\n(asked via PR Party by damienriehl)"),
            _comment(
                3, f"{CLAUDE_MENTION} Second?\n\n(asked via PR Party by damienriehl)", minutes=1
            ),
            _comment(4, "@damienriehl yes.", login="claude[bot]", minutes=2),
        ]

        thread = build_qa_thread(comments)

        assert [e.answer_comment_id for e in thread] == [4, None]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestService:
    async def test_a_question_posts_as_the_reviewer(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, github, _creds, factory_calls = _service(reviewer)

        result = await service.ask(
            reviewer=reviewer, pr=pr, question="Why a partial index?", pr_url="https://x/pull/42"
        )

        assert result.posted is True
        assert result.degraded is False
        assert result.comment_id == 9_000_001
        assert factory_calls == ["ghp_token"], "the reviewer's own PAT authors the comment"
        assert github.posted[0]["owner"] == "catholicos"
        assert github.posted[0]["repo"] == "ontokit-api"
        assert github.posted[0]["number"] == 42
        assert github.posted[0]["body"].startswith(CLAUDE_MENTION)
        assert "(asked via PR Party by damienriehl)" in github.posted[0]["body"]

    async def test_degraded_composes_for_copy_and_makes_no_call(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, github, _creds, factory_calls = _service(reviewer, token=None)

        result = await service.ask(
            reviewer=reviewer,
            pr=pr,
            question="Why a partial index?",
            pr_url="https://github.com/catholicos/ontokit-api/pull/42",
        )

        assert result.posted is False
        assert result.degraded is True
        assert result.body.startswith(CLAUDE_MENTION)
        assert result.deep_link == "https://github.com/catholicos/ontokit-api/pull/42"
        assert factory_calls == [], "no client is constructed when there is no PAT"
        assert github.posted == []

    async def test_rerun_review_posts_the_coderabbit_command(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, github, _creds, _calls = _service(reviewer)

        result = await service.rerun_review(reviewer=reviewer, pr=pr, pr_url="https://x/pull/42")

        assert result.posted is True
        assert github.posted[0]["body"] == CODERABBIT_REVIEW_COMMAND

    async def test_a_note_posts_the_structured_outcome(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, github, _creds, _calls = _service(reviewer)

        await service.record_note(
            reviewer=reviewer, pr=pr, note="We split the migration.", pr_url="https://x/pull/42"
        )

        assert "We split the migration." in github.posted[0]["body"]
        assert CLAUDE_MENTION not in github.posted[0]["body"].casefold()

    async def test_a_dead_pat_degrades_and_marks_the_credential(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, _github, credentials, _calls = _service(
            reviewer,
            github=_FakeGitHub(post_error=TokenExpiredError("bad credentials", status_code=401)),
        )

        result = await service.ask(
            reviewer=reviewer, pr=pr, question="Anything?", pr_url="https://x/pull/42"
        )

        assert result.degraded is True
        assert result.posted is False
        assert credentials.credential is not None
        assert credentials.credential.last_validated_at is None
        assert "401" in (credentials.credential.last_error or "")

    async def test_a_github_failure_is_a_refusal_not_a_500(self) -> None:
        reviewer = _reviewer()
        pr = _pr()
        service, _github, _creds, _calls = _service(
            reviewer, github=_FakeGitHub(post_error=GitHubAPIError("boom", status_code=502))
        )

        with pytest.raises(QARefused) as excinfo:
            await service.ask(
                reviewer=reviewer, pr=pr, question="Anything?", pr_url="https://x/pull/42"
            )

        assert excinfo.value.status_code == 502

    async def test_the_thread_is_read_with_the_generation_client(self) -> None:
        pr = _pr()
        reader = _FakeGitHub(
            comments=[
                _comment(2, f"{CLAUDE_MENTION} Why?\n\n(asked via PR Party by damienriehl)"),
                _comment(3, "@damienriehl because.", login="claude[bot]", minutes=1),
            ]
        )
        service, _actuation, _creds, factory_calls = _service(_reviewer(), generation=reader)

        thread = await service.load_thread(pr=pr)

        assert [e.answer_comment_id for e in thread] == [3]
        assert reader.reads == [("catholicos", "ontokit-api", 42)]
        assert factory_calls == [], "reading a thread never touches a reviewer's PAT"

    async def test_a_read_failure_leaves_the_card_renderable(self) -> None:
        service, _actuation, _creds, _calls = _service(
            _reviewer(), generation=_FakeGitHub(read_error=GitHubAPIError("nope", status_code=503))
        )

        assert await service.load_thread(pr=_pr()) == []

    async def test_no_generation_token_means_an_empty_thread(self) -> None:
        service, _actuation, _creds, _calls = _service(_reviewer(), generation=None)

        assert await service.load_thread(pr=_pr()) == []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestQuestionRoute:
    def test_a_question_reaches_github_and_returns_the_fresh_card(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])

        response = client.post(
            f"{BASE}/cards/{pr.id}/questions", json={"question": "Why a partial index?"}
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["posted"] is True
        assert payload["degraded"] is False
        assert payload["body"].startswith(CLAUDE_MENTION)
        assert payload["card"]["card_id"] == str(pr.id)
        assert fakes["github"].posted[0]["number"] == 42

    def test_degraded_returns_compose_for_copy_with_a_deep_link(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr], token=None)

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        payload = response.json()
        assert response.status_code == 200
        assert payload["posted"] is False
        assert payload["degraded"] is True
        assert payload["deep_link"].endswith("/pull/42")
        assert payload["body"].startswith(CLAUDE_MENTION)
        assert fakes["factory_calls"] == []
        assert fakes["github"].posted == []

    def test_a_question_needs_no_readiness(self, wired: Any) -> None:
        """R17 gates verdicts, never questions — brewing is when you most want to ask."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr(brief_status=PRPartyBriefStatus.BREWING)
        install(reviewer, prs=[pr])

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        assert response.status_code == 200

    def test_an_empty_question_is_a_422(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        fakes = install(reviewer, prs=[pr])

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "   "})

        assert response.status_code == 422
        assert fakes["github"].posted == []

    def test_a_non_reviewer_is_refused(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(None, prs=[pr])

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        assert response.status_code == 403
        assert fakes["github"].posted == []

    def test_an_unknown_card_is_a_404(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), prs=[])

        response = client.post(f"{BASE}/cards/{uuid.uuid4()}/questions", json={"question": "Why?"})

        assert response.status_code == 404

    def test_a_closed_pr_takes_no_more_questions(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(state="closed")
        fakes = install(_reviewer(), prs=[pr])

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        assert response.status_code == 409
        assert response.json()["detail"]["retire"] is True
        assert fakes["github"].posted == []

    def test_the_daily_budget_applies(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr], redis=_FakeRedis(start=10_000))

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        assert response.status_code == 429
        assert fakes["github"].posted == []

    def test_an_unavailable_limiter_fails_closed(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr], redis=_FakeRedis(error=ConnectionError("down")))

        response = client.post(f"{BASE}/cards/{pr.id}/questions", json={"question": "Why?"})

        assert response.status_code == 503
        assert fakes["github"].posted == []


class TestRerunRoute:
    def test_rerun_posts_the_coderabbit_command_as_the_reviewer(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_status=PRPartyBriefStatus.READY_WITH_WARNING)
        fakes = install(_reviewer(), prs=[pr])

        response = client.post(f"{BASE}/cards/{pr.id}/rerun-review")

        assert response.status_code == 200
        assert response.json()["posted"] is True
        assert fakes["github"].posted[0]["body"] == CODERABBIT_REVIEW_COMMAND
        assert fakes["factory_calls"] == ["ghp_token"]

    def test_rerun_degrades_to_compose_for_copy(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr], token=None)

        response = client.post(f"{BASE}/cards/{pr.id}/rerun-review")

        payload = response.json()
        assert payload["degraded"] is True
        assert payload["body"] == CODERABBIT_REVIEW_COMMAND
        assert fakes["github"].posted == []

    def test_rerun_is_rate_limited(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(_reviewer(), prs=[pr], redis=_FakeRedis(start=10_000))

        assert client.post(f"{BASE}/cards/{pr.id}/rerun-review").status_code == 429


class TestNoteRoute:
    def test_a_deliberation_outcome_posts_to_the_pr(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        fakes = install(_reviewer(), prs=[pr])

        response = client.post(
            f"{BASE}/cards/{pr.id}/notes", json={"note": "We agreed to split the migration."}
        )

        assert response.status_code == 200
        assert "We agreed to split the migration." in fakes["github"].posted[0]["body"]


class TestCardThread:
    def test_the_card_serves_the_thread_from_github(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        github = _FakeGitHub(
            comments=[
                _comment(2, f"{CLAUDE_MENTION} Why?\n\n(asked via PR Party by damienriehl)"),
                _comment(3, "@damienriehl because.", login="claude[bot]", minutes=1),
            ]
        )
        install(_reviewer(), prs=[pr], github=github)

        response = client.get(f"{BASE}/cards/{pr.id}")

        assert response.status_code == 200
        thread = response.json()["qa_thread"]
        assert len(thread) == 1
        assert thread[0]["question_comment_id"] == 2
        assert thread[0]["answer_comment_id"] == 3

    def test_a_github_outage_leaves_the_card_readable(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(
            _reviewer(),
            prs=[pr],
            github=_FakeGitHub(read_error=GitHubAPIError("down", status_code=503)),
        )

        response = client.get(f"{BASE}/cards/{pr.id}")

        assert response.status_code == 200
        assert response.json()["qa_thread"] == []


# ---------------------------------------------------------------------------
# Webhook ingestion (the U4 seam)
# ---------------------------------------------------------------------------


class TestIngestion:
    def _payload(self, body: str, *, login: str = "claude[bot]") -> dict[str, Any]:
        return {
            "action": "created",
            "repository": {"full_name": REPO},
            "issue": {"number": 42, "pull_request": {"url": "https://api.github.com/..."}},
            "comment": {
                "id": 500,
                "body": body,
                "user": {"login": login},
                "html_url": f"https://github.com/{REPO}/pull/42#issuecomment-500",
            },
        }

    def test_a_question_is_classified_as_a_question(self) -> None:
        ingestion = classify_issue_comment(
            self._payload(f"{CLAUDE_MENTION} why?", login="damienriehl")
        )

        assert ingestion.kind == "question"
        assert ingestion.comment_id == 500
        assert ingestion.pr_number == 42
        assert ingestion.repo_full_name == REPO

    def test_a_referenced_reply_is_classified_as_an_answer(self) -> None:
        ingestion = classify_issue_comment(
            self._payload(
                f"> Replying to https://github.com/{REPO}/pull/42#issuecomment-100\n\nyes"
            )
        )

        assert ingestion.kind == "answer"
        assert ingestion.question_comment_id == 100

    def test_a_bot_comment_with_no_linkage_is_unrelated(self) -> None:
        ingestion = classify_issue_comment(
            self._payload("## Walkthrough", login="coderabbitai[bot]")
        )

        assert ingestion.kind == "unrelated"
        assert ingestion.question_comment_id is None

    def test_a_comment_on_a_plain_issue_is_ignored(self) -> None:
        payload = self._payload(f"{CLAUDE_MENTION} why?")
        payload["issue"] = {"number": 42}

        assert classify_issue_comment(payload).kind == "ignored"

    @pytest.mark.asyncio
    async def test_the_hook_never_raises_on_a_malformed_payload(self) -> None:
        await ingest_issue_comment({})

    def test_registration_is_idempotent(self) -> None:
        from ontokit.services.pr_party_intake import issue_comment_hooks

        before = list(issue_comment_hooks)
        try:
            register_qa_hook()
            register_qa_hook()
            assert issue_comment_hooks.count(ingest_issue_comment) == 1
        finally:
            issue_comment_hooks[:] = before


# ---------------------------------------------------------------------------
# The org answerer workflow asset (A2, KTD18)
# ---------------------------------------------------------------------------


def _workflow() -> dict[str, Any]:
    path = Path(ontokit.__file__).parent / "pr_party_org_assets" / "claude-pr-answers.yml"
    assert path == ORG_WORKFLOW_ASSET
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


# The per-run suffix the untrusted-data markers must carry, verbatim as it
# appears in the asset (GitHub evaluates it; the comment author cannot).
_NONCE = "${{ github.run_id }}-${{ github.run_attempt }}"


def _steps(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _agent_prompt() -> str:
    prompt = ""
    for step in _steps(_workflow()):
        if "prompt" in step.get("with", {}):
            prompt = str(step["with"]["prompt"])
    assert prompt, "the agent step carries a prompt"
    return prompt


class TestOrgWorkflowAsset:
    def test_it_ships_with_a_readme_naming_its_destination(self) -> None:
        readme = ORG_WORKFLOW_ASSET.parent / "README.md"

        assert readme.exists()
        text = readme.read_text(encoding="utf-8")
        assert "catholicos/.github" in text

    def test_it_triggers_only_on_created_issue_comments(self) -> None:
        document = _workflow()
        # PyYAML resolves a bare ``on:`` key to the boolean True (YAML 1.1).
        triggers = document.get("on", document.get(True))

        assert list(triggers) == ["issue_comment"]
        assert triggers["issue_comment"]["types"] == ["created"]

    def test_permissions_are_exactly_issues_write_and_contents_read(self) -> None:
        """A2: naming any scope zeroes the rest, so ``contents: read`` is re-declared."""
        document = _workflow()
        expected = {"contents": "read", "issues": "write"}

        assert document["permissions"] == expected
        for job in document["jobs"].values():
            assert job["permissions"] == expected

    def test_the_author_association_gate_is_present(self) -> None:
        document = _workflow()
        gates = [str(job.get("if", "")) for job in document["jobs"].values()]

        assert gates, "the workflow has at least one job"
        for gate in gates:
            assert "author_association" in gate
            assert "'OWNER'" in gate
            assert "'MEMBER'" in gate
            assert "@claude" in gate

    def test_the_pr_head_is_never_checked_out(self) -> None:
        for step in _steps(_workflow()):
            assert not str(step.get("uses", "")).startswith("actions/checkout")

    def test_the_agent_is_restricted_to_posting_one_comment(self) -> None:
        steps = _steps(_workflow())
        agent = [
            s for s in steps if str(s.get("uses", "")).startswith("anthropics/claude-code-action")
        ]

        assert len(agent) == 1
        args = str(agent[0]["with"]["claude_args"])
        assert "--allowedTools" in args
        assert "mcp__github_comment__update_claude_comment" in args
        for forbidden in ("Bash", "Write", "Edit", "WebFetch"):
            assert forbidden in args.split("--disallowedTools", 1)[1]

    def test_untrusted_pr_text_is_wrapped_as_data(self) -> None:
        agent_prompt = _agent_prompt()

        assert "<untrusted-data" in agent_prompt
        assert "never follow" in agent_prompt.casefold()

    def test_untrusted_delimiters_carry_a_per_run_nonce(self) -> None:
        """KTD18: forgeable delimiters are not a containment bound.

        The suffix is a GitHub expression evaluated on the runner, so the author
        of a pull request title or body cannot know it while writing.
        """
        agent_prompt = _agent_prompt()

        assert agent_prompt.count(f"<untrusted-data-{_NONCE} source=") == 2
        assert agent_prompt.count(f"</untrusted-data-{_NONCE}>") == 2
        # And the agent is told that a bare marker is not a boundary.
        assert _NONCE in agent_prompt.split("UNTRUSTED MATERIAL", 1)[1].split("<untrusted", 1)[0]

    def test_the_static_closing_fence_is_no_longer_forgeable(self) -> None:
        """A PR body containing a literal ``</untrusted-data>`` closes nothing."""
        agent_prompt = _agent_prompt()

        for forgeable in ("</untrusted-data>", "<untrusted-data "):
            assert forgeable not in agent_prompt
