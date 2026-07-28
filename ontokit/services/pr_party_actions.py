"""PR Party actuation: turning a tap into a real GitHub review or merge (KTD16).

This is the only place in PR Party that changes something outside our database,
and everything about its shape follows from that.

**The PR identity is never the client's to supply.** Callers name a *card* — the
UUID of a row this service already owns — and the repo, number, and head SHA all
come off that row. There is no body field that could aim a verdict at a
different pull request, and the acting reviewer is always the caller (R23). A
compromised or confused client can, at worst, act on a card it can already see.

**The action row is evidence, not bookkeeping.** :meth:`PRPartyActionService.actuate`
commits a ``pending`` row *before* the GitHub call and only then makes it. A
process that dies mid-call therefore leaves a row that says "someone was here",
which is what makes the reclaim window below safe: the alternative — writing the
row after the call — turns every crash into a silent, invisible double review on
the retry.

**Idempotency is per revision, per kind, per reviewer.** The live-fingerprint
index (U1) permits one non-``failed`` row per
``(reviewer, pr, head_sha, action_kind)``. This service is what decides what a
*second* request against that fingerprint means:

===================================  ==========================================
Existing row                          What a new request does
===================================  ==========================================
``succeeded``, same key               replays the stored receipt (200)
``succeeded``, different key          409 — already actuated at this head
``pending``, inside reclaim window    409 — an attempt is in flight
``pending``, past the window          reclaims the row and proceeds
``failed`` (any key)                  re-opens the row and proceeds
``degraded_intent``, same key         retries if the credential is now usable,
                                      else replays the recorded intent
``degraded_intent``, different key    re-opens the row and proceeds
===================================  ==========================================

Re-opening rather than inserting is the important half. The index puts ``failed``
rows *outside* its predicate so a dead attempt cannot wedge a retry — but that
freedom would let a retry storm accumulate one dead row per attempt. One row per
fingerprint, whatever its history, keeps the audit trail readable and keeps the
"has this reviewer decided?" question answerable with a single row.

**A reclaimed review asks GitHub before it posts.** A ``pending`` row past the
window, or a ``failed`` one, may have died *after* GitHub accepted the review —
the row is evidence an attempt happened, not evidence it failed. Re-actuating
blindly is how one crash becomes two reviews on the pull request, so the reclaim
path lists the PR's reviews first and adopts a match (same reviewer, same head,
same verdict) instead of posting a second one. The degraded paths are excluded
on purpose: nothing was ever sent from here, and a hand-cast review at that
fingerprint is the reconciler's to confirm.

**Refusals are decided from the row, not from the card the client rendered.**
Readiness (R17/R26), drift, lifecycle, own-PR, and merge authorization are all
re-checked here at actuation time. The card is an input; the row is the
authority.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

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
from ontokit.schemas.pr_party import (
    PR_PARTY_REVIEW_VERDICTS,
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PRPartyActionRequest,
)
from ontokit.services.pr_party_credentials import CredentialResolver, mark_credential_dead
from ontokit.services.pr_party_github import (
    GitHubAPIError,
    MergeNotAllowedError,
    PRPartyGitHubClient,
    PRPartyGitHubError,
    ReviewEvent,
    ReviewNotSubmittedError,
    SelfApprovalError,
    StaleCardError,
    TokenExpiredError,
    actuation_client,
    scrub_error,
    split_repo,
)
from ontokit.services.pr_party_reconcile import find_matching_review

logger = logging.getLogger(__name__)

__all__ = [
    "ActionRefused",
    "ActionResult",
    "PRPartyActionService",
    "PRPartyActionStore",
    "merge_is_authorized",
    "review_deep_link",
]

#: Statuses that count as "this reviewer's stance stands" — a settled row.
_SETTLED: frozenset[PRPartyActionStatus] = frozenset(
    {PRPartyActionStatus.SUCCEEDED, PRPartyActionStatus.DEGRADED_CONFIRMED}
)

#: ``mergeable_state`` values that mean the branch cannot be merged as-is. Both
#: vocabularies appear in the column: intake prefers GitHub's own
#: ``mergeable_state`` string and falls back to our :class:`Mergeability` enum
#: (``pr_party_intake.resolve_mergeable_state``), so the guard has to know both.
_UNMERGEABLE_STATES: frozenset[str] = frozenset({"dirty", "not_mergeable"})

#: Written to a credential's ``last_error`` when GitHub rejects the reviewer's
#: PAT mid-verdict. Names the operation, so the settings surface can say what
#: the reviewer was doing when it broke.
_DEAD_PAT_DURING_VERDICT = "GitHub rejected this token during a verdict (401). Submit a fresh PAT."

_VERDICT_EVENTS: dict[str, ReviewEvent] = {
    "approve": ReviewEvent.APPROVE,
    "request_changes": ReviewEvent.REQUEST_CHANGES,
    "comment": ReviewEvent.COMMENT,
}


class ActionRefused(Exception):
    """A refusal the caller should see as an HTTP status, not a 500.

    ``needs_card`` asks the route to attach a freshly projected card to the
    detail: the client refused for drift or lifecycle reasons needs the current
    truth to re-render, and making it re-fetch is a second round trip during
    which the head can move again.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        needs_card: bool = False,
        retire: bool = False,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.needs_card = needs_card
        self.retire = retire
        super().__init__(message)


@dataclass
class ActionResult:
    """The outcome of one actuation, as the route needs it."""

    action: PRPartyAction
    degraded: bool = False
    deep_link: str | None = None
    replayed: bool = False
    merged: bool = False


@dataclass
class _ClaimedRow:
    """What :meth:`PRPartyActionService._claim_row` decided about a fingerprint.

    ``replay`` short-circuits everything downstream; the other two fields only
    matter when it is ``None``.
    """

    action: PRPartyAction
    #: A finished receipt to hand straight back — no credential, no GitHub call.
    replay: ActionResult | None = None
    #: The row was taken over from a dead attempt (``pending`` past the window,
    #: or ``failed``), so GitHub may already hold this row's review.
    reclaimed: bool = False
    #: Resolved while claiming (degraded takeover) so ``actuate`` does not pay
    #: for a second credential lookup.
    token: str | None = None


class ActionStore(Protocol):
    """Data access for the action table, narrow enough to fake in a test."""

    async def find_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None: ...

    async def claim_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None: ...

    async def actions_at_head(self, *, pr_id: uuid.UUID, head_sha: str) -> list[PRPartyAction]: ...

    async def persist(self, action: PRPartyAction) -> None: ...

    async def save(self) -> None: ...

    async def delete(self, action: PRPartyAction) -> None: ...

    async def rollback(self) -> None: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def review_deep_link(pr_url: str, kind: PRPartyActionKind) -> str:
    """Where the reviewer finishes a degraded action by hand (R12).

    Reviews land on the Files tab, where "Review changes" lives; merges land on
    the conversation tab, where the merge button is. A deep link that drops
    someone on the wrong tab is a deep link they have to think about.
    """
    return f"{pr_url}/files" if kind is PRPartyActionKind.REVIEW else pr_url


def merge_is_authorized(actions: Sequence[PRPartyAction]) -> bool:
    """R18/R11: is there a real approval standing at this revision?

    One rule for everyone, which is what makes it defensible: *some* settled
    ``approve`` action at the current head, by any reviewer. The author case
    falls out of it rather than needing its own branch — GitHub will not let
    anyone approve their own PR (and :meth:`PRPartyActionService.actuate`
    refuses before asking), so an approval at head is by construction somebody
    else's. "The merge affordance appears once the counterpart's approval
    exists" is therefore the same sentence as this function.

    Scoping to the current head is the point: an approval of a revision that has
    since been pushed over authorizes nothing (C1).
    """
    return any(
        PRPartyActionKind(a.action_kind) is PRPartyActionKind.REVIEW
        and (a.verdict or "").casefold() == PR_PARTY_VERDICT_APPROVE
        and PRPartyActionStatus(a.status) in _SETTLED
        for a in actions
    )


def _split_repo(repo_full_name: str) -> tuple[str, str]:
    """:func:`~ontokit.services.pr_party_github.split_repo` in this module's refusal type."""
    try:
        return split_repo(repo_full_name)
    except ValueError as e:
        raise ActionRefused(500, "This card's repository name is malformed.") from e


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PRPartyActionStore:
    """SQL for the action table — no policy, so the policy stays testable."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def find_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None:
        """The row occupying this fingerprint, live one first.

        There is at most one non-``failed`` row by index; ``failed`` rows sit
        outside the predicate, so a race could in principle leave more than one.
        Preferring the live row and then the most recent dead one gives the
        re-open path a deterministic target either way.
        """
        result = await self._db.execute(
            select(PRPartyAction)
            .where(
                PRPartyAction.reviewer_id == reviewer_id,
                PRPartyAction.pr_id == pr_id,
                PRPartyAction.head_sha == head_sha,
                PRPartyAction.action_kind == action_kind,
            )
            .order_by(PRPartyAction.created_at.desc())
        )
        rows = list(result.scalars().all())
        live = [r for r in rows if PRPartyActionStatus(r.status) is not PRPartyActionStatus.FAILED]
        pool = live or rows
        return pool[0] if pool else None

    async def claim_action(
        self,
        *,
        reviewer_id: uuid.UUID,
        pr_id: uuid.UUID,
        head_sha: str,
        action_kind: PRPartyActionKind,
    ) -> PRPartyAction | None:
        """Lock the existing fingerprint row until its pending claim commits.

        PostgreSQL serializes concurrent reclaim/repair requests on this row.
        The waiter re-reads the status after the winner commits and therefore
        observes a fresh in-flight ``pending`` row instead of posting again.
        """
        result = await self._db.execute(
            select(PRPartyAction)
            .where(
                PRPartyAction.reviewer_id == reviewer_id,
                PRPartyAction.pr_id == pr_id,
                PRPartyAction.head_sha == head_sha,
                PRPartyAction.action_kind == action_kind,
            )
            .order_by(PRPartyAction.created_at.desc())
            .with_for_update()
        )
        rows = list(result.scalars().all())
        live = [r for r in rows if PRPartyActionStatus(r.status) is not PRPartyActionStatus.FAILED]
        pool = live or rows
        return pool[0] if pool else None

    async def actions_at_head(self, *, pr_id: uuid.UUID, head_sha: str) -> list[PRPartyAction]:
        """Every reviewer's actions against one revision — merge authorization."""
        result = await self._db.execute(
            select(PRPartyAction).where(
                PRPartyAction.pr_id == pr_id,
                PRPartyAction.head_sha == head_sha,
            )
        )
        return list(result.scalars().all())

    async def persist(self, action: PRPartyAction) -> None:
        """Insert-or-update and commit. KTD16's "evidence before the call"."""
        self._db.add(action)
        await self._db.commit()

    async def save(self) -> None:
        await self._db.commit()

    async def delete(self, action: PRPartyAction) -> None:
        await self._db.delete(action)
        await self._db.commit()

    async def rollback(self) -> None:
        await self._db.rollback()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PRPartyActionService:
    """Idempotency, credentials, the GitHub call, and the row's final state.

    Deliberately *not* responsible for projection: readiness, own-vs-counterpart,
    and card serialization are the read side's pure functions (U15), and the
    route composes the two. That keeps this class free of any import from the
    API layer and keeps those rules single-sourced.
    """

    def __init__(
        self,
        *,
        store: ActionStore,
        credentials: CredentialResolver,
        actuation_factory: Callable[[str], PRPartyGitHubClient] = actuation_client,
        reclaim_minutes: int | None = None,
    ) -> None:
        self._store = store
        self._credentials = credentials
        self._actuation_factory = actuation_factory
        self._reclaim_minutes = (
            settings.pr_party_action_reclaim_minutes if reclaim_minutes is None else reclaim_minutes
        )

    # --- Public API ---

    async def actuate(
        self,
        *,
        reviewer: PRPartyReviewer,
        pr: PRPartyPR,
        request: PRPartyActionRequest,
        pr_url: str,
    ) -> ActionResult:
        """Record and (usually) deliver one action. Raises :class:`ActionRefused`.

        ``pr_url`` is passed in rather than derived here so this service holds no
        opinion about GitHub's web URL scheme — the read side already owns that
        one, and one owner is the whole point.
        """
        kind = request.action_kind
        await self._check_merge_preconditions(pr=pr, request=request)

        claim = await self._claim_row(reviewer=reviewer, pr=pr, request=request, pr_url=pr_url)
        if claim.replay is not None:
            return claim.replay
        action = claim.action

        # Discuss-live is a stance, not a delivery: no credential is consulted
        # and no call is made, so parking a card keeps working while degraded.
        if request.verdict == PR_PARTY_VERDICT_DISCUSS_LIVE:
            action.status = PRPartyActionStatus.SUCCEEDED
            action.error = None
            await self._store.save()
            return ActionResult(action=action)

        token = claim.token or await self._credentials.resolve_token(reviewer)
        if token is None:
            return await self._degrade(action, pr_url=pr_url, kind=kind)

        client = self._actuation_factory(token)

        if claim.reclaimed and kind is PRPartyActionKind.REVIEW:
            adopted = await self._adopt_existing_review(
                client=client, reviewer=reviewer, pr=pr, request=request, action=action
            )
            if adopted is not None:
                return adopted

        return await self._call_github(
            reviewer=reviewer, pr=pr, request=request, action=action, client=client, pr_url=pr_url
        )

    async def unpark(self, *, reviewer: PRPartyReviewer, pr: PRPartyPR) -> bool:
        """R9: drop the caller's live ``discuss_live`` row at the current head.

        Deleting is right here where it would be wrong for a verdict: a park is
        a UI state ("we will talk about this"), not a decision anyone reviews
        later, and the alternative — a tombstone status — would make the queue's
        parked test read two rows instead of one. Only ``discuss_live`` is ever
        removed, so a real verdict can never be un-cast through this path.

        Idempotent: nothing parked means nothing to do, not an error.
        """
        action = await self._store.find_action(
            reviewer_id=reviewer.id,
            pr_id=pr.id,
            head_sha=pr.head_sha,
            action_kind=PRPartyActionKind.REVIEW,
        )
        if action is None:
            return False
        if (action.verdict or "").casefold() != PR_PARTY_VERDICT_DISCUSS_LIVE:
            return False
        if PRPartyActionStatus(action.status) is PRPartyActionStatus.FAILED:
            return False
        await self._store.delete(action)
        return True

    async def rollback(self) -> None:
        """Restore the session after a database claim conflict."""
        await self._store.rollback()

    # --- Preconditions ---

    async def _check_merge_preconditions(
        self, *, pr: PRPartyPR, request: PRPartyActionRequest
    ) -> None:
        if request.action_kind is not PRPartyActionKind.MERGE:
            return

        state = (pr.mergeable_state or "").casefold()
        if state in _UNMERGEABLE_STATES:
            raise ActionRefused(
                409,
                "This branch has conflicts with its base and cannot be merged until "
                "they are resolved.",
                needs_card=True,
            )

        at_head = await self._store.actions_at_head(pr_id=pr.id, head_sha=pr.head_sha)
        if not merge_is_authorized(at_head):
            raise ActionRefused(
                409,
                "Nobody has approved this revision yet. A merge needs a standing "
                "approval at the current head.",
                needs_card=True,
            )

    # --- Idempotency ---

    async def _claim_row(
        self,
        *,
        reviewer: PRPartyReviewer,
        pr: PRPartyPR,
        request: PRPartyActionRequest,
        pr_url: str,
    ) -> _ClaimedRow:
        """Take ownership of this fingerprint, or hand back a replayed receipt."""
        existing = await self._store.claim_action(
            reviewer_id=reviewer.id,
            pr_id=pr.id,
            head_sha=request.head_sha,
            action_kind=request.action_kind,
        )

        if existing is None:
            action = PRPartyAction(
                # Minted here rather than left to the column default: the
                # receipt carries this id back to the client, and a value that
                # only exists after a flush would be None on every path that
                # answers before one.
                id=uuid.uuid4(),
                reviewer_id=reviewer.id,
                pr_id=pr.id,
                head_sha=request.head_sha,
                action_kind=request.action_kind,
                verdict=request.verdict,
                override=request.override,
                status=PRPartyActionStatus.PENDING,
                idempotency_key=request.idempotency_key,
                body=request.body,
            )
            # Committed BEFORE any GitHub call: a crash from here on leaves a
            # row that says an attempt happened (KTD16).
            await self._store.persist(action)
            return _ClaimedRow(action=action)

        status = PRPartyActionStatus(existing.status)
        same_key = existing.idempotency_key == request.idempotency_key
        token: str | None = None

        if status in _SETTLED:
            if same_key:
                return _ClaimedRow(action=existing, replay=self._replay(existing, pr_url=pr_url))
            raise ActionRefused(
                409,
                "You have already recorded this action at this revision. Push a new "
                "commit, or open the PR on GitHub to change it.",
                needs_card=True,
            )

        if status is PRPartyActionStatus.DEGRADED_INTENT and same_key:
            # A degraded row is a *pending* delivery, so replaying it forever
            # would make a repaired credential unusable: the only way back to a
            # real review would be a new key the client has no reason to mint.
            # Ask the credential first — if one now resolves, this request takes
            # the row over and actuates for real.
            token = await self._credentials.resolve_token(reviewer)
            if token is None:
                # Still nothing to deliver with. Hand back the same intent *and*
                # the same deep link the first degrade gave (R12) — a replay the
                # client cannot act on is worse than the original response.
                return _ClaimedRow(action=existing, replay=self._replay(existing, pr_url=pr_url))

        if status is PRPartyActionStatus.PENDING and self._in_flight(existing):
            raise ActionRefused(
                409,
                "An attempt for this action is already in flight. Give it a moment "
                "and refresh before trying again.",
            )

        # Reclaim (abandoned pending), re-open (failed), or retry a degraded
        # intent. All are the same write: this request now owns the row, and its
        # history is cleared so the receipt is unambiguous.
        #
        # Only the first two are *reclaims* in the sense the adoption check
        # cares about — a dead attempt that may already have reached GitHub. A
        # degraded row never sent anything from here, so there is nothing of
        # ours to adopt.
        reclaimed = status in (PRPartyActionStatus.PENDING, PRPartyActionStatus.FAILED)
        existing.idempotency_key = request.idempotency_key
        existing.verdict = request.verdict
        existing.override = request.override
        existing.body = request.body
        existing.status = PRPartyActionStatus.PENDING
        existing.error = None
        existing.github_review_id = None
        await self._store.persist(existing)
        return _ClaimedRow(action=existing, reclaimed=reclaimed, token=token)

    def _in_flight(self, action: PRPartyAction) -> bool:
        """Has a ``pending`` row been pending for less than the reclaim window?

        ``updated_at`` when the row has been touched, else ``created_at``: a
        reclaimed row's age must restart, or a single stale row would be
        reclaimable by every concurrent request at once.
        """
        stamp = action.updated_at or action.created_at
        if stamp is None:  # pragma: no cover — server_default always fills it
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return datetime.now(UTC) - stamp < timedelta(minutes=self._reclaim_minutes)

    def _replay(self, action: PRPartyAction, *, pr_url: str) -> ActionResult:
        """The stored row's receipt, verbatim.

        Everything here reads off ``action`` and nothing off the new request:
        the stored row is what actually happened, so a replay whose body differs
        from the stored one still renders the stored one.
        """
        status = PRPartyActionStatus(action.status)
        kind = PRPartyActionKind(action.action_kind)
        degraded = status is PRPartyActionStatus.DEGRADED_INTENT
        return ActionResult(
            action=action,
            degraded=degraded,
            # A degraded replay is still an undelivered verdict, so it needs the
            # same "finish it here" link its first response carried.
            deep_link=review_deep_link(pr_url, kind) if degraded else None,
            replayed=True,
            merged=(kind is PRPartyActionKind.MERGE and status in _SETTLED),
        )

    # --- Delivery ---

    async def _degrade(
        self, action: PRPartyAction, *, pr_url: str, kind: PRPartyActionKind
    ) -> ActionResult:
        """R12: the verdict is real, the delivery is the reviewer's to finish."""
        action.status = PRPartyActionStatus.DEGRADED_INTENT
        await self._store.save()
        return ActionResult(
            action=action,
            degraded=True,
            deep_link=review_deep_link(pr_url, kind),
        )

    async def _adopt_existing_review(
        self,
        *,
        client: PRPartyGitHubClient,
        reviewer: PRPartyReviewer,
        pr: PRPartyPR,
        request: PRPartyActionRequest,
        action: PRPartyAction,
    ) -> ActionResult | None:
        """Did the attempt this row is reclaiming already reach GitHub? (KTD16)

        The row was committed *before* the call that may have killed the
        process, so "there is a row" says an attempt happened — not that it
        failed. Posting again on the strength of that row is precisely how a
        crash turns into a duplicate review on the pull request, and GitHub will
        happily accept the second one.

        So the reclaim asks. A review by this reviewer, at this head, in the
        state this row's verdict would have produced, *is* this row's review:
        it is adopted (``succeeded`` plus the real ``github_review_id``) and
        nothing is posted. Anything weaker — a different verdict, a review of
        another revision, somebody else's — is not this row and does not stop
        the actuation; :func:`~ontokit.services.pr_party_reconcile.find_matching_review`
        is the single place that judgement lives, shared with the reconciler.

        Returning ``None`` means "carry on and actuate", which is also the
        answer when the listing itself fails: a GitHub read that is down must
        not make a verdict impossible to cast. That trades a rare duplicate for
        never blocking the reviewer, and the duplicate is visible and
        reversible where a swallowed verdict is neither.
        """
        owner, repo = _split_repo(pr.repo_full_name)
        try:
            reviews = await client.get_pr_reviews(owner, repo, pr.pr_number)
        except (GitHubAPIError, PRPartyGitHubError) as e:
            logger.warning(
                "PR Party: could not list reviews while reclaiming action %s (%s); "
                "actuating without the duplicate check",
                action.id,
                scrub_error(e),
            )
            return None

        match = find_matching_review(
            reviews,
            reviewer=reviewer,
            head_sha=request.head_sha,
            verdict=request.verdict,
        )
        if match is None:
            return None

        logger.info(
            "PR Party: adopted existing review %s for reclaimed action %s", match.id, action.id
        )
        action.github_review_id = match.id
        action.status = PRPartyActionStatus.SUCCEEDED
        action.error = None
        await self._store.save()
        return ActionResult(action=action)

    async def _call_github(
        self,
        *,
        reviewer: PRPartyReviewer,
        pr: PRPartyPR,
        request: PRPartyActionRequest,
        action: PRPartyAction,
        client: PRPartyGitHubClient,
        pr_url: str,
    ) -> ActionResult:
        owner, repo = _split_repo(pr.repo_full_name)

        try:
            if request.action_kind is PRPartyActionKind.REVIEW:
                verdict = request.verdict or ""
                if verdict not in PR_PARTY_REVIEW_VERDICTS:  # pragma: no cover — schema-guarded
                    raise ActionRefused(422, f"Verdict {verdict!r} is not a GitHub review event.")
                review = await client.create_review(
                    owner,
                    repo,
                    pr.pr_number,
                    commit_id=request.head_sha,
                    event=_VERDICT_EVENTS[verdict],
                    body=request.body,
                )
                action.github_review_id = review.id
                merged = False
            else:
                result = await client.merge_pull_request(
                    owner,
                    repo,
                    pr.pr_number,
                    sha=request.head_sha,
                    merge_method=request.merge_method,
                )
                merged = result.merged
        except TokenExpiredError as e:
            # U2 never learns a PAT died unless the actuation path tells it:
            # validation only runs on submission, so this is the only signal
            # that turns a working credential into a visibly broken one.
            await self._mark_credential_dead(reviewer, e)
            action.error = scrub_error(e)
            return await self._degrade(action, pr_url=pr_url, kind=request.action_kind)
        except SelfApprovalError as e:
            await self._fail(action, e)
            raise ActionRefused(
                403, "GitHub will not accept your review of your own pull request."
            ) from e
        except StaleCardError as e:
            await self._fail(action, e)
            raise ActionRefused(
                409,
                "The branch moved while this merge was in flight. Re-read the new "
                "revision before merging.",
                needs_card=True,
            ) from e
        except MergeNotAllowedError as e:
            await self._fail(action, e)
            raise ActionRefused(
                409,
                "GitHub refused this merge — the pull request is not in a mergeable "
                "state, or this repository does not allow that merge method.",
                needs_card=True,
            ) from e
        except (ReviewNotSubmittedError, GitHubAPIError, PRPartyGitHubError) as e:
            await self._fail(action, e)
            raise ActionRefused(
                502,
                "GitHub could not complete this action. Nothing was recorded as done "
                "and the card stays in your queue — try again shortly.",
            ) from e

        action.status = PRPartyActionStatus.SUCCEEDED
        action.error = None
        await self._store.save()
        return ActionResult(action=action, merged=merged)

    async def _fail(self, action: PRPartyAction, error: BaseException) -> None:
        action.status = PRPartyActionStatus.FAILED
        action.error = scrub_error(error)
        await self._store.save()

    async def _mark_credential_dead(self, reviewer: PRPartyReviewer, error: BaseException) -> None:
        # The shared helper mutates but never commits, so the write rides this
        # service's own unit of work — the action store's session.
        if await mark_credential_dead(
            self._credentials, reviewer, error, message=_DEAD_PAT_DURING_VERDICT
        ):
            await self._store.save()
