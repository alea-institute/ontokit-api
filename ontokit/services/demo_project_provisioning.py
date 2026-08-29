"""Generation-atomic database provisioning for the public demo projects."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.demo_targets import (
    DEMO_REPOSITORY_PAIRS,
    normalize_repository,
)
from ontokit.core.demo_targets import build_demo_generation_key as _build_demo_generation_key
from ontokit.models.demo_generation import DemoGeneration, DemoGenerationStatus
from ontokit.models.ontology_index import IndexingStatus, OntologyIndexStatus
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration


class DemoProvisioningRefused(RuntimeError):
    """Raised when existing rows do not match the immutable demo contract."""


DEMO_DEFAULT_BRANCH = "main"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_GENERATION_KEY_RE = re.compile(r"[0-9a-f]{64}")
_FAILURE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class ProvisionedDemo:
    project_id: UUID
    generation_id: UUID
    source_repository: str
    destination_repository: str
    created: bool
    already_active: bool


def build_demo_generation_key(commits: Mapping[str, str]) -> str:
    """Build a generation identity while preserving the service refusal contract."""
    try:
        return _build_demo_generation_key(commits)
    except ValueError as exc:
        raise DemoProvisioningRefused(str(exc)) from exc


def _validate_generation_key(generation_key: str) -> str:
    normalized = generation_key.lower()
    if _GENERATION_KEY_RE.fullmatch(normalized) is None:
        raise DemoProvisioningRefused("generation key must be a SHA-256 hexadecimal digest")
    return normalized


async def _find_source_integration(
    db: AsyncSession, owner: str, repo: str
) -> GitHubIntegration | None:
    """Find and lock one live source, ignoring historical demo generations."""
    result = await db.execute(
        select(GitHubIntegration)
        .join(Project, Project.id == GitHubIntegration.project_id)
        .options(selectinload(GitHubIntegration.project))
        .where(
            func.lower(GitHubIntegration.repo_owner) == owner,
            func.lower(GitHubIntegration.repo_name) == repo,
            Project.is_demo.is_(False),
        )
        .with_for_update(of=GitHubIntegration)
    )
    integrations = list(result.scalars().all())
    if len(integrations) > 1:
        raise DemoProvisioningRefused(
            f"live source repository {owner}/{repo} is attached to more than one project"
        )
    return integrations[0] if integrations else None


async def ensure_demo_projects(
    db: AsyncSession,
    generation_key: str,
) -> tuple[ProvisionedDemo, ...]:
    """Create or resume a complete hidden generation for the approved pair."""
    generation_key = _validate_generation_key(generation_key)
    sources: dict[tuple[str, str], GitHubIntegration] = {}
    for source_target in DEMO_REPOSITORY_PAIRS:
        source = await _find_source_integration(db, *source_target)
        if source is None:
            raise DemoProvisioningRefused(
                f"live source project {source_target[0]}/{source_target[1]} does not exist"
            )
        sources[source_target] = source

    generation_result = await db.execute(
        select(DemoGeneration)
        .where(DemoGeneration.generation_key == generation_key)
        .with_for_update()
    )
    generation = generation_result.scalar_one_or_none()
    if generation is None:
        generation = DemoGeneration(
            generation_key=generation_key,
            status=DemoGenerationStatus.PREPARING.value,
            attempt_count=1,
            failure_count=0,
        )
        db.add(generation)
        await db.flush()
    elif generation.status == DemoGenerationStatus.RETIRED.value:
        raise DemoProvisioningRefused("a retired demo generation cannot be republished")
    elif generation.status == DemoGenerationStatus.FAILED.value:
        generation.status = DemoGenerationStatus.PREPARING.value
        generation.attempt_count += 1
    elif generation.status not in {
        DemoGenerationStatus.PREPARING.value,
        DemoGenerationStatus.ACTIVE.value,
    }:
        raise DemoProvisioningRefused("demo generation has an invalid lifecycle status")

    provisioned: list[ProvisionedDemo] = []
    for source_target, destination_target in DEMO_REPOSITORY_PAIRS.items():
        source = sources[source_target]
        candidate_result = await db.execute(
            select(Project)
            .options(selectinload(Project.github_integration))
            .where(
                Project.demo_source_project_id == source.project_id,
                Project.demo_generation_id == generation.id,
            )
        )
        candidate = candidate_result.scalar_one_or_none()
        created = candidate is None
        if candidate is None:
            candidate = Project(
                name=f"{source.project.name} Demo",
                description=(
                    f"Resettable OntoKit demo cloned from {source_target[0]}/{source_target[1]}."
                ),
                is_public=False,
                is_demo=True,
                demo_source_project_id=source.project_id,
                demo_generation_id=generation.id,
                owner_id=source.project.owner_id,
                ontology_iri=source.project.ontology_iri,
                source_file_path=source.project.source_file_path,
            )
            db.add(candidate)
            await db.flush()
            integration = GitHubIntegration(
                project_id=candidate.id,
                repo_owner=destination_target[0],
                repo_name=destination_target[1],
                default_branch=DEMO_DEFAULT_BRANCH,
                ontology_file_path=source.ontology_file_path,
                turtle_file_path=source.turtle_file_path,
                sync_enabled=True,
                sync_status="idle",
                connected_by_user_id=None,
                webhooks_enabled=False,
            )
            db.add(integration)
            candidate.github_integration = integration
        else:
            existing_integration = candidate.github_integration
            if not candidate.is_demo or (
                candidate.is_public and generation.status != DemoGenerationStatus.ACTIVE.value
            ):
                raise DemoProvisioningRefused(
                    "preparing demo generation contains a visible or non-demo project"
                )
            if existing_integration is None:
                raise DemoProvisioningRefused("candidate demo has no GitHub integration")
            if normalize_repository(
                existing_integration.repo_owner, existing_integration.repo_name
            ) != (destination_target):
                raise DemoProvisioningRefused("candidate demo target changed during preparation")
            if existing_integration.default_branch != DEMO_DEFAULT_BRANCH:
                raise DemoProvisioningRefused(
                    f"candidate target must use the immutable {DEMO_DEFAULT_BRANCH!r} branch"
                )

        provisioned.append(
            ProvisionedDemo(
                project_id=candidate.id,
                generation_id=generation.id,
                source_repository="/".join(source_target),
                destination_repository="/".join(destination_target),
                created=created,
                already_active=generation.status == DemoGenerationStatus.ACTIVE.value,
            )
        )

    await db.commit()
    return tuple(provisioned)


async def record_demo_preparation(
    db: AsyncSession,
    item: ProvisionedDemo,
    commit_hash: str,
) -> None:
    """Correlate a hidden project's repository and verified index commit."""
    commit_hash = commit_hash.lower()
    if _COMMIT_RE.fullmatch(commit_hash) is None:
        raise DemoProvisioningRefused("prepared demo requires a full hexadecimal commit hash")
    project_result = await db.execute(
        select(Project).where(Project.id == item.project_id).with_for_update()
    )
    project = project_result.scalar_one_or_none()
    if (
        project is None
        or not project.is_demo
        or project.demo_generation_id != item.generation_id
        or project.is_public
    ):
        raise DemoProvisioningRefused("prepared project left its hidden generation contract")
    status_result = await db.execute(
        select(OntologyIndexStatus).where(
            OntologyIndexStatus.project_id == item.project_id,
            OntologyIndexStatus.branch == DEMO_DEFAULT_BRANCH,
        )
    )
    status = status_result.scalar_one_or_none()
    if (
        status is None
        or status.status != IndexingStatus.READY.value
        or status.commit_hash != commit_hash
    ):
        raise DemoProvisioningRefused(
            "prepared demo repository and ready index do not share one commit"
        )
    project.demo_commit_hash = commit_hash
    await db.commit()


async def fail_demo_generation(
    db: AsyncSession,
    provisioned: Sequence[ProvisionedDemo],
    reason: str,
) -> None:
    """Record a safe failure receipt without changing the active generation."""
    generation_ids = {item.generation_id for item in provisioned}
    if len(generation_ids) != 1:
        raise DemoProvisioningRefused("failure receipt requires exactly one generation")
    if _FAILURE_REASON_RE.fullmatch(reason) is None:
        raise DemoProvisioningRefused("failure receipt reason must be an allowlisted safe label")
    generation_id = next(iter(generation_ids))
    result = await db.execute(
        select(DemoGeneration).where(DemoGeneration.id == generation_id).with_for_update()
    )
    generation = result.scalar_one_or_none()
    if generation is None:
        raise DemoProvisioningRefused("failed demo generation disappeared")
    if generation.status == DemoGenerationStatus.ACTIVE.value:
        raise DemoProvisioningRefused("an active demo generation cannot be marked failed")
    generation.status = DemoGenerationStatus.FAILED.value
    generation.failure_count += 1
    generation.last_failure_reason = reason
    generation.last_failed_at = datetime.now(UTC)
    await db.commit()


async def finalize_demo_publication(
    db: AsyncSession,
    provisioned: Sequence[ProvisionedDemo],
) -> None:
    """Atomically switch public visibility to one fully prepared generation."""
    expected_pairs = {
        ("/".join(source), "/".join(destination))
        for source, destination in DEMO_REPOSITORY_PAIRS.items()
    }
    observed_pairs = {(item.source_repository, item.destination_repository) for item in provisioned}
    project_ids = {item.project_id for item in provisioned}
    generation_ids = {item.generation_id for item in provisioned}
    if (
        observed_pairs != expected_pairs
        or len(project_ids) != len(expected_pairs)
        or len(generation_ids) != 1
    ):
        raise DemoProvisioningRefused(
            "publication requires one complete approved demo generation exactly once"
        )
    generation_id = next(iter(generation_ids))

    project_result = await db.execute(
        select(Project)
        .options(selectinload(Project.github_integration))
        .where(Project.id.in_(project_ids))
        .with_for_update()
    )
    projects = {project.id: project for project in project_result.scalars().all()}
    if set(projects) != project_ids:
        raise DemoProvisioningRefused("publication target disappeared during preparation")

    source_ids = {project.demo_source_project_id for project in projects.values()}
    if None in source_ids or len(source_ids) != len(expected_pairs):
        raise DemoProvisioningRefused("publication target lost a unique live source")
    locked_sources_result = await db.execute(
        select(Project).where(Project.id.in_(source_ids)).order_by(Project.id).with_for_update()
    )
    locked_sources = {project.id: project for project in locked_sources_result.scalars().all()}
    if set(locked_sources) != source_ids:
        raise DemoProvisioningRefused("publication source disappeared during preparation")

    generation_result = await db.execute(
        select(DemoGeneration).where(DemoGeneration.id == generation_id).with_for_update()
    )
    generation = generation_result.scalar_one_or_none()
    if generation is None:
        raise DemoProvisioningRefused("publication generation disappeared during preparation")
    if generation.status == DemoGenerationStatus.ACTIVE.value:
        if all(project.is_public for project in projects.values()):
            await db.rollback()
            return
        raise DemoProvisioningRefused("active generation has inconsistent project visibility")
    if generation.status != DemoGenerationStatus.PREPARING.value:
        raise DemoProvisioningRefused("only a preparing generation can be published")

    source_integrations_result = await db.execute(
        select(GitHubIntegration).where(GitHubIntegration.project_id.in_(source_ids))
    )
    source_integrations = {
        integration.project_id: integration
        for integration in source_integrations_result.scalars().all()
    }
    index_result = await db.execute(
        select(OntologyIndexStatus).where(
            OntologyIndexStatus.project_id.in_(project_ids),
            OntologyIndexStatus.branch == DEMO_DEFAULT_BRANCH,
        )
    )
    indexes = {status.project_id: status for status in index_result.scalars().all()}

    for item in provisioned:
        project = projects[item.project_id]
        integration = project.github_integration
        source_target = tuple(item.source_repository.split("/", 1))
        destination_target = tuple(item.destination_repository.split("/", 1))
        source_id = project.demo_source_project_id
        source = source_integrations.get(source_id) if source_id is not None else None
        index = indexes.get(project.id)
        if (
            len(source_target) != 2
            or len(destination_target) != 2
            or DEMO_REPOSITORY_PAIRS.get(source_target) != destination_target
            or project.demo_generation_id != generation_id
            or not project.is_demo
            or project.is_public
            or project.demo_commit_hash is None
            or source is None
            or normalize_repository(source.repo_owner, source.repo_name) != source_target
            or integration is None
            or normalize_repository(integration.repo_owner, integration.repo_name)
            != destination_target
            or integration.default_branch != DEMO_DEFAULT_BRANCH
            or index is None
            or index.status != IndexingStatus.READY.value
            or index.commit_hash != project.demo_commit_hash
        ):
            raise DemoProvisioningRefused(
                "publication candidate failed repository/project/index correlation"
            )

    now = datetime.now(UTC)
    active_result = await db.execute(
        select(DemoGeneration)
        .where(DemoGeneration.status == DemoGenerationStatus.ACTIVE.value)
        .with_for_update()
    )
    for active in active_result.scalars().all():
        if active.id != generation_id:
            active.status = DemoGenerationStatus.RETIRED.value
            active.retired_at = now
    # The partial unique index permits exactly one active row. Flush the old
    # retirement inside this still-uncommitted transaction before assigning
    # the new active status so statement ordering cannot trip that invariant.
    await db.flush()

    visible_result = await db.execute(
        select(Project)
        .where(Project.is_demo.is_(True), Project.is_public.is_(True))
        .with_for_update()
    )
    for project in visible_result.scalars().all():
        if project.demo_generation_id != generation_id:
            project.is_public = False
    for project in projects.values():
        project.is_public = True

    generation.status = DemoGenerationStatus.ACTIVE.value
    generation.activated_at = now
    generation.retired_at = None
    await db.commit()
