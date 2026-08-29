"""PR Party read API: the reviewer's queue and one card's detail.

This is the surface the dashboard types against. Three things about it are
load-bearing.

**1. Access control is the feature.** Every card here is derived from a private
repository — the brief summarizes a diff, the links point into the repo, the
title (when there is one) is the PR's own. So there is no "public shape" of
these responses to fall back on: a non-reviewer gets a 403 whose body carries
no PR data whatsoever, and an anonymous caller gets a 401 before a row is read.
The registry is the whole allowlist (KTD12); there is no per-repo grant to
consult, because the reviewers are exactly the people who already have access
to every repo the sweep touches.

**2. The client re-derives nothing (KTD19).** Readiness, staleness, parking,
and own-vs-counterpart are all decided here and shipped as data. Each of them
is a rule with an edge case — ``checks_rollup='none'`` is not pending, a
``failed`` action is not a verdict, a node-id mismatch outranks a matching
login — and a rule that lives in two codebases drifts in one of them. The
payload is conclusions.

**3. Caller-relative projection, not per-caller storage.** ``pr_party_pr`` holds
one row per PR and knows nothing about who is looking. ``own`` vs
``counterpart``, whose action state is "mine", whether the card is parked — all
of it is computed from the caller's registry row at read time. That is what
makes R24 (two reviewers, fully independent state) true by construction rather
than by two sets of rows kept in sync.

**4. The verdict endpoint reuses all of it (U6).** ``POST /cards/{id}/actions``
re-checks readiness, drift, own-vs-counterpart, and lifecycle *from the same
pure functions the queue projects with* before anything reaches GitHub. The card
a client rendered is an input, never an authority — and because the rules are
functions rather than duplicated conditionals, "what the queue showed" and "what
the server will allow" cannot disagree. The actuation itself — idempotency,
credentials, the GitHub call — lives in
:mod:`ontokit.services.pr_party_actions`, which deliberately imports nothing
from this layer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# Imported rather than restated: "who may use PR Party" must have one answer
# across the settings, read, and verdict surfaces. ``_require_reviewer`` is
# private to its module only in the sense that nothing outside PR Party should
# call it.
from ontokit.api.routes.pr_party_settings import (
    CredentialService,
    _require_reviewer,
    get_actions_redis,
)
from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.models.pr_party import (
    PRPartyAction,
    PRPartyActionKind,
    PRPartyActionStatus,
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.pr_party import (
    PR_PARTY_REVIEW_VERDICTS,
    PR_PARTY_UNSETTLED_STATUSES,
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PRPartyActionReceipt,
    PRPartyActionRequest,
    PRPartyActionResponse,
    PRPartyActionState,
    PRPartyCardDetail,
    PRPartyCommentResponse,
    PRPartyNoteRequest,
    PRPartyOtherReviewerState,
    PRPartyQAEntry,
    PRPartyQuestionRequest,
    PRPartyQueueCard,
    PRPartyQueueResponse,
    PRPartyReadiness,
)
from ontokit.services.pr_party_actions import (
    ActionRefused,
    ActionResult,
    PRPartyActionService,
    PRPartyActionStore,
)
from ontokit.services.pr_party_credentials import PRPartyCredentialService
from ontokit.services.pr_party_github import ChecksRollup
from ontokit.services.pr_party_intake import (
    PR_STATE_DRAFT,
    PR_STATE_OPEN,
    missing_long_enough_to_retire,
)
from ontokit.services.pr_party_qa import PRPartyQAService, QARefused, QAResult
from ontokit.services.pr_party_rate_limiter import (
    ActionBudgetReservation,
    ActionLimiterRedis,
    LimiterOutcome,
    refund_action_budget,
    reserve_action_budget,
)

__all__ = [
    "ActionService",
    "PRPartyQueueReader",
    "QAService",
    "QueueReader",
    "RequiredReviewer",
    "get_action_service",
    "get_actions_redis",
    "get_qa_service",
    "get_queue_reader",
    "router",
]

logger = logging.getLogger(__name__)

router = APIRouter()

GITHUB_WEB_BASE = "https://github.com"

#: Brief states that mean the AI pass is *finished*, however it ended. ``failed``
#: counts: a brief that will never arrive must not hold a PR hostage — the
#: reviewer still has the diff, and the links on the card still work (R17).
_BRIEF_SETTLED: frozenset[PRPartyBriefStatus] = frozenset(
    {
        PRPartyBriefStatus.READY,
        PRPartyBriefStatus.READY_WITH_WARNING,
        PRPartyBriefStatus.FAILED,
    }
)

_REASON_BRIEF_BREWING = "AI review still running"
_REASON_CHECKS_PENDING = "checks still running"

_TRUNCATED_NOTE = "This brief was shortened to fit. Open the diff for the parts it left out."

#: Order the caller's actions render in: the verdict first, then the merge
#: claim, then questions. Stable so the UI never has to sort.
_ACTION_KIND_ORDER: tuple[PRPartyActionKind, ...] = (
    PRPartyActionKind.REVIEW,
    PRPartyActionKind.MERGE,
    PRPartyActionKind.QUESTION,
)


# ---------------------------------------------------------------------------
# Dependencies (shared with U6)
# ---------------------------------------------------------------------------


async def get_current_reviewer(user: RequiredUser, service: CredentialService) -> PRPartyReviewer:
    """The caller's registry row, or 403.

    Delegates to the settings module's check rather than restating it, so
    "who may use PR Party" has exactly one answer across every route. The
    dependency is deliberately a *row*, not a boolean: the caller's GitHub
    identity is what own-vs-counterpart is computed against.
    """
    return await _require_reviewer(user, service)


RequiredReviewer = Annotated[PRPartyReviewer, Depends(get_current_reviewer)]


class PRPartyQueueReader:
    """The read side's data access — SQL only, no projection.

    Kept separate from the route functions so the interesting part (projection
    and the readiness rules) is pure and testable without a database, and so
    U6 can reuse the same narrowing when it loads a PR to actuate against.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_open_prs(self) -> list[PRPartyPR]:
        """Every PR in the ``open`` lifecycle state.

        Drafts, closed, and merged rows are excluded here; the retirement
        window (C7) is applied by the caller, because it depends on the sweep
        cadence rather than on anything the database knows.
        """
        result = await self._db.execute(
            select(PRPartyPR)
            .where(PRPartyPR.state == PR_STATE_OPEN)
            .order_by(PRPartyPR.updated_at_github.desc().nullslast(), PRPartyPR.pr_number.desc())
        )
        return list(result.scalars().all())

    async def get_pr(self, card_id: uuid.UUID) -> PRPartyPR | None:
        result = await self._db.execute(select(PRPartyPR).where(PRPartyPR.id == card_id))
        return result.scalars().first()

    async def list_actions(self, pr_ids: Sequence[uuid.UUID]) -> list[PRPartyAction]:
        """Every reviewer's actions on these PRs.

        Both reviewers' rows come back because the card needs the caller's own
        state *and* a compact summary of the counterpart's. Splitting the
        caller's row out is the projection's job — see :func:`_build_card`,
        which is also where the other reviewer's prose gets dropped.
        """
        if not pr_ids:
            return []
        result = await self._db.execute(
            select(PRPartyAction)
            .where(PRPartyAction.pr_id.in_(list(pr_ids)))
            .order_by(PRPartyAction.created_at.desc())
        )
        return list(result.scalars().all())


def get_queue_reader(db: Annotated[AsyncSession, Depends(get_db)]) -> PRPartyQueueReader:
    """Dependency for the PR Party read side."""
    return PRPartyQueueReader(db)


QueueReader = Annotated[PRPartyQueueReader, Depends(get_queue_reader)]


def get_action_service(db: Annotated[AsyncSession, Depends(get_db)]) -> PRPartyActionService:
    """Dependency for the PR Party actuation service."""
    return PRPartyActionService(
        store=PRPartyActionStore(db),
        credentials=PRPartyCredentialService(db),
    )


ActionService = Annotated[PRPartyActionService, Depends(get_action_service)]


def get_qa_service(db: Annotated[AsyncSession, Depends(get_db)]) -> PRPartyQAService:
    """Dependency for the PR Party Q&A service (U7).

    Takes a session only to hand the credential service one: the Q&A service
    itself writes no rows, because GitHub is the system of record for the
    thread (KD5).
    """
    return PRPartyQAService(credentials=PRPartyCredentialService(db))


QAService = Annotated[PRPartyQAService, Depends(get_qa_service)]


#: Re-exported from the settings module so both PR Party write surfaces share
#: one dependency (and one test seam) without an import cycle.
ActionsRedis = Annotated["ActionLimiterRedis | None", Depends(get_actions_redis)]


# ---------------------------------------------------------------------------
# Pure projection
# ---------------------------------------------------------------------------


def is_visible(pr: PRPartyPR, *, now: datetime | None = None) -> bool:
    """Whether a row is a live card for anyone.

    Open only — a draft has not been offered for review, and closed/merged are
    over — and not absent from GitHub long enough to be retired (C7). Applied
    on top of the reader's SQL narrowing rather than instead of it, so a card
    fetched by id passes exactly the same gate the queue applies.
    """
    if pr.state != PR_STATE_OPEN:
        return False
    return not missing_long_enough_to_retire(pr, now=now)


def project_author_kind(pr: PRPartyPR, reviewer: PRPartyReviewer) -> PRPartyAuthorKind:
    """R18/R19: ``counterpart`` becomes ``own`` when the author *is* the caller.

    ``third_party`` and ``bot`` pass through untouched — they are facts about
    the PR, not about who is looking.

    Matching follows U4's precedent with one sharpening: node ids are
    authoritative when *both* sides have one, so a mismatch there ends the
    question rather than falling through to logins. Intake can afford the login
    fallback after a node-id miss because it is only asking "is this anyone in
    the registry?"; here the question is "is this *you*?", and a login that was
    renamed or taken over is precisely the case where the answer must be no.
    """
    stored = PRPartyAuthorKind(pr.author_kind)
    if stored is not PRPartyAuthorKind.COUNTERPART:
        return stored
    return PRPartyAuthorKind.OWN if _is_same_principal(pr, reviewer) else stored


def _is_same_principal(pr: PRPartyPR, reviewer: PRPartyReviewer) -> bool:
    if pr.author_node_id and reviewer.github_node_id:
        return pr.author_node_id == reviewer.github_node_id
    if not pr.author_github_login or not reviewer.github_login:
        return False
    return pr.author_github_login.casefold() == reviewer.github_login.casefold()


def compute_readiness(pr: PRPartyPR) -> PRPartyReadiness:
    """R17: is there anything left to wait for before deciding?

    Two blockers, both reported when both apply — telling a reviewer only about
    CI when the brief is also missing just makes them come back twice.

    ``checks_rollup='none'`` is *not* a blocker: a repo with no configured
    checks has nothing to wait for. U3 made that a distinct value from
    ``success`` so this line could tell them apart without guessing.
    """
    reasons: list[str] = []
    if PRPartyBriefStatus(pr.brief_status) not in _BRIEF_SETTLED:
        reasons.append(_REASON_BRIEF_BREWING)
    if pr.checks_rollup == ChecksRollup.PENDING:
        reasons.append(_REASON_CHECKS_PENDING)

    if not reasons:
        return PRPartyReadiness(ready=True, reason=None)
    return PRPartyReadiness(ready=False, reason="; ".join(reasons))


def pr_web_url(pr: PRPartyPR) -> str:
    """The PR on GitHub, built from facts.

    ``repo_full_name`` comes from GitHub, but it is percent-encoded anyway: a
    deep link is the one thing on a card a reviewer will certainly click, and
    "the input was trustworthy" is a worse guarantee than "the output cannot
    leave the path".
    """
    repo = quote(pr.repo_full_name.strip("/"), safe="/")
    return f"{GITHUB_WEB_BASE}/{repo}/pull/{pr.pr_number}"


def _action_state(action: PRPartyAction) -> PRPartyActionState:
    return PRPartyActionState(
        kind=PRPartyActionKind(action.action_kind),
        verdict=action.verdict,
        status=PRPartyActionStatus(action.status),
        head_sha=action.head_sha,
        override=action.override,
        created_at=action.created_at,
    )


def _is_live(action: PRPartyAction) -> bool:
    """C6: a ``failed`` attempt is a dead row, not a stance.

    It sits outside the live-fingerprint index precisely so a retry can replace
    it, and it must not make a card look stale or parked either.
    """
    return PRPartyActionStatus(action.status) is not PRPartyActionStatus.FAILED


def _sort_key(action: PRPartyAction) -> datetime:
    return action.created_at or datetime.min.replace(tzinfo=UTC)


def _latest_per_kind(actions: Sequence[PRPartyAction]) -> list[PRPartyAction]:
    latest: dict[str, PRPartyAction] = {}
    for action in sorted(actions, key=_sort_key):
        latest[action.action_kind] = action
    return [latest[kind] for kind in _ACTION_KIND_ORDER if kind in latest]


def _latest_live_review(actions: Sequence[PRPartyAction]) -> PRPartyAction | None:
    reviews = [
        a
        for a in actions
        if PRPartyActionKind(a.action_kind) is PRPartyActionKind.REVIEW and _is_live(a)
    ]
    return max(reviews, key=_sort_key) if reviews else None


def _verdict_is(action: PRPartyAction, verdict: str) -> bool:
    """Compare verdicts case-insensitively.

    U1 typed ``verdict`` as a free string and U6 writes it; matching on the
    casefolded value means a writer that stores GitHub's ``"APPROVE"`` spelling
    reads back the same as the canonical ``"approve"``.
    """
    return (action.verdict or "").casefold() == verdict


def _other_reviewer_state(
    actions: Sequence[PRPartyAction], *, head_sha: str
) -> PRPartyOtherReviewerState:
    """Two booleans about the counterpart, scoped to the current revision.

    Everything else about their rows — bodies, verdicts, error text, timing —
    is dropped on the floor here, which is the whole point: R24 gives them
    independent state, and this is the narrowest window into it that still
    answers "has anyone else already handled this?".
    """
    current = [a for a in actions if a.head_sha == head_sha and _is_live(a)]
    return PRPartyOtherReviewerState(
        has_approved=any(
            PRPartyActionKind(a.action_kind) is PRPartyActionKind.REVIEW
            and _verdict_is(a, PR_PARTY_VERDICT_APPROVE)
            and PRPartyActionStatus(a.status)
            in (PRPartyActionStatus.SUCCEEDED, PRPartyActionStatus.DEGRADED_CONFIRMED)
            for a in current
        ),
        has_pending_intent=any(
            PRPartyActionStatus(a.status) in PR_PARTY_UNSETTLED_STATUSES for a in current
        ),
    )


def _build_card(
    pr: PRPartyPR,
    *,
    reviewer: PRPartyReviewer,
    actions: Sequence[PRPartyAction],
) -> PRPartyQueueCard:
    mine = [a for a in actions if a.reviewer_id == reviewer.id]
    theirs = [a for a in actions if a.reviewer_id != reviewer.id]

    author_kind = project_author_kind(pr, reviewer)
    latest_review = _latest_live_review(mine)
    url = pr_web_url(pr)

    return PRPartyQueueCard(
        card_id=pr.id,
        repo_full_name=pr.repo_full_name,
        pr_number=pr.pr_number,
        title=pr.title,
        author_kind=author_kind,
        author_github_login=pr.author_github_login,
        read_only=author_kind is PRPartyAuthorKind.OWN,
        state=pr.state,
        head_sha=pr.head_sha,
        mergeable_state=pr.mergeable_state,
        checks_rollup=pr.checks_rollup,
        brief_status=PRPartyBriefStatus(pr.brief_status),
        brief_truncated=pr.brief_truncated,
        ready_at=pr.ready_at,
        updated_at_github=pr.updated_at_github,
        pr_url=url,
        diff_url=f"{url}/files",
        readiness=compute_readiness(pr),
        actions=[_action_state(a) for a in _latest_per_kind(mine)],
        other_reviewer=_other_reviewer_state(theirs, head_sha=pr.head_sha),
        stale=latest_review is not None and latest_review.head_sha != pr.head_sha,
        parked=(
            latest_review is not None
            and latest_review.head_sha == pr.head_sha
            and _verdict_is(latest_review, PR_PARTY_VERDICT_DISCUSS_LIVE)
        ),
    )


def _build_detail(
    pr: PRPartyPR,
    *,
    reviewer: PRPartyReviewer,
    actions: Sequence[PRPartyAction],
    qa_thread: Sequence[PRPartyQAEntry] = (),
) -> PRPartyCardDetail:
    """The card, opened. ``qa_thread`` is passed in rather than fetched here.

    Projection stays pure and synchronous: the thread is a GitHub read (U7), and
    burying it in this function would put a network call behind every response
    that re-projects a card — including the verdict endpoint's fresh card, which
    has no business paying for one.
    """
    card = _build_card(pr, reviewer=reviewer, actions=actions)
    return PRPartyCardDetail(
        **card.model_dump(),
        brief_what=pr.brief_what or "",
        brief_why=pr.brief_why or "",
        brief_decisions=list(pr.brief_decisions or []),
        brief_links=list(pr.brief_links or []),
        truncated_note=_TRUNCATED_NOTE if pr.brief_truncated else None,
        brewing_since=pr.brewing_since,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        qa_thread=list(qa_thread),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _no_store(response: Response) -> None:
    """Never cache a card.

    A card is the input to an irreversible action, and its readiness and head
    SHA go stale on someone else's push. A cached queue would let a reviewer
    approve a revision that no longer exists.
    """
    response.headers["Cache-Control"] = "no-store"


@router.get("/queue", response_model=PRPartyQueueResponse)
async def get_queue(
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
) -> PRPartyQueueResponse:
    """The calling reviewer's whole queue, projected for them.

    Reviewers only: a non-reviewer gets a 403 with no PR data in the body, and
    an anonymous caller a 401 — the registry is the allowlist (KTD12), and
    every card here summarizes a private repository.
    """
    _no_store(response)

    now = datetime.now(UTC)
    prs = [pr for pr in await reader.list_open_prs() if is_visible(pr, now=now)]
    actions = await reader.list_actions([pr.id for pr in prs])

    by_pr: dict[uuid.UUID, list[PRPartyAction]] = {pr.id: [] for pr in prs}
    for action in actions:
        by_pr.setdefault(action.pr_id, []).append(action)

    return PRPartyQueueResponse(
        generated_at=now,
        cards=[_build_card(pr, reviewer=reviewer, actions=by_pr.get(pr.id, ())) for pr in prs],
    )


@router.get("/cards/{card_id}", response_model=PRPartyCardDetail)
async def get_card(
    card_id: uuid.UUID,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    qa: QAService,
) -> PRPartyCardDetail:
    """One card, opened — the queue payload plus the brief itself.

    Addressed by the PR row's UUID rather than ``{owner}/{repo}/{number}``. The
    natural key would have to survive URL-escaping a repo name on every hop,
    and the queue already hands the client a ``card_id``, so the path that
    cannot be mis-escaped is the one worth having.

    A row that is not a live card — retired, closed, merged, or a draft — is a
    404 rather than a stale read: the same visibility gate the queue applies.

    ``qa_thread`` is read live from GitHub on every open (U7). There is no Q&A
    table: the conversation belongs to the pull request, and a copy of it here
    could only be a staler second opinion (KD5/R13). The read is best-effort —
    a card without its Q&A panel is worth rendering; a 500 is not.
    """
    _no_store(response)

    pr = await reader.get_pr(card_id)
    if pr is None or not is_visible(pr):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such PR Party card.",
        )

    actions = await reader.list_actions([pr.id])
    return _build_detail(
        pr, reviewer=reviewer, actions=actions, qa_thread=await qa.load_thread(pr=pr)
    )


# ---------------------------------------------------------------------------
# Verdict endpoint (U6)
# ---------------------------------------------------------------------------


def _refusal(message: str, **extra: Any) -> dict[str, Any]:
    """Refusal bodies are objects, always.

    ``detail`` as a bare string would mean the drift and retire cases — the two
    that carry structure a client must act on — have a different body shape from
    every other refusal. One shape, optional keys.
    """
    return {"message": message, **extra}


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Read the violated name through psycopg or asyncpg's adapter layers."""
    current: Any = exc.orig
    for _ in range(3):
        direct = getattr(current, "constraint_name", None)
        if isinstance(direct, str):
            return direct
        diag = getattr(current, "diag", None)
        diagnosed = getattr(diag, "constraint_name", None)
        if isinstance(diagnosed, str):
            return diagnosed
        current = getattr(current, "orig", None)
        if current is None:
            break
    return None


async def _load_card(reader: PRPartyQueueReader, card_id: uuid.UUID) -> PRPartyPR:
    """The row behind a card id, or 404. Never trusts a client-supplied PR."""
    pr = await reader.get_pr(card_id)
    if pr is None or missing_long_enough_to_retire(pr):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_refusal("No such PR Party card."),
        )
    return pr


async def _consume_action_budget(
    redis: ActionLimiterRedis | None,
    reviewer_id: str,
    *,
    reservation_id: str | None = None,
) -> ActionBudgetReservation:
    """Runaway-loop protection, failing closed (R10).

    Actuation is the one PR Party surface that changes something irreversible
    outside our database, so an unmetered write path is worse than a brief
    outage. Reads are untouched: the dashboard keeps rendering through a Redis
    failure, and only the buttons stop working.
    """
    reservation = await reserve_action_budget(
        redis,
        reviewer_id,
        reservation_id=reservation_id,
    )
    if reservation.outcome is LimiterOutcome.UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_refusal(
                "PR Party cannot safely record actions right now. Nothing was sent "
                "to GitHub — try again in a moment."
            ),
        )
    if reservation.outcome is LimiterOutcome.OVER_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_refusal("You have hit today's PR Party action limit."),
        )
    return reservation


def _check_lifecycle(pr: PRPartyPR) -> None:
    """C7/R25: a PR that is over cannot be acted on, and its card should go.

    ``retire`` is a signal rather than a 404 because the client is holding a
    card it rendered from a valid read: telling it "gone" leaves a ghost in the
    queue until the next poll, while telling it "retire this" lets it drop the
    card immediately and explain why.
    """
    if pr.state == PR_STATE_OPEN:
        return
    if pr.state == "merged":
        message = (
            "This pull request was already merged on GitHub, so there is nothing left to decide."
        )
    elif pr.state == PR_STATE_DRAFT:
        message = (
            "This pull request was converted back to draft on GitHub, so it is not ready "
            "for a decision."
        )
    else:
        message = (
            "This pull request was already closed on GitHub, so there is nothing left to decide."
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_refusal(message, retire=True),
    )


def _check_head(pr: PRPartyPR, request: PRPartyActionRequest, card: PRPartyCardDetail) -> None:
    """C1: a verdict belongs to the revision the reviewer actually read."""
    if request.head_sha == pr.head_sha:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_refusal(
            "New commits landed while you were reading. Re-read the current "
            "revision before deciding.",
            card=card.model_dump(mode="json"),
        ),
    )


def _check_actor(pr: PRPartyPR, reviewer: PRPartyReviewer, request: PRPartyActionRequest) -> None:
    """R18: you cannot review your own pull request.

    Refused here rather than left to GitHub's 422 so the reviewer gets a
    sentence instead of an API error — and so no row is ever written for an
    action that could not have succeeded. Merge is deliberately exempt: the
    author merging their own PR *after* the counterpart approved is the normal
    ending (R11), and that precondition is checked in the service.
    """
    if request.action_kind is not PRPartyActionKind.REVIEW:
        return
    if project_author_kind(pr, reviewer) is not PRPartyAuthorKind.OWN:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_refusal(
            "This is your own pull request — GitHub does not accept a review from "
            "its author. Your counterpart decides this one."
        ),
    )


def _check_readiness(pr: PRPartyPR, request: PRPartyActionRequest) -> None:
    """R17/R26: verdicts wait for the brief and CI unless explicitly overridden.

    Only *GitHub* verdicts are gated. ``discuss_live`` — and, when U7 lands,
    questions — stay available on every card: a reviewer who wants to talk about
    a PR should never be told to wait for a test run first.

    The check is re-run here from the row rather than trusted from the card the
    client rendered, because a brief can finish (or CI can start failing) between
    the read and the tap. ``override`` is recorded on the action row, so a
    decision made early is visible as such forever (C12).
    """
    if request.action_kind is not PRPartyActionKind.REVIEW:
        return
    if request.verdict not in PR_PARTY_REVIEW_VERDICTS:
        return
    if request.override:
        return
    readiness = compute_readiness(pr)
    if readiness.ready:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=_refusal(
            f"This PR is not ready to decide on yet: {readiness.reason}. "
            "Send it again with override to decide anyway.",
            reason=readiness.reason,
        ),
    )


def _receipt(result: ActionResult) -> PRPartyActionReceipt:
    action = result.action
    return PRPartyActionReceipt(
        action_id=action.id,
        kind=PRPartyActionKind(action.action_kind),
        verdict=action.verdict,
        status=PRPartyActionStatus(action.status),
        head_sha=action.head_sha,
        override=action.override,
        idempotency_key=action.idempotency_key,
        github_review_id=action.github_review_id,
        merged=result.merged,
        created_at=action.created_at,
    )


@router.post("/cards/{card_id}/actions", response_model=PRPartyActionResponse)
async def create_action(
    card_id: uuid.UUID,
    request: PRPartyActionRequest,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    service: ActionService,
    redis: ActionsRedis,
) -> PRPartyActionResponse:
    """Cast one verdict, or claim one merge (KTD16, R7/R8/R11/R25).

    The request names a **card**, not a pull request. Repo, number, and the row's
    own head SHA all come from the server; the only PR fact the client supplies
    is the ``head_sha`` it *believes* it read, and that exists to be compared and
    refused on mismatch. There is no reviewer field either — the actor is the
    authenticated caller, always (R23).

    Gates run cheapest-first and all of them before any row is written:
    rate limit, card exists, PR still open, head still matches, not your own PR,
    ready (or explicitly overridden). Only then does
    :class:`~ontokit.services.pr_party_actions.PRPartyActionService` claim the
    idempotency fingerprint, commit a ``pending`` row, and call GitHub.

    Three outcomes are all 200:

    - **Actuated** — ``action.status='succeeded'`` with the review id or
      ``merged: true``.
    - **Replayed** — the same idempotency key arriving twice returns the stored
      receipt with ``replayed: true`` and makes no second GitHub call.
    - **Degraded** — no usable PAT, or one that died mid-call: the verdict is
      recorded as intent and ``deep_link`` points at where to finish it by hand
      (R12). The reviewer is not blocked; the reconciler confirms it later.

    Everything else is a refusal with a plain-language ``message``, and nothing
    reached GitHub.
    """
    _no_store(response)

    reservation = await _consume_action_budget(
        redis,
        reviewer.zitadel_user_id,
        reservation_id=(
            f"{card_id}:{request.head_sha}:{request.action_kind.value}:{request.idempotency_key}"
        ),
    )

    pr = await _load_card(reader, card_id)
    _check_lifecycle(pr)
    if request.head_sha != pr.head_sha:
        _check_head(pr, request, await _fresh_card(reader, pr, reviewer))
    _check_actor(pr, reviewer, request)
    _check_readiness(pr, request)

    try:
        result = await service.actuate(
            reviewer=reviewer, pr=pr, request=request, pr_url=pr_web_url(pr)
        )
    except ActionRefused as e:
        extra: dict[str, Any] = {}
        if e.needs_card:
            card = await _fresh_card(reader, pr, reviewer)
            extra["card"] = card.model_dump(mode="json")
        if e.retire:
            extra["retire"] = True
        raise HTTPException(status_code=e.status_code, detail=_refusal(e.message, **extra)) from e
    except IntegrityError as e:
        await service.rollback()
        if _integrity_constraint_name(e) != "uq_pr_party_action_live_fingerprint":
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_refusal(
                "An attempt for this action is already in flight. Give it a moment "
                "and refresh before trying again."
            ),
        ) from e

    if result.replayed:
        await refund_action_budget(redis, reservation)

    return PRPartyActionResponse(
        action=_receipt(result),
        card=await _fresh_card(reader, pr, reviewer),
        degraded=result.degraded,
        deep_link=result.deep_link,
        replayed=result.replayed,
    )


@router.post("/cards/{card_id}/unpark", response_model=PRPartyCardDetail)
async def unpark_card(
    card_id: uuid.UUID,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    service: ActionService,
    redis: ActionsRedis,
) -> PRPartyCardDetail:
    """Un-park a card the caller parked for live discussion (R9).

    A park is one reviewer's ``discuss_live`` action row at the current head —
    there is no park column — so un-parking is deleting exactly that row. Only
    ``discuss_live`` is ever removed: a real verdict has been delivered to GitHub
    and cannot be un-cast from here.

    Idempotent and 200 either way. "This card is not parked" is the state the
    caller asked for, and a 404 would make a double-tap look like a failure.
    """
    _no_store(response)

    await _consume_action_budget(redis, reviewer.zitadel_user_id)

    pr = await _load_card(reader, card_id)
    await service.unpark(reviewer=reviewer, pr=pr)
    return await _fresh_card(reader, pr, reviewer)


# ---------------------------------------------------------------------------
# Q&A and re-trigger (U7)
# ---------------------------------------------------------------------------


async def _post_comment(
    *,
    result_factory: Callable[[PRPartyPR], Awaitable[QAResult]],
    reader: PRPartyQueueReader,
    reviewer: PRPartyReviewer,
    card_id: uuid.UUID,
    redis: ActionLimiterRedis | None,
) -> PRPartyCommentResponse:
    """The shape all three comment surfaces share.

    Same gates as a verdict, minus the two that would be wrong here. There is no
    readiness gate — a reviewer most wants to ask while the brief is still
    brewing (R17 governs verdicts only) — and no own-PR gate: asking a question
    about your own pull request, or recording what a call concluded, is
    something an author does all the time. The lifecycle gate stays: a closed PR
    is over, and a comment on it helps nobody.
    """
    await _consume_action_budget(redis, reviewer.zitadel_user_id)

    pr = await _load_card(reader, card_id)
    _check_lifecycle(pr)

    try:
        result: QAResult = await result_factory(pr)
    except QARefused as e:
        raise HTTPException(status_code=e.status_code, detail=_refusal(e.message)) from e

    return PRPartyCommentResponse(
        posted=result.posted,
        degraded=result.degraded,
        body=result.body,
        comment_id=result.comment_id,
        comment_url=result.comment_url,
        deep_link=result.deep_link,
        card=await _fresh_card(reader, pr, reviewer),
    )


@router.post("/cards/{card_id}/questions", response_model=PRPartyCommentResponse)
async def ask_question(
    card_id: uuid.UUID,
    request: PRPartyQuestionRequest,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    qa: QAService,
    redis: ActionsRedis,
) -> PRPartyCommentResponse:
    """Put a question to the AI reviewer on this card (R13, KTD18).

    The comment is authored by the **asking reviewer's own PAT**, which is
    load-bearing rather than incidental: GitHub does not run Actions workflows
    for events triggered by ``GITHUB_TOKEN``, so a question posted by the app
    would summon nobody. It carries the ``@claude`` mention the org answerer
    triggers on and an attribution line naming the human who asked.

    Nothing is stored. The question and its answer live on the pull request,
    where the reviewer can already see them and where the answerer reads them
    (KD5) — ``GET /cards/{id}`` projects the thread back out of GitHub's
    comments on every open.

    Degraded (R12) is a 200, not a failure: with no usable PAT the response
    carries ``posted=false``, the exact comment text in ``body``, and a
    ``deep_link`` to paste it. Nothing reached GitHub.
    """
    _no_store(response)

    return await _post_comment(
        result_factory=lambda pr: qa.ask(
            reviewer=reviewer, pr=pr, question=request.question, pr_url=pr_web_url(pr)
        ),
        reader=reader,
        reviewer=reviewer,
        card_id=card_id,
        redis=redis,
    )


@router.post("/cards/{card_id}/notes", response_model=PRPartyCommentResponse)
async def record_note(
    card_id: uuid.UUID,
    request: PRPartyNoteRequest,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    qa: QAService,
    redis: ActionsRedis,
) -> PRPartyCommentResponse:
    """Post the outcome of a live discussion back to the PR (R14).

    What a call concluded otherwise evaporates with the call. This puts it where
    the next reader — human or model — will find it, as a structured comment
    that deliberately does *not* mention ``@claude``: a decision already made
    does not need an answer.
    """
    _no_store(response)

    return await _post_comment(
        result_factory=lambda pr: qa.record_note(
            reviewer=reviewer, pr=pr, note=request.note, pr_url=pr_web_url(pr)
        ),
        reader=reader,
        reviewer=reviewer,
        card_id=card_id,
        redis=redis,
    )


@router.post("/cards/{card_id}/rerun-review", response_model=PRPartyCommentResponse)
async def rerun_review(
    card_id: uuid.UUID,
    response: Response,
    reviewer: RequiredReviewer,
    reader: QueueReader,
    qa: QAService,
    redis: ActionsRedis,
) -> PRPartyCommentResponse:
    """Ask the AI reviewer to review again (R3).

    The control a card carries when its brief timed out brewing, or when the
    branch moved on after the review landed. Posts ``@coderabbitai review`` as
    the reviewer — same reason a question is posted as them — and degrades to
    compose-for-copy exactly like every other comment surface.

    No body: there is nothing about a re-trigger for a client to supply.
    """
    _no_store(response)

    return await _post_comment(
        result_factory=lambda pr: qa.rerun_review(reviewer=reviewer, pr=pr, pr_url=pr_web_url(pr)),
        reader=reader,
        reviewer=reviewer,
        card_id=card_id,
        redis=redis,
    )


async def _fresh_card(
    reader: PRPartyQueueReader, pr: PRPartyPR, reviewer: PRPartyReviewer
) -> PRPartyCardDetail:
    """Re-project the card from the rows as they now stand.

    Shipped with every verdict response so the client never renders action state
    it composed itself — the same reason the read side ships conclusions rather
    than ingredients (KTD19).
    """
    actions = await reader.list_actions([pr.id])
    return _build_detail(pr, reviewer=reviewer, actions=actions)
