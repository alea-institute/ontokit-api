"""Ready-notification fan-out for PR Party (U9 — R22, R27, KTD20).

When a card leaves ``brewing`` — because its brief landed, because the brief
failed, or because U4's 90-minute timeout released it — every reviewer that card
routes to gets told exactly once, on two channels:

- an **in-app notification** row (the bell U12 renders), and
- an **ntfy push**, for reviewers who registered a topic.

Three properties carry the design:

**Once per reviewer per revision.** The uniqueness key is the U1 partial unique
index on ``(user_id, type, target_id)`` where ``type = 'pr_party_ready'``, with
``target_id`` = ``{repo_full_name}#{pr_number}:{head_sha}``. This module inserts
optimistically inside a SAVEPOINT and treats the violation as a no-op. Doing it
in the database rather than with a read-then-write check is what makes a
concurrent sweep and brief worker — which genuinely do fire for the same card at
the same time — collapse to one ping instead of two. Because the key includes
the reviewer, a reviewer registered *after* the first fan-out still receives
their first ping for a revision everyone else was already told about; because it
includes the head SHA, a re-push notifies again.

**The push carries no PR-derived text (KTD20).** An ntfy topic is a bearer
capability in a URL: whoever holds it can subscribe. So the message is a fixed
title plus the card link — never the repo name, the PR title, or a line of the
brief. The link is useless without an OntoKit session, so a leaked topic leaks
the *timing* of reviews and nothing else. The in-app row is deliberately held to
the same copy: it is the same sentence rendered in two places, and the card it
points at is where the details belong.

**A notifier can never break a lifecycle transition.** Both hook callers already
committed the transition before firing, and they swallow hook exceptions — but
this module also never lets ntfy failure affect the in-app row: the DB commit
happens first, publishing after, and a publish failure is a WARNING carrying
:data:`NTFY_FAILURE_MARKER` so it is greppable in production logs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.notification import Notification
from ontokit.models.pr_party import PRPartyPR, PRPartyReviewer
from ontokit.schemas.pr_party import NTFY_TOPIC_PATTERN

if TYPE_CHECKING:  # pragma: no cover — import-cycle-free typing only
    from ontokit.services.pr_party_intake import ReadyTransition

logger = logging.getLogger(__name__)

#: The ``notifications.type`` value the U1 partial unique index is scoped to.
#: Changing this string silently disables the once-per-revision guarantee.
NOTIFICATION_TYPE: Final = "pr_party_ready"

#: The only sentence either channel ever says (KTD20). Fixed by design.
NTFY_TITLE: Final = "A PR is ready for your review"

#: Greppable marker for a failed push. Ops looks for this, not for a stack.
NTFY_FAILURE_MARKER: Final = "pr_party_ntfy_publish_failed"

#: Short by intent: this runs inside a hook on a lifecycle transition, and a
#: hanging notifier would hold the sweep or the brief worker open.
NTFY_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)

#: ``(topic, link) -> None``. Injectable so the fan-out is testable without HTTP.
NtfyPublisher = Callable[[str, str], Awaitable[None]]


def revision_target_id(repo_full_name: str, pr_number: int, head_sha: str) -> str:
    """The per-revision uniqueness key stored on ``notifications.target_id``."""
    return f"{repo_full_name}#{pr_number}:{head_sha}"


def card_path(card_id: uuid.UUID | str) -> str:
    """The web route for one card — KTD19's ``?card=`` deep link."""
    return f"/pr-party?card={card_id}"


async def publish_ntfy(
    topic: str,
    link: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """POST the fixed payload to one reviewer's topic. Returns whether it sent.

    Returns ``False`` (rather than raising) for the two "not configured" cases —
    no base URL, or a topic that does not match :data:`NTFY_TOPIC_PATTERN`. The
    pattern check is a second line of defense behind the settings-route
    validator: a topic becomes a URL path segment, and a stored value with a
    ``/`` or ``..`` in it would aim this POST somewhere else entirely.
    """
    base = settings.pr_party_ntfy_base_url.strip().rstrip("/")
    if not base:
        return False
    if not NTFY_TOPIC_PATTERN.match(topic):
        logger.warning("%s: refusing to publish to malformed ntfy topic", NTFY_FAILURE_MARKER)
        return False

    async with httpx.AsyncClient(timeout=NTFY_TIMEOUT, transport=transport) as client:
        response = await client.post(
            f"{base}/{topic}",
            content=link.encode("utf-8"),
            headers={"Title": NTFY_TITLE},
        )
        response.raise_for_status()
    return True


async def emit_ready_notification(
    db: AsyncSession,
    transition: ReadyTransition,
    *,
    publish: NtfyPublisher | None = None,
) -> list[str]:
    """Notify every reviewer this card routes to. Returns the Zitadel ids told.

    Only reviewers whose insert actually landed are returned — and only those
    get a push — so a re-fire of an already-notified revision is silent on both
    channels.
    """
    card = await _load_card(db, transition)
    if card is None:
        logger.warning(
            "PR Party ready notification skipped: no card row for %s#%s (%s)",
            transition.repo_full_name,
            transition.pr_number,
            transition.reason,
        )
        return []

    reviewers = list((await db.execute(select(PRPartyReviewer))).scalars().all())
    recipients = [r for r in reviewers if not _is_author(r, card)]
    if not recipients:
        return []

    target_id = revision_target_id(
        transition.repo_full_name, transition.pr_number, transition.head_sha
    )
    link = card_path(card.id)

    notified: list[tuple[str, str | None]] = []
    for reviewer in recipients:
        if await _insert_once(db, user_id=reviewer.zitadel_user_id, target_id=target_id, link=link):
            notified.append((reviewer.zitadel_user_id, reviewer.ntfy_topic))
    if not notified:
        return []

    # Commit before pushing: an unreachable ntfy must not cost the bell row, and
    # a push for a row that was never committed would deep-link into nothing.
    await db.commit()

    if settings.pr_party_ntfy_base_url.strip():
        publisher = publish or publish_ntfy
        for user_id, topic in notified:
            if not topic:
                continue  # in-app only — a topic is opt-in (R27)
            try:
                await publisher(topic, link)
            except Exception as exc:  # noqa: BLE001 — the bell row already landed
                logger.warning("%s for reviewer %s: %s", NTFY_FAILURE_MARKER, user_id, exc)

    return [user_id for user_id, _topic in notified]


async def notify_ready_transition(transition: ReadyTransition) -> None:
    """The registered hook: same work, on a session of its own.

    The seam hands hooks a value, not a session — the sweep's and the brief
    worker's sessions have both just committed the transition, and reusing one
    would let a notification failure land in the middle of that caller's unit of
    work. A short-lived session keeps the two independent.
    """
    from ontokit.core.database import async_session_maker

    async with async_session_maker() as db:
        await emit_ready_notification(db, transition)


def register_ready_hook() -> None:
    """Attach the notifier to the intake seam. Idempotent by design.

    Called from both the API lifespan and the arq worker startup — the two
    processes that can fire a ready transition, neither of which imports the
    other. The membership check is what makes calling it from both (or twice in
    one process, as a reloading dev server does) safe: a duplicated hook would
    double every ping, and the constraint could not tell the second insert of a
    *fresh* revision from a real duplicate.
    """
    from ontokit.services.pr_party_intake import ready_transition_hooks

    if notify_ready_transition not in ready_transition_hooks:
        ready_transition_hooks.append(notify_ready_transition)


async def _load_card(db: AsyncSession, transition: ReadyTransition) -> PRPartyPR | None:
    """Resolve the card row — by id when the transition carried one.

    ``pr_id`` is optional on the payload, so fall back to the natural key. The
    row is needed for two things a transition does not carry: the card id the
    deep link is built from, and the author identity own-PR routing turns on.
    """
    if transition.pr_id is not None:
        stmt = select(PRPartyPR).where(PRPartyPR.id == transition.pr_id)
    else:
        stmt = select(PRPartyPR).where(
            PRPartyPR.repo_full_name == transition.repo_full_name,
            PRPartyPR.pr_number == transition.pr_number,
        )
    rows = list((await db.execute(stmt)).scalars().all())
    return rows[0] if rows else None


def _is_author(reviewer: PRPartyReviewer, card: PRPartyPR) -> bool:
    """Is this reviewer the PR's author? (R22 — own-PR cards route to nobody.)

    Node-id-first, because a GitHub login can be renamed: when both sides carry
    a node id, that comparison is the whole answer and a stale login on the
    registry row cannot override it. The casefolded login fallback covers the
    rows where U2 could not resolve a node id, or where intake never saw one.
    """
    if reviewer.github_node_id and card.author_node_id:
        return reviewer.github_node_id == card.author_node_id
    if not reviewer.github_login or not card.author_github_login:
        return False
    return reviewer.github_login.casefold() == card.author_github_login.casefold()


async def _insert_once(
    db: AsyncSession,
    *,
    user_id: str,
    target_id: str,
    link: str,
) -> bool:
    """Insert one bell row, or report that this revision already notified.

    The SAVEPOINT is load-bearing: a unique violation aborts the enclosing
    Postgres transaction, so without it the *first* reviewer who was already
    notified would take every later reviewer's insert down with them.
    """
    try:
        async with db.begin_nested():
            db.add(
                Notification(
                    user_id=user_id,
                    type=NOTIFICATION_TYPE,
                    title=NTFY_TITLE,
                    body=None,
                    project_id=None,
                    project_name=None,
                    target_id=target_id,
                    target_url=link,
                )
            )
            await db.flush()
    except IntegrityError:
        logger.debug("PR Party: %s already notified for %s", user_id, target_id)
        return False
    return True
