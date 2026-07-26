"""Tests for the PR Party queue and card read API (U15).

The contracts these pin:

- **Access control is the feature, not a wrapper.** These endpoints serve
  content derived from *private* repositories. A non-reviewer gets a 403 whose
  body carries no PR data at all — not a redacted card, nothing — and an
  anonymous caller gets a 401 before any row is touched.
- **Readiness is computed server-side (KTD19/R17).** The client renders
  ``readiness.ready`` and ``readiness.reason``; it never re-derives them from
  ``brief_status`` and ``checks_rollup``, so the rule lives in exactly one
  place and cannot drift between API and UI.
- **Own-vs-counterpart is caller-relative (R18/R19).** The row stores
  ``counterpart`` for any registry-member author; ``own`` exists only as this
  projection, computed per caller.
- **R24: two reviewers, two independent payloads.** The same PR yields
  different action state, staleness, and parking for each reviewer.
- **R21: brief content is plain strings.** Never markup, never null-vs-empty
  ambiguity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.routes.pr_party import PRPartyQueueReader, get_queue_reader
from ontokit.api.routes.pr_party_settings import get_credential_service
from ontokit.main import app
from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyMergeDefault,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.pr_party import (
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PR_PARTY_VERDICT_REQUEST_CHANGES,
)

BASE = "/api/v1/pr-party"
USER_ID = "test-user-id"

REPO = "catholicos/ontokit-api"
HEAD = "a" * 40
OLD_HEAD = "b" * 40


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
    repo_full_name: str = REPO,
    pr_number: int = 42,
    state: str = "open",
    head_sha: str = HEAD,
    author_kind: PRPartyAuthorKind = PRPartyAuthorKind.COUNTERPART,
    author_github_login: str | None = "damienriehl",
    author_node_id: str | None = "MDQ6VXNlcjE=",
    mergeable_state: str | None = "mergeable",
    checks_rollup: str | None = "success",
    brief_status: PRPartyBriefStatus = PRPartyBriefStatus.READY,
    brief_what: str | None = "Adds the queue read API.",
    brief_why: str | None = "The dashboard needs something to render.",
    brief_decisions: list[str] | None = None,
    brief_links: list[str] | None = None,
    brief_truncated: bool = False,
    missing_since: datetime | None = None,
    ready_at: datetime | None = None,
) -> PRPartyPR:
    row = PRPartyPR(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        state=state,
        head_sha=head_sha,
        author_kind=author_kind,
        author_github_login=author_github_login,
        author_node_id=author_node_id,
        mergeable_state=mergeable_state,
        checks_rollup=checks_rollup,
        brief_status=brief_status,
        brief_what=brief_what,
        brief_why=brief_why,
        brief_decisions=brief_decisions,
        brief_links=brief_links,
        brief_truncated=brief_truncated,
        missing_since=missing_since,
        ready_at=ready_at or datetime.now(UTC),
        brewing_since=datetime.now(UTC) - timedelta(minutes=10),
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
    override: bool = False,
    body: str | None = None,
    created_at: datetime | None = None,
) -> PRPartyAction:
    row = PRPartyAction(
        reviewer_id=reviewer.id,
        pr_id=pr.id,
        head_sha=head_sha or pr.head_sha,
        action_kind=kind,
        verdict=verdict,
        override=override,
        status=status,
        idempotency_key=uuid.uuid4().hex,
        body=body,
    )
    row.id = uuid.uuid4()
    row.created_at = created_at or datetime.now(UTC)
    return row


# ---------------------------------------------------------------------------
# Fakes at the dependency boundary
# ---------------------------------------------------------------------------


class _FakeCredentialService:
    """The slice of PRPartyCredentialService the read routes use."""

    def __init__(self, reviewer: PRPartyReviewer | None) -> None:
        self.reviewer = reviewer

    async def get_reviewer(self, zitadel_user_id: str) -> PRPartyReviewer | None:
        if self.reviewer is not None and self.reviewer.zitadel_user_id == zitadel_user_id:
            return self.reviewer
        return None


class _FakeReader:
    """Returns rows *unfiltered* so the route's own visibility rules are tested.

    Production narrows by ``state`` in SQL as well; that narrowing is pinned
    separately in :class:`TestReaderQueries`.
    """

    def __init__(
        self, prs: list[PRPartyPR] | None = None, actions: list[PRPartyAction] | None = None
    ) -> None:
        self.prs = prs or []
        self.actions = actions or []

    async def list_open_prs(self) -> list[PRPartyPR]:
        return list(self.prs)

    async def get_pr(self, card_id: uuid.UUID) -> PRPartyPR | None:
        return next((p for p in self.prs if p.id == card_id), None)

    async def list_actions(self, pr_ids: list[uuid.UUID]) -> list[PRPartyAction]:
        wanted = set(pr_ids)
        return [a for a in self.actions if a.pr_id in wanted]


@pytest.fixture
def wired(authed_client: tuple[TestClient, Any]) -> Any:
    """(client, install) — ``install(reviewer, prs, actions)`` binds the fakes."""
    client, _db = authed_client

    def install(
        reviewer: PRPartyReviewer | None,
        prs: list[PRPartyPR] | None = None,
        actions: list[PRPartyAction] | None = None,
    ) -> _FakeReader:
        reader = _FakeReader(prs, actions)
        app.dependency_overrides[get_credential_service] = lambda: _FakeCredentialService(reviewer)
        app.dependency_overrides[get_queue_reader] = lambda: reader
        return reader

    return client, install


def _only_card(client: TestClient) -> dict[str, Any]:
    response = client.get(f"{BASE}/queue")
    assert response.status_code == 200, response.text
    cards = response.json()["cards"]
    assert len(cards) == 1, cards
    card: dict[str, Any] = cards[0]
    return card


# ---------------------------------------------------------------------------
# Access control — these endpoints serve private-repo content
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_unauthenticated_is_401(self) -> None:
        app.dependency_overrides.clear()
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get(f"{BASE}/queue").status_code == 401
        assert client.get(f"{BASE}/cards/{uuid.uuid4()}").status_code == 401

    def test_non_reviewer_forbidden_with_no_pr_data_in_body(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_what="Rewrites the private billing pipeline.")
        install(None, [pr])

        response = client.get(f"{BASE}/queue")

        assert response.status_code == 403
        # Not a redacted card — no PR data reaches a non-reviewer at all.
        assert REPO not in response.text
        assert "billing" not in response.text
        assert str(pr.pr_number) not in response.text
        assert "cards" not in response.json()

    def test_card_detail_forbidden_for_non_reviewer(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_what="Rewrites the private billing pipeline.")
        install(None, [pr])

        response = client.get(f"{BASE}/cards/{pr.id}")

        assert response.status_code == 403
        assert "billing" not in response.text
        assert REPO not in response.text

    def test_both_endpoints_are_no_store(self, wired: Any) -> None:
        """A cached card actuates a stale verdict — never store these."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(reviewer, [pr])

        queue = client.get(f"{BASE}/queue")
        card = client.get(f"{BASE}/cards/{pr.id}")

        assert queue.headers["cache-control"] == "no-store"
        assert card.headers["cache-control"] == "no-store"

    def test_routes_are_mounted_in_the_live_app(self) -> None:
        paths = app.openapi()["paths"]

        assert f"{BASE}/queue" in paths
        assert f"{BASE}/cards/{{card_id}}" in paths


# ---------------------------------------------------------------------------
# Queue membership
# ---------------------------------------------------------------------------


class TestQueueMembership:
    def test_reviewer_sees_active_prs(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(pr_number=1), _pr(pr_number=2)])

        body = client.get(f"{BASE}/queue").json()

        assert {c["pr_number"] for c in body["cards"]} == {1, 2}
        assert body["generated_at"] is not None

    @pytest.mark.parametrize("state", ["draft", "closed", "merged"])
    def test_inactive_states_are_excluded(self, wired: Any, state: str) -> None:
        client, install = wired
        install(_reviewer(), [_pr(state=state)])

        assert client.get(f"{BASE}/queue").json()["cards"] == []

    def test_missing_retired_rows_are_excluded(self, wired: Any) -> None:
        """C7: absent long enough to be retired, so it is not a live card."""
        client, install = wired
        install(_reviewer(), [_pr(missing_since=datetime.now(UTC) - timedelta(hours=4))])

        assert client.get(f"{BASE}/queue").json()["cards"] == []

    def test_briefly_missing_row_is_still_shown(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(missing_since=datetime.now(UTC) - timedelta(seconds=30))])

        assert len(client.get(f"{BASE}/queue").json()["cards"]) == 1


# ---------------------------------------------------------------------------
# Caller-relative author projection (R18/R19)
# ---------------------------------------------------------------------------


class TestAuthorProjection:
    def test_own_pr_projects_own_and_is_read_only(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id="NODE-A")
        install(reviewer, [_pr(author_github_login="damienriehl", author_node_id="NODE-A")])

        card = _only_card(client)

        assert card["author_kind"] == PRPartyAuthorKind.OWN
        assert card["read_only"] is True

    def test_counterpart_pr_projects_counterpart(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id="NODE-A")
        install(reviewer, [_pr(author_github_login="colleague", author_node_id="NODE-B")])

        card = _only_card(client)

        assert card["author_kind"] == PRPartyAuthorKind.COUNTERPART
        assert card["read_only"] is False

    def test_unidentifiable_author_is_not_me(self, wired: Any) -> None:
        """A deleted account leaves no identity to match — so it is not mine."""
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id=None)
        install(reviewer, [_pr(author_github_login=None, author_node_id=None)])

        card = _only_card(client)

        assert card["author_kind"] == PRPartyAuthorKind.COUNTERPART
        assert card["read_only"] is False

    def test_node_id_outranks_a_matching_login(self, wired: Any) -> None:
        """A renamed/taken-over login must not make someone else's PR "mine"."""
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id="NODE-A")
        install(reviewer, [_pr(author_github_login="damienriehl", author_node_id="NODE-B")])

        assert _only_card(client)["author_kind"] == PRPartyAuthorKind.COUNTERPART

    def test_login_fallback_when_node_ids_absent(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer(login="DamienRiehl", node_id=None)
        install(reviewer, [_pr(author_github_login="damienriehl", author_node_id=None)])

        assert _only_card(client)["author_kind"] == PRPartyAuthorKind.OWN

    @pytest.mark.parametrize("stored", [PRPartyAuthorKind.THIRD_PARTY, PRPartyAuthorKind.BOT])
    def test_third_party_and_bot_pass_through(self, wired: Any, stored: PRPartyAuthorKind) -> None:
        client, install = wired
        reviewer = _reviewer(login="damienriehl", node_id="NODE-A")
        install(
            reviewer,
            [_pr(author_kind=stored, author_github_login="damienriehl", author_node_id="NODE-A")],
        )

        card = _only_card(client)

        assert card["author_kind"] == stored
        assert card["read_only"] is False


# ---------------------------------------------------------------------------
# Readiness is computed here, never in the client (R17 / KTD19)
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_brewing_brief_is_not_ready_and_says_why(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(brief_status=PRPartyBriefStatus.BREWING)])

        readiness = _only_card(client)["readiness"]

        assert readiness["ready"] is False
        assert "AI review" in readiness["reason"]

    def test_pending_checks_block_readiness(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(checks_rollup="pending")])

        readiness = _only_card(client)["readiness"]

        assert readiness["ready"] is False
        assert "checks" in readiness["reason"].lower()

    def test_both_blockers_are_reported(self, wired: Any) -> None:
        client, install = wired
        install(
            _reviewer(),
            [_pr(brief_status=PRPartyBriefStatus.BREWING, checks_rollup="pending")],
        )

        reason = _only_card(client)["readiness"]["reason"]

        assert "AI review" in reason
        assert "checks" in reason.lower()

    def test_ready_brief_and_settled_checks_are_ready(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr()])

        readiness = _only_card(client)["readiness"]

        assert readiness["ready"] is True
        assert readiness["reason"] is None

    def test_ready_with_warning_is_ready_and_surfaces_the_warning(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(brief_status=PRPartyBriefStatus.READY_WITH_WARNING)])

        card = _only_card(client)

        assert card["readiness"]["ready"] is True
        assert card["brief_status"] == PRPartyBriefStatus.READY_WITH_WARNING

    def test_no_checks_configured_does_not_block(self, wired: Any) -> None:
        """``none`` is "nothing ran", not "still running"."""
        client, install = wired
        install(_reviewer(), [_pr(checks_rollup="none")])

        assert _only_card(client)["readiness"]["ready"] is True

    def test_failed_brief_is_ready_with_empty_prose_but_live_links(self, wired: Any) -> None:
        """A dead brief must not strand the PR: the human links still work."""
        client, install = wired
        pr = _pr(
            pr_number=7,
            brief_status=PRPartyBriefStatus.FAILED,
            brief_what=None,
            brief_why=None,
            brief_decisions=None,
            brief_links=None,
        )
        install(_reviewer(), [pr])

        card = _only_card(client)
        detail = client.get(f"{BASE}/cards/{pr.id}").json()

        assert card["readiness"]["ready"] is True
        assert card["pr_url"] == f"https://github.com/{REPO}/pull/7"
        assert card["diff_url"] == f"https://github.com/{REPO}/pull/7/files"
        assert detail["brief_what"] == ""
        assert detail["brief_why"] == ""
        assert detail["brief_decisions"] == []
        assert detail["brief_links"] == []

    def test_computing_mergeability_passes_through_verbatim(self, wired: Any) -> None:
        """R4: "computing" is a third state; never render it as "not mergeable"."""
        client, install = wired
        install(_reviewer(), [_pr(mergeable_state="computing")])

        assert _only_card(client)["mergeable_state"] == "computing"

    def test_unknown_mergeability_stays_null(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr(mergeable_state=None)])

        assert _only_card(client)["mergeable_state"] is None


# ---------------------------------------------------------------------------
# Per-caller action state (R24, C1)
# ---------------------------------------------------------------------------


class TestActionState:
    def test_callers_latest_action_is_carried(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(
            reviewer,
            [pr],
            [
                _action(
                    reviewer,
                    pr,
                    verdict=PR_PARTY_VERDICT_REQUEST_CHANGES,
                    override=True,
                    created_at=datetime.now(UTC),
                )
            ],
        )

        actions = _only_card(client)["actions"]

        assert len(actions) == 1
        assert actions[0]["kind"] == PRPartyActionKind.REVIEW
        assert actions[0]["verdict"] == PR_PARTY_VERDICT_REQUEST_CHANGES
        assert actions[0]["status"] == PRPartyActionStatus.SUCCEEDED
        assert actions[0]["head_sha"] == pr.head_sha
        assert actions[0]["override"] is True

    def test_only_the_latest_action_per_kind_survives(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        older = _action(
            reviewer,
            pr,
            verdict=PR_PARTY_VERDICT_REQUEST_CHANGES,
            created_at=datetime.now(UTC) - timedelta(hours=1),
        )
        newer = _action(reviewer, pr, verdict=PR_PARTY_VERDICT_APPROVE)
        install(reviewer, [pr], [newer, older])

        actions = _only_card(client)["actions"]

        assert [a["verdict"] for a in actions] == [PR_PARTY_VERDICT_APPROVE]

    def test_no_action_means_an_empty_list(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr()])

        assert _only_card(client)["actions"] == []

    def test_stale_when_the_head_moved_under_the_verdict(self, wired: Any) -> None:
        """C1: a verdict cast at an old revision does not settle the new one."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr(head_sha=HEAD)
        install(reviewer, [pr], [_action(reviewer, pr, head_sha=OLD_HEAD)])

        assert _only_card(client)["stale"] is True

    def test_not_stale_at_the_current_head(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(reviewer, [pr], [_action(reviewer, pr)])

        assert _only_card(client)["stale"] is False

    def test_failed_action_never_makes_a_card_stale(self, wired: Any) -> None:
        """C6: a dead attempt is not a verdict, so it cannot go stale."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(
            reviewer,
            [pr],
            [_action(reviewer, pr, head_sha=OLD_HEAD, status=PRPartyActionStatus.FAILED)],
        )

        assert _only_card(client)["stale"] is False


# ---------------------------------------------------------------------------
# discuss-live parking (contract U6 implements to)
# ---------------------------------------------------------------------------


class TestParking:
    def test_discuss_live_on_the_current_head_parks_the_card(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(reviewer, [pr], [_action(reviewer, pr, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)])

        assert _only_card(client)["parked"] is True

    def test_discuss_live_at_an_old_head_does_not_park(self, wired: Any) -> None:
        """A new revision is a new conversation — the park does not carry over."""
        client, install = wired
        reviewer = _reviewer()
        pr = _pr(head_sha=HEAD)
        install(
            reviewer,
            [pr],
            [_action(reviewer, pr, head_sha=OLD_HEAD, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE)],
        )

        card = _only_card(client)

        assert card["parked"] is False
        assert card["stale"] is True

    def test_a_later_verdict_unparks(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr()
        install(
            reviewer,
            [pr],
            [
                _action(
                    reviewer,
                    pr,
                    verdict=PR_PARTY_VERDICT_DISCUSS_LIVE,
                    created_at=datetime.now(UTC) - timedelta(hours=1),
                ),
                _action(reviewer, pr, verdict=PR_PARTY_VERDICT_APPROVE),
            ],
        )

        assert _only_card(client)["parked"] is False


# ---------------------------------------------------------------------------
# The other reviewer, compactly (R24)
# ---------------------------------------------------------------------------


class TestOtherReviewerState:
    def test_other_reviewers_approval_shows_without_its_body(self, wired: Any) -> None:
        client, install = wired
        me = _reviewer(login="damienriehl", node_id="NODE-A")
        them = _reviewer(login="colleague", node_id="NODE-B", zitadel_user_id="other-user")
        pr = _pr()
        install(
            me,
            [pr],
            [_action(them, pr, verdict=PR_PARTY_VERDICT_APPROVE, body="secret review prose")],
        )

        response = client.get(f"{BASE}/queue")
        card = response.json()["cards"][0]

        assert card["other_reviewer"] == {"has_approved": True, "has_pending_intent": False}
        assert "secret review prose" not in response.text

    def test_other_reviewers_pending_intent_shows(self, wired: Any) -> None:
        client, install = wired
        me = _reviewer(login="damienriehl", node_id="NODE-A")
        them = _reviewer(login="colleague", node_id="NODE-B", zitadel_user_id="other-user")
        pr = _pr()
        install(
            me,
            [pr],
            [_action(them, pr, status=PRPartyActionStatus.DEGRADED_INTENT)],
        )

        other = _only_card(client)["other_reviewer"]

        assert other["has_pending_intent"] is True
        assert other["has_approved"] is False

    def test_my_own_approval_is_not_the_other_reviewers(self, wired: Any) -> None:
        client, install = wired
        me = _reviewer()
        pr = _pr()
        install(me, [pr], [_action(me, pr, verdict=PR_PARTY_VERDICT_APPROVE)])

        assert _only_card(client)["other_reviewer"]["has_approved"] is False

    def test_stale_approval_by_the_other_reviewer_does_not_count(self, wired: Any) -> None:
        client, install = wired
        me = _reviewer()
        them = _reviewer(login="colleague", node_id="NODE-B", zitadel_user_id="other-user")
        pr = _pr(head_sha=HEAD)
        install(
            me,
            [pr],
            [_action(them, pr, head_sha=OLD_HEAD, verdict=PR_PARTY_VERDICT_APPROVE)],
        )

        assert _only_card(client)["other_reviewer"]["has_approved"] is False

    def test_two_reviewers_get_independent_payloads(self, wired: Any) -> None:
        """R24: the same PR, two reviewers, no shared state."""
        client, install = wired
        me = _reviewer(login="damienriehl", node_id="NODE-A")
        them = _reviewer(login="colleague", node_id="NODE-B", zitadel_user_id=USER_ID)
        pr = _pr(author_github_login="colleague", author_node_id="NODE-B")
        actions = [_action(them, pr, verdict=PR_PARTY_VERDICT_APPROVE)]

        install(me, [pr], actions)
        mine = _only_card(client)

        install(them, [pr], actions)
        theirs = _only_card(client)

        # I see their approval as "the other reviewer"; they see it as their own.
        assert mine["author_kind"] == PRPartyAuthorKind.COUNTERPART
        assert mine["actions"] == []
        assert mine["other_reviewer"]["has_approved"] is True

        assert theirs["author_kind"] == PRPartyAuthorKind.OWN
        assert theirs["actions"][0]["verdict"] == PR_PARTY_VERDICT_APPROVE
        assert theirs["other_reviewer"]["has_approved"] is False


# ---------------------------------------------------------------------------
# Card detail
# ---------------------------------------------------------------------------


class TestCardDetail:
    def test_detail_is_a_superset_of_the_queue_card(self, wired: Any) -> None:
        client, install = wired
        reviewer = _reviewer()
        pr = _pr(
            brief_decisions=["Chose a UUID path over repo/number."],
            brief_links=[f"https://github.com/{REPO}/pull/42/files"],
        )
        install(reviewer, [pr])

        card = _only_card(client)
        detail = client.get(f"{BASE}/cards/{pr.id}").json()

        assert set(card).issubset(set(detail))
        assert detail["card_id"] == str(pr.id)
        assert detail["brief_what"] == "Adds the queue read API."
        assert detail["brief_why"] == "The dashboard needs something to render."
        assert detail["brief_decisions"] == ["Chose a UUID path over repo/number."]
        assert detail["brief_links"] == [f"https://github.com/{REPO}/pull/42/files"]
        assert detail["author_github_login"] == "damienriehl"

    def test_truncation_carries_a_server_written_note(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(brief_truncated=True)
        install(_reviewer(), [pr])

        detail = client.get(f"{BASE}/cards/{pr.id}").json()

        assert detail["brief_truncated"] is True
        assert detail["truncated_note"]

    def test_untruncated_brief_has_no_note(self, wired: Any) -> None:
        client, install = wired
        pr = _pr()
        install(_reviewer(), [pr])

        assert client.get(f"{BASE}/cards/{pr.id}").json()["truncated_note"] is None

    def test_qa_thread_placeholder_is_present_and_empty(self, wired: Any) -> None:
        """U7 fills this; the field exists now so the UI contract does not move."""
        client, install = wired
        pr = _pr()
        install(_reviewer(), [pr])

        assert client.get(f"{BASE}/cards/{pr.id}").json()["qa_thread"] == []

    def test_unknown_card_id_is_404(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr()])

        assert client.get(f"{BASE}/cards/{uuid.uuid4()}").status_code == 404

    def test_retired_row_is_not_reachable_by_id(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(state="closed")
        install(_reviewer(), [pr])

        assert client.get(f"{BASE}/cards/{pr.id}").status_code == 404

    def test_malformed_card_id_is_rejected(self, wired: Any) -> None:
        client, install = wired
        install(_reviewer(), [_pr()])

        assert client.get(f"{BASE}/cards/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------
# R21: brief content is plain strings
# ---------------------------------------------------------------------------


class TestBriefIsPlainStrings:
    def test_brief_fields_serialize_as_plain_strings(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(
            brief_what="<b>bold</b> & <script>alert(1)</script>",
            brief_why="Line one\nLine two",
            brief_decisions=["<i>italic</i>"],
            brief_links=[f"https://github.com/{REPO}/pull/42"],
        )
        install(_reviewer(), [pr])

        detail = client.get(f"{BASE}/cards/{pr.id}").json()

        # Round-trips as text, byte for byte — no markup type, no sanitizer
        # rewrite, nothing that could tempt a client into rendering it as HTML.
        assert isinstance(detail["brief_what"], str)
        assert detail["brief_what"] == "<b>bold</b> & <script>alert(1)</script>"
        assert isinstance(detail["brief_why"], str)
        assert detail["brief_why"] == "Line one\nLine two"
        assert all(isinstance(d, str) for d in detail["brief_decisions"])
        assert all(isinstance(link, str) for link in detail["brief_links"])

    def test_absent_prose_is_an_empty_string_not_null(self, wired: Any) -> None:
        """One empty representation, so the client has one rendering path."""
        client, install = wired
        pr = _pr(brief_what=None, brief_why=None)
        install(_reviewer(), [pr])

        detail = client.get(f"{BASE}/cards/{pr.id}").json()

        assert detail["brief_what"] == ""
        assert detail["brief_why"] == ""

    def test_deep_links_come_from_pr_facts_not_stored_text(self, wired: Any) -> None:
        client, install = wired
        pr = _pr(pr_number=99, brief_links=["https://github.com/catholicos/other/pull/1"])
        install(_reviewer(), [pr])

        card = _only_card(client)

        assert card["pr_url"] == f"https://github.com/{REPO}/pull/99"
        assert card["diff_url"] == f"https://github.com/{REPO}/pull/99/files"


# ---------------------------------------------------------------------------
# The reader's SQL narrows before anything reaches the projection
# ---------------------------------------------------------------------------


class TestReaderQueries:
    @staticmethod
    def _session() -> AsyncMock:
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_list_open_prs_filters_on_state(self) -> None:
        session = self._session()

        await PRPartyQueueReader(session).list_open_prs()

        statement = str(session.execute.await_args.args[0])
        assert "pr_party_pr" in statement
        assert "state" in statement

    async def test_get_pr_filters_on_id(self) -> None:
        session = self._session()
        session.execute.return_value.scalars.return_value.all.return_value = []

        await PRPartyQueueReader(session).get_pr(uuid.uuid4())

        statement = str(session.execute.await_args.args[0])
        assert "pr_party_pr" in statement
        assert "id" in statement

    async def test_list_actions_short_circuits_on_no_prs(self) -> None:
        session = self._session()

        assert await PRPartyQueueReader(session).list_actions([]) == []
        session.execute.assert_not_awaited()

    async def test_list_actions_filters_on_pr_ids(self) -> None:
        session = self._session()

        await PRPartyQueueReader(session).list_actions([uuid.uuid4()])

        statement = str(session.execute.await_args.args[0])
        assert "pr_party_action" in statement
        assert "pr_id" in statement
