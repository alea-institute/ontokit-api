"""Add contribution trust ladder tables and columns.

Every column is additive with a server_default, so this migration is safe on a
populated table: existing members land on the untrusted rung and existing
projects land with auto-accept OFF (KTD8) — behaviorally unchanged.

Revision ID: w0x1y2z3a4b5
Revises: t8u9v0w1x2y3
Create Date: 2026-07-24
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "w0x1y2z3a4b5"
down_revision = "t8u9v0w1x2y3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Project-level trust settings (R6, R11) ---
    op.add_column(
        "projects",
        sa.Column("trust_promotion_threshold", sa.Integer(), server_default="5", nullable=False),
    )
    op.add_column(
        "projects",
        sa.Column("auto_accept_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "projects",
        sa.Column("auto_accept_quiet_days", sa.Integer(), server_default="7", nullable=False),
    )

    # --- Per-project membership trust grant (R4, R6; KTD1) ---
    op.add_column(
        "project_members",
        sa.Column("is_trusted", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "project_members",
        sa.Column("trust_override", sa.String(length=20), server_default="none", nullable=False),
    )
    op.add_column(
        "project_members",
        sa.Column("trust_granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_members",
        sa.Column("trust_granted_by", sa.String(length=255), nullable=True),
    )

    # --- Suggestion session trust fields (R10, R11, R12, R13) ---
    op.add_column(
        "suggestion_sessions",
        sa.Column("is_llm_generated", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "suggestion_sessions",
        sa.Column("auto_accept_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "suggestion_sessions",
        sa.Column("auto_accept_halted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "suggestion_sessions",
        sa.Column("verification_passed", sa.Boolean(), server_default="false", nullable=False),
    )

    # --- Append-only outcome log (R5; KTD2) ---
    op.create_table(
        "suggestion_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column(
            "counts_toward_promotion", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("is_anonymous", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["suggestion_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_suggestion_outcomes_project_user",
        "suggestion_outcomes",
        ["project_id", "user_id"],
    )
    # Partial index: the promotion count is the only hot query.
    op.create_index(
        "ix_suggestion_outcomes_promotion",
        "suggestion_outcomes",
        ["project_id", "user_id"],
        postgresql_where=sa.text("outcome = 'accepted' AND counts_toward_promotion"),
    )

    # --- Opt-in commit-authoring identity (R14, R15) ---
    op.create_table(
        "user_commit_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("commit_email", sa.String(length=320), nullable=True),
        sa.Column("commit_email_verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("use_verified_email", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_commit_identity_user"),
    )
    op.create_index(
        "ix_user_commit_identities_user_id", "user_commit_identities", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_user_commit_identities_user_id", table_name="user_commit_identities")
    op.drop_table("user_commit_identities")

    op.drop_index("ix_suggestion_outcomes_promotion", table_name="suggestion_outcomes")
    op.drop_index("ix_suggestion_outcomes_project_user", table_name="suggestion_outcomes")
    op.drop_table("suggestion_outcomes")

    op.drop_column("suggestion_sessions", "verification_passed")
    op.drop_column("suggestion_sessions", "auto_accept_halted_at")
    op.drop_column("suggestion_sessions", "auto_accept_after")
    op.drop_column("suggestion_sessions", "is_llm_generated")

    op.drop_column("project_members", "trust_granted_by")
    op.drop_column("project_members", "trust_granted_at")
    op.drop_column("project_members", "trust_override")
    op.drop_column("project_members", "is_trusted")

    op.drop_column("projects", "auto_accept_quiet_days")
    op.drop_column("projects", "auto_accept_enabled")
    op.drop_column("projects", "trust_promotion_threshold")
