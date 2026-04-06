"""Add LLM config and audit tables, and can_self_merge_structural to project_members.

Revision ID: u9v0w1x2y3a4
Revises: t8u9v0w1x2y3
Create Date: 2026-04-06

Adds:
- project_llm_configs table (per-project LLM provider configuration with encrypted key)
- llm_audit_logs table (metadata-only audit trail for every LLM call)
- can_self_merge_structural column on project_members (per-user permission override)
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "u9v0w1x2y3a4"
down_revision = "t8u9v0w1x2y3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── project_llm_configs ──────────────────────────────────────────────────
    op.create_table(
        "project_llm_configs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("model_tier", sa.String(20), nullable=False, server_default="quality"),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
        sa.Column("daily_cap_usd", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
    )

    # ── llm_audit_logs ───────────────────────────────────────────────────────
    op.create_table(
        "llm_audit_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("endpoint", sa.String(200), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_estimate_usd", sa.Float(), nullable=False),
        sa.Column(
            "is_byo_key", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_audit_project_date", "llm_audit_logs", ["project_id", "created_at"]
    )
    op.create_index(
        "ix_llm_audit_project_user", "llm_audit_logs", ["project_id", "user_id"]
    )

    # ── project_members: add can_self_merge_structural ────────────────────────
    op.add_column(
        "project_members",
        sa.Column(
            "can_self_merge_structural",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_members", "can_self_merge_structural")
    op.drop_index("ix_llm_audit_project_user", table_name="llm_audit_logs")
    op.drop_index("ix_llm_audit_project_date", table_name="llm_audit_logs")
    op.drop_table("llm_audit_logs")
    op.drop_table("project_llm_configs")
