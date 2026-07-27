"""PR Party Q&A: asking the AI on a card, and binding the answer that comes back.

Three decisions shape everything in this module.

**1. The question is posted as the human who asked it (KTD18).** It rides the
*actuation* client — the asking reviewer's own PAT — for a reason that is easy
to get wrong: GitHub deliberately does not run Actions workflows for events
triggered by ``GITHUB_TOKEN``, so a question posted by the app would summon
nobody. Posting as the reviewer is what makes the org answerer fire at all, and
it has the side benefit that the audit trail on GitHub names a person.

**2. Nothing about the thread is persisted.** There is no Q&A table and no
``pr_party_action`` row for a question, and both absences are deliberate:

- U1's live-fingerprint index permits one non-``failed`` action row per
  ``(reviewer, pr, head_sha, action_kind)`` — *every* kind, questions included.
  Multiple questions at one head are entirely legitimate, so writing action rows
  for them would make the second question of a revision a constraint violation.
  Bending the index (or the row shape) to fit would be a schema change U7 has no
  business making, and the honest alternative is better anyway:
- **GitHub is the system of record for the conversation (KD5/R13).** The thread
  lives where the reviewer can already see it, reply to it, and where the org
  answerer reads it. :meth:`PRPartyQAService.load_thread` therefore *projects*
  the card's ``qa_thread`` from the PR's issue comments at read time and caches
  nothing. A copy in our database could only ever be a staler second opinion
  about a conversation we do not own.

  The cost is one GitHub read per card open, paid with the shared read-only
  generation token, and a card that renders with an empty thread when GitHub is
  unreachable. Both are cheaper than a divergent copy.

**3. An answer binds by linkage, never by author identity (C4).** The prototype
bound "the next comment by the bot" to the question, which quietly mislabels
every CodeRabbit walkthrough that lands mid-conversation as an answer. Here a
later comment binds only if it *points back*: a reference to the question's
comment id, a quote of the question, or an @-mention of the asker. Being the
right author is not evidence; saying which question you are answering is.

Degraded mode (R12) is compose-for-copy: with no usable PAT the reviewer gets
the exact comment text and a deep link, no client is constructed, and nothing
is recorded — there is nothing to reconcile later, because an unposted comment
left no trace anywhere.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ontokit.models.pr_party import PRPartyPR, PRPartyReviewer
from ontokit.schemas.pr_party import PRPartyQAEntry
from ontokit.services.pr_party_credentials import CredentialResolver, mark_credential_dead
from ontokit.services.pr_party_github import (
    IssueComment,
    PRPartyGitHubClient,
    PRPartyGitHubError,
    TokenExpiredError,
    actuation_client,
    default_generation_client,
    split_repo,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ATTRIBUTION_PREFIX",
    "CLAUDE_MENTION",
    "CODERABBIT_REVIEW_COMMAND",
    "NOTE_HEADING",
    "ORG_WORKFLOW_ASSET",
    "PRPartyQAService",
    "QAIngestion",
    "QARefused",
    "QAResult",
    "answer_binds",
    "build_qa_thread",
    "classify_issue_comment",
    "compose_note",
    "compose_question",
    "ingest_issue_comment",
    "is_question",
    "linkage_signals",
    "parse_attribution",
    "register_qa_hook",
]

#: What the org answerer workflow triggers on. Case-insensitive everywhere it is
#: matched: GitHub renders ``@Claude`` as the same mention.
CLAUDE_MENTION: Final = "@claude"

#: R3: the reviewer's re-trigger for an AI review that never arrived (or arrived
#: before the branch was finished). CodeRabbit reads this as a command.
CODERABBIT_REVIEW_COMMAND: Final = "@coderabbitai review"

#: Names the human behind a PAT-authored comment. Parsed back out in
#: :func:`parse_attribution`, which is how the thread knows who to look for an
#: @-mention of.
ATTRIBUTION_PREFIX: Final = "asked via PR Party by "

#: Written to a credential's ``last_error`` when GitHub rejects the reviewer's
#: PAT while posting a comment — the only signal U2 ever gets that it died.
_DEAD_PAT_POSTING_COMMENT = (
    "GitHub rejected this token while posting a comment (401). Submit a fresh PAT."
)

#: R14. Distinct from a question so the answerer is not summoned by a decision
#: that has already been made.
NOTE_HEADING: Final = "**PR Party — outcome of live discussion**"
NOTE_ATTRIBUTION_PREFIX: Final = "recorded via PR Party by "

#: The org answerer workflow this repo *carries but never runs* — it is destined
#: for ``catholicos/.github``. See the README beside it.
ORG_WORKFLOW_ASSET: Final = (
    Path(__file__).resolve().parent.parent / "pr_party_org_assets" / "claude-pr-answers.yml"
)

_ATTRIBUTION_RE: Final = re.compile(
    r"\(\s*(?:asked|recorded)\s+via\s+PR\s+Party\s+by\s+([A-Za-z0-9][A-Za-z0-9-]{0,38})\s*\)",
    re.IGNORECASE,
)

#: ``#issuecomment-12345`` — the fragment every GitHub comment permalink ends in,
#: and the most unambiguous linkage signal there is.
_COMMENT_REF_RE: Final = re.compile(r"issuecomment-(\d+)")

#: A markdown quote line. Reply-by-quoting is what GitHub's own "Quote reply"
#: button produces, so it is the linkage signal humans generate without trying.
_QUOTE_LINE_RE: Final = re.compile(r"^\s*>\s?(.*)$", re.MULTILINE)

#: A quoted fragment shorter than this proves nothing — "> yes" appears under
#: every question ever asked.
_MIN_QUOTE_MATCH: Final = 12

_WHITESPACE_RE: Final = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Value shapes
# ---------------------------------------------------------------------------


class QARefused(Exception):
    """A refusal the route should render as a status, not a 500."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class QAResult:
    """What became of one comment PR Party tried to post.

    ``body`` is populated on every path, delivered or not: on the degraded path
    it *is* the deliverable (R12), and on the posted path it is what the client
    shows back without re-reading GitHub.
    """

    body: str
    posted: bool = False
    degraded: bool = False
    comment_id: int | None = None
    comment_url: str | None = None
    deep_link: str | None = None


@dataclass(frozen=True)
class QAIngestion:
    """What one ``issue_comment`` delivery turned out to be.

    Observational only — see the module docstring. ``kind`` is one of
    ``question``, ``answer``, ``unrelated``, or ``ignored`` (not a PR comment).
    """

    kind: str
    repo_full_name: str = ""
    pr_number: int | None = None
    comment_id: int | None = None
    author: str | None = None
    #: Present only when the comment carried an explicit reply reference. A
    #: webhook payload holds one comment, so mention- and quote-linkage cannot
    #: be resolved here — the card's live thread does that with full context.
    question_comment_id: int | None = None


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def compose_question(question: str, *, github_login: str | None) -> str:
    """The comment body a question becomes.

    The mention leads, because that is what the org workflow's ``contains``
    gate looks for, and the attribution trails, because a reviewer skimming the
    conversation on GitHub should be able to tell a PR Party question from one
    typed into the comment box.

    A reviewer who types the mention themselves does not get it twice — the
    duplicate would be harmless to the workflow and jarring to read.
    """
    text = question.strip()
    if not text:
        raise ValueError("A question cannot be empty.")
    if text.casefold().startswith(CLAUDE_MENTION):
        text = text[len(CLAUDE_MENTION) :].lstrip()
        if not text:
            raise ValueError("A question cannot be empty.")
    who = (github_login or "").strip() or "a reviewer"
    return f"{CLAUDE_MENTION} {text}\n\n({ATTRIBUTION_PREFIX}{who})"


def compose_note(note: str, *, github_login: str | None) -> str:
    """R14: the outcome of a live discussion, as a comment on the PR.

    Deliberately carries no ``@claude`` mention. A decision that has already
    been made does not need an AI to weigh in on it, and summoning one on every
    recorded outcome would turn the conversation into a machine's.
    """
    text = note.strip()
    if not text:
        raise ValueError("A note cannot be empty.")
    who = (github_login or "").strip() or "a reviewer"
    return f"{NOTE_HEADING}\n\n{text}\n\n({NOTE_ATTRIBUTION_PREFIX}{who})"


def parse_attribution(body: str | None) -> str | None:
    """The login named in a PR Party attribution line, if there is one."""
    match = _ATTRIBUTION_RE.search(body or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Binding (C4)
# ---------------------------------------------------------------------------


def is_question(comment: IssueComment) -> bool:
    """Does this comment summon the answerer?

    The same test the workflow's ``contains`` gate applies, so what we call a
    question and what actually triggers an answer cannot drift apart.
    """
    return CLAUDE_MENTION in (comment.body or "").casefold()


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _mentions(body: str, login: str) -> bool:
    """``@login``, bounded so ``@dami`` does not match ``@damienriehl``."""
    pattern = re.compile(rf"@{re.escape(login)}(?![A-Za-z0-9-])", re.IGNORECASE)
    return bool(pattern.search(body))


def linkage_signals(
    answer: IssueComment,
    *,
    question: IssueComment,
    asker_login: str | None,
) -> frozenset[str]:
    """Every way this comment points back at that question. Possibly none.

    Returned as a set rather than a boolean so a caller (and a failing test) can
    say *which* signal fired. Three of them, in descending order of how hard
    they are to produce by accident:

    ``reply_reference``
        The body cites the question's comment id — the permalink GitHub's own
        reply affordances paste, and what the org workflow's prompt instructs
        the answerer to lead with.
    ``quote``
        A markdown quote line whose text appears in the question. Long enough to
        mean something; ``> yes`` is not linkage.
    ``mention``
        An @-mention of the person who asked.
    """
    body = answer.body or ""
    signals: set[str] = set()

    if question.id and str(question.id) in _COMMENT_REF_RE.findall(body):
        signals.add("reply_reference")

    question_text = _normalize(question.body or "")
    for quoted in _QUOTE_LINE_RE.findall(body):
        fragment = _normalize(quoted)
        if len(fragment) >= _MIN_QUOTE_MATCH and fragment in question_text:
            signals.add("quote")
            break

    if asker_login and _mentions(body, asker_login):
        signals.add("mention")

    return frozenset(signals)


def _created_at(comment: IssueComment) -> datetime:
    stamp = comment.created_at
    if stamp is None:
        return datetime.min.replace(tzinfo=UTC)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def answer_binds(
    answer: IssueComment,
    *,
    question: IssueComment,
    asker_login: str | None,
) -> bool:
    """C4: is this comment *the answer* to that question?

    Three disqualifications before linkage is even consulted — a comment cannot
    answer itself, a comment that predates the question cannot answer it, and a
    comment that summons the answerer is another question, not an answer to this
    one. Then, and only then, a linkage signal decides. Who wrote it never does.
    """
    if answer.id == question.id:
        return False
    if _created_at(answer) < _created_at(question):
        return False
    if is_question(answer):
        return False
    return bool(linkage_signals(answer, question=question, asker_login=asker_login))


def build_qa_thread(comments: Sequence[IssueComment]) -> list[PRPartyQAEntry]:
    """Project the ``@claude`` exchange out of a PR's whole conversation.

    Everything that is neither a question nor an answer bound to one is dropped:
    the card shows the exchange, not the comment log.

    An answer is claimed by the *earliest* question it binds to and never
    re-used, so a single "@asker yes" under two questions answers the first and
    leaves the second honestly open rather than pretending to answer both.
    """
    ordered = sorted(comments, key=lambda c: (_created_at(c), c.id))
    questions = [c for c in ordered if is_question(c)]
    claimed: set[int] = set()
    entries: list[PRPartyQAEntry] = []

    for question in questions:
        asker = parse_attribution(question.body) or question.user_login
        answer = next(
            (
                c
                for c in ordered
                if c.id not in claimed and answer_binds(c, question=question, asker_login=asker)
            ),
            None,
        )
        if answer is not None:
            claimed.add(answer.id)

        entries.append(
            PRPartyQAEntry(
                question_comment_id=question.id,
                question_body=question.body or "",
                question_author=asker,
                question_url=question.html_url,
                asked_at=question.created_at,
                answer_comment_id=answer.id if answer else None,
                answer_body=answer.body if answer else None,
                answer_author=answer.user_login if answer else None,
                answer_url=answer.html_url if answer else None,
                answered_at=answer.created_at if answer else None,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _split_repo(repo_full_name: str) -> tuple[str, str]:
    """:func:`~ontokit.services.pr_party_github.split_repo` in this module's refusal type."""
    try:
        return split_repo(repo_full_name)
    except ValueError as e:
        raise QARefused(500, "This card's repository name is malformed.") from e


class PRPartyQAService:
    """Posting comments as a reviewer, and reading the thread back.

    Holds no database session on purpose: nothing here writes a row, so there is
    nothing for one to do. The two GitHub domains it needs are injected as
    factories, which is also the seam the tests use to prove that the degraded
    path constructs no client at all.
    """

    def __init__(
        self,
        *,
        credentials: CredentialResolver,
        actuation_factory: Callable[[str], PRPartyGitHubClient] = actuation_client,
        generation_factory: Callable[[], PRPartyGitHubClient | None] = default_generation_client,
    ) -> None:
        self._credentials = credentials
        self._actuation_factory = actuation_factory
        self._generation_factory = generation_factory

    # --- Writes ---

    async def ask(
        self, *, reviewer: PRPartyReviewer, pr: PRPartyPR, question: str, pr_url: str
    ) -> QAResult:
        """R13: put a question to the AI reviewer, as the reviewer asking it."""
        body = compose_question(question, github_login=reviewer.github_login)
        return await self._post(reviewer=reviewer, pr=pr, body=body, pr_url=pr_url)

    async def record_note(
        self, *, reviewer: PRPartyReviewer, pr: PRPartyPR, note: str, pr_url: str
    ) -> QAResult:
        """R14: put the outcome of a live discussion back on the PR."""
        body = compose_note(note, github_login=reviewer.github_login)
        return await self._post(reviewer=reviewer, pr=pr, body=body, pr_url=pr_url)

    async def rerun_review(
        self, *, reviewer: PRPartyReviewer, pr: PRPartyPR, pr_url: str
    ) -> QAResult:
        """R3: ask CodeRabbit to review again.

        Posted as the reviewer for the same reason a question is: a bot command
        authored by our app would be a command from nobody, and the reviewer's
        identity is what the AI reviewer's own permissions are checked against.
        """
        return await self._post(
            reviewer=reviewer, pr=pr, body=CODERABBIT_REVIEW_COMMAND, pr_url=pr_url
        )

    async def _post(
        self, *, reviewer: PRPartyReviewer, pr: PRPartyPR, body: str, pr_url: str
    ) -> QAResult:
        """One comment, delivered as the reviewer or handed back to be pasted."""
        token = await self._credentials.resolve_token(reviewer)
        if token is None:
            # R12: no client is constructed, so there is provably no HTTP here.
            return QAResult(body=body, degraded=True, deep_link=pr_url)

        owner, repo = _split_repo(pr.repo_full_name)
        client = self._actuation_factory(token)

        try:
            comment = await client.create_comment(owner, repo, pr.pr_number, body=body)
        except TokenExpiredError as e:
            # The only signal U2 ever gets that a stored PAT died: validation
            # runs on submission, and nothing else re-checks it.
            await self._mark_credential_dead(reviewer, e)
            return QAResult(body=body, degraded=True, deep_link=pr_url)
        except PRPartyGitHubError as e:
            logger.warning("PR Party: comment post failed (%s)", type(e).__name__)
            raise QARefused(
                502,
                "GitHub could not post that comment. Nothing was sent — try again "
                "shortly, or paste it on the pull request yourself.",
            ) from e

        return QAResult(
            body=body,
            posted=True,
            comment_id=comment.id,
            comment_url=comment.html_url,
        )

    async def _mark_credential_dead(self, reviewer: PRPartyReviewer, error: BaseException) -> None:
        # This service holds no session of its own (see the class docstring), so
        # the commit is the credential service's.
        if await mark_credential_dead(
            self._credentials, reviewer, error, message=_DEAD_PAT_POSTING_COMMENT
        ):
            await self._credentials.save()

    # --- Reads ---

    async def load_thread(self, *, pr: PRPartyPR) -> list[PRPartyQAEntry]:
        """The card's ``@claude`` exchange, live from GitHub (KD5).

        Best-effort by design. A card is worth rendering without its Q&A panel;
        it is not worth failing to render because GitHub is slow, the generation
        token is unset, or the repo name on the row is malformed. Every one of
        those degrades to an empty thread and a log line.
        """
        client = self._generation_factory()
        if client is None:
            return []

        try:
            owner, repo = _split_repo(pr.repo_full_name)
            comments = await client.get_issue_comments(owner, repo, pr.pr_number)
        except (PRPartyGitHubError, QARefused) as e:
            logger.warning(
                "PR Party: could not read the Q&A thread for %s#%s (%s)",
                pr.repo_full_name,
                pr.pr_number,
                type(e).__name__,
            )
            return []

        return build_qa_thread(comments)


# ---------------------------------------------------------------------------
# Webhook ingestion (U4's ``issue_comment_hooks`` seam)
# ---------------------------------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def classify_issue_comment(payload: Mapping[str, Any]) -> QAIngestion:
    """What an ``issue_comment`` delivery is, without storing any of it.

    Classification only, and deliberately conservative about ``answer``: a
    webhook carries one comment with no thread around it, so the only linkage
    signal resolvable here is an explicit reply reference. Mention- and
    quote-linkage need the question to compare against, which
    :func:`build_qa_thread` has and this does not. Under-classifying costs a log
    line; over-classifying would be exactly the C4 mistake in a second place.
    """
    issue = _mapping(payload.get("issue"))
    if not issue.get("pull_request"):
        # Comments on ordinary issues are not PR Party's business.
        return QAIngestion(kind="ignored")

    comment = _mapping(payload.get("comment"))
    body = str(comment.get("body") or "")
    repository = _mapping(payload.get("repository"))
    author = _mapping(comment.get("user")).get("login")

    number = issue.get("number")
    common: dict[str, Any] = {
        "repo_full_name": str(repository.get("full_name") or ""),
        "pr_number": int(number) if isinstance(number, int) else None,
        "comment_id": int(comment["id"]) if isinstance(comment.get("id"), int) else None,
        "author": str(author) if author else None,
    }

    if CLAUDE_MENTION in body.casefold():
        return QAIngestion(kind="question", **common)

    referenced = _COMMENT_REF_RE.findall(body)
    if referenced:
        return QAIngestion(kind="answer", question_comment_id=int(referenced[0]), **common)

    return QAIngestion(kind="unrelated", **common)


async def ingest_issue_comment(payload: dict[str, Any]) -> None:
    """The hook U4 left a seam for. Classifies, logs, and stores nothing.

    Storing is the thing this deliberately does not do (see the module
    docstring): the card's thread is projected from GitHub on read, so a copy
    written here could only go stale. What the seam buys is *observability* —
    a greppable record that a question or an answer crossed the boundary, which
    is what makes "the answerer never fired" diagnosable without opening GitHub.
    """
    try:
        ingestion = classify_issue_comment(payload)
    except Exception as e:  # noqa: BLE001 — a malformed delivery is not our crash
        logger.warning("PR Party: unclassifiable issue_comment delivery (%r)", e)
        return

    if ingestion.kind == "ignored":
        return

    logger.info(
        "PR Party Q&A: %s comment on %s#%s by %s",
        ingestion.kind,
        ingestion.repo_full_name,
        ingestion.pr_number,
        ingestion.author,
        extra={
            "event": "pr_party_qa_comment",
            "qa_kind": ingestion.kind,
            "comment_id": ingestion.comment_id,
            "question_comment_id": ingestion.question_comment_id,
        },
    )


def register_qa_hook() -> None:
    """Attach Q&A ingestion to the intake seam. Idempotent, like U9's.

    Imported lazily so this module and intake stay free of an import cycle —
    intake owns the hook list, and this module owns what goes in it.
    """
    from ontokit.services.pr_party_intake import issue_comment_hooks

    if ingest_issue_comment not in issue_comment_hooks:
        issue_comment_hooks.append(ingest_issue_comment)
