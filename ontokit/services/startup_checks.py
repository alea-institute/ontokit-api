"""Startup health checks — run once during app lifespan initialization."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ontokit.core.database import async_session_maker
from ontokit.models.embedding import EmbeddingJob, EntityEmbedding, ProjectEmbeddingConfig

logger = logging.getLogger(__name__)

# If last full embed is older than this, trigger rebuild
STALE_THRESHOLD = timedelta(hours=24)


async def check_and_trigger_embedding_rebuilds() -> None:
    """Check all projects with EmbeddingConfig for stale/missing embedding indexes and enqueue rebuilds.

    Called during app startup (D-05, DEDUP-01). Handles two cases:

    1. **First-time full embed**: Projects that have an EmbeddingConfig but zero embeddings
       get a full rebuild regardless of auto_embed_on_save setting. This ensures DEDUP-01
       (pre-compute embeddings for all existing classes and properties) is satisfied when a
       project owner first configures their embedding provider.

    2. **Stale index rebuild**: Projects with auto_embed_on_save=True whose last_full_embed_at
       is None or older than STALE_THRESHOLD get a rebuild to keep the index fresh.
    """
    async with async_session_maker() as db:
        # Query ALL projects with an EmbeddingConfig (not just auto_embed_on_save=True)
        all_configs = (await db.execute(select(ProjectEmbeddingConfig))).scalars().all()

        now = datetime.now(UTC)
        rebuild_count = 0

        for config in all_configs:
            # Check if this project has ANY embeddings at all
            embedding_count = (
                await db.execute(
                    select(func.count()).select_from(EntityEmbedding).where(
                        EntityEmbedding.project_id == config.project_id,
                    )
                )
            ).scalar() or 0

            needs_rebuild = False
            reason = ""

            if embedding_count == 0:
                # Case 1: First-time full embed (DEDUP-01) — no embeddings exist yet
                needs_rebuild = True
                reason = "first-time full embed (zero embeddings)"
            elif config.auto_embed_on_save:
                # Case 2: Stale index check for auto-embed projects
                is_stale = config.last_full_embed_at is None or (
                    now - config.last_full_embed_at
                ) > STALE_THRESHOLD
                if is_stale:
                    needs_rebuild = True
                    reason = "stale index (last_full_embed_at too old or None)"

            if not needs_rebuild:
                continue

            # Check for active job
            active = (
                await db.execute(
                    select(EmbeddingJob).where(
                        EmbeddingJob.project_id == config.project_id,
                        EmbeddingJob.status.in_(["pending", "processing"]),
                    )
                )
            ).scalars().first()

            if active:
                logger.info(
                    "Project %s has active embedding job, skipping startup rebuild",
                    config.project_id,
                )
                continue

            # Enqueue rebuild
            try:
                import uuid

                from ontokit.api.utils.redis import get_arq_pool

                job_id = uuid.uuid4()
                new_job = EmbeddingJob(
                    id=job_id,
                    project_id=config.project_id,
                    branch="main",
                    status="pending",
                )
                db.add(new_job)
                await db.commit()

                pool = await get_arq_pool()
                await pool.enqueue_job(
                    "run_embedding_generation_task",
                    str(config.project_id),
                    "main",
                    str(job_id),
                )
                rebuild_count += 1
                logger.info(
                    "Startup: enqueued embedding rebuild for project %s (%s)",
                    config.project_id,
                    reason,
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue startup rebuild for project %s", config.project_id
                )

        if rebuild_count:
            logger.info("Startup: triggered %d embedding rebuild(s)", rebuild_count)
        else:
            logger.info("Startup: all embedding indexes are fresh")
