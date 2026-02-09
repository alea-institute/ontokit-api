"""ARQ worker for background task processing."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from arq import ArqRedis
from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.lint import LintIssue, LintRun, LintRunStatus
from app.models.project import Project
from app.services.linter import LintResult, get_linter
from app.services.ontology import get_ontology_service
from app.services.storage import get_storage_service

logger = logging.getLogger(__name__)

# Redis pubsub channel for lint updates
LINT_UPDATES_CHANNEL = "lint:updates"


async def run_lint_task(
    ctx: dict[str, Any],
    project_id: str,
) -> dict[str, Any]:
    """
    Background task to lint an ontology project.

    Args:
        ctx: ARQ context with db session and services
        project_id: The project UUID to lint

    Returns:
        Dict with run_id and issues_found
    """
    db: AsyncSession = ctx["db"]
    redis: ArqRedis = ctx["redis"]

    project_uuid = UUID(project_id)
    run: LintRun | None = None

    try:
        # Verify project exists and get its details
        result = await db.execute(
            select(Project).where(Project.id == project_uuid)
        )
        project = result.scalar_one_or_none()

        if not project:
            raise ValueError(f"Project {project_id} not found")

        if not project.source_file_path:
            raise ValueError(f"Project {project_id} has no ontology file")

        # Create lint run record
        run = LintRun(
            project_id=project_uuid,
            status=LintRunStatus.RUNNING.value,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        run_id = run.id
        logger.info(f"Started lint run {run_id} for project {project_id}")

        # Notify via pubsub that lint has started
        await redis.publish(
            LINT_UPDATES_CHANNEL,
            f'{{"type": "lint_started", "project_id": "{project_id}", "run_id": "{run_id}"}}',
        )

        # Load ontology from storage
        storage = get_storage_service()
        ontology_service = get_ontology_service(storage)

        graph = await ontology_service.load_from_storage(
            project_uuid,
            project.source_file_path,
        )

        # Run linting
        linter = get_linter()
        lint_results: list[LintResult] = await linter.lint(graph, project_uuid)

        # Save issues to database
        for result in lint_results:
            issue = LintIssue(
                run_id=run_id,
                project_id=project_uuid,
                issue_type=result.issue_type,
                rule_id=result.rule_id,
                message=result.message,
                subject_iri=result.subject_iri,
                details=result.details,
            )
            db.add(issue)

        # Update run status
        run.status = LintRunStatus.COMPLETED.value
        run.completed_at = datetime.now(timezone.utc)
        run.issues_found = len(lint_results)

        await db.commit()

        logger.info(
            f"Completed lint run {run_id} for project {project_id}: "
            f"found {len(lint_results)} issues"
        )

        # Notify via pubsub that lint is complete
        await redis.publish(
            LINT_UPDATES_CHANNEL,
            f'{{"type": "lint_complete", "project_id": "{project_id}", '
            f'"run_id": "{run_id}", "issues_found": {len(lint_results)}}}',
        )

        return {
            "run_id": str(run_id),
            "issues_found": len(lint_results),
            "status": "completed",
        }

    except Exception as e:
        logger.exception(f"Lint run failed for project {project_id}: {e}")

        # Update run status to failed
        if run:
            run.status = LintRunStatus.FAILED.value
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = str(e)
            await db.commit()

            # Notify via pubsub that lint failed
            await redis.publish(
                LINT_UPDATES_CHANNEL,
                f'{{"type": "lint_failed", "project_id": "{project_id}", '
                f'"run_id": "{run.id}", "error": "{str(e)}"}}',
            )

        raise


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize worker context on startup."""
    logger.info("Starting ARQ worker...")

    # Create database engine and session factory
    engine = create_async_engine(
        str(settings.database_url),
        echo=settings.debug,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Store in context
    ctx["engine"] = engine
    ctx["session_factory"] = session_factory

    logger.info("ARQ worker started successfully")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Cleanup worker context on shutdown."""
    logger.info("Shutting down ARQ worker...")

    # Close database engine
    engine = ctx.get("engine")
    if engine:
        await engine.dispose()

    logger.info("ARQ worker shut down successfully")


async def on_job_start(ctx: dict[str, Any]) -> None:
    """Called before each job starts. Creates a new database session."""
    session_factory = ctx["session_factory"]
    ctx["db"] = session_factory()


async def on_job_end(ctx: dict[str, Any]) -> None:
    """Called after each job ends. Closes the database session."""
    db = ctx.get("db")
    if db:
        await db.close()


def get_redis_settings() -> RedisSettings:
    """Get Redis settings from application config."""
    # Parse Redis URL
    redis_url = str(settings.redis_url)
    # RedisSettings expects host, port, database separately
    # URL format: redis://host:port/db

    from urllib.parse import urlparse

    parsed = urlparse(redis_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    database = int(parsed.path.lstrip("/") or "0")

    return RedisSettings(
        host=host,
        port=port,
        database=database,
    )


class WorkerSettings:
    """ARQ worker settings."""

    functions = [run_lint_task]
    redis_settings = get_redis_settings()

    on_startup = startup
    on_shutdown = shutdown
    on_job_start = on_job_start
    on_job_end = on_job_end

    # Job settings
    max_jobs = 10
    job_timeout = 300  # 5 minutes
    keep_result = 3600  # 1 hour
    poll_delay = 0.5

    # Queue name
    queue_name = "arq:queue"
