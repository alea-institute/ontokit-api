"""Real-PostgreSQL proofs for vector snapshot and ANN-index integrity."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.embedding import EmbeddingJob, EntityEmbedding, EntityEmbeddingStaging
from ontokit.models.project import Project
from ontokit.services.embedding_service import _staged_snapshot_activation


@pytest.mark.asyncio
async def test_staged_snapshot_activation_assigns_unique_ids(
    real_db_session: AsyncSession,
) -> None:
    project_id = uuid4()
    job_id = uuid4()
    real_db_session.add(Project(id=project_id, name="Embedding activation", owner_id="test-owner"))
    real_db_session.add(
        EmbeddingJob(
            id=job_id,
            project_id=project_id,
            branch="main",
            status="running",
        )
    )
    await real_db_session.flush()
    for suffix in ("First", "Second"):
        real_db_session.add(
            EntityEmbeddingStaging(
                job_id=job_id,
                project_id=project_id,
                branch="main",
                entity_iri=f"https://example.test/{suffix}",
                entity_type="class",
                label=suffix,
                embedding_text=suffix,
                embedding=[1.0, 0.0, 0.0],
                dimensions=3,
                provider="local",
                model_name="integration-vector",
            )
        )
    await real_db_session.commit()

    try:
        await real_db_session.execute(_staged_snapshot_activation(job_id))
        await real_db_session.commit()

        rows = (
            (
                await real_db_session.execute(
                    select(EntityEmbedding).where(EntityEmbedding.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert len({row.id for row in rows}) == 2
        assert {row.dimensions for row in rows} == {3}
    finally:
        await real_db_session.execute(delete(Project).where(Project.id == project_id))
        await real_db_session.commit()


@pytest.mark.asyncio
async def test_dimension_specific_hnsw_indexes_exist_and_are_valid(
    real_db_session: AsyncSession,
) -> None:
    rows = (
        await real_db_session.execute(
            text("""
                SELECT indexrelid::regclass::text AS index_name, indisvalid
                FROM pg_index
                WHERE indexrelid::regclass::text LIKE 'ix_entity_embeddings_hnsw_%'
                ORDER BY index_name
            """)
        )
    ).all()

    assert [(row.index_name, row.indisvalid) for row in rows] == [
        ("ix_entity_embeddings_hnsw_1024", True),
        ("ix_entity_embeddings_hnsw_1536", True),
        ("ix_entity_embeddings_hnsw_3072", True),
        ("ix_entity_embeddings_hnsw_384", True),
    ]
