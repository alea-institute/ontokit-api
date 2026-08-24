"""Add PR Party tables and relax the project columns notifications/audit share.

Four new tables (empty on arrival, so the enum-ish and boolean columns carry
server_defaults purely for consistency with the model) plus three NOT NULL
drops. Dropping NOT NULL is always safe on a populated table — no backfill, no
rewrite — so this migration is behaviorally inert until PR Party writes rows.

Structural intent worth restating here because it is what the DDL buys:
  * KTD16 — a partial UNIQUE index over (reviewer, pr, head_sha, action_kind)
    restricted to non-`failed` rows makes double actuation an integrity error
    rather than a second GitHub review.
  * R24  — that index leads with reviewer_id, so two reviewers hold
    independent action rows on the same PR revision.
  * KTD20 — a partial UNIQUE index makes `pr_party_ready` notifications
    once-per-revision, and nullable project columns let PR Party share the
    existing notifications table and the one bell endpoint.

Revision ID: h7i8j9k0l1m2
Revises: g6h7i8j9k0l1
Create Date: 2026-07-26
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "h7i8j9k0l1m2"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Reviewer registry (KTD12) ---
    # Provisioned by startup reconcile from PR_PARTY_REVIEWERS; no seed rows.
    op.create_table(
        "pr_party_reviewer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("zitadel_user_id", sa.String(length=255), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.Column("github_node_id", sa.String(length=255), nullable=True),
        sa.Column("merge_default", sa.String(length=20), server_default="manual", nullable=False),
        # Secret: never serialized into the capability payload.
        sa.Column("ntfy_topic", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Uniqueness rides the index rather than a separate UNIQUE constraint: the
    # model declares `unique=True, index=True`, which renders exactly one
    # unique index. A second, redundant UNIQUE constraint here would show up
    # forever as autogenerate drift.
    op.create_index(
        "ix_pr_party_reviewer_zitadel_user_id",
        "pr_party_reviewer",
        ["zitadel_user_id"],
        unique=True,
    )

    # --- Per-reviewer write PAT (KTD13) ---
    # Unique FK + CASCADE: one credential per reviewer, and de-registering a
    # reviewer cannot leave an orphaned secret behind.
    op.create_table(
        "pr_party_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reviewer_id"], ["pr_party_reviewer.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reviewer_id", name="uq_pr_party_credential_reviewer_id"),
    )

    # --- PR projection (KTD15) ---
    # Column ownership is enforced socially by the sweep/brief split; the schema
    # keeps the groups adjacent so an upsert that reaches across is obvious.
    op.create_table(
        "pr_party_pr",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        # Enrichment only — never part of the key.
        sa.Column("pr_node_id", sa.String(length=255), nullable=True),
        # R19: per-PR authorship classification (not per-reviewer).
        sa.Column(
            "author_kind", sa.String(length=20), server_default="third_party", nullable=False
        ),
        sa.Column("author_github_login", sa.String(length=255), nullable=True),
        sa.Column("author_node_id", sa.String(length=255), nullable=True),
        # GitHub's own PR title (poller-owned). Nullable: clients fall back to
        # `{repo}#{number}` for any row written before intake carried it.
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("state", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        # NULL = GitHub is still computing, not "unmergeable".
        sa.Column("mergeable_state", sa.String(length=30), nullable=True),
        sa.Column("checks_rollup", sa.String(length=30), nullable=True),
        # C7: aged out, not deleted on first absence from the sweep.
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_github", sa.DateTime(timezone=True), nullable=True),
        # Brief columns (R21: plain strings / JSON string lists, never markup).
        sa.Column("brief_status", sa.String(length=20), server_default="brewing", nullable=False),
        sa.Column("brief_what", sa.Text(), nullable=True),
        sa.Column("brief_why", sa.Text(), nullable=True),
        sa.Column("brief_decisions", sa.JSON(), nullable=True),
        sa.Column("brief_links", sa.JSON(), nullable=True),
        sa.Column("brief_truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("brewing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_full_name", "pr_number", name="uq_pr_party_pr_repo_number"),
    )
    op.create_index("ix_pr_party_pr_state", "pr_party_pr", ["state"])
    op.create_index("ix_pr_party_pr_brief_status", "pr_party_pr", ["brief_status"])

    # --- Per-reviewer action rows (KTD15/KTD16, R24, R25) ---
    op.create_table(
        "pr_party_action",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("pr_id", sa.Uuid(), nullable=False),
        # C1: the revision the verdict was cast against.
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("action_kind", sa.String(length=20), nullable=False),
        sa.Column("verdict", sa.String(length=30), nullable=True),
        # R26/C12: override is recorded, never inferred.
        sa.Column("override", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        # GitHub review ids exceed 32 bits.
        sa.Column("github_review_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["reviewer_id"], ["pr_party_reviewer.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pr_id"], ["pr_party_pr.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Card reads fan out from one PR to its actions, and the ON DELETE CASCADE
    # has to find the same rows — the FK column gets its own index (Postgres
    # does not create one for a foreign key the way it does for a PK).
    op.create_index("ix_pr_party_action_pr_id", "pr_party_action", ["pr_id"])
    # KTD16: at most ONE live action per (reviewer, PR, revision, kind).
    # `failed` rows fall outside the predicate so a dead attempt never wedges a
    # retry (C6). R24: leading with reviewer_id means reviewers never
    # cross-block on the same PR.
    op.create_index(
        "uq_pr_party_action_live_fingerprint",
        "pr_party_action",
        ["reviewer_id", "pr_id", "head_sha", "action_kind"],
        unique=True,
        postgresql_where=sa.text("status != 'failed'"),
    )
    # Replay lookup; uniqueness lives on the fingerprint above.
    op.create_index("ix_pr_party_action_idempotency_key", "pr_party_action", ["idempotency_key"])

    # --- KTD20: notifications and LLM audit shed their project requirement ---
    # Dropping NOT NULL never rewrites the table and never needs a backfill.
    op.alter_column("notifications", "project_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column(
        "notifications", "project_name", existing_type=sa.String(length=255), nullable=True
    )
    op.alter_column("llm_audit_logs", "project_id", existing_type=sa.Uuid(), nullable=True)

    # R22: one `pr_party_ready` per reviewer per PR revision, enforced by the
    # database rather than by a read-then-write in the notifier.
    op.create_index(
        "uq_notification_pr_party_ready",
        "notifications",
        ["user_id", "type", "target_id"],
        unique=True,
        postgresql_where=sa.text("type = 'pr_party_ready'"),
    )


def downgrade() -> None:
    op.drop_index("uq_notification_pr_party_ready", table_name="notifications")

    # Restoring NOT NULL requires the null rows to be gone first; PR Party rows
    # are the only source of them, and they go with the tables below. Alembic
    # runs statements in order, so delete them before re-tightening.
    op.execute(
        "DELETE FROM notifications WHERE type = 'pr_party_ready' "
        "AND (project_id IS NULL OR project_name IS NULL)"
    )
    op.execute("DELETE FROM llm_audit_logs WHERE project_id IS NULL AND endpoint LIKE 'pr-party/%'")

    op.alter_column("llm_audit_logs", "project_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column(
        "notifications", "project_name", existing_type=sa.String(length=255), nullable=False
    )
    op.alter_column("notifications", "project_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index("ix_pr_party_action_idempotency_key", table_name="pr_party_action")
    op.drop_index("uq_pr_party_action_live_fingerprint", table_name="pr_party_action")
    op.drop_index("ix_pr_party_action_pr_id", table_name="pr_party_action")
    op.drop_table("pr_party_action")

    op.drop_index("ix_pr_party_pr_brief_status", table_name="pr_party_pr")
    op.drop_index("ix_pr_party_pr_state", table_name="pr_party_pr")
    op.drop_table("pr_party_pr")

    op.drop_table("pr_party_credential")

    op.drop_index("ix_pr_party_reviewer_zitadel_user_id", table_name="pr_party_reviewer")
    op.drop_table("pr_party_reviewer")
