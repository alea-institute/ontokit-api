"""Tests for the PR Party reconciler (U8).

The reconciler exists because two records of the same events drift: ours (action
rows) and GitHub's (reviews, PR lifecycle). Every property below is about a
drift that is otherwise *silent* — nothing errors, the card just quietly says
something untrue.

- **A crashed actuation that succeeded on GitHub must be back-filled, never
  re-posted (C2).** The row is written ``pending`` before the call, so a crash
  between the POST and the update leaves a `pending` row and a real review. The
  reconciler's job is to notice the review and adopt it; posting again would
  double-review the PR.
- **Degraded intent is confirmed by node id, not by login (C3).** A reviewer who
  renames their GitHub account must still have their hand-cast review matched.
  Login is a fallback for rows whose node id was never resolved — never a
  credential slot: a login that matches while the node ids *disagree* is a
  different account and is refused.
- **A dismissed approval must stop authorizing a merge.** GitHub can dismiss a
  review after the fact; our settled row would otherwise keep the merge button
  lit against an approval that no longer exists.
- **Absence is not deletion (C7).** A row missing from the sweep is verified with
  a detail fetch before anything is retired — org search is eventually
  consistent and flaps.

The store and the client are both Protocols with hand-rolled fakes: the pass is
policy over two I/O surfaces, and the policy is the thing under test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyBriefStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.pr_party import (
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_COMMENT,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PR_PARTY_VERDICT_REQUEST_CHANGES,
)
from ontokit.services.pr_party_actions import merge_is_authorized
from ontokit.services.pr_party_github import (
    GitHubAPIError,
    Mergeability,
    PRDetail,
    PRPartyReview,
)
from ontokit.services.pr_party_intake import PR_STATE_CLOSED, PR_STATE_MERGED, PR_STATE_OPEN
from ontokit.services.pr_party_reconcile import (
    ARCHIVE_AFTER_DAYS,
    ERROR_REVIEW_DISMISSED,
    NAG_SWEEP_THRESHOLD,
    ActionContext,
    ReconcileResult,
    find_matching_review,
    is_nagging,
    reconcile_pass,
    review_matches_reviewer,
    should_archive,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
OLD_HEAD = "b" * 40


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _reviewer(
    *, login: str = "damienriehl", node_id: str | None = "MDQ6VXNlcjE="
) -> PRPartyReviewer:
    return PRPartyReviewer(
        id=uuid.uuid4(),
        zitadel_user_id=f"zit-{login}",
        github_login=login,
        github_node_id=node_id,
    )


def _pr(
    *,
    state: str = PR_STATE_OPEN,
    head_sha: str = HEAD,
    missing_since: datetime | None = None,
    updated_at: datetime | None = None,
) -> PRPartyPR:
    return PRPartyPR(
        id=uuid.uuid4(),
        repo_full_name="CatholicOS/ontokit-api",
        pr_number=42,
        state=state,
        head_sha=head_sha,
        brief_status=PRPartyBriefStatus.READY,
        missing_since=missing_since,
        created_at=NOW - timedelta(days=30),
        updated_at=updated_at,
    )


def _action(
    reviewer: PRPartyReviewer,
    pr: PRPartyPR,
    *,
    status: PRPartyActionStatus,
    kind: PRPartyActionKind = PRPartyActionKind.REVIEW,
    verdict: str | None = PR_PARTY_VERDICT_APPROVE,
    head_sha: str = HEAD,
    github_review_id: int | None = None,
    age_minutes: int = 0,
) -> PRPartyAction:
    stamp = NOW - timedelta(minutes=age_minutes)
    return PRPartyAction(
        id=uuid.uuid4(),
        reviewer_id=reviewer.id,
        pr_id=pr.id,
        head_sha=head_sha,
        action_kind=kind,
        verdict=verdict,
        status=status,
        github_review_id=github_review_id,
        idempotency_key=str(uuid.uuid4()),
        created_at=stamp,
        updated_at=stamp,
    )


def _review(
    *,
    review_id: int = 9001,
    state: str = "APPROVED",
    commit_id: str | None = HEAD,
    login: str | None = "damienriehl",
    node_id: str | None = "MDQ6VXNlcjE=",
) -> PRPartyReview:
    return PRPartyReview(
        id=review_id,
        state=state,
        body=None,
        commit_id=commit_id,
        user_login=login,
        user_node_id=node_id,
        submitted_at=NOW - timedelta(minutes=5),
        html_url="https://github.com/CatholicOS/ontokit-api/pull/42#pullrequestreview-9001",
    )


@dataclass
class _FakeStore:
    unsettled: list[ActionContext] = field(default_factory=list)
    standing: list[ActionContext] = field(default_factory=list)
    closed: list[PRPartyPR] = field(default_factory=list)
    missing: list[PRPartyPR] = field(default_factory=list)
    deleted: list[PRPartyPR] = field(default_factory=list)
    saves: int = 0

    async def unsettled_actions(self) -> list[ActionContext]:
        return list(self.unsettled)

    async def standing_approvals(self) -> list[ActionContext]:
        return list(self.standing)

    async def closed_prs(self) -> list[PRPartyPR]:
        return list(self.closed)

    async def missing_prs(self) -> list[PRPartyPR]:
        return list(self.missing)

    async def delete_pr(self, pr: PRPartyPR) -> None:
        self.deleted.append(pr)

    async def save(self) -> None:
        self.saves += 1

    async def rollback(self) -> None:
        return None


@dataclass
class _FakeClient:
    reviews: dict[tuple[str, int], list[PRPartyReview]] = field(default_factory=dict)
    details: dict[tuple[str, int], Any] = field(default_factory=dict)
    review_calls: list[tuple[str, str, int]] = field(default_factory=list)
    detail_calls: list[tuple[str, str, int]] = field(default_factory=list)
    reviews_error: Exception | None = None

    async def get_pr_reviews(self, owner: str, repo: str, number: int) -> list[PRPartyReview]:
        self.review_calls.append((owner, repo, number))
        if self.reviews_error is not None:
            raise self.reviews_error
        return list(self.reviews.get((f"{owner}/{repo}", number), []))

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PRDetail:
        self.detail_calls.append((owner, repo, number))
        outcome = self.details.get((f"{owner}/{repo}", number))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AssertionError("test did not stage a detail response")
        return outcome

    # A write surface the reconciler must never touch.
    async def create_review(self, *args: Any, **kwargs: Any) -> PRPartyReview:  # noqa: ARG002
        raise AssertionError("the reconciler must never post a review")


def _detail(*, state: str = "open", merged: bool = False, head_sha: str = HEAD) -> PRDetail:
    return PRDetail(
        repo_full_name="CatholicOS/ontokit-api",
        number=42,
        title="Title",
        body=None,
        state=state,
        draft=False,
        merged=merged,
        head_sha=head_sha,
        head_ref="feat/x",
        base_ref="main",
        author_login="someone",
        author_node_id="MDQ6VXNlcjk=",
        author_type="User",
        node_id="PR_kw",
        mergeability=Mergeability.MERGEABLE,
        mergeable_state="clean",
        html_url="https://github.com/CatholicOS/ontokit-api/pull/42",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


async def _run(store: _FakeStore, client: _FakeClient, **kwargs: Any) -> ReconcileResult:
    return await reconcile_pass(
        client=client,
        store=store,
        now=kwargs.pop("now", NOW),
        reclaim_minutes=kwargs.pop("reclaim_minutes", 30),
        sweep_minutes=kwargs.pop("sweep_minutes", 5),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Abandoned pending rows — the C2 crash-healing case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandoned_pending_backfilled_from_matching_review() -> None:
    """A crash between POST and update leaves a real review we must adopt."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.PENDING, age_minutes=90)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review()]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.SUCCEEDED
    assert action.github_review_id == 9001
    assert result.backfilled == 1
    # No duplicate post: the only GitHub traffic was the reviews read.
    assert client.review_calls == [("CatholicOS", "ontokit-api", 42)]


@pytest.mark.asyncio
async def test_abandoned_pending_without_match_is_left_for_reclaim() -> None:
    """No review means the attempt really did fail — U6's reclaim owns it."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.PENDING, age_minutes=90)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): []})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert action.github_review_id is None
    assert result.backfilled == 0
    assert result.reclaimable == 1


@pytest.mark.asyncio
async def test_abandoned_pending_ignores_review_at_another_head() -> None:
    """C1: a review of an older revision does not settle this one."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.PENDING, age_minutes=90)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(commit_id=OLD_HEAD)]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert result.backfilled == 0


@pytest.mark.asyncio
async def test_pending_inside_reclaim_window_is_untouched() -> None:
    """An attempt still in flight is not the reconciler's business."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.PENDING, age_minutes=2)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient()

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert result.backfilled == 0
    assert result.reclaimable == 0
    assert client.review_calls == []


# ---------------------------------------------------------------------------
# Degraded intent — confirmation by node id (C3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_intent_confirmed_by_node_id_after_rename() -> None:
    """The reviewer renamed on GitHub; the node id still identifies them."""
    reviewer, pr = _reviewer(login="old-login"), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.DEGRADED_INTENT, age_minutes=60)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(
        reviews={("CatholicOS/ontokit-api", 42): [_review(login="brand-new-login")]}
    )

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.DEGRADED_CONFIRMED
    assert action.github_review_id == 9001
    assert result.confirmed == 1


@pytest.mark.asyncio
async def test_degraded_intent_confirmed_by_login_when_node_id_missing() -> None:
    """Login is the fallback for identities GitHub never resolved for us."""
    reviewer, pr = _reviewer(node_id=None), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.DEGRADED_INTENT, age_minutes=60)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(
        reviews={("CatholicOS/ontokit-api", 42): [_review(login="DamienRiehl", node_id=None)]}
    )

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.DEGRADED_CONFIRMED
    assert result.confirmed == 1


def test_login_match_is_refused_when_node_ids_disagree() -> None:
    """C3: login is never a credential slot — disagreeing node ids win."""
    reviewer = _reviewer(login="damienriehl", node_id="MDQ6VXNlcjE=")
    impostor = _review(login="damienriehl", node_id="MDQ6VXNlcjk5OQ==")

    assert review_matches_reviewer(impostor, reviewer) is False


def test_node_id_match_ignores_login_entirely() -> None:
    reviewer = _reviewer(login="old", node_id="MDQ6VXNlcjE=")
    assert review_matches_reviewer(_review(login="new"), reviewer) is True


# ---------------------------------------------------------------------------
# Nagging — derived, never stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_intent_without_match_nags_after_three_sweeps() -> None:
    reviewer, pr = _reviewer(), _pr()
    stale = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.DEGRADED_INTENT,
        age_minutes=5 * NAG_SWEEP_THRESHOLD,
    )
    store = _FakeStore(unsettled=[ActionContext(action=stale, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): []})

    result = await _run(store, client)

    assert stale.status == PRPartyActionStatus.DEGRADED_INTENT
    assert result.nagging == 1
    assert is_nagging(stale, now=NOW, sweep_minutes=5) is True


def test_is_nagging_false_before_the_threshold() -> None:
    reviewer, pr = _reviewer(), _pr()
    fresh = _action(reviewer, pr, status=PRPartyActionStatus.DEGRADED_INTENT, age_minutes=5)
    assert is_nagging(fresh, now=NOW, sweep_minutes=5) is False


def test_is_nagging_only_applies_to_degraded_intent() -> None:
    reviewer, pr = _reviewer(), _pr()
    settled = _action(reviewer, pr, status=PRPartyActionStatus.SUCCEEDED, age_minutes=600)
    assert is_nagging(settled, now=NOW, sweep_minutes=5) is False


# ---------------------------------------------------------------------------
# Divergence (a): a dismissed approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismissed_approval_at_head_fails_the_action() -> None:
    """Merge authorization must lose an approval GitHub has dismissed."""
    reviewer, pr = _reviewer(), _pr()
    approval = _action(reviewer, pr, status=PRPartyActionStatus.SUCCEEDED, github_review_id=9001)
    assert merge_is_authorized([approval]) is True

    store = _FakeStore(standing=[ActionContext(action=approval, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="DISMISSED")]})

    result = await _run(store, client)

    assert approval.status == PRPartyActionStatus.FAILED
    assert approval.error == ERROR_REVIEW_DISMISSED
    assert result.dismissed == 1
    assert merge_is_authorized([approval]) is False


@pytest.mark.asyncio
async def test_standing_approval_still_approved_is_left_alone() -> None:
    reviewer, pr = _reviewer(), _pr()
    approval = _action(reviewer, pr, status=PRPartyActionStatus.SUCCEEDED, github_review_id=9001)
    store = _FakeStore(standing=[ActionContext(action=approval, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review()]})

    result = await _run(store, client)

    assert approval.status == PRPartyActionStatus.SUCCEEDED
    assert result.dismissed == 0


@pytest.mark.asyncio
async def test_confirmed_degraded_approval_dismissal_matched_by_identity() -> None:
    """A hand-cast approval has no review id of ours until we match it."""
    reviewer, pr = _reviewer(), _pr()
    approval = _action(
        reviewer, pr, status=PRPartyActionStatus.DEGRADED_CONFIRMED, github_review_id=None
    )
    store = _FakeStore(standing=[ActionContext(action=approval, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="DISMISSED")]})

    result = await _run(store, client)

    assert approval.status == PRPartyActionStatus.FAILED
    assert result.dismissed == 1


# ---------------------------------------------------------------------------
# Divergence (b): archival of settled PRs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merged_pr_archived_after_fourteen_days() -> None:
    pr = _pr(state=PR_STATE_MERGED, updated_at=NOW - timedelta(days=ARCHIVE_AFTER_DAYS, hours=1))
    store = _FakeStore(closed=[pr])

    result = await _run(store, _FakeClient())

    assert store.deleted == [pr]
    assert result.archived == 1


@pytest.mark.asyncio
async def test_closed_pr_not_archived_before_fourteen_days() -> None:
    pr = _pr(state=PR_STATE_CLOSED, updated_at=NOW - timedelta(days=ARCHIVE_AFTER_DAYS - 1))
    store = _FakeStore(closed=[pr])

    result = await _run(store, _FakeClient())

    assert store.deleted == []
    assert result.archived == 0


def test_should_archive_falls_back_to_created_at() -> None:
    pr = _pr(state=PR_STATE_CLOSED, updated_at=None)
    assert should_archive(pr, now=NOW) is True


def test_should_archive_refuses_an_open_pr() -> None:
    pr = _pr(state=PR_STATE_OPEN, updated_at=NOW - timedelta(days=365))
    assert should_archive(pr, now=NOW) is False


# ---------------------------------------------------------------------------
# Divergence (c): missing_since verification (C7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_long_enough_and_gone_is_retired() -> None:
    pr = _pr(missing_since=NOW - timedelta(minutes=60))
    store = _FakeStore(missing=[pr])
    client = _FakeClient(
        details={("CatholicOS/ontokit-api", 42): GitHubAPIError("gone", status_code=404)}
    )

    result = await _run(store, client)

    assert pr.state == PR_STATE_CLOSED
    assert pr.missing_since is None
    assert result.retired == 1
    # Freshly retired, so archival does not fire in the same pass.
    assert store.deleted == []


@pytest.mark.asyncio
async def test_missing_but_still_open_clears_the_stamp() -> None:
    """C7: org search flapped. The PR is fine; the stamp was wrong."""
    pr = _pr(missing_since=NOW - timedelta(minutes=60))
    store = _FakeStore(missing=[pr])
    client = _FakeClient(details={("CatholicOS/ontokit-api", 42): _detail(state="open")})

    result = await _run(store, client)

    assert pr.state == PR_STATE_OPEN
    assert pr.missing_since is None
    assert result.missing_cleared == 1
    assert result.retired == 0


@pytest.mark.asyncio
async def test_missing_and_merged_on_detail_is_recorded_as_merged() -> None:
    pr = _pr(missing_since=NOW - timedelta(minutes=60))
    store = _FakeStore(missing=[pr])
    client = _FakeClient(
        details={("CatholicOS/ontokit-api", 42): _detail(state="closed", merged=True)}
    )

    result = await _run(store, client)

    assert pr.state == PR_STATE_MERGED
    assert pr.missing_since is None
    assert result.retired == 1


@pytest.mark.asyncio
async def test_recently_missing_pr_is_not_verified_yet() -> None:
    pr = _pr(missing_since=NOW - timedelta(minutes=1))
    store = _FakeStore(missing=[pr])
    client = _FakeClient()

    result = await _run(store, client)

    assert pr.missing_since is not None
    assert client.detail_calls == []
    assert result.retired == 0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_item_error_does_not_abort_the_pass() -> None:
    """One unreachable PR must not cost us the rest of the reconciliation."""
    reviewer = _reviewer()
    broken = _pr()
    stale_missing = _pr(missing_since=NOW - timedelta(minutes=60))
    archivable = _pr(state=PR_STATE_MERGED, updated_at=NOW - timedelta(days=30))

    action = _action(reviewer, broken, status=PRPartyActionStatus.PENDING, age_minutes=90)
    store = _FakeStore(
        unsettled=[ActionContext(action=action, pr=broken, reviewer=reviewer)],
        missing=[stale_missing],
        closed=[archivable],
    )
    client = _FakeClient(
        reviews_error=GitHubAPIError("boom", status_code=500),
        details={("CatholicOS/ontokit-api", 42): _detail(state="open")},
    )

    result = await _run(store, client)

    assert result.errors == 1
    assert action.status == PRPartyActionStatus.PENDING
    # The other two stages still ran.
    assert result.missing_cleared == 1
    assert result.archived == 1


@pytest.mark.asyncio
async def test_reviews_are_fetched_once_per_pr() -> None:
    """Two reviewers on one PR is one reviews call, not two."""
    pr = _pr()
    a, b = _reviewer(login="one", node_id="N1"), _reviewer(login="two", node_id="N2")
    store = _FakeStore(
        unsettled=[
            ActionContext(
                action=_action(a, pr, status=PRPartyActionStatus.PENDING, age_minutes=90),
                pr=pr,
                reviewer=a,
            ),
            ActionContext(
                action=_action(b, pr, status=PRPartyActionStatus.PENDING, age_minutes=90),
                pr=pr,
                reviewer=b,
            ),
        ]
    )
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(node_id="N1")]})

    result = await _run(store, client)

    assert len(client.review_calls) == 1
    assert result.backfilled == 1
    assert result.reclaimable == 1


@pytest.mark.asyncio
async def test_non_review_kinds_are_left_to_reclaim() -> None:
    """The reviews list says nothing about a merge; do not guess."""
    reviewer, pr = _reviewer(), _pr()
    merge = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.PENDING,
        kind=PRPartyActionKind.MERGE,
        verdict=None,
        age_minutes=90,
    )
    store = _FakeStore(unsettled=[ActionContext(action=merge, pr=pr, reviewer=reviewer)])
    client = _FakeClient()

    result = await _run(store, client)

    assert merge.status == PRPartyActionStatus.PENDING
    assert client.review_calls == []
    assert result.reclaimable == 1


@pytest.mark.asyncio
async def test_later_unsettled_failure_does_not_erase_an_earlier_repair() -> None:
    first_reviewer = _reviewer(login="one", node_id="N1")
    second_reviewer = _reviewer(login="two", node_id="N2")
    first_pr, second_pr = _pr(), _pr()
    second_pr.pr_number = 43
    first = _action(
        first_reviewer,
        first_pr,
        status=PRPartyActionStatus.PENDING,
        age_minutes=90,
    )
    second = _action(
        second_reviewer,
        second_pr,
        status=PRPartyActionStatus.PENDING,
        age_minutes=90,
    )

    class FailingSecondClient(_FakeClient):
        async def get_pr_reviews(
            self, owner: str, repo: str, number: int
        ) -> list[PRPartyReview]:
            if number == 43:
                raise GitHubAPIError("boom", status_code=503)
            return await super().get_pr_reviews(owner, repo, number)

    @dataclass
    class RollbackErasesStore(_FakeStore):
        rollbacks: int = 0

        async def rollback(self) -> None:
            self.rollbacks += 1
            first.status = PRPartyActionStatus.PENDING
            first.github_review_id = None

    store = RollbackErasesStore(
        unsettled=[
            ActionContext(action=first, pr=first_pr, reviewer=first_reviewer),
            ActionContext(action=second, pr=second_pr, reviewer=second_reviewer),
        ]
    )
    client = FailingSecondClient(
        reviews={(first_pr.repo_full_name, first_pr.pr_number): [_review(node_id="N1")]}
    )

    result = await _run(store, client)

    assert first.status == PRPartyActionStatus.SUCCEEDED
    assert first.github_review_id == 9001
    assert result.backfilled == 1
    assert result.errors == 1
    assert store.rollbacks == 0


@pytest.mark.asyncio
async def test_result_serializes_every_counter() -> None:
    payload = ReconcileResult(backfilled=1, confirmed=2, nagging=3).as_dict()
    assert payload["backfilled"] == 1
    assert payload["confirmed"] == 2
    assert payload["nagging"] == 3
    assert set(payload) == {
        "backfilled",
        "reclaimable",
        "confirmed",
        "nagging",
        "dismissed",
        "retired",
        "missing_cleared",
        "archived",
        "errors",
    }


@pytest.mark.asyncio
async def test_a_pass_that_changed_nothing_still_saves_once() -> None:
    result = await _run(_FakeStore(), _FakeClient())
    assert result.as_dict() == {
        "backfilled": 0,
        "reclaimable": 0,
        "confirmed": 0,
        "nagging": 0,
        "dismissed": 0,
        "retired": 0,
        "missing_cleared": 0,
        "archived": 0,
        "errors": 0,
    }


# ---------------------------------------------------------------------------
# Verdict-kind scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_verdict_backfills_from_a_commented_review() -> None:
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.PENDING,
        verdict=PR_PARTY_VERDICT_COMMENT,
        age_minutes=90,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="COMMENTED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.SUCCEEDED
    assert result.backfilled == 1


@pytest.mark.asyncio
async def test_dismissed_review_never_backfills_a_pending_row() -> None:
    """A dismissed review is not evidence the verdict stands."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(reviewer, pr, status=PRPartyActionStatus.PENDING, age_minutes=90)
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="DISMISSED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert result.backfilled == 0


# ---------------------------------------------------------------------------
# Verdict ↔ review state — a row is only settled by its *own* verdict
#
# Head and identity are not enough. The reviews feed at one head holds every
# review anybody left there, so a pending ``approve`` sitting next to the
# reviewer's own drive-by ``COMMENTED`` would otherwise be marked delivered —
# reporting an approval nobody cast, and (via merge_is_authorized) lighting the
# merge button on it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_row_is_not_backfilled_by_a_commented_review() -> None:
    """The bug: same head, same person, different verdict — must not settle."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.PENDING,
        verdict=PR_PARTY_VERDICT_APPROVE,
        age_minutes=90,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="COMMENTED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert action.github_review_id is None
    assert result.backfilled == 0
    # Unchanged path for "no matching review": U6's reclaim still owns the row.
    assert result.reclaimable == 1


@pytest.mark.asyncio
async def test_approve_row_is_backfilled_by_an_approved_review() -> None:
    """Positive control for the case above — the same fixtures, right state."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.PENDING,
        verdict=PR_PARTY_VERDICT_APPROVE,
        age_minutes=90,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="APPROVED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.SUCCEEDED
    assert action.github_review_id == 9001
    assert result.backfilled == 1
    assert result.reclaimable == 0


@pytest.mark.asyncio
async def test_request_changes_row_is_not_backfilled_by_an_approval() -> None:
    """The mirror image, and the more dangerous direction."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.PENDING,
        verdict=PR_PARTY_VERDICT_REQUEST_CHANGES,
        age_minutes=90,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="APPROVED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.PENDING
    assert result.backfilled == 0


@pytest.mark.asyncio
async def test_degraded_approve_intent_is_not_confirmed_by_a_commented_review() -> None:
    """ "I'll approve it by hand" is not discharged by leaving a comment."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.DEGRADED_INTENT,
        verdict=PR_PARTY_VERDICT_APPROVE,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="COMMENTED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.DEGRADED_INTENT
    assert action.github_review_id is None
    assert result.confirmed == 0


@pytest.mark.asyncio
async def test_degraded_approve_intent_is_confirmed_by_an_approval() -> None:
    """Positive control for the degraded path."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.DEGRADED_INTENT,
        verdict=PR_PARTY_VERDICT_APPROVE,
    )
    store = _FakeStore(unsettled=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="APPROVED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.DEGRADED_CONFIRMED
    assert result.confirmed == 1


def test_find_matching_review_filters_on_verdict() -> None:
    """The unit behind the pass, including the states that never match."""
    reviewer = _reviewer()
    approved = _review(state="APPROVED")
    commented = _review(review_id=9002, state="COMMENTED")

    assert (
        find_matching_review(
            [commented, approved],
            reviewer=reviewer,
            head_sha=HEAD,
            verdict=PR_PARTY_VERDICT_APPROVE,
        )
        is approved
    )
    assert (
        find_matching_review(
            [commented], reviewer=reviewer, head_sha=HEAD, verdict=PR_PARTY_VERDICT_APPROVE
        )
        is None
    )
    # Head scoping still wins over a state that would otherwise match.
    assert (
        find_matching_review(
            [_review(state="APPROVED", commit_id=OLD_HEAD)],
            reviewer=reviewer,
            head_sha=HEAD,
            verdict=PR_PARTY_VERDICT_APPROVE,
        )
        is None
    )


def test_find_matching_review_tolerates_payload_casing_and_missing_state() -> None:
    """GitHub sends upper-case, but neither casing nor a null state may crash."""
    reviewer = _reviewer()
    lowercased = _review(state="approved")
    assert (
        find_matching_review(
            [lowercased], reviewer=reviewer, head_sha=HEAD, verdict=PR_PARTY_VERDICT_APPROVE
        )
        is lowercased
    )

    stateless = replace(_review(), state=cast(str, None))
    assert (
        find_matching_review(
            [stateless], reviewer=reviewer, head_sha=HEAD, verdict=PR_PARTY_VERDICT_APPROVE
        )
        is None
    )
    # And with no verdict filter at all it is still not a crash.
    assert find_matching_review([stateless], reviewer=reviewer, head_sha=HEAD) is stateless


def test_find_matching_review_never_matches_a_pending_draft() -> None:
    """An unsubmitted review is not a cast verdict, filter or no filter."""
    reviewer = _reviewer()
    draft = _review(state="PENDING")

    assert (
        find_matching_review(
            [draft], reviewer=reviewer, head_sha=HEAD, verdict=PR_PARTY_VERDICT_APPROVE
        )
        is None
    )
    assert find_matching_review([draft], reviewer=reviewer, head_sha=HEAD) is None
    assert (
        find_matching_review([draft], reviewer=reviewer, head_sha=HEAD, include_dismissed=True)
        is None
    )


def test_find_matching_review_refuses_an_undeliverable_verdict() -> None:
    """``discuss_live`` is not a review, so no review can ever settle it."""
    reviewer = _reviewer()
    reviews = [_review(state="APPROVED")]

    assert (
        find_matching_review(
            reviews, reviewer=reviewer, head_sha=HEAD, verdict=PR_PARTY_VERDICT_DISCUSS_LIVE
        )
        is None
    )
    assert find_matching_review(reviews, reviewer=reviewer, head_sha=HEAD, verdict="") is None
    # verdict=None is the explicit "any state" opt-out the dismissal detector uses.
    assert find_matching_review(reviews, reviewer=reviewer, head_sha=HEAD) is reviews[0]


@pytest.mark.asyncio
async def test_dismissal_detection_still_matches_across_verdicts() -> None:
    """The dismissal detector must keep finding a DISMISSED review by identity."""
    reviewer, pr = _reviewer(), _pr()
    action = _action(
        reviewer,
        pr,
        status=PRPartyActionStatus.DEGRADED_CONFIRMED,
        verdict=PR_PARTY_VERDICT_APPROVE,
    )
    store = _FakeStore(standing=[ActionContext(action=action, pr=pr, reviewer=reviewer)])
    client = _FakeClient(reviews={("CatholicOS/ontokit-api", 42): [_review(state="DISMISSED")]})

    result = await _run(store, client)

    assert action.status == PRPartyActionStatus.FAILED
    assert action.error == ERROR_REVIEW_DISMISSED
    assert result.dismissed == 1


# ---------------------------------------------------------------------------
# The client extension
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pr_reviews_parses_identity_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from ontokit.services.pr_party_github import generation_client

    client = generation_client("tok")
    payload = [
        {
            "id": 4242424242424,
            "state": "APPROVED",
            "body": "lgtm",
            "commit_id": HEAD,
            "user": {"login": "damienriehl", "node_id": "MDQ6VXNlcjE="},
            "submitted_at": "2026-07-26T11:00:00Z",
            "html_url": "https://example.invalid/r",
        },
        "not-an-object",
    ]

    async def fake_request_list(method: str, endpoint: str) -> list[Any]:
        assert method == "GET"
        assert endpoint == "/repos/CatholicOS/ontokit-api/pulls/42/reviews?per_page=100"
        return payload

    monkeypatch.setattr(client, "_request_list", fake_request_list)

    reviews = await client.get_pr_reviews("CatholicOS", "ontokit-api", 42)

    assert len(reviews) == 1
    assert reviews[0].id == 4242424242424
    assert reviews[0].user_node_id == "MDQ6VXNlcjE="
    assert reviews[0].commit_id == HEAD
    assert reviews[0].submitted_at is not None


@pytest.mark.asyncio
async def test_get_pr_reviews_is_available_in_generation_mode() -> None:
    """It is a read: the sweep's generation token must be enough (U3)."""
    from ontokit.services.pr_party_github import PRPartyClientMode, generation_client

    client = generation_client("tok")
    assert client.mode is PRPartyClientMode.GENERATION
    assert client.can_write is False
    assert hasattr(client, "get_pr_reviews")


# ---------------------------------------------------------------------------
# Sweep wiring
# ---------------------------------------------------------------------------


class _EmptyResult:
    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    """Enough AsyncSession for a sweep that discovers nothing."""

    def __init__(self) -> None:
        self.commits = 0

    async def execute(self, *args: Any, **kwargs: Any) -> _EmptyResult:  # noqa: ARG002
        return _EmptyResult()

    async def commit(self) -> None:
        self.commits += 1

    def add(self, obj: Any) -> None:  # noqa: ARG002 # pragma: no cover - unused
        raise AssertionError("the empty sweep adds nothing")


class _EmptySweepClient:
    async def search_org_open_prs(self, *args: Any, **kwargs: Any) -> list[Any]:  # noqa: ARG002
        return []


@pytest.mark.asyncio
async def test_sweep_runs_reconcile_and_reports_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    from ontokit.services import pr_party_intake, pr_party_reconcile

    seen: dict[str, Any] = {}

    async def fake_pass(**kwargs: Any) -> ReconcileResult:
        seen.update(kwargs)
        return ReconcileResult(backfilled=2, archived=1)

    monkeypatch.setattr(pr_party_reconcile, "reconcile_pass", fake_pass)

    db = _EmptySession()
    result = await pr_party_intake.sweep_open_prs(
        db,  # type: ignore[arg-type]
        client=_EmptySweepClient(),  # type: ignore[arg-type]
        org="CatholicOS",
        now=NOW,
    )

    assert result.reconcile is not None
    assert result.reconcile.backfilled == 2
    assert result.as_dict()["reconcile"]["archived"] == 1
    assert seen["now"] == NOW


@pytest.mark.asyncio
async def test_sweep_survives_a_failing_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    from ontokit.services import pr_party_intake, pr_party_reconcile

    async def boom(**kwargs: Any) -> ReconcileResult:  # noqa: ARG001
        raise GitHubAPIError("nope", status_code=500)

    monkeypatch.setattr(pr_party_reconcile, "reconcile_pass", boom)

    result = await pr_party_intake.sweep_open_prs(
        _EmptySession(),  # type: ignore[arg-type]
        client=_EmptySweepClient(),  # type: ignore[arg-type]
        org="CatholicOS",
        now=NOW,
    )

    assert result.reconcile is None
    assert result.errors == 1
    assert result.as_dict()["reconcile"] is None
