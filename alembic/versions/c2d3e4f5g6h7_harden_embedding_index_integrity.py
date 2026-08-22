"""Harden embedding refresh and ANN index integrity.

Revision ID: c2d3e4f5g6h7
Revises: a4b5c6d7e8f9
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5g6h7"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entity_embeddings", sa.Column("dimensions", sa.Integer(), nullable=True))
    op.execute("UPDATE entity_embeddings SET dimensions = vector_dims(embedding)")
    op.execute("""
        ALTER TABLE entity_embeddings
        ADD CONSTRAINT ck_entity_embeddings_dimensions
        CHECK (
            dimensions IS NOT NULL
            AND dimensions > 0
            AND dimensions <= 16000
            AND vector_dims(embedding) = dimensions
        ) NOT VALID
    """)
    op.execute("ALTER TABLE entity_embeddings VALIDATE CONSTRAINT ck_entity_embeddings_dimensions")
    # PostgreSQL can use the validated IS NOT NULL check to avoid a second
    # full-table validation scan when enforcing the column property.
    op.alter_column("entity_embeddings", "dimensions", nullable=False)

    op.create_table(
        "entity_embedding_staging",
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("embedding_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("entity_iri", sa.String(2000), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("label", sa.String(500), nullable=True),
        sa.Column("embedding_text", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint(
            "dimensions > 0 AND dimensions <= 16000",
            name="ck_entity_embedding_staging_dimensions",
        ),
    )
    op.execute("ALTER TABLE entity_embedding_staging ADD COLUMN embedding vector NOT NULL")
    op.create_check_constraint(
        "ck_entity_embedding_staging_vector_dimensions",
        "entity_embedding_staging",
        "vector_dims(embedding) = dimensions",
    )
    # Index DDL runs outside the migration transaction so writes remain
    # available. Capability checks fail explicitly instead of silently leaving
    # production without the ANN contract promised by this revision.
    with op.get_context().autocommit_block():
        op.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_am WHERE amname = 'hnsw') THEN
                    RAISE EXCEPTION 'pgvector HNSW access method is required';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_opclass c
                    JOIN pg_am a ON a.oid = c.opcmethod
                    WHERE a.amname = 'hnsw' AND c.opcname = 'vector_cosine_ops'
                ) THEN
                    RAISE EXCEPTION 'pgvector vector_cosine_ops HNSW opclass is required';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_opclass c
                    JOIN pg_am a ON a.oid = c.opcmethod
                    WHERE a.amname = 'hnsw' AND c.opcname = 'halfvec_cosine_ops'
                ) THEN
                    RAISE EXCEPTION 'pgvector halfvec_cosine_ops HNSW opclass is required';
                END IF;
            END $$
        """)
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entity_embeddings_hnsw")
        for dimensions in (384, 1024, 1536):
            op.execute(f"""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entity_embeddings_hnsw_{dimensions}
                ON entity_embeddings
                USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE dimensions = {dimensions}
            """)
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entity_embeddings_hnsw_3072
            ON entity_embeddings
            USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE dimensions = 3072
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for dimensions in (384, 1024, 1536, 3072):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ix_entity_embeddings_hnsw_{dimensions}")
    op.drop_table("entity_embedding_staging")
    op.drop_constraint(
        "ck_entity_embeddings_dimensions",
        "entity_embeddings",
        type_="check",
    )
    op.drop_column("entity_embeddings", "dimensions")
    # Restore the predecessor revision's best-effort index contract. The
    # predecessor allowed pgvector installations that cannot index an
    # unbounded vector column, so downgrade must preserve that guarded behavior.
    op.execute("""
        DO $$
        BEGIN
            CREATE INDEX IF NOT EXISTS ix_entity_embeddings_hnsw
            ON entity_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        EXCEPTION WHEN others THEN
            RAISE WARNING 'HNSW index restoration failed; preserving predecessor fallback. Error: %', SQLERRM;
        END $$
    """)
