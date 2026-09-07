"""Bound demo content while preserving permanent generation and project identities.

Use a dedicated AsyncSession (as with async_session_maker). Each generation owns
the refresh lease across all of its commits; the lease uses a separate physical
connection so an embedding-service commit cannot release it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError, computed_field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.core.database import engine
from ontokit.git.bare_repository import get_bare_git_service
from ontokit.models.demo_generation import DemoGeneration
from ontokit.models.embedding import EmbeddingJob, EntityEmbedding, EntityEmbeddingStaging
from ontokit.models.lint import LintIssue, LintRun
from ontokit.models.normalization import NormalizationRun
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration
from ontokit.services.demo_project_provisioning import (
    DemoProvisioningRefused,
    demo_generation_attempt_lease,
)
from ontokit.services.embedding_service import EmbeddingService
from ontokit.services.ontology_index import OntologyIndexService
from ontokit.services.storage import get_storage_service

Step = Literal["repositories", "index", "embeddings", "lint", "normalization", "storage"]
STEPS: tuple[Step, ...] = (
    "repositories",
    "index",
    "embeddings",
    "lint",
    "normalization",
    "storage",
)
Reason = Literal["active", "preparing", "keep", "age", "purged"]
Deletion = Callable[[UUID], Awaitable[int]]
LeaseFactory = Callable[[], AbstractAsyncContextManager[None]]
FailureClass = Literal["RuntimeError", "OSError", "ValueError", "CancelledError", "Exception"]


class StepResult(BaseModel):
    step: Step
    project_id: UUID
    count: int = Field(ge=0)


class PurgeAttempt(BaseModel):
    attempt_id: UUID = Field(default_factory=uuid4)
    started_at: datetime
    finished_at: datetime | None = None
    outcome: Literal["running", "success", "failed", "yielded"] = "running"
    completed_steps: list[Step] = Field(default_factory=list)
    counts: dict[Step, int] = Field(default_factory=dict)
    results: list[StepResult] = Field(default_factory=list)
    failure_class: FailureClass | None = None


class PurgeReceipt(BaseModel):
    """Only typed identifiers, counts and safe labels cross the audit boundary."""

    generation_id: UUID
    generation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_ids: list[UUID]
    integration_ids: list[UUID]
    retained: tuple[
        Literal["demo_generations", "projects", "github_integrations", "source_file_path_objects"],
        ...,
    ] = ("demo_generations", "projects", "github_integrations", "source_file_path_objects")
    attempts: list[PurgeAttempt] = Field(default_factory=list)

    @computed_field
    def retained_counts(self) -> dict[str, int]:
        return {
            "demo_generations": 1,
            "projects": len(self.project_ids),
            "github_integrations": len(self.integration_ids),
        }


@dataclass(frozen=True)
class RetentionEntry:
    generation_id: UUID
    generation_key: str
    status: str
    age_days: float | None
    reason: Reason | None


@dataclass
class RetentionPlan:
    eligible: list[RetentionEntry] = field(default_factory=list)
    retained: list[RetentionEntry] = field(default_factory=list)


@dataclass
class RetentionSummary:
    retained: list[RetentionEntry]
    purged: list[str] = field(default_factory=list)
    yielded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    budget_deferred: list[str] = field(default_factory=list)


@asynccontextmanager
async def _refresh_lease() -> AsyncIterator[None]:
    async with engine.connect() as connection, demo_generation_attempt_lease(connection):
        yield


def _failure_class(exc: BaseException) -> FailureClass:
    # Never persist exception messages or attacker-controlled exception names.
    if isinstance(exc, asyncio.CancelledError):
        return "CancelledError"
    if isinstance(exc, OSError):
        return "OSError"
    if isinstance(exc, ValueError):
        return "ValueError"
    if isinstance(exc, RuntimeError):
        return "RuntimeError"
    return "Exception"


class DemoRetentionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        git: Deletion | None = None,
        index: Deletion | None = None,
        embeddings: Deletion | None = None,
        lint: Deletion | None = None,
        normalization: Deletion | None = None,
        storage: Deletion | None = None,
        lease_factory: LeaseFactory = _refresh_lease,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        keep_retired: int | None = None,
        min_age_days: int | None = None,
        run_budget_seconds: int | None = None,
    ) -> None:
        self.db = db
        self.lease_factory = lease_factory
        self.clock = clock
        self.monotonic = monotonic
        self.keep_retired = (
            settings.demo_retention_keep_retired if keep_retired is None else keep_retired
        )
        self.min_age_days = (
            settings.demo_retention_min_age_days if min_age_days is None else min_age_days
        )
        self.run_budget_seconds = (
            settings.demo_retention_run_budget_seconds
            if run_budget_seconds is None
            else run_budget_seconds
        )
        if self.keep_retired < 1 or self.min_age_days < 0 or self.run_budget_seconds < 1:
            raise ValueError("invalid demo retention limits")
        self.operations: dict[Step, Deletion] = dict(
            zip(
                STEPS,
                (
                    git or self._delete_repository,
                    index or OntologyIndexService(db).delete_project_index,
                    embeddings or self._clear_embeddings,
                    lint or self._delete_lint,
                    normalization or self._delete_normalization,
                    storage or self._delete_storage,
                ),
                strict=True,
            )
        )

    async def plan(self) -> RetentionPlan:
        result = await self.db.execute(
            select(DemoGeneration).execution_options(populate_existing=True)
        )
        generations = list(result.scalars().all())
        now = self.clock()
        retired = sorted(
            (g for g in generations if g.status == "retired"),
            key=lambda g: (g.retired_at or datetime.max.replace(tzinfo=UTC), str(g.id)),
            reverse=True,
        )
        keep = {g.id for g in retired[: self.keep_retired]}
        plan = RetentionPlan()
        for g in generations:
            timestamp = g.retired_at if g.status == "retired" else g.last_failed_at
            age = (now - timestamp).total_seconds() / 86400 if timestamp else None
            reason: Reason | None
            if g.status in ("active", "preparing"):
                reason = "active" if g.status == "active" else "preparing"
                age = None
            elif g.purged_at is not None:
                reason = "purged"
            elif g.id in keep:
                reason = "keep"
            elif (
                g.status not in ("retired", "failed")
                or timestamp is None
                or now - timestamp <= timedelta(days=self.min_age_days)
            ):
                reason = "age"
            else:
                reason = None
            entry = RetentionEntry(g.id, g.generation_key, g.status, age, reason)
            (plan.eligible if reason is None else plan.retained).append(entry)
        plan.eligible.sort(key=lambda e: (-(e.age_days or 0), e.generation_key))
        return plan

    async def _receipt(self, entry: RetentionEntry) -> PurgeReceipt:
        projects = await self.db.execute(
            select(Project.id).where(
                Project.demo_generation_id == entry.generation_id, Project.is_demo.is_(True)
            )
        )
        project_ids = sorted(projects.scalars().all(), key=str)
        integrations = await self.db.execute(
            select(GitHubIntegration.id).where(GitHubIntegration.project_id.in_(project_ids))
        )
        receipt = PurgeReceipt(
            generation_id=entry.generation_id,
            generation_key=entry.generation_key,
            project_ids=project_ids,
            integration_ids=sorted(integrations.scalars().all(), key=str),
        )
        result = await self.db.execute(
            select(DemoGeneration)
            .where(DemoGeneration.id == entry.generation_id)
            .execution_options(populate_existing=True)
        )
        generation = next(g for g in result.scalars().all() if g.id == entry.generation_id)
        if generation.purge_receipt:
            try:
                previous = PurgeReceipt.model_validate_json(generation.purge_receipt)
                if previous.generation_id == entry.generation_id:
                    receipt.attempts = previous.attempts
            except ValidationError:
                # Unrecognized audit text is never copied into a new safe receipt.
                pass
        receipt.attempts.append(PurgeAttempt(started_at=self.clock()))
        return receipt

    async def _save(self, receipt: PurgeReceipt, *, success: bool = False) -> None:
        # A contending retention caller may append a yielded attempt while this
        # caller owns the advisory lease. Merge under a short row lock so neither
        # caller can overwrite the other's durable history.
        result = await self.db.execute(
            select(DemoGeneration)
            .where(DemoGeneration.id == receipt.generation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        generation = next(g for g in result.scalars().all() if g.id == receipt.generation_id)
        if generation.purge_receipt:
            try:
                previous = PurgeReceipt.model_validate_json(generation.purge_receipt)
            except ValidationError:
                previous = None
            if previous is not None and previous.generation_id == receipt.generation_id:
                current = receipt.attempts[-1]
                receipt.attempts = [
                    attempt
                    for attempt in previous.attempts
                    if attempt.attempt_id != current.attempt_id
                ] + [current]
        statement = update(DemoGeneration).where(DemoGeneration.id == receipt.generation_id)
        statement = statement.values(purge_receipt=receipt.model_dump_json())
        if success:
            statement = statement.values(purged_at=self.clock())
        await self.db.execute(statement)
        await self.db.commit()

    async def purge(self, generation: RetentionEntry) -> PurgeReceipt | RetentionEntry:
        """Acquire the lease, recheck eligibility, and journal each content step."""
        async with self.lease_factory():
            current = await self.plan()
            retained = next(
                (e for e in current.retained if e.generation_id == generation.generation_id), None
            )
            if retained is not None:
                return retained
            entry = next(e for e in current.eligible if e.generation_id == generation.generation_id)
            receipt = await self._receipt(entry)
            attempt = receipt.attempts[-1]
            await self._save(receipt)
            try:
                for step in STEPS:
                    for project_id in receipt.project_ids:
                        count = await self.operations[step](project_id)
                        result = StepResult(step=step, project_id=project_id, count=count)
                        # Commit DB effects before claiming them as completed. Some
                        # existing primitives also commit; all are absence tolerant.
                        await self.db.commit()
                        attempt.results.append(result)
                        attempt.counts[step] = attempt.counts.get(step, 0) + count
                        await self._save(receipt)
                    attempt.counts.setdefault(step, 0)
                    attempt.completed_steps.append(step)
                    await self._save(receipt)
                attempt.outcome = "success"
                attempt.finished_at = self.clock()
                await self._save(receipt, success=True)
            except (Exception, asyncio.CancelledError) as exc:
                await self.db.rollback()
                attempt.outcome = "failed"
                attempt.finished_at = self.clock()
                attempt.failure_class = _failure_class(exc)
                await self._save(receipt)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            return receipt

    async def apply(self) -> RetentionSummary:
        started = self.monotonic()
        plan = await self.plan()
        summary = RetentionSummary(retained=plan.retained)
        for i, entry in enumerate(plan.eligible):
            if self.monotonic() - started >= self.run_budget_seconds:
                summary.budget_deferred.extend(e.generation_key for e in plan.eligible[i:])
                break
            try:
                result = await self.purge(entry)
            except DemoProvisioningRefused:
                receipt = await self._receipt(entry)
                attempt = receipt.attempts[-1]
                attempt.outcome = "yielded"
                attempt.finished_at = self.clock()
                await self._save(receipt)
                summary.yielded.append(entry.generation_key)
                continue
            if isinstance(result, RetentionEntry):
                summary.retained.append(result)
            elif result.attempts[-1].outcome == "success":
                summary.purged.append(entry.generation_key)
            else:
                summary.failed.append(entry.generation_key)
        return summary

    async def _delete_repository(self, project_id: UUID) -> int:
        def remove() -> int:
            service = get_bare_git_service()
            exists = (service.base_path / f"{project_id}.git").exists()
            service.delete_repository(project_id)
            return int(exists)

        task = asyncio.create_task(asyncio.to_thread(remove))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation must not release the refresh lease while rmtree
            # continues running on the worker thread.
            await task
            raise

    async def _clear_embeddings(self, project_id: UUID) -> int:
        count = 0
        for model in (EntityEmbedding, EmbeddingJob, EntityEmbeddingStaging):
            result = await self.db.execute(
                select(func.count()).select_from(model).where(model.project_id == project_id)
            )
            count += result.scalar_one()
        await EmbeddingService(self.db).clear_embeddings(project_id)
        return count

    async def _delete_lint(self, project_id: UUID) -> int:
        count = 0
        for model in (LintIssue, LintRun):
            result = await self.db.execute(
                select(func.count()).select_from(model).where(model.project_id == project_id)
            )
            count += result.scalar_one()
            await self.db.execute(delete(model).where(model.project_id == project_id))
        return count

    async def _delete_normalization(self, project_id: UUID) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(NormalizationRun)
            .where(NormalizationRun.project_id == project_id)
        )
        count: int = result.scalar_one()
        await self.db.execute(
            delete(NormalizationRun).where(NormalizationRun.project_id == project_id)
        )
        return count

    async def _delete_storage(self, project_id: UUID) -> int:
        return await get_storage_service().delete_project_files(project_id, db=self.db)
