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
    op.alter_column("entity_embeddings", "dimensions", nullable=False)
    op.create_check_constraint(
        "ck_entity_embeddings_dimensions",
        "entity_embeddings",
        "dimensions > 0 AND dimensions <= 16000 AND vector_dims(embedding) = dimensions",
    )

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
    # The legacy migration swallowed an invalid index on a dimensionless vector
    # column. Replace it with real expression indexes whose partial predicates
    # match the dimensions filter used by search. Fail the migration if the
    # installed pgvector cannot provide the promised ANN capability.
    op.execute("DROP INDEX IF EXISTS ix_entity_embeddings_hnsw")
    for dimensions in (384, 1024, 1536):
        op.execute(f"""
            CREATE INDEX ix_entity_embeddings_hnsw_{dimensions}
            ON entity_embeddings
            USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE dimensions = {dimensions}
        """)
    op.execute("""
        CREATE INDEX ix_entity_embeddings_hnsw_3072
        ON entity_embeddings
        USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE dimensions = 3072
    """)


def downgrade() -> None:
    for dimensions in (384, 1024, 1536, 3072):
        op.execute(f"DROP INDEX IF EXISTS ix_entity_embeddings_hnsw_{dimensions}")
    op.drop_table("entity_embedding_staging")
    op.drop_constraint(
        "ck_entity_embeddings_dimensions",
        "entity_embeddings",
        type_="check",
    )
    op.drop_column("entity_embeddings", "dimensions")
