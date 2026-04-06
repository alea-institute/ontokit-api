"""Tests for embeddings index rebuild and cross-branch search — Plans 01, 03 (DEDUP-01, DEDUP-02, DEDUP-03)."""

import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


def test_hnsw_index_creation_migration():
    """Alembic migration creates HNSW index on entity_embeddings.embedding column (DEDUP-02)."""
    migration_dir = (
        pathlib.Path(__file__).parent.parent.parent / "alembic" / "versions"
    )
    migration_file = migration_dir / "v9w0x1y2z3a4_add_hnsw_index_and_duplicate_rejections.py"
    assert migration_file.exists(), f"HNSW migration not found: {migration_file}"

    content = migration_file.read_text()
    assert "hnsw" in content, "Migration does not mention HNSW index type"
    assert "vector_cosine_ops" in content, "Migration does not use vector_cosine_ops operator class"
    assert "entity_embeddings" in content, "Migration does not target entity_embeddings table"


async def test_all_branch_embedding_query():
    """Semantic similarity search returns candidates across all branches of a project (DEDUP-08)."""
    from ontokit.services.embedding_service import EmbeddingService

    project_id = uuid.uuid4()
    captured_sql: list[str] = []

    # Count result — non-zero to pass early-exit guard
    count_result = MagicMock()
    count_result.scalar.return_value = 10

    # Two rows from different branches
    row1 = MagicMock()
    row1.entity_iri = "http://example.org/A"
    row1.label = "Entity A"
    row1.entity_type = "class"
    row1.branch = "main"
    row1.deprecated = False
    row1.score = 0.85

    row2 = MagicMock()
    row2.entity_iri = "http://example.org/B"
    row2.label = "Entity B"
    row2.entity_type = "class"
    row2.branch = "suggestion/user/123"
    row2.deprecated = False
    row2.score = 0.72

    search_result = MagicMock()
    search_result.__iter__ = MagicMock(return_value=iter([row1, row2]))

    call_idx = 0

    async def execute_side_effect(stmt, params=None):
        nonlocal call_idx
        call_idx += 1
        if params is not None and "query_vec" in params:
            captured_sql.append(str(stmt))
            return search_result
        return count_result

    mock_db = MagicMock()
    mock_db.execute = execute_side_effect

    mock_provider = AsyncMock()
    mock_provider.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

    service = EmbeddingService(mock_db)

    with patch.object(service, "_get_provider", AsyncMock(return_value=mock_provider)):
        results = await service.semantic_search_all_branches(
            project_id=project_id,
            query="legal entity",
            limit=10,
            threshold=0.5,
        )

    # Results include both branches
    assert len(results) == 2
    branches = {r.branch for r in results}
    assert "main" in branches
    assert "suggestion/user/123" in branches

    # No branch filter in the SQL
    for sql in captured_sql:
        assert "AND branch" not in sql, "all_branches query must NOT filter by branch"
        assert ":br" not in sql, "all_branches query must NOT use :br parameter"


async def test_webhook_triggers_rebuild_job():
    """Webhook merge event enqueues an ARQ background job for embedding rebuild (DEDUP-03)."""
    from ontokit.services.pull_request_service import PullRequestService

    project_id = uuid.uuid4()
    default_branch = "main"
    merged_branch = "suggestion/feature/abc"

    pr_data = {
        "number": 42,
        "merged": True,
        "head": {"ref": merged_branch},
        "base": {"ref": default_branch},
    }

    # PR lookup — no existing PR record
    pr_result = MagicMock()
    pr_result.scalar_one_or_none.return_value = None

    # EmbeddingJob active check — no active job
    job_scalars = MagicMock()
    job_scalars.first.return_value = None
    job_result_mock = MagicMock()
    job_result_mock.scalars.return_value = job_scalars

    call_idx = 0

    async def execute_side_effect(stmt, params=None):
        nonlocal call_idx
        call_idx += 1
        if call_idx <= 1:
            return pr_result
        return job_result_mock

    mock_db = MagicMock()
    mock_db.execute = execute_side_effect
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()

    # ARQ pool mock
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock()

    # EmbeddingService cleanup mock — returned when EmbeddingService() is constructed
    mock_emb_instance = AsyncMock()
    mock_emb_instance.cleanup_merged_branch_embeddings = AsyncMock(return_value=5)

    # Integration (sync_enabled=True)
    integration = MagicMock()
    integration.sync_enabled = True

    service = PullRequestService(mock_db)

    with patch.object(service, "_get_github_integration", AsyncMock(return_value=integration)):
        # Patch EmbeddingService at the module where it is imported from
        with patch(
            "ontokit.services.embedding_service.EmbeddingService",
            return_value=mock_emb_instance,
        ):
            # Patch get_arq_pool at the module level it lives in
            with patch(
                "ontokit.api.utils.redis.get_arq_pool",
                new_callable=AsyncMock,
                return_value=mock_pool,
            ):
                await service.handle_github_pr_webhook(project_id, "closed", pr_data)

    # Cleanup called for merged branch
    mock_emb_instance.cleanup_merged_branch_embeddings.assert_called_once_with(
        project_id, merged_branch
    )

    # enqueue_job called with correct args
    mock_pool.enqueue_job.assert_called_once()
    call_args = mock_pool.enqueue_job.call_args
    assert call_args[0][0] == "run_embedding_generation_task"
    assert call_args[0][1] == str(project_id)
    assert call_args[0][2] == default_branch


async def test_startup_freshness_check():
    """Stale embeddings index triggers background rebuild on startup (D-05, DEDUP-01)."""
    from ontokit.services.startup_checks import check_and_trigger_embedding_rebuilds

    project_id = uuid.uuid4()

    # Config: auto_embed=True, last_full_embed_at 48h ago (stale)
    mock_config = MagicMock()
    mock_config.project_id = project_id
    mock_config.auto_embed_on_save = True
    mock_config.last_full_embed_at = datetime.now(UTC) - timedelta(hours=48)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock()

    # Embedding count > 0 (has embeddings but stale)
    count_scalar = MagicMock()
    count_scalar.scalar.return_value = 100

    # No active job
    active_scalars = MagicMock()
    active_scalars.first.return_value = None
    active_job_result = MagicMock()
    active_job_result.scalars.return_value = active_scalars

    # All configs
    configs_scalars = MagicMock()
    configs_scalars.all.return_value = [mock_config]
    configs_result = MagicMock()
    configs_result.scalars.return_value = configs_scalars

    call_idx = 0

    async def execute_side_effect(stmt):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return configs_result
        elif call_idx == 2:
            return count_scalar
        else:
            return active_job_result

    mock_db = MagicMock()
    mock_db.execute = execute_side_effect
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "ontokit.services.startup_checks.async_session_maker",
            return_value=mock_db,
        ),
        # Patch get_arq_pool where it lives so the lazy import resolves to it
        patch(
            "ontokit.api.utils.redis.get_arq_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ),
    ):
        await check_and_trigger_embedding_rebuilds()

    # Rebuild was enqueued
    mock_pool.enqueue_job.assert_called_once()
    call_args = mock_pool.enqueue_job.call_args
    assert call_args[0][0] == "run_embedding_generation_task"
    assert call_args[0][1] == str(project_id)


async def test_startup_first_time_embed_no_auto_embed():
    """First-time embed triggers even when auto_embed_on_save=False (DEDUP-01)."""
    from ontokit.services.startup_checks import check_and_trigger_embedding_rebuilds

    project_id = uuid.uuid4()

    # Config: auto_embed=False (would skip stale check) but ZERO embeddings → first-time path
    mock_config = MagicMock()
    mock_config.project_id = project_id
    mock_config.auto_embed_on_save = False
    mock_config.last_full_embed_at = None

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock()

    # Embedding count == 0 → first-time full embed
    count_scalar = MagicMock()
    count_scalar.scalar.return_value = 0

    # No active job
    active_scalars = MagicMock()
    active_scalars.first.return_value = None
    active_job_result = MagicMock()
    active_job_result.scalars.return_value = active_scalars

    # All configs
    configs_scalars = MagicMock()
    configs_scalars.all.return_value = [mock_config]
    configs_result = MagicMock()
    configs_result.scalars.return_value = configs_scalars

    call_idx = 0

    async def execute_side_effect(stmt):
        nonlocal call_idx
        call_idx += 1
        if call_idx == 1:
            return configs_result
        elif call_idx == 2:
            return count_scalar
        else:
            return active_job_result

    mock_db = MagicMock()
    mock_db.execute = execute_side_effect
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "ontokit.services.startup_checks.async_session_maker",
            return_value=mock_db,
        ),
        patch(
            "ontokit.api.utils.redis.get_arq_pool",
            new_callable=AsyncMock,
            return_value=mock_pool,
        ),
    ):
        await check_and_trigger_embedding_rebuilds()

    # Rebuild IS enqueued even with auto_embed_on_save=False
    mock_pool.enqueue_job.assert_called_once()
    call_args = mock_pool.enqueue_job.call_args
    assert call_args[0][0] == "run_embedding_generation_task"
    assert call_args[0][1] == str(project_id)
