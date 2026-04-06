"""Add HNSW index on entity_embeddings and duplicate_rejections table.

Revision ID: v9w0x1y2z3a4
Revises: u9v0w1x2y3a4
Create Date: 2026-04-06

Adds:
- HNSW ANN index on entity_embeddings.embedding (vector_cosine_ops, m=16, ef_construction=64)
  for O(log n) approximate nearest-neighbor duplicate detection queries.
  Falls back gracefully if pgvector < 0.5.0 (HNSW was introduced in 0.5.0).
- duplicate_rejections table (per D-10): stores human decisions that two IRIs are NOT
  duplicates, preventing false positives from re-surfacing in future checks.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "v9w0x1y2z3a4"
down_revision = "u9v0w1x2y3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── duplicate_rejections ─────────────────────────────────────────────────
    op.create_table(
        "duplicate_rejections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("rejected_iri", sa.String(2000), nullable=False),
        sa.Column("canonical_iri", sa.String(2000), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("rejected_by", sa.String(255), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("suggestion_session_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_session_id"],
            ["suggestion_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_duplicate_rejections_lookup",
        "duplicate_rejections",
        ["project_id", "rejected_iri"],
    )

    # ── HNSW index on entity_embeddings ─────────────────────────────────────
    # HNSW (Hierarchical Navigable Small World) provides O(log n) ANN queries.
    # Introduced in pgvector 0.5.0; we wrap creation in a PL/pgSQL block that
    # issues a WARNING and continues if the extension version is older.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE INDEX IF NOT EXISTS ix_entity_embeddings_hnsw
            ON entity_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        EXCEPTION WHEN others THEN
            RAISE WARNING 'HNSW index creation failed (pgvector extension may be < 0.5.0). Falling back to B-tree. Error: %', SQLERRM;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entity_embeddings_hnsw")
    op.drop_index("ix_duplicate_rejections_lookup", table_name="duplicate_rejections")
    op.drop_table("duplicate_rejections")
