"""Wave 0 stubs for embeddings index rebuild and cross-branch search — Plans 01, 03 (DEDUP-01, DEDUP-02, DEDUP-03)."""
import pytest


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plans 01/03")
def test_hnsw_index_creation_migration():
    """Alembic migration creates HNSW index on entity_embeddings.embedding column (DEDUP-02)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plans 01/03")
def test_all_branch_embedding_query():
    """Semantic similarity search returns candidates across all branches of a project (DEDUP-01)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plans 01/03")
def test_webhook_triggers_rebuild_job():
    """POST to embedding-rebuild webhook endpoint enqueues an ARQ background job (DEDUP-03)."""


@pytest.mark.skip(reason="Wave 0 stub — implementation in Plans 01/03")
def test_startup_freshness_check():
    """Stale embeddings index (entries missing for known entities) triggers background rebuild on startup (D-05)."""
