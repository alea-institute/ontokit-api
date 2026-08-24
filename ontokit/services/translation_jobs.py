"""Commit-diff discovery and bounded ARQ fan-out for machine translations.

The Redis counter covers queued plus running per-entity jobs for one project. Reservation is
atomic, rejected reservations are rolled back, enqueue failures release unqueued slots, and each
worker releases its slot in ``finally``. ``mode=batch`` is preserved in every payload as the U7
provider-batch seam; until that adapter lands, execution uses the existing standard service path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pygit2
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS, SKOS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.git import GitRepositoryService, get_git_service
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.project import Project, get_git_ontology_path
from ontokit.models.translation import (
    ProjectTranslationConfig,
    TranslationJob,
    TranslationRecord,
    hash_literal_value,
)
from ontokit.services.llm import check_llm_access, consume_rate_limit_units
from ontokit.services.translation_annotations import read_annotation, translation_record_digest
from ontokit.services.translation_backfill import BackfillLiteral, select_backfill_literals
from ontokit.services.translation_service import TranslationService

logger = logging.getLogger(__name__)

AUTO_PREDICATES = frozenset((RDFS.label, SKOS.prefLabel, SKOS.altLabel))
SCOPED_PREDICATES = {SKOS.definition: "translate_definitions", SKOS.example: "translate_examples"}
MAX_PENDING_TRANSLATIONS_PER_PROJECT = 100
# Five default ARQ attempts can each consume the worker's five-minute timeout.
# Keep the release receipt beyond that retry window, but below the worker's
# one-hour result retention so a later legitimate reuse of the deterministic
# job ID starts after its old exactly-once receipt has expired.
RELEASE_RECEIPT_TTL_SECONDS = 45 * 60


@dataclass(frozen=True, slots=True)
class TranslationTask:
    entity_iri: str
    predicate: str
    source_value: str | None
    source_language: str | None
    target_language: str | None
    mode: str


class TranslationEnqueueError(RuntimeError):
    """Queue failure carrying how much of the logical batch is already durable."""

    def __init__(
        self,
        message: str,
        *,
        queue_unavailable: bool = False,
    ) -> None:
        super().__init__(message)
        self.queue_unavailable = queue_unavailable


def discover_translation_tasks(
    parent: Graph,
    current: Graph,
    config: ProjectTranslationConfig,
    covered: set[tuple[str, str, str, str]],
) -> list[TranslationTask]:
    """Return newly-added source literals; replacement edits deliberately produce no work."""
    if not config.language_tags:
        return []
    predicates = set(AUTO_PREDICATES)
    predicates.update(
        predicate for predicate, flag in SCOPED_PREDICATES.items() if bool(getattr(config, flag))
    )
    tasks: list[TranslationTask] = []
    for subject, predicate, literal in current - parent:
        if (
            predicate not in predicates
            or not isinstance(subject, URIRef)
            or not isinstance(literal, Literal)
        ):
            continue
        # A remove+add at the same entity/predicate is an edit, not a mint.
        if any(True for _ in (parent - current).triples((subject, predicate, None))):
            continue
        source_language = literal.language
        prompt_language = source_language or "und"
        for language in config.language_tags:
            if language.casefold() == prompt_language.casefold():
                continue
            key = (str(subject), str(predicate), str(literal), language)
            if key not in covered:
                tasks.append(
                    TranslationTask(
                        str(subject),
                        str(predicate),
                        str(literal),
                        source_language,
                        language,
                        config.speed_mode,
                    )
                )
    return tasks


def _pending_key(project_id: UUID) -> str:
    return f"translation:pending:{project_id}"


def translation_entity_job_id(
    project_id: UUID,
    branch: str,
    task: TranslationTask,
    *,
    commit_hash: str | None = None,
) -> str:
    """Return a deterministic identity isolated across projects and branches."""
    identity = ":".join(
        (
            str(project_id),
            branch,
            commit_hash or "on-demand",
            task.entity_iri,
            task.predicate,
            hash_literal_value(task.source_value or ""),
            task.target_language or "all",
        )
    )
    return f"translation-entity:{hash_literal_value(identity)}"


def _provider_call_units(config: ProjectTranslationConfig, task_count: int) -> int:
    calls_per_task = 4 if config.verification_mechanism == "consensus" else 2
    return task_count * calls_per_task


async def enqueue_translation_tasks(
    pool: Any,
    redis: Any,
    project_id: UUID,
    branch: str,
    actor_id: str,
    tasks: list[TranslationTask],
    *,
    commit_hash: str | None = None,
) -> list[str]:
    """Atomically reserve bounded project fan-out before any paid work is queued."""
    if not tasks:
        return []
    reserved = len(tasks)
    pending = int(await redis.incrby(_pending_key(project_id), reserved))
    if pending > MAX_PENDING_TRANSLATIONS_PER_PROJECT:
        await redis.decrby(_pending_key(project_id), reserved)
        raise TranslationEnqueueError("project translation fan-out cap reached")
    job_ids: list[str] = []
    newly_queued = 0
    owned_slots = reserved
    try:
        for task in tasks:
            requested_job_id = translation_entity_job_id(
                project_id, branch, task, commit_hash=commit_hash
            )
            job = await pool.enqueue_job(
                "run_translation_entity_task",
                str(project_id),
                branch,
                task.entity_iri,
                task.predicate,
                task.source_value,
                task.source_language,
                task.target_language,
                actor_id,
                task.mode,
                _job_id=requested_job_id,
            )
            if job is None:
                # ARQ returns None when this deterministic job already exists.
                # That existing job owns the original pending slot; release only
                # the duplicate reservation made by this invocation.
                await redis.decrby(_pending_key(project_id), 1)
                owned_slots -= 1
                job_ids.append(requested_job_id)
            else:
                newly_queued += 1
                job_ids.append(str(job.job_id))
    except Exception as exc:
        unqueued_slots = owned_slots - newly_queued
        if unqueued_slots:
            await redis.decrby(_pending_key(project_id), unqueued_slots)
        raise TranslationEnqueueError(
            "translation queue failed to accept the batch",
            queue_unavailable=True,
        ) from exc
    return job_ids


async def enqueue_label_diff_after_commit(
    *, project_id: UUID, branch: str, commit_hash: str, actor_id: str, role: str
) -> bool:
    """Apply the shared LLM role/rate gate before queueing a post-commit trigger."""
    if not check_llm_access(role, is_anonymous=False):
        return False
    from ontokit.api.utils.redis import get_arq_pool

    pool = await get_arq_pool()
    if pool is None:
        return False
    identity = hash_literal_value(f"{project_id}:{branch}:{commit_hash}")
    job = await pool.enqueue_job(
        "run_translation_label_diff_task",
        str(project_id),
        branch,
        commit_hash,
        actor_id,
        role,
        _job_id=f"translation-label-diff:{identity}",
    )
    return job is not None


async def _covered_values(
    db: AsyncSession, project_id: UUID, graph: Graph
) -> set[tuple[str, str, str, str]]:
    result = await db.execute(
        select(TranslationRecord).where(TranslationRecord.project_id == project_id)
    )
    covered: set[tuple[str, str, str, str]] = set()
    for record in result.scalars().all():
        if record.source_value is None or record.proposed_value is None:
            continue
        subject, predicate = URIRef(record.entity_iri), URIRef(record.predicate)
        translated = Literal(record.proposed_value, lang=record.language)
        annotation = read_annotation(graph, subject, predicate, translated)
        if (
            hash_literal_value(record.source_value) == record.source_value_hash
            and annotation is not None
            and annotation.record_digest == translation_record_digest(record)
        ):
            covered.add((record.entity_iri, record.predicate, record.source_value, record.language))
    return covered


async def run_label_diff_job(
    ctx: dict[str, Any],
    project_id: str,
    branch: str,
    commit_hash: str,
    actor_id: str,
    role: str,
) -> dict[str, Any]:
    db: AsyncSession = ctx["db"]
    redis = ctx["redis"]
    git: GitRepositoryService = get_git_service()
    project_uuid = UUID(project_id)
    config = await db.scalar(
        select(ProjectTranslationConfig).where(ProjectTranslationConfig.project_id == project_uuid)
    )
    if config is None or not config.language_tags:
        return {"queued": 0}
    project = await db.scalar(
        select(Project)
        .options(selectinload(Project.github_integration))
        .where(Project.id == project_uuid)
    )
    if project is None:
        raise RuntimeError("translation project not found")
    filename = get_git_ontology_path(project)
    repository = git.get_repository(project_uuid)
    commit = cast(pygit2.Commit, repository.repo.revparse_single(commit_hash))
    current = Graph().parse(data=repository.read_file(commit_hash, filename), format="turtle")
    parent = Graph()
    if commit.parents:
        parent.parse(
            data=repository.read_file(str(commit.parents[0].id), filename), format="turtle"
        )
    tasks = discover_translation_tasks(
        parent, current, config, await _covered_values(db, project_uuid, current)
    )
    if tasks:
        reservation_id = hash_literal_value(
            f"translation-label-diff:{project_id}:{branch}:{commit_hash}:{actor_id}"
        )
        if not await consume_rate_limit_units(
            redis,
            project_id,
            actor_id,
            role,
            _provider_call_units(config, len(tasks)),
            reservation_id=reservation_id,
        ):
            return {"queued": 0, "job_ids": [], "rate_limited": True}
    job_ids = await enqueue_translation_tasks(
        redis, redis, project_uuid, branch, actor_id, tasks, commit_hash=commit_hash
    )
    return {"queued": len(job_ids), "job_ids": job_ids}


async def run_translation_entity_job(
    ctx: dict[str, Any],
    project_id: str,
    branch: str,
    entity_iri: str,
    predicate: str,
    source_value: str | None,
    source_language: str | None,
    target_language: str | None,
    actor_id: str,
    mode: str,
    *,
    release_slot: bool = True,
) -> dict[str, Any]:
    db: AsyncSession = ctx["db"]
    project_uuid = UUID(project_id)
    try:
        config = await db.scalar(
            select(ProjectTranslationConfig).where(
                ProjectTranslationConfig.project_id == project_uuid
            )
        )
        llm_config = await db.scalar(
            select(ProjectLLMConfig).where(ProjectLLMConfig.project_id == project_uuid)
        )
        project = await db.scalar(
            select(Project)
            .options(selectinload(Project.github_integration))
            .where(Project.id == project_uuid)
        )
        if config is None or llm_config is None or project is None:
            raise RuntimeError("translation configuration is incomplete")
        filename = get_git_ontology_path(project)
        git = get_git_service()
        if source_value is None:
            graph = Graph().parse(
                data=git.get_file_from_branch(project_uuid, branch, filename), format="turtle"
            )
            candidates = [
                value
                for value in graph.objects(URIRef(entity_iri), URIRef(predicate))
                if isinstance(value, Literal)
            ]
            if not candidates:
                raise RuntimeError("requested source field is absent")
            literal = candidates[0]
            source_value, source_language = str(literal), literal.language
        service = TranslationService(db, config, llm_config, actor_id, git_service=git)
        languages = [target_language] if target_language else None
        results = await service.translate(source_value, source_language or "und", languages)
        outcome = await service.apply_results(
            branch=branch,
            filename=filename,
            entity_iri=entity_iri,
            predicate=predicate,
            source_value=source_value,
            source_language=source_language,
            source_value_hash=hash_literal_value(source_value),
            results=results,
            model_version=config.primary_model or "unknown",
        )
        return {"mode": mode, "commit_hash": outcome.commit.hash if outcome.commit else None}
    finally:
        if release_slot:
            arq_job_id = str(ctx.get("job_id", ""))
            release_key = f"translation:released:{project_uuid}"
            if arq_job_id:
                first_release = await ctx["redis"].sadd(release_key, arq_job_id)
                await ctx["redis"].expire(release_key, RELEASE_RECEIPT_TTL_SECONDS)
                if first_release:
                    await ctx["redis"].decrby(_pending_key(project_uuid), 1)


async def _run_backfill_literal(
    ctx: dict[str, Any],
    project_id: str,
    branch: str,
    actor_id: str,
    literal: BackfillLiteral,
) -> None:
    await run_translation_entity_job(
        ctx,
        project_id,
        branch,
        literal.entity_iri,
        literal.predicate,
        literal.source_value,
        literal.source_language,
        literal.target_language,
        actor_id,
        "batch",
        release_slot=False,
    )


async def run_translation_backfill_job(
    ctx: dict[str, Any],
    project_id: str,
    branch: str,
    job_id: str,
    actor_id: str,
    *,
    task_runner: Any = _run_backfill_literal,
) -> dict[str, Any]:
    """Run a durable backfill serially, committing progress after each literal.

    Each literal reuses entity translation execution and the gated translation commit path.
    A failed job keeps completed progress; a relaunch reselects branch gaps so hash-covered
    work is skipped.
    """
    db: AsyncSession = ctx["db"]
    project_uuid, job_uuid = UUID(project_id), UUID(job_id)
    job = await db.get(TranslationJob, job_uuid)
    if job is None:
        raise RuntimeError("translation backfill job not found")
    if job.status == "completed":
        return {"job_id": job_id, "completed": job.completed_literals}
    first_attempt = job.started_at is None
    job.status = "running"
    job.started_at = job.started_at or datetime.now(UTC)
    try:
        git = get_git_service()
        literals = await select_backfill_literals(
            db,
            git,
            project_uuid,
            branch,
            language=job.language,
            era_before=job.era_before,
            never_confirmed=job.never_confirmed,
        )
        if first_attempt:
            job.total_literals = len(literals)
        await db.commit()
        for literal in literals:
            await task_runner(ctx, project_id, branch, actor_id, literal)
            job.completed_literals = min(job.total_literals, (job.completed_literals or 0) + 1)
            await db.commit()
    except Exception as exc:
        job.status = "failed"
        job.error_message = str(exc)
        job.completed_at = datetime.now(UTC)
        await db.commit()
        raise
    job.status = "completed"
    job.error_message = None
    job.completed_at = datetime.now(UTC)
    await db.commit()
    return {"job_id": job_id, "completed": job.completed_literals}


__all__ = [
    "MAX_PENDING_TRANSLATIONS_PER_PROJECT",
    "RELEASE_RECEIPT_TTL_SECONDS",
    "TranslationTask",
    "TranslationEnqueueError",
    "discover_translation_tasks",
    "enqueue_translation_tasks",
    "enqueue_label_diff_after_commit",
    "run_label_diff_job",
    "run_translation_backfill_job",
    "run_translation_entity_job",
    "translation_entity_job_id",
]
