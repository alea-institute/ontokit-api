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

U6's verdict endpoint lands in this module and reuses :data:`RequiredReviewer`
and :data:`QueueReader` below — the DI helpers are module-level for that reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Imported rather than restated: "who may use PR Party" must have one answer
# across the settings, read, and (later) verdict surfaces. ``_require_reviewer``
# is private to its module only in the sense that nothing outside PR Party
# should call it.
from ontokit.api.routes.pr_party_settings import CredentialService, _require_reviewer
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
    PR_PARTY_UNSETTLED_STATUSES,
    PR_PARTY_VERDICT_APPROVE,
    PR_PARTY_VERDICT_DISCUSS_LIVE,
    PRPartyActionState,
    PRPartyCardDetail,
    PRPartyOtherReviewerState,
    PRPartyQueueCard,
    PRPartyQueueResponse,
    PRPartyReadiness,
)
from ontokit.services.pr_party_github import ChecksRollup
from ontokit.services.pr_party_intake import PR_STATE_OPEN, missing_long_enough_to_retire

__all__ = [
    "PRPartyQueueReader",
    "QueueReader",
    "RequiredReviewer",
    "get_queue_reader",
    "router",
]

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
        title=None,
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
        actions=[_action_state(a) for a in reversed(_latest_per_kind(mine))],
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
) -> PRPartyCardDetail:
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
        qa_thread=[],
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
) -> PRPartyCardDetail:
    """One card, opened — the queue payload plus the brief itself.

    Addressed by the PR row's UUID rather than ``{owner}/{repo}/{number}``. The
    natural key would have to survive URL-escaping a repo name on every hop,
    and the queue already hands the client a ``card_id``, so the path that
    cannot be mis-escaped is the one worth having.

    A row that is not a live card — retired, closed, merged, or a draft — is a
    404 rather than a stale read: the same visibility gate the queue applies.
    """
    _no_store(response)

    pr = await reader.get_pr(card_id)
    if pr is None or not is_visible(pr):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No such PR Party card.",
        )

    actions = await reader.list_actions([pr.id])
    return _build_detail(pr, reviewer=reviewer, actions=actions)
