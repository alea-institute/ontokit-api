"""PR Party data model — async PR review for the CatholicOS GitHub org.

GitHub is the system of record; these tables are a *projection* of it plus the
state GitHub cannot hold (brief content, per-reviewer intent, idempotency).

Two structural decisions carry most of the weight:

- **Column ownership (KTD15).** ``pr_party_pr`` is written by three disjoint
  writers: the sweep/webhook path owns the PR-facts columns, the brief worker
  owns the ``brief_*`` columns, and the verdict path owns nothing here (it
  writes ``pr_party_action``). Keeping them in separate column groups is what
  makes "refresh must not clobber the brief" a property of the schema rather
  than a rule someone has to remember at each upsert site.
- **One live action per fingerprint (KTD16).** ``pr_party_action`` carries a
  *partial* unique index over ``(reviewer_id, pr_id, head_sha, action_kind)``
  restricted to non-``failed`` rows. Double actuation — from the web client's
  5xx retry loop or a webhook redelivery — becomes an integrity error instead
  of a second GitHub review. ``failed`` rows sit outside the predicate so a
  dead attempt can never wedge a retry (C6).

Per R24 the fingerprint leads with ``reviewer_id``: two reviewers hold entirely
independent action rows on the same PR at the same head SHA.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontokit.core.database import Base


class PRPartyMergeDefault(StrEnum):
    """R11: where a reviewer's merges happen by default."""

    DASHBOARD = "dashboard"
    MANUAL = "manual"


class PRPartyAuthorKind(StrEnum):
    """R19: how a PR's author relates to the reviewer pair.

    The stored kind is the PR's own authorship fact: intake persists
    ``counterpart`` for ANY registry-member author ("a principal authored
    this"), alongside ``author_github_login``/``author_node_id``. Own-vs-
    counterpart is caller-relative and is derived at read time (U15) by
    comparing the stored author identity to the caller — ``OWN`` exists for
    that derived, per-caller projection and is never persisted by intake.
    """

    COUNTERPART = "counterpart"
    OWN = "own"
    THIRD_PARTY = "third_party"
    BOT = "bot"


class PRPartyBriefStatus(StrEnum):
    """Lifecycle of the LLM-generated brief attached to a PR revision."""

    BREWING = "brewing"
    READY = "ready"
    READY_WITH_WARNING = "ready_with_warning"
    FAILED = "failed"


class PRPartyActionKind(StrEnum):
    """What a reviewer actuated. Merge is its own claim, not a review verdict."""

    REVIEW = "review"
    MERGE = "merge"
    QUESTION = "question"


class PRPartyActionStatus(StrEnum):
    """KTD16: the row is inserted ``pending`` *before* the GitHub call.

    ``degraded_intent`` records a verdict the app could not deliver (expired
    PAT); ``degraded_confirmed`` records the reviewer confirming they completed
    it on GitHub by hand.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED_INTENT = "degraded_intent"
    DEGRADED_CONFIRMED = "degraded_confirmed"


class PRPartyReviewer(Base):
    """A registered reviewer (KTD12).

    Rows reconcile at startup from the ``PR_PARTY_REVIEWERS`` setting —
    reviewer identity is environment data, not schema. There is no seed
    migration and no mutation endpoint.
    """

    __tablename__ = "pr_party_reviewer"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    zitadel_user_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    # Resolved from the login via GET /users/{login}. Rename-proof identity for
    # own-PR detection (R18) and degraded-verdict confirmation; nullable because
    # reconcile can run before GitHub is reachable.
    github_node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merge_default: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PRPartyMergeDefault.MANUAL,
        server_default="manual",
    )
    # SECRET. Never serialized into the capability payload (GET /pr-party/me)
    # or any queue/card response — readable only through the reviewer's own
    # settings route, and deliberately absent from __repr__ below.
    ntfy_topic: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    credential: Mapped["PRPartyCredential | None"] = relationship(
        back_populates="reviewer", uselist=False, cascade="all, delete-orphan"
    )
    # ``passive_deletes`` hands the delete to the FK's ON DELETE CASCADE. Without
    # it the ORM loads every action row and UPDATEs its ``reviewer_id`` to NULL —
    # a NOT NULL column, so that path is an IntegrityError, not a slow success.
    actions: Mapped[list["PRPartyAction"]] = relationship(
        back_populates="reviewer", passive_deletes=True
    )

    def __repr__(self) -> str:
        return (
            f"<PRPartyReviewer(zitadel_user_id={self.zitadel_user_id!r}, "
            f"github_login={self.github_login!r}, merge_default={self.merge_default!r})>"
        )


class PRPartyCredential(Base):
    """One write PAT per reviewer (KTD13).

    MultiFernet-encrypted with previous-key rotation support. ``expires_at``
    comes from GitHub's ``github-authentication-token-expiration`` response
    header; ``last_validated_at`` / ``last_error`` back the settings health
    surface and the T-30 expiry warning.
    """

    __tablename__ = "pr_party_credential"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Unique + CASCADE: exactly one credential per reviewer, and de-registering
    # a reviewer must not leave an orphaned secret behind.
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pr_party_reviewer.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    reviewer: Mapped["PRPartyReviewer"] = relationship(back_populates="credential")

    def __repr__(self) -> str:
        # Deliberately excludes encrypted_token.
        return (
            f"<PRPartyCredential(reviewer_id={self.reviewer_id}, "
            f"expires_at={self.expires_at!r}, has_error={self.last_error is not None})>"
        )


class PRPartyPR(Base):
    """One row per pull request (KTD15).

    Column ownership, which the sweep-after-fold test in U4 enforces:

    - sweep / webhook: ``pr_node_id``, ``title``, ``author_*``, ``state``,
      ``head_sha``, ``mergeable_state``, ``checks_rollup``, ``missing_since``,
      ``updated_at_github``
    - brief worker: ``brief_*``, ``ready_at``, ``brewing_since``

    ``title`` is GitHub's own PR title, carried so the queue can name a card
    without borrowing brief prose — it is a poller fact, never brief content.
    """

    __tablename__ = "pr_party_pr"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # --- Identity (KTD15: (repo, number) is the natural key) ---
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # GraphQL node id — enrichment only; never part of the key.
    pr_node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Authorship (R18, R19) ---
    author_kind: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PRPartyAuthorKind.THIRD_PARTY,
        server_default="third_party",
    )
    author_github_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- PR facts (sweep/webhook-owned) ---
    # GitHub's PR title. Nullable because a row can predate the column and
    # because intake only writes what a payload actually carried; clients fall
    # back to ``{repo_full_name}#{pr_number}``, which is never absent.
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    # NULL means GitHub is still computing mergeability — not "unmergeable".
    mergeable_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    checks_rollup: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # C7: a PR that disappears from the sweep is aged out, not deleted on sight.
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at_github: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Brief (brief-worker-owned; R21: plain strings only, never markup) ---
    brief_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PRPartyBriefStatus.BREWING,
        server_default="brewing",
    )
    brief_what: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_why: Mapped[str | None] = mapped_column(Text, nullable=True)
    brief_decisions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    brief_links: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    brief_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    brewing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    # Same reasoning as PRPartyReviewer.actions: the database's ON DELETE CASCADE
    # owns this deletion, not an ORM null-out of a NOT NULL ``pr_id``.
    actions: Mapped[list["PRPartyAction"]] = relationship(back_populates="pr", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("repo_full_name", "pr_number", name="uq_pr_party_pr_repo_number"),
        # The sweep and the notifier both scan by lifecycle state.
        Index("ix_pr_party_pr_state", "state"),
        Index("ix_pr_party_pr_brief_status", "brief_status"),
    )

    def __repr__(self) -> str:
        return (
            f"<PRPartyPR({self.repo_full_name}#{self.pr_number}, state={self.state!r}, "
            f"head_sha={self.head_sha!r}, brief_status={self.brief_status!r})>"
        )


class PRPartyAction(Base):
    """One reviewer's actuation against one PR revision (KTD16, R25).

    The verdict, the GitHub review id it produced, and the head SHA it was cast
    against are a single atomic row — retirement of a card is derived from this
    record, never from file presence (R25).
    """

    __tablename__ = "pr_party_action"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pr_party_reviewer.id", ondelete="CASCADE"), nullable=False
    )
    pr_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pr_party_pr.id", ondelete="CASCADE"), nullable=False
    )
    # C1: a verdict cast at an old head SHA must not settle a new revision.
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    verdict: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # R26/C12: honored only as an explicit flag, recorded on the row.
    override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=PRPartyActionStatus.PENDING,
        server_default="pending",
    )
    # GitHub review ids exceed 32 bits.
    github_review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    reviewer: Mapped["PRPartyReviewer"] = relationship(back_populates="actions")
    pr: Mapped["PRPartyPR"] = relationship(back_populates="actions")

    __table_args__ = (
        # Every card read fans out from one PR to its action rows, and the FK's
        # ON DELETE CASCADE has to find them too — neither should seq-scan.
        Index("ix_pr_party_action_pr_id", "pr_id"),
        # KTD16: at most ONE live action per (reviewer, PR, revision, kind).
        # ``failed`` rows fall outside the predicate so a dead attempt never
        # wedges a retry (C6); re-opening a failed row rather than inserting a
        # second one is the endpoint's contract, not the index's.
        # R24: leading with reviewer_id means two reviewers never cross-block.
        Index(
            "uq_pr_party_action_live_fingerprint",
            "reviewer_id",
            "pr_id",
            "head_sha",
            "action_kind",
            unique=True,
            postgresql_where=text("status != 'failed'"),
        ),
        # Replay lookup by the client's Idempotency-Key. Not unique — the
        # uniqueness that matters is the fingerprint above.
        Index("ix_pr_party_action_idempotency_key", "idempotency_key"),
    )

    def __repr__(self) -> str:
        return (
            f"<PRPartyAction(reviewer_id={self.reviewer_id}, pr_id={self.pr_id}, "
            f"kind={self.action_kind!r}, status={self.status!r}, "
            f"head_sha={self.head_sha!r})>"
        )
