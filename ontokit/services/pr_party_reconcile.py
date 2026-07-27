"""PR Party reconciliation — making our record and GitHub's converge (U8, R20).

The sweep (U4) keeps *PR facts* fresh. This module keeps the other half honest:
the verdict record. Those two records drift in four ways, each of which fails
silently if nobody looks:

1. **A crashed actuation that succeeded (C2).** ``pr_party_action`` rows are
   written ``pending`` *before* the GitHub call, precisely so a crash leaves
   evidence. If the process died between the POST and the update, GitHub holds a
   real review that our row knows nothing about. The reconciler adopts it —
   sets ``github_review_id`` and ``succeeded`` — and never posts again. Posting
   would be the one genuinely destructive mistake available here, so the whole
   back-fill path is a *read* plus a local write.

2. **A degraded verdict the reviewer completed by hand (R12, C3).** When the app
   could not deliver a verdict it records ``degraded_intent`` and hands back a
   deep link. Confirmation is not a button: it is the review showing up on
   GitHub under the reviewer's **node id**. Login is the fallback for rows whose
   node id was never resolved, and it is *only* a fallback — a login that
   matches while the node ids disagree is a different account and is refused.
   Identity here decides whether someone else's approval settles your card, so
   it is treated as an identity check, never as a credential slot.

3. **A dismissed approval.** GitHub lets an approval be dismissed after the
   fact. Our settled row would keep authorizing a merge against an approval that
   no longer exists, so a dismissal at the *current* head fails the row
   (``error='ReviewDismissed'``) and :func:`~ontokit.services.pr_party_actions.
   merge_is_authorized` stops counting it. The mirror case — a force-push — needs
   no write at all: actions are head-scoped, so a new head retires old verdicts
   by construction, and re-brewing the new revision is U4's job.

4. **A PR that left without saying goodbye (C7).** The sweep stamps
   ``missing_since`` on rows a *complete* discovery pass did not see, and
   deliberately retires nothing. This module acts on the stamp, and it verifies
   before it acts: a detail fetch decides between "really gone" (404 → treat as
   closed) and "org search flapped" (still open → clear the stamp). Settled PRs
   are then archived after :data:`ARCHIVE_AFTER_DAYS`.

**Nagging is derived, never stored.** A ``degraded_intent`` row that no matching
review has confirmed after :data:`NAG_SWEEP_THRESHOLD` sweeps deserves a nudge —
but there is no nag column and no nag row. :func:`is_nagging` computes it from
the row's own timestamp and the sweep cadence, which is the same information
with one fewer thing to keep consistent (the same argument
``missing_long_enough_to_retire`` makes for the miss count). The pass counts
nagging rows so the cron log shows the number; a read surface that wants to
render the nudge calls the helper.

The store and the client are Protocols. The pass is policy over two I/O
surfaces, and keeping both behind narrow interfaces is what lets the policy be
tested without a database or a network.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.pr_party import PR_PARTY_VERDICT_APPROVE
from ontokit.services.pr_party_github import GitHubAPIError, PRDetail, PRPartyReview
from ontokit.services.pr_party_intake import (
    PR_STATE_CLOSED,
    PR_STATE_MERGED,
    missing_long_enough_to_retire,
)

__all__ = [
    "ARCHIVE_AFTER_DAYS",
    "ERROR_REVIEW_DISMISSED",
    "NAG_SWEEP_THRESHOLD",
    "ActionContext",
    "PRPartyReconcileStore",
    "ReconcileClient",
    "ReconcileResult",
    "ReconcileStore",
    "find_matching_review",
    "is_nagging",
    "reconcile_pass",
    "review_matches_reviewer",
    "should_archive",
]

logger = logging.getLogger(__name__)

# How many sweeps a ``degraded_intent`` row may sit unconfirmed before it is
# worth nudging the reviewer about. Expressed in sweeps rather than minutes so
# it tracks PR_PARTY_SWEEP_MINUTES, exactly like MISSING_MISS_THRESHOLD.
NAG_SWEEP_THRESHOLD: Final = 3

# A closed or merged PR stops being a card immediately; the row lingers only so
# the queue can show recent history. After this it is deleted outright (actions
# cascade), which is what "archive" means here — there is no cold table.
ARCHIVE_AFTER_DAYS: Final = 14

# Persisted in ``PRPartyAction.error``. Follows scrub_error's rule: a class-shaped
# token, never GitHub prose.
ERROR_REVIEW_DISMISSED: Final = "ReviewDismissed"

REVIEW_STATE_DISMISSED: Final = "DISMISSED"

# Statuses whose GitHub-side truth this module re-checks on every pass.
_UNSETTLED: Final[frozenset[str]] = frozenset(
    {PRPartyActionStatus.PENDING.value, PRPartyActionStatus.DEGRADED_INTENT.value}
)
_STANDING: Final[frozenset[str]] = frozenset(
    {PRPartyActionStatus.SUCCEEDED.value, PRPartyActionStatus.DEGRADED_CONFIRMED.value}
)
_ARCHIVABLE_STATES: Final[frozenset[str]] = frozenset({PR_STATE_CLOSED, PR_STATE_MERGED})


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionContext:
    """One action plus the two rows needed to judge it.

    Carried together because every decision below is a three-way comparison —
    the row's claim, the PR's current head, and the reviewer's GitHub identity —
    and re-querying either side per action is how an N+1 gets into a cron job.
    """

    action: PRPartyAction
    pr: PRPartyPR
    reviewer: PRPartyReviewer


@dataclass
class ReconcileResult:
    """What one pass changed, shaped like :class:`SweepResult` for the cron log."""

    backfilled: int = 0
    reclaimable: int = 0
    confirmed: int = 0
    nagging: int = 0
    dismissed: int = 0
    retired: int = 0
    missing_cleared: int = 0
    archived: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "backfilled": self.backfilled,
            "reclaimable": self.reclaimable,
            "confirmed": self.confirmed,
            "nagging": self.nagging,
            "dismissed": self.dismissed,
            "retired": self.retired,
            "missing_cleared": self.missing_cleared,
            "archived": self.archived,
            "errors": self.errors,
        }


class ReconcileClient(Protocol):
    """The slice of :class:`PRPartyGitHubClient` reconciliation depends on.

    Both members are reads, which is the point: the reconciler runs on the
    shared generation token and has no write surface at all.
    """

    async def get_pr_reviews(self, owner: str, repo: str, number: int) -> list[PRPartyReview]: ...

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PRDetail: ...


class ReconcileStore(Protocol):
    """Data access for the pass, narrow enough to fake in a test."""

    async def unsettled_actions(self) -> list[ActionContext]: ...

    async def standing_approvals(self) -> list[ActionContext]: ...

    async def closed_prs(self) -> list[PRPartyPR]: ...

    async def missing_prs(self) -> list[PRPartyPR]: ...

    async def delete_pr(self, pr: PRPartyPR) -> None: ...

    async def save(self) -> None: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _stamp(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive column value to UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _age_reference(action: PRPartyAction) -> datetime | None:
    """When this row last claimed to be true — ``updated_at`` if it has one."""
    return _stamp(action.updated_at) or _stamp(action.created_at)


def review_matches_reviewer(review: PRPartyReview, reviewer: PRPartyReviewer) -> bool:
    """Is this GitHub review the work of this reviewer? (KTD12, C3)

    Node id wins outright when both sides have one — that is the identity that
    survives a username rename, and it is the *only* comparison made in that
    case: two accounts with disagreeing node ids are two accounts no matter how
    their logins read, so a matching login there is refused rather than trusted.

    Casefolded login is the fallback for exactly one situation: an identity
    GitHub never resolved a node id for (``github_node_id`` is nullable because
    registry reconcile can run before GitHub is reachable). It is a fallback for
    *matching*, never an authorization: nothing in this module posts, so the
    worst a wrong match can do is mislabel a row, and the node-id branch above
    keeps it from doing even that whenever the data exists.
    """
    if reviewer.github_node_id and review.user_node_id:
        return reviewer.github_node_id == review.user_node_id
    if not review.user_login or not reviewer.github_login:
        return False
    return review.user_login.casefold() == reviewer.github_login.casefold()


def _review_at_head(review: PRPartyReview, head_sha: str) -> bool:
    """C1: a review of another revision is not evidence about this one.

    A review with no ``commit_id`` cannot be placed on a revision and therefore
    never matches — the failure mode of guessing "probably this head" is
    adopting somebody's stale approval onto a new push.
    """
    return bool(review.commit_id) and review.commit_id == head_sha


def find_matching_review(
    reviews: Sequence[PRPartyReview],
    *,
    reviewer: PRPartyReviewer,
    head_sha: str,
    include_dismissed: bool = False,
) -> PRPartyReview | None:
    """The reviewer's own submitted review at this revision, if GitHub has one.

    Dismissed reviews are excluded by default: a dismissed review is not evidence
    that a verdict stands, so it must never back-fill or confirm a row. The
    dismissal *detector* passes ``include_dismissed=True`` because for it the
    dismissal is the whole finding.
    """
    for review in reviews:
        if not _review_at_head(review, head_sha):
            continue
        if not review_matches_reviewer(review, reviewer):
            continue
        if not include_dismissed and review.state.upper() == REVIEW_STATE_DISMISSED:
            continue
        return review
    return None


def is_abandoned(
    action: PRPartyAction, *, now: datetime | None = None, reclaim_minutes: int | None = None
) -> bool:
    """Has a ``pending`` row outlived U6's reclaim window?

    Mirrors ``PRPartyActionService._is_in_flight`` deliberately: inside the
    window an attempt may genuinely still be running, and the reconciler must
    not race the request that owns the row.
    """
    if PRPartyActionStatus(action.status) is not PRPartyActionStatus.PENDING:
        return False
    stamp = _age_reference(action)
    if stamp is None:
        return False
    window = (
        settings.pr_party_action_reclaim_minutes if reclaim_minutes is None else reclaim_minutes
    )
    return (now or datetime.now(UTC)) - stamp >= timedelta(minutes=window)


def is_nagging(
    action: PRPartyAction, *, now: datetime | None = None, sweep_minutes: int | None = None
) -> bool:
    """Should the reviewer be nudged about an unconfirmed degraded verdict?

    Pure and derived — see the module docstring on why there is no nag column.
    A read surface that wants to render "you said you'd do this by hand three
    sweeps ago" calls this; nothing needs to be written for it to become true.
    """
    if PRPartyActionStatus(action.status) is not PRPartyActionStatus.DEGRADED_INTENT:
        return False
    stamp = _age_reference(action)
    if stamp is None:
        return False
    cadence = sweep_minutes or settings.pr_party_sweep_minutes or 5
    elapsed = (now or datetime.now(UTC)) - stamp
    return elapsed >= timedelta(minutes=cadence * NAG_SWEEP_THRESHOLD)


def should_archive(
    pr: PRPartyPR, *, now: datetime | None = None, days: int = ARCHIVE_AFTER_DAYS
) -> bool:
    """Has a settled PR been settled long enough to delete the row?

    Age is taken from the row's own timestamps, not GitHub's: what is being aged
    out is our projection, and ``updated_at`` is when we last had anything to say
    about it. ``created_at`` is the fallback for a row nothing ever updated.
    """
    if pr.state not in _ARCHIVABLE_STATES:
        return False
    stamp = _stamp(pr.updated_at) or _stamp(pr.created_at)
    if stamp is None:
        return False
    return (now or datetime.now(UTC)) - stamp >= timedelta(days=days)


def _split_repo(repo_full_name: str) -> tuple[str, str]:
    owner, _, repo = repo_full_name.strip("/").partition("/")
    if not owner or not repo:
        raise ValueError(f"Malformed repository name: {repo_full_name!r}")
    return owner, repo


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PRPartyReconcileStore:
    """SQL for the pass — no policy, so the policy stays testable."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def unsettled_actions(self) -> list[ActionContext]:
        """``pending`` and ``degraded_intent`` rows, with their PR and reviewer."""
        result = await self._db.execute(
            select(PRPartyAction, PRPartyPR, PRPartyReviewer)
            .join(PRPartyPR, PRPartyPR.id == PRPartyAction.pr_id)
            .join(PRPartyReviewer, PRPartyReviewer.id == PRPartyAction.reviewer_id)
            .where(PRPartyAction.status.in_(sorted(_UNSETTLED)))
        )
        return [
            ActionContext(action=action, pr=pr, reviewer=reviewer)
            for action, pr, reviewer in result.all()
        ]

    async def standing_approvals(self) -> list[ActionContext]:
        """Settled approvals that still authorize a merge *at the current head*.

        Head-scoped in SQL for the same reason ``merge_is_authorized`` is
        head-scoped in Python: an approval of a revision that has been pushed
        over authorizes nothing, so its dismissal is not news.
        """
        result = await self._db.execute(
            select(PRPartyAction, PRPartyPR, PRPartyReviewer)
            .join(PRPartyPR, PRPartyPR.id == PRPartyAction.pr_id)
            .join(PRPartyReviewer, PRPartyReviewer.id == PRPartyAction.reviewer_id)
            .where(
                PRPartyAction.status.in_(sorted(_STANDING)),
                PRPartyAction.action_kind == PRPartyActionKind.REVIEW.value,
                PRPartyAction.verdict == PR_PARTY_VERDICT_APPROVE,
                PRPartyAction.head_sha == PRPartyPR.head_sha,
            )
        )
        return [
            ActionContext(action=action, pr=pr, reviewer=reviewer)
            for action, pr, reviewer in result.all()
        ]

    async def closed_prs(self) -> list[PRPartyPR]:
        """Every settled row. Bounded by ``ARCHIVE_AFTER_DAYS`` of org volume."""
        result = await self._db.execute(
            select(PRPartyPR).where(PRPartyPR.state.in_(sorted(_ARCHIVABLE_STATES)))
        )
        return list(result.scalars().all())

    async def missing_prs(self) -> list[PRPartyPR]:
        result = await self._db.execute(
            select(PRPartyPR).where(PRPartyPR.missing_since.is_not(None))
        )
        return list(result.scalars().all())

    async def delete_pr(self, pr: PRPartyPR) -> None:
        await self._db.delete(pr)

    async def save(self) -> None:
        await self._db.commit()


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


async def reconcile_pass(
    *,
    client: ReconcileClient,
    store: ReconcileStore,
    now: datetime | None = None,
    reclaim_minutes: int | None = None,
    sweep_minutes: int | None = None,
) -> ReconcileResult:
    """One convergence cycle. Reads GitHub, writes only our own rows.

    Every stage is individually guarded: a repo we cannot reach costs its own
    items and nothing else. That is not politeness — the sweep runs this once
    every few minutes, and a pass that aborts on the first 500 would let the
    other 99% of the drift accumulate indefinitely.
    """
    moment = now or datetime.now(UTC)
    result = ReconcileResult()
    reviews_cache: dict[tuple[str, int], list[PRPartyReview]] = {}

    async def reviews_for(pr: PRPartyPR) -> list[PRPartyReview]:
        """One reviews call per PR per pass, however many rows reference it."""
        key = (pr.repo_full_name, pr.pr_number)
        if key not in reviews_cache:
            owner, repo = _split_repo(pr.repo_full_name)
            reviews_cache[key] = await client.get_pr_reviews(owner, repo, pr.pr_number)
        return reviews_cache[key]

    await _reconcile_unsettled(
        store,
        reviews_for,
        result=result,
        moment=moment,
        reclaim_minutes=reclaim_minutes,
        sweep_minutes=sweep_minutes,
    )
    await _detect_dismissals(store, reviews_for, result=result)
    await _verify_missing(store, client, result=result, moment=moment)
    await _archive_settled(store, result=result, moment=moment)

    await store.save()

    logger.info("PR Party reconcile complete: %s", result.as_dict())
    return result


async def _reconcile_unsettled(
    store: ReconcileStore,
    reviews_for: Any,
    *,
    result: ReconcileResult,
    moment: datetime,
    reclaim_minutes: int | None,
    sweep_minutes: int | None,
) -> None:
    """Back-fill abandoned attempts (C2) and confirm degraded intent (C3)."""
    try:
        contexts = await store.unsettled_actions()
    except Exception as exc:  # noqa: BLE001
        result.errors += 1
        logger.exception("PR Party reconcile could not load unsettled actions: %s", exc)
        return

    for ctx in contexts:
        try:
            await _reconcile_one_unsettled(
                ctx,
                reviews_for,
                result=result,
                moment=moment,
                reclaim_minutes=reclaim_minutes,
                sweep_minutes=sweep_minutes,
            )
        except Exception as exc:  # noqa: BLE001 — one bad PR must not end the pass
            result.errors += 1
            logger.exception(
                "PR Party reconcile failed for action %s on %s#%s: %s",
                ctx.action.id,
                ctx.pr.repo_full_name,
                ctx.pr.pr_number,
                exc,
            )


async def _reconcile_one_unsettled(
    ctx: ActionContext,
    reviews_for: Any,
    *,
    result: ReconcileResult,
    moment: datetime,
    reclaim_minutes: int | None,
    sweep_minutes: int | None,
) -> None:
    action = ctx.action
    status = PRPartyActionStatus(action.status)

    # Only reviews are visible in the reviews feed. A pending merge or question
    # is left to U6's reclaim rather than guessed at from PR state — inferring
    # "the PR is merged, so my merge attempt must have won" would credit one
    # reviewer with somebody else's merge.
    if PRPartyActionKind(action.action_kind) is not PRPartyActionKind.REVIEW:
        if status is PRPartyActionStatus.PENDING and is_abandoned(
            action, now=moment, reclaim_minutes=reclaim_minutes
        ):
            result.reclaimable += 1
        return

    if status is PRPartyActionStatus.PENDING:
        if not is_abandoned(action, now=moment, reclaim_minutes=reclaim_minutes):
            return  # An attempt may still be in flight; it owns the row.
        match = find_matching_review(
            await reviews_for(ctx.pr), reviewer=ctx.reviewer, head_sha=action.head_sha
        )
        if match is None:
            # The attempt really did die before GitHub saw it. U6 reclaims the
            # fingerprint on the reviewer's next tap; nothing to write here.
            result.reclaimable += 1
            return
        action.github_review_id = match.id or None
        action.status = PRPartyActionStatus.SUCCEEDED
        action.error = None
        result.backfilled += 1
        logger.info("PR Party back-filled abandoned action %s from review %s", action.id, match.id)
        return

    # degraded_intent: confirmation is the review appearing under the reviewer.
    match = find_matching_review(
        await reviews_for(ctx.pr), reviewer=ctx.reviewer, head_sha=action.head_sha
    )
    if match is not None:
        action.github_review_id = match.id or None
        action.status = PRPartyActionStatus.DEGRADED_CONFIRMED
        action.error = None
        result.confirmed += 1
        return
    if is_nagging(action, now=moment, sweep_minutes=sweep_minutes):
        result.nagging += 1


async def _detect_dismissals(
    store: ReconcileStore, reviews_for: Any, *, result: ReconcileResult
) -> None:
    """Divergence (a): an approval GitHub has since dismissed."""
    try:
        contexts = await store.standing_approvals()
    except Exception as exc:  # noqa: BLE001
        result.errors += 1
        logger.exception("PR Party reconcile could not load standing approvals: %s", exc)
        return

    for ctx in contexts:
        try:
            reviews = await reviews_for(ctx.pr)
            if not _approval_was_dismissed(ctx, reviews):
                continue
            ctx.action.status = PRPartyActionStatus.FAILED
            ctx.action.error = ERROR_REVIEW_DISMISSED
            result.dismissed += 1
            logger.info(
                "PR Party dropped dismissed approval %s on %s#%s",
                ctx.action.id,
                ctx.pr.repo_full_name,
                ctx.pr.pr_number,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors += 1
            logger.exception(
                "PR Party dismissal check failed for %s#%s: %s",
                ctx.pr.repo_full_name,
                ctx.pr.pr_number,
                exc,
            )


def _approval_was_dismissed(ctx: ActionContext, reviews: Sequence[PRPartyReview]) -> bool:
    """Match by our stored review id first, by identity only as a fallback.

    A row we posted ourselves carries the exact id, which is unambiguous. A
    ``degraded_confirmed`` row adopted from a hand-cast review may have been
    confirmed before we had one, so identity + head is the fallback. An id we
    hold that is absent from the feed is *not* treated as a dismissal — a
    truncated page must not fail a real approval.
    """
    review_id = ctx.action.github_review_id
    if review_id:
        for review in reviews:
            if review.id == review_id:
                return review.state.upper() == REVIEW_STATE_DISMISSED
        return False
    match = find_matching_review(
        reviews,
        reviewer=ctx.reviewer,
        head_sha=ctx.action.head_sha,
        include_dismissed=True,
    )
    return match is not None and match.state.upper() == REVIEW_STATE_DISMISSED


async def _verify_missing(
    store: ReconcileStore,
    client: ReconcileClient,
    *,
    result: ReconcileResult,
    moment: datetime,
) -> None:
    """Divergence (c): act on ``missing_since``, but verify first (C7).

    The sweep only ever stamps; it never concludes. Search is eventually
    consistent and does flap, so the stamp is a *question* — this asks GitHub
    directly before answering it.
    """
    try:
        rows = await store.missing_prs()
    except Exception as exc:  # noqa: BLE001
        result.errors += 1
        logger.exception("PR Party reconcile could not load missing PRs: %s", exc)
        return

    for pr in rows:
        if not missing_long_enough_to_retire(pr, now=moment):
            continue
        try:
            owner, repo = _split_repo(pr.repo_full_name)
            detail = await client.get_pull_request(owner, repo, pr.pr_number)
        except GitHubAPIError as exc:
            if exc.status_code != 404:
                result.errors += 1
                logger.exception(
                    "PR Party could not verify missing %s#%s: %s",
                    pr.repo_full_name,
                    pr.pr_number,
                    exc,
                )
                continue
            # Really gone — deleted repo, deleted PR, or lost visibility. It is
            # closed as far as this projection is concerned, and archival will
            # collect it once the row has been settled long enough.
            pr.state = PR_STATE_CLOSED
            pr.missing_since = None
            result.retired += 1
            continue
        except Exception as exc:  # noqa: BLE001
            result.errors += 1
            logger.exception(
                "PR Party could not verify missing %s#%s: %s",
                pr.repo_full_name,
                pr.pr_number,
                exc,
            )
            continue

        pr.missing_since = None
        if detail.merged:
            pr.state = PR_STATE_MERGED
            result.retired += 1
        elif detail.state == "closed":
            pr.state = PR_STATE_CLOSED
            result.retired += 1
        else:
            # The search flapped. Nothing was wrong except our stamp.
            result.missing_cleared += 1


async def _archive_settled(
    store: ReconcileStore, *, result: ReconcileResult, moment: datetime
) -> None:
    """Divergence (b): delete settled rows once they stop being recent history.

    "Archive" is a delete: ``pr_party_action`` cascades off the PR, and the
    durable record of what happened lives on GitHub, which is the system of
    record. Keeping a second copy forever would make this table the thing that
    has to be reconciled.
    """
    try:
        rows = await store.closed_prs()
    except Exception as exc:  # noqa: BLE001
        result.errors += 1
        logger.exception("PR Party reconcile could not load closed PRs: %s", exc)
        return

    for pr in rows:
        if not should_archive(pr, now=moment):
            continue
        try:
            await store.delete_pr(pr)
            result.archived += 1
        except Exception as exc:  # noqa: BLE001
            result.errors += 1
            logger.exception(
                "PR Party could not archive %s#%s: %s", pr.repo_full_name, pr.pr_number, exc
            )
