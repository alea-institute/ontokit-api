"""PR Party intake — the one path by which a GitHub PR becomes a row.

Every open pull request in the CatholicOS org has to exist as a current
``pr_party_pr`` row, and there are two ways for that to happen: the
reconciliation sweep (an arq cron), and the org webhook. **Sweep-first
(KTD14):** the sweep is complete intake on its own — it is not a backstop for
the webhook, because creating an org-level hook needs an org owner and that is
an external gate. The webhook, when it exists, is a latency upgrade. Both
converge on :func:`upsert_pr`, keyed ``(repo_full_name, pr_number)``, so a fact
delivered twice in one cycle produces one row.

Four rules carry the weight here.

**1. A revision is a head SHA.** ``head_sha`` changing is the *only* thing that
restarts brewing, re-enqueues a brief, and (via U8) invalidates verdicts cast
against the old revision. Drawing that boundary in exactly one place is what
lets C1's stale-verdict detection be a comparison rather than a heuristic.

**2. Column ownership (KTD15).** This module writes *poller-owned* columns —
``pr_node_id``, ``title``, ``author_*``, ``state``, ``head_sha``,
``mergeable_state``, ``checks_rollup``, ``missing_since``,
``updated_at_github`` — and nothing else.
The ``brief_*`` columns belong to the brief worker (U5) and survive every
refresh untouched; that is the DB form of prototype finding B3 and
``test_refresh_preserves_brief_columns`` is its guard. There is exactly one
carve-out, documented rather than implicit: the brewing lifecycle
(``brief_status`` / ``brewing_since`` / ``ready_at``) is written here on three
occasions only — row creation, a head-SHA change, and the 90-minute brewing
timeout (R17). A *same-head* refresh never touches them. Brief *content* is
never written or cleared here; U5 replaces it when it regenerates.

**3. ``author_kind`` is a property of the row, so it cannot be
``own``.** R19's four kinds read naturally until you notice that own-vs-
counterpart is *relative to whoever is looking*: the same PR is "own" for its
author and "counterpart" for the other reviewer, and a row has no viewer. The
resolution, which U15 depends on: a PR authored by **any** registry member
stores ``author_kind='counterpart'`` — read it as "a principal authored this" —
together with ``author_github_login`` and ``author_node_id``. The read API
derives own-vs-counterpart per caller by comparing that identity to the caller's
own. ``PRPartyAuthorKind.OWN`` is therefore never persisted by this module; it
remains available as a *projection* value for the queue API.
(Note: ``models/pr_party.py``'s enum docstring describes the opposite
convention — ``own`` for a registered reviewer's PR. This module is the writer,
so this is the operative rule; the model docstring wants a one-line correction
that belongs to whoever owns that file.)

**4. Absence is not evidence (C7).** GitHub's search index is eventually
consistent, so a PR missing from one discovery pass means very little. A row
absent from a *complete* pass gets ``missing_since`` stamped; reappearing clears
it; only after :data:`MISSING_MISS_THRESHOLD` consecutive misses is it fair to
act — and acting (retirement) is U8's job, not this module's. A discovery pass
that errored partway stamps nothing at all.

Seams left for later units:

- **U5** registers the arq task named :data:`BRIEF_TASK_NAME` with the
  signature ``generate_pr_brief(ctx, pr_id: str, repo_full_name: str,
  pr_number: int, head_sha: str)``. This module enqueues it by name with
  ``_job_id=brief:{repo}#{number}:{head_sha}`` (see :func:`brief_job_id`); arq
  enqueues by string, so enqueueing before U5 registers the function is safe —
  the job simply waits.
- **U9** appends to :data:`ready_transition_hooks` to emit the once-per-revision
  ready notification (R22). Transitions are also returned in
  :class:`SweepResult` so a caller can observe them without installing a hook.
- **U7** appends to :data:`issue_comment_hooks` for the Q&A thread; this module
  deliberately stores nothing from ``issue_comment`` events.
- **U8** consumes :func:`missing_long_enough_to_retire` and the single-PR
  refresh (:func:`refresh_pull_request`) that ``pull_request_review`` events
  trigger — review state itself is never stored here.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.pr_party import (
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.services.pr_party_github import (
    ChecksRollup,
    Mergeability,
    PRDetail,
    PRPartyGitHubClient,
    SearchedPR,
    generation_client,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BREWING_TIMEOUT",
    "BRIEF_TASK_NAME",
    "MISSING_MISS_THRESHOLD",
    "PR_STATE_CLOSED",
    "PR_STATE_DRAFT",
    "PR_STATE_MERGED",
    "PR_STATE_OPEN",
    "TITLE_MAX_LENGTH",
    "IntakeResult",
    "PRFacts",
    "ReadyTransition",
    "ReviewerRegistry",
    "SweepResult",
    "apply_brewing_timeout",
    "brief_job_id",
    "classify_author",
    "facts_from_detail",
    "facts_from_webhook_pr",
    "handle_webhook_event",
    "issue_comment_hooks",
    "load_reviewer_registry",
    "missing_long_enough_to_retire",
    "ready_transition_hooks",
    "refresh_pull_request",
    "resolve_mergeable_state",
    "sweep_open_prs",
    "upsert_pr",
]

#: R17: the worst-case CodeRabbit queue depth for a six-PR batch. A card stuck
#: brewing past this is released as ``ready_with_warning`` rather than blocking
#: the reviewer indefinitely on a review that may never arrive.
BREWING_TIMEOUT: Final = timedelta(minutes=90)

#: C7: consecutive misses from a *complete* discovery pass before a row's
#: absence is trustworthy. Retirement itself is U8's.
MISSING_MISS_THRESHOLD: Final = 3

#: The arq task U5 registers. Enqueued by name, so the ordering between units
#: does not matter — arq resolves the function at execution time.
BRIEF_TASK_NAME: Final = "generate_pr_brief"

#: Search caps at 1000 results; this bounds discovery so a pathological org
#: cannot spin the sweep forever.
MAX_DISCOVERY_PAGES: Final = 10
DISCOVERY_PAGE_SIZE: Final = 100

#: Redis TTL for the webhook delivery-id dedupe key (KTD14). A day covers
#: GitHub's redelivery window comfortably.
DELIVERY_DEDUPE_TTL_SECONDS: Final = 24 * 60 * 60
DELIVERY_KEY_PREFIX: Final = "pr_party:delivery:"

#: Width of ``pr_party_pr.title``. GitHub caps PR titles at 256 characters, so
#: this is headroom rather than a limit anyone should hit — but the column is
#: the thing that would raise, and a sweep must never die on a long title.
TITLE_MAX_LENGTH: Final = 512

# --- Lifecycle states -------------------------------------------------------
# ``pr_party_pr.state`` is a plain string column (U1). These are its values.

PR_STATE_OPEN: Final = "open"
#: Parked: a draft is excluded from the active queue but keeps its row and its
#: history, so converting back to ready-for-review does not start from nothing.
PR_STATE_DRAFT: Final = "draft"
PR_STATE_CLOSED: Final = "closed"
PR_STATE_MERGED: Final = "merged"

#: States a row can plausibly still be seen in an open-PR search under.
ACTIVE_STATES: Final[frozenset[str]] = frozenset({PR_STATE_OPEN, PR_STATE_DRAFT})

#: ``pull_request`` actions that change a fact this module owns. Everything else
#: (``labeled``, ``assigned``, ``review_requested``, …) is noise for intake.
PR_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "opened",
        "synchronize",
        "closed",
        "reopened",
        "ready_for_review",
        "converted_to_draft",
        "edited",
    }
)


# --- Hook seams -------------------------------------------------------------

ReadyHook = Callable[["ReadyTransition"], Awaitable[None]]
IssueCommentHook = Callable[[dict[str, Any]], Awaitable[None]]

#: U9 (R22) appends here. Hooks are best-effort: a failing notifier must never
#: roll back a lifecycle transition that already happened in the database.
ready_transition_hooks: list[ReadyHook] = []

#: U7 appends here. This module stores nothing from ``issue_comment``.
issue_comment_hooks: list[IssueCommentHook] = []


class _EnqueuePool(Protocol):
    """The slice of ``ArqRedis`` intake uses (enqueue + SETNX dedupe)."""

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any: ...


# --- Value shapes -----------------------------------------------------------


@dataclass(frozen=True)
class ReviewerRegistry:
    """A snapshot of the reviewer registry for author classification (R19).

    Matching is node-id-first because a GitHub login can be renamed, but node
    ids are *best-effort* on both sides — U2 resolves them at startup and leaves
    them NULL when GitHub is unreachable, and search items occasionally omit the
    author object entirely. So a casefolded login match is a real fallback, not
    a nicety. The residual risk it accepts: a stranger who takes over an
    abandoned login that a registry row still names would classify as a
    principal. That is bounded by the operator owning ``PR_PARTY_REVIEWERS``,
    and node-id matching wins whenever both sides have one.
    """

    node_ids: frozenset[str]
    logins: frozenset[str]

    @classmethod
    def from_rows(cls, rows: list[PRPartyReviewer]) -> ReviewerRegistry:
        return cls(
            node_ids=frozenset(r.github_node_id for r in rows if r.github_node_id),
            logins=frozenset(r.github_login.casefold() for r in rows if r.github_login),
        )

    def is_member(self, *, login: str | None, node_id: str | None) -> bool:
        if node_id and node_id in self.node_ids:
            return True
        if not login:
            return False
        return login.casefold() in self.logins


@dataclass(frozen=True)
class PRFacts:
    """The poller-owned facts about one PR, normalized from either source.

    ``checks_known`` distinguishes "no checks ran" (``checks_rollup='none'``,
    which U3 made a distinct value precisely so readiness gating can tell it
    from success) from "this source does not carry check state" — a
    ``pull_request`` webhook payload has no check runs, and blanking the column
    on every edit event would make CI status flicker.
    """

    repo_full_name: str
    pr_number: int
    head_sha: str
    state: str
    draft: bool
    merged: bool
    author_login: str | None
    author_node_id: str | None
    author_type: str | None
    node_id: str | None
    mergeable_state: str | None
    updated_at_github: datetime | None
    checks_rollup: str | None = None
    checks_known: bool = False
    #: GitHub's PR title. ``None`` means "this payload did not carry one", which
    #: the upsert treats as "leave the stored title alone" — a webhook envelope
    #: that omits it must not blank a title the sweep already recorded.
    title: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.repo_full_name, self.pr_number)

    @property
    def resolved_state(self) -> str:
        """GitHub's ``state`` plus the two distinctions the queue needs."""
        if self.merged:
            return PR_STATE_MERGED
        if self.state == "closed":
            return PR_STATE_CLOSED
        return PR_STATE_DRAFT if self.draft else PR_STATE_OPEN


@dataclass(frozen=True)
class ReadyTransition:
    """A card became actionable. U9 turns this into a notification (R22)."""

    pr_id: uuid.UUID | None
    repo_full_name: str
    pr_number: int
    head_sha: str
    brief_status: str
    reason: str


@dataclass
class IntakeResult:
    """What one upsert did, so callers can count without re-querying."""

    pr: PRPartyPR | None = None
    created: bool = False
    new_revision: bool = False
    parked: bool = False
    enqueued: bool = False
    skipped_reason: str | None = None


@dataclass
class SweepResult:
    """``{total, synced, errors}`` plus the detail the sweep is judged on.

    Shaped after ``sync_github_projects`` so the cron log reads the same across
    the worker.
    """

    total: int = 0
    synced: int = 0
    errors: int = 0
    created: int = 0
    parked: int = 0
    skipped: int = 0
    detail_fetches: int = 0
    missing_stamped: int = 0
    missing_cleared: int = 0
    timed_out: int = 0
    discovery_complete: bool = True
    skipped_reason: str | None = None
    transitions: list[ReadyTransition] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "synced": self.synced,
            "errors": self.errors,
            "created": self.created,
            "parked": self.parked,
            "skipped": self.skipped,
            "detail_fetches": self.detail_fetches,
            "missing_stamped": self.missing_stamped,
            "missing_cleared": self.missing_cleared,
            "timed_out": self.timed_out,
            "discovery_complete": self.discovery_complete,
            "skipped_reason": self.skipped_reason,
        }


# --- Classification ---------------------------------------------------------


def classify_author(
    *,
    login: str | None,
    node_id: str | None,
    user_type: str | None,
    registry: ReviewerRegistry,
) -> PRPartyAuthorKind:
    """R19's row-level author kind. Never returns ``own`` — see module docstring.

    ``user_type == "Bot"`` is GitHub's own answer and is checked first; the
    ``[bot]`` login suffix is the fallback for payloads that omit the user type,
    which some webhook deliveries do.
    """
    if (user_type or "").casefold() == "bot":
        return PRPartyAuthorKind.BOT
    if login and login.casefold().endswith("[bot]"):
        return PRPartyAuthorKind.BOT
    if registry.is_member(login=login, node_id=node_id):
        return PRPartyAuthorKind.COUNTERPART
    return PRPartyAuthorKind.THIRD_PARTY


async def load_reviewer_registry(db: AsyncSession) -> ReviewerRegistry:
    """Snapshot the registry once per sweep rather than per PR."""
    result = await db.execute(select(PRPartyReviewer))
    return ReviewerRegistry.from_rows(list(result.scalars().all()))


def resolve_mergeable_state(detail: PRDetail) -> str | None:
    """R4: ``mergeable: null`` is *computing*, and outranks any stale raw state.

    GitHub can return a leftover ``mergeable_state`` alongside a null
    ``mergeable``; persisting that word would render a definite answer to a
    question GitHub has not finished asking. Otherwise the raw state is stored
    verbatim (``clean``, ``dirty``, ``behind``, ``blocked``, …) because it is
    strictly more informative than the tri-state, which is kept only as the
    fallback for a response that omitted it.
    """
    if detail.is_computing:
        return Mergeability.COMPUTING.value
    return detail.mergeable_state or detail.mergeability.value


# --- Fact construction ------------------------------------------------------


def facts_from_detail(detail: PRDetail, *, rollup: ChecksRollup | None = None) -> PRFacts:
    """Facts from ``GET /pulls/{n}`` — the authoritative source (sweep stage 2)."""
    return PRFacts(
        repo_full_name=detail.repo_full_name,
        pr_number=detail.number,
        head_sha=detail.head_sha,
        state=detail.state,
        draft=detail.draft,
        merged=detail.merged,
        author_login=detail.author_login,
        author_node_id=detail.author_node_id,
        author_type=detail.author_type,
        node_id=detail.node_id,
        mergeable_state=resolve_mergeable_state(detail),
        updated_at_github=detail.updated_at,
        checks_rollup=rollup.value if rollup is not None else None,
        checks_known=rollup is not None,
        title=detail.title or None,
    )


def facts_from_webhook_pr(payload: Mapping[str, Any]) -> PRFacts | None:
    """Facts from a webhook envelope carrying a ``pull_request`` object.

    Returns ``None`` when the envelope has no usable PR — a malformed delivery
    is ignored, never guessed at.
    """
    pr = payload.get("pull_request")
    if not isinstance(pr, Mapping):
        return None

    repo_full_name = _repo_full_name(payload, pr)
    number = _to_int(pr.get("number"))
    if not repo_full_name or number is None:
        return None

    head = _sub_mapping(pr, "head")
    user = _sub_mapping(pr, "user")

    mergeable = pr.get("mergeable")
    if mergeable is None:
        mergeable_state = Mergeability.COMPUTING.value
    else:
        raw = pr.get("mergeable_state")
        fallback = Mergeability.MERGEABLE if mergeable else Mergeability.NOT_MERGEABLE
        mergeable_state = str(raw) if raw else fallback.value

    return PRFacts(
        repo_full_name=repo_full_name,
        pr_number=number,
        head_sha=str(head.get("sha") or ""),
        state=str(pr.get("state") or "open"),
        draft=bool(pr.get("draft", False)),
        merged=bool(pr.get("merged", False)),
        author_login=_opt_str(user.get("login")),
        author_node_id=_opt_str(user.get("node_id")),
        author_type=_opt_str(user.get("type")),
        node_id=_opt_str(pr.get("node_id")),
        mergeable_state=mergeable_state,
        updated_at_github=_parse_dt(pr.get("updated_at")),
        # A ``pull_request`` payload carries no check runs; leave the column be.
        checks_known=False,
        title=_opt_str(pr.get("title")),
    )


# --- The single upsert path -------------------------------------------------


def brief_job_id(repo_full_name: str, pr_number: int, head_sha: str) -> str:
    """Revision-scoped arq job id: one brief per PR revision, ever (KTD17)."""
    return f"brief:{repo_full_name}#{pr_number}:{head_sha}"


async def upsert_pr(
    db: AsyncSession,
    facts: PRFacts | None,
    *,
    registry: ReviewerRegistry,
    now: datetime | None = None,
    pool: _EnqueuePool | None = None,
) -> IntakeResult:
    """Bring one PR's row in line with ``facts``. The sweep and every webhook
    handler end here (KTD14), so double delivery is a no-op.

    Writes poller-owned columns only; see the module docstring for the brewing
    carve-out and for why ``author_kind`` is never ``own``.
    """
    if facts is None:
        return IntakeResult(skipped_reason="unparseable")

    moment = now or datetime.now(UTC)
    row = await _get_pr(db, facts.repo_full_name, facts.pr_number)

    if row is None:
        if facts.draft:
            # R-draft: a draft has not been offered for review, so it does not
            # enter. It enters on ``ready_for_review``, or on the first sweep
            # after that flips it.
            return IntakeResult(skipped_reason="draft")
        row = PRPartyPR(
            id=uuid.uuid4(),
            repo_full_name=facts.repo_full_name,
            pr_number=facts.pr_number,
            head_sha=facts.head_sha,
            author_kind=PRPartyAuthorKind.THIRD_PARTY,
        )
        db.add(row)
        created = True
        new_revision = True
    else:
        created = False
        new_revision = bool(facts.head_sha) and facts.head_sha != row.head_sha

    was_parked = row.state == PR_STATE_DRAFT

    # --- Poller-owned columns ---
    if facts.node_id:
        row.pr_node_id = facts.node_id
    if facts.head_sha:
        row.head_sha = facts.head_sha
    if facts.title:
        row.title = facts.title[:TITLE_MAX_LENGTH]
    row.state = facts.resolved_state
    row.mergeable_state = facts.mergeable_state
    if facts.checks_known:
        row.checks_rollup = facts.checks_rollup
    if facts.updated_at_github is not None:
        row.updated_at_github = facts.updated_at_github
    # We are looking straight at it, so it is not missing.
    row.missing_since = None

    row.author_github_login = facts.author_login
    row.author_node_id = facts.author_node_id
    row.author_kind = classify_author(
        login=facts.author_login,
        node_id=facts.author_node_id,
        user_type=facts.author_type,
        registry=registry,
    )

    # --- Brewing lifecycle: creation and head-SHA change only (see docstring) ---
    if created or new_revision:
        row.brief_status = PRPartyBriefStatus.BREWING
        row.brewing_since = moment
        row.ready_at = None

    await db.commit()

    result = IntakeResult(
        pr=row,
        created=created,
        new_revision=new_revision,
        parked=row.state == PR_STATE_DRAFT and not was_parked,
    )

    if (created or new_revision) and pool is not None:
        result.enqueued = await _enqueue_brief(pool, row)

    return result


async def _enqueue_brief(pool: _EnqueuePool, row: PRPartyPR) -> bool:
    """Ask U5 for a brief on this revision.

    R19: bot PRs render as link-only rows, so they never cost an LLM call. A
    Redis failure is logged and swallowed — the row is already durable, and the
    next sweep that sees a new revision will try again; losing the row to a
    queueing blip would be far worse than losing a brief.
    """
    if row.author_kind == PRPartyAuthorKind.BOT:
        return False
    if not row.head_sha:
        return False
    try:
        await pool.enqueue_job(
            BRIEF_TASK_NAME,
            str(row.id),
            row.repo_full_name,
            row.pr_number,
            row.head_sha,
            _job_id=brief_job_id(row.repo_full_name, row.pr_number, row.head_sha),
        )
    except Exception as exc:  # noqa: BLE001 — never let queueing sink the upsert
        logger.warning(
            "Could not enqueue PR Party brief job for %s#%s: %s",
            row.repo_full_name,
            row.pr_number,
            exc,
        )
        return False
    return True


async def _get_pr(db: AsyncSession, repo_full_name: str, pr_number: int) -> PRPartyPR | None:
    result = await db.execute(
        select(PRPartyPR).where(
            PRPartyPR.repo_full_name == repo_full_name,
            PRPartyPR.pr_number == pr_number,
        )
    )
    return result.scalar_one_or_none()


# --- Single-PR refresh (detail + checks) ------------------------------------


async def refresh_pull_request(
    db: AsyncSession,
    client: PRPartyGitHubClient,
    repo_full_name: str,
    pr_number: int,
    *,
    registry: ReviewerRegistry,
    now: datetime | None = None,
    pool: _EnqueuePool | None = None,
) -> IntakeResult:
    """Sweep stage 2, also used by ``check_suite`` and ``pull_request_review``.

    Two calls: PR detail (head SHA + mergeability, which search cannot give) and
    the check-runs rollup for that head. A rollup failure degrades to "checks
    unknown" rather than failing the whole refresh — a PR with stale CI state is
    far more useful than no row at all.
    """
    owner, _, repo = repo_full_name.partition("/")
    detail = await client.get_pull_request(owner, repo, pr_number)

    rollup: ChecksRollup | None = None
    if detail.head_sha:
        try:
            rollup = await client.get_check_runs_rollup(owner, repo, detail.head_sha)
        except Exception as exc:  # noqa: BLE001 — checks are not worth the row
            logger.warning("Check-runs rollup failed for %s#%s: %s", repo_full_name, pr_number, exc)

    return await upsert_pr(
        db, facts_from_detail(detail, rollup=rollup), registry=registry, now=now, pool=pool
    )


# --- Brewing timeout (R17) --------------------------------------------------


async def apply_brewing_timeout(
    db: AsyncSession, *, now: datetime | None = None
) -> list[ReadyTransition]:
    """Release cards stuck brewing past :data:`BREWING_TIMEOUT`.

    Sweep-owned even though it writes brief lifecycle columns: the brief worker
    cannot time *itself* out, and a card that waits forever on a CodeRabbit
    review that never arrives is a queue that silently stops working.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - BREWING_TIMEOUT

    result = await db.execute(
        select(PRPartyPR).where(
            PRPartyPR.brief_status == PRPartyBriefStatus.BREWING.value,
            PRPartyPR.brewing_since < cutoff,
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    transitions: list[ReadyTransition] = []
    for row in rows:
        row.brief_status = PRPartyBriefStatus.READY_WITH_WARNING
        row.ready_at = moment
        transitions.append(
            ReadyTransition(
                pr_id=row.id,
                repo_full_name=row.repo_full_name,
                pr_number=row.pr_number,
                head_sha=row.head_sha,
                brief_status=PRPartyBriefStatus.READY_WITH_WARNING.value,
                reason="brewing_timeout",
            )
        )
    await db.commit()

    for transition in transitions:
        await _emit_ready(transition)

    logger.info("PR Party: %d card(s) timed out of brewing to ready-with-warning", len(rows))
    return transitions


async def _emit_ready(transition: ReadyTransition) -> None:
    """Fire U9's seam. A broken notifier must not undo a committed transition."""
    for hook in list(ready_transition_hooks):
        try:
            await hook(transition)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PR Party ready hook failed for %s#%s: %s",
                transition.repo_full_name,
                transition.pr_number,
                exc,
            )


# --- missing_since bookkeeping (C7) -----------------------------------------


def missing_long_enough_to_retire(
    pr: PRPartyPR, *, now: datetime | None = None, sweep_minutes: int | None = None
) -> bool:
    """Whether a row's absence has survived :data:`MISSING_MISS_THRESHOLD` passes.

    The miss *count* is not stored; it is inferred from ``missing_since`` and
    the sweep cadence, which is the same information with one fewer column to
    keep consistent. Changing ``PR_PARTY_SWEEP_MINUTES`` therefore changes how
    long a row must be absent, which is the intended relationship. U8 owns what
    happens when this is true.
    """
    if pr.missing_since is None:
        return False
    cadence = sweep_minutes or settings.pr_party_sweep_minutes or 5
    moment = now or datetime.now(UTC)
    stamped = pr.missing_since
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    return moment - stamped >= timedelta(minutes=cadence * MISSING_MISS_THRESHOLD)


# --- The sweep (KTD14) ------------------------------------------------------


async def sweep_open_prs(
    db: AsyncSession,
    *,
    client: PRPartyGitHubClient | None = None,
    org: str | None = None,
    now: datetime | None = None,
    pool: _EnqueuePool | None = None,
) -> SweepResult:
    """One reconciliation cycle: discover, detail-fetch what moved, reconcile.

    Stage 1 is a single org-scoped search. Stage 2 fetches detail + checks
    **only** for rows whose ``updated_at`` moved (or that we have never seen),
    which is what keeps the per-cycle budget at roughly ``1 + 3×changed`` calls
    against a 5000/hr limit — a sweep that detail-fetched everything would burn
    its budget and then stop seeing new PRs entirely.
    """
    moment = now or datetime.now(UTC)
    target_org = org or settings.pr_party_org
    result = SweepResult()

    active_client = client or _generation_client()
    if active_client is None:
        result.skipped_reason = "no_generation_token"
        logger.info("PR Party sweep skipped: no generation token configured.")
        return result

    registry = await load_reviewer_registry(db)
    known = await _load_known_prs(db)

    items, result.discovery_complete = await _discover(active_client, target_org, result)
    result.total = len(items)

    for item in items:
        try:
            await _process_item(
                db,
                active_client,
                item,
                known=known,
                registry=registry,
                moment=moment,
                pool=pool,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 — one bad PR must not end the cycle
            result.errors += 1
            logger.exception(
                "PR Party sweep failed for %s#%s: %s", item.repo_full_name, item.number, exc
            )

    if result.discovery_complete:
        _reconcile_missing(known, items=items, moment=moment, result=result)
        await db.commit()

    result.transitions = await apply_brewing_timeout(db, now=moment)
    result.timed_out = len(result.transitions)

    logger.info(
        "PR Party sweep complete: %d total, %d synced, %d errors, %d detail fetches",
        result.total,
        result.synced,
        result.errors,
        result.detail_fetches,
    )
    return result


async def _discover(
    client: PRPartyGitHubClient, org: str, result: SweepResult
) -> tuple[list[SearchedPR], bool]:
    """Stage 1: paginate the org search until a short page comes back.

    A page that raises aborts discovery and reports it incomplete — whatever was
    collected is still worth upserting, but it is *not* evidence about what is
    missing (C7).
    """
    items: list[SearchedPR] = []
    for page in range(1, MAX_DISCOVERY_PAGES + 1):
        try:
            batch = await client.search_org_open_prs(org, page=page, per_page=DISCOVERY_PAGE_SIZE)
        except Exception as exc:  # noqa: BLE001
            result.errors += 1
            logger.exception("PR Party discovery failed on page %d for org %s: %s", page, org, exc)
            return items, False
        items.extend(batch)
        if len(batch) < DISCOVERY_PAGE_SIZE:
            return items, True
    logger.warning("PR Party discovery hit the %d-page cap for org %s", MAX_DISCOVERY_PAGES, org)
    return items, True


async def _load_known_prs(db: AsyncSession) -> dict[tuple[str, int], PRPartyPR]:
    """Every row the sweep could touch, in one query rather than N."""
    result = await db.execute(select(PRPartyPR).where(PRPartyPR.state.in_(sorted(ACTIVE_STATES))))
    return {(row.repo_full_name, row.pr_number): row for row in result.scalars().all()}


async def _process_item(
    db: AsyncSession,
    client: PRPartyGitHubClient,
    item: SearchedPR,
    *,
    known: dict[tuple[str, int], PRPartyPR],
    registry: ReviewerRegistry,
    moment: datetime,
    pool: _EnqueuePool | None,
    result: SweepResult,
) -> None:
    key = (item.repo_full_name, item.number)
    row = known.get(key)

    if row is None and item.draft:
        # Never seen, still a draft: it has not been offered for review.
        result.skipped += 1
        return

    if not _needs_detail(row, item):
        result.synced += 1
        return

    result.detail_fetches += 1
    outcome = await refresh_pull_request(
        db,
        client,
        item.repo_full_name,
        item.number,
        registry=registry,
        now=moment,
        pool=pool,
    )
    result.synced += 1
    if outcome.created:
        result.created += 1
        if outcome.pr is not None:
            known[key] = outcome.pr
    if outcome.parked:
        result.parked += 1
    if outcome.skipped_reason:
        result.skipped += 1


def _needs_detail(row: PRPartyPR | None, item: SearchedPR) -> bool:
    """KTD14's budget gate: fetch detail only when something actually moved.

    A row with no ``head_sha`` or no recorded ``updated_at`` is incomplete and is
    always refetched; otherwise the search item's ``updated_at`` is compared for
    inequality rather than for being *newer*, so a corrected timestamp (GitHub
    has emitted them) still triggers a refresh instead of being ignored.
    """
    if row is None:
        return True
    if not row.head_sha or row.updated_at_github is None:
        return True
    if item.updated_at is None:
        return True
    stored = row.updated_at_github
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    return item.updated_at != stored


def _reconcile_missing(
    known: dict[tuple[str, int], PRPartyPR],
    *,
    items: list[SearchedPR],
    moment: datetime,
    result: SweepResult,
) -> None:
    """C7: stamp what a *complete* pass did not see; clear what it did.

    Only ever called after a complete discovery pass. Retirement is U8's.
    """
    present = {(item.repo_full_name, item.number) for item in items}
    for key, row in known.items():
        if key in present:
            if row.missing_since is not None:
                row.missing_since = None
                result.missing_cleared += 1
        elif row.missing_since is None:
            row.missing_since = moment
            result.missing_stamped += 1


# --- Webhook dispatch -------------------------------------------------------


async def handle_webhook_event(
    db: AsyncSession,
    event: str,
    payload: Mapping[str, Any],
    *,
    pool: _EnqueuePool | None = None,
    client: PRPartyGitHubClient | None = None,
    registry: ReviewerRegistry | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Route one delivered event onto the same upsert path as the sweep.

    Every branch converges on :func:`upsert_pr`, so a webhook and a sweep
    describing the same PR in the same cycle produce one row (KTD14).
    Unrecognized events are *ignored*, not errors: GitHub sends whatever the
    hook subscribes to, and a receiver that 500s on an unexpected event gets its
    hook disabled.
    """
    known_registry = registry if registry is not None else await load_reviewer_registry(db)
    active_client = client if client is not None else _generation_client()

    if event == "pull_request":
        action = str(payload.get("action") or "")
        if action not in PR_ACTIONS:
            return {"status": "ignored", "event": event, "action": action}
        outcome = await upsert_pr(
            db,
            facts_from_webhook_pr(payload),
            registry=known_registry,
            now=now,
            pool=pool,
        )
        return _outcome_payload(event, outcome, action=action)

    if event == "pull_request_review":
        # U8 reconciles review state from the sweep; all this event buys is
        # freshness, so it refreshes the PR and stores nothing about the review.
        outcome = await _refresh_from_envelope(
            db, payload, client=active_client, registry=known_registry, now=now, pool=pool
        )
        return _outcome_payload(event, outcome)

    if event == "check_suite":
        return await _handle_check_suite(
            db, payload, client=active_client, registry=known_registry, now=now, pool=pool
        )

    if event == "issue_comment":
        for hook in list(issue_comment_hooks):
            try:
                await hook(dict(payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("PR Party issue_comment hook failed: %s", exc)
        return {"status": "deferred", "event": event}

    # ``push`` carries no PR identity, so there is nothing to converge on; the
    # ``synchronize`` event covers the same fact with the PR attached.
    return {"status": "ignored", "event": event}


async def _refresh_from_envelope(
    db: AsyncSession,
    payload: Mapping[str, Any],
    *,
    client: PRPartyGitHubClient | None,
    registry: ReviewerRegistry,
    now: datetime | None,
    pool: _EnqueuePool | None,
) -> IntakeResult:
    """Refresh the PR an envelope refers to, degrading to the embedded object.

    With no generation token configured there is nothing to fetch *with*, but
    the envelope usually carries a full ``pull_request`` object — using it keeps
    the receiver useful on a deployment that has not been given a read token.
    """
    pr = payload.get("pull_request")
    repo_full_name = _repo_full_name(payload, pr if isinstance(pr, Mapping) else {})
    number = _to_int(pr.get("number")) if isinstance(pr, Mapping) else None

    if client is not None and repo_full_name and number is not None:
        return await refresh_pull_request(
            db, client, repo_full_name, number, registry=registry, now=now, pool=pool
        )
    return await upsert_pr(
        db, facts_from_webhook_pr(payload), registry=registry, now=now, pool=pool
    )


async def _handle_check_suite(
    db: AsyncSession,
    payload: Mapping[str, Any],
    *,
    client: PRPartyGitHubClient | None,
    registry: ReviewerRegistry,
    now: datetime | None,
    pool: _EnqueuePool | None,
) -> dict[str, Any]:
    """Check state only exists on the check-runs surface, so this must fetch."""
    suite = payload.get("check_suite")
    repo_full_name = _repo_full_name(payload, {})
    if not isinstance(suite, Mapping) or client is None or not repo_full_name:
        return {"status": "ignored", "event": "check_suite"}

    prs = suite.get("pull_requests")
    numbers = [
        n
        for n in (
            _to_int(p.get("number"))
            for p in (prs if isinstance(prs, list) else [])
            if isinstance(p, Mapping)
        )
        if n is not None
    ]
    if not numbers:
        return {"status": "ignored", "event": "check_suite"}

    handled = 0
    for number in numbers:
        try:
            await refresh_pull_request(
                db, client, repo_full_name, number, registry=registry, now=now, pool=pool
            )
            handled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_suite refresh failed for %s#%s: %s", repo_full_name, number, exc)
    return {"status": "processed", "event": "check_suite", "handled": handled}


def _outcome_payload(event: str, outcome: IntakeResult, **extra: Any) -> dict[str, Any]:
    if outcome.pr is None:
        return {"status": "ignored", "event": event, "reason": outcome.skipped_reason, **extra}
    return {
        "status": "processed",
        "event": event,
        "created": outcome.created,
        "new_revision": outcome.new_revision,
        **extra,
    }


# --- Module helpers ---------------------------------------------------------


def _generation_client() -> PRPartyGitHubClient | None:
    """The shared read-only client (KTD13), or ``None`` if unconfigured."""
    token = settings.pr_party_readonly_token
    return generation_client(token) if token else None


def _repo_full_name(payload: Mapping[str, Any], pr: Mapping[str, Any]) -> str:
    """``owner/repo`` from the envelope, falling back to the PR's base repo."""
    repository = payload.get("repository")
    if isinstance(repository, Mapping):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name:
            return full_name
    base = pr.get("base")
    if isinstance(base, Mapping):
        repo = base.get("repo")
        if isinstance(repo, Mapping):
            full_name = repo.get("full_name")
            if isinstance(full_name, str) and full_name:
                return full_name
    return ""


def _sub_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """A nested JSON object, or an empty mapping if GitHub omitted it."""
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
