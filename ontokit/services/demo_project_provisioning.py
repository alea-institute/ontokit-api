"""Idempotent database provisioning for the two public demo projects."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.demo_targets import DEMO_REPOSITORY_PAIRS, normalize_repository
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration


class DemoProvisioningRefused(RuntimeError):
    """Raised when existing rows do not match the immutable demo contract."""


DEMO_DEFAULT_BRANCH = "main"


@dataclass(frozen=True)
class ProvisionedDemo:
    project_id: UUID
    source_repository: str
    destination_repository: str
    created: bool


async def _find_integration(db: AsyncSession, owner: str, repo: str) -> GitHubIntegration | None:
    result = await db.execute(
        select(GitHubIntegration)
        .options(selectinload(GitHubIntegration.project))
        .where(
            func.lower(GitHubIntegration.repo_owner) == owner,
            func.lower(GitHubIntegration.repo_name) == repo,
        )
    )
    integrations = list(result.scalars().all())
    if len(integrations) > 1:
        raise DemoProvisioningRefused(
            f"repository {owner}/{repo} is attached to more than one project"
        )
    return integrations[0] if integrations else None


async def ensure_demo_projects(db: AsyncSession) -> tuple[ProvisionedDemo, ...]:
    """Ensure exactly one linked demo project exists for each approved pair.

    This function changes database records only. Repository cloning and index
    rebuild happen after the transaction succeeds, so a failed clone cannot
    leave a half-written target identity.
    """
    provisioned: list[ProvisionedDemo] = []
    for source_target, destination_target in DEMO_REPOSITORY_PAIRS.items():
        source = await _find_integration(db, *source_target)
        if source is None:
            raise DemoProvisioningRefused(
                f"live source project {source_target[0]}/{source_target[1]} does not exist"
            )
        if source.project.is_demo:
            raise DemoProvisioningRefused(
                f"configured live source {source_target[0]}/{source_target[1]} is itself a demo"
            )

        destination = await _find_integration(db, *destination_target)
        linked_result = await db.execute(
            select(Project)
            .options(selectinload(Project.github_integration))
            .where(Project.demo_source_project_id == source.project_id)
        )
        linked = linked_result.scalar_one_or_none()

        if destination is not None:
            if not destination.project.is_demo:
                raise DemoProvisioningRefused(
                    f"demo repository {destination_target[0]}/{destination_target[1]} "
                    "is attached to a live project"
                )
            if destination.project.demo_source_project_id != source.project_id:
                raise DemoProvisioningRefused(
                    f"demo repository {destination_target[0]}/{destination_target[1]} "
                    "is linked to the wrong live source"
                )
            if linked is not None and linked.id != destination.project_id:
                raise DemoProvisioningRefused("source and destination resolve to different demos")
            demo = destination.project
            integration = destination
            created = False
        elif linked is not None:
            if not linked.is_demo:
                raise DemoProvisioningRefused("linked project is missing its demo identity")
            if linked.github_integration is not None:
                raise DemoProvisioningRefused("linked demo has an unexpected GitHub target")
            demo = linked
            integration = None
            created = False
        else:
            demo = Project(
                name=f"{source.project.name} Demo",
                description=(
                    f"Resettable OntoKit demo cloned from {source_target[0]}/{source_target[1]}."
                ),
                # Publish only after both repositories and commit-matched
                # indexes have been prepared by the resync entrypoint.
                is_public=False,
                is_demo=True,
                demo_source_project_id=source.project_id,
                owner_id=source.project.owner_id,
                ontology_iri=source.project.ontology_iri,
                source_file_path=source.project.source_file_path,
            )
            db.add(demo)
            await db.flush()
            db.add(
                ProjectMember(
                    project_id=demo.id,
                    user_id=source.project.owner_id,
                    role="owner",
                )
            )
            integration = None
            created = True

        if integration is None:
            integration = GitHubIntegration(
                project_id=demo.id,
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
            demo.github_integration = integration
        elif (
            integration.repo_owner.lower(),
            integration.repo_name.lower(),
        ) != destination_target:
            raise DemoProvisioningRefused("linked demo target changed during provisioning")
        elif integration.default_branch != DEMO_DEFAULT_BRANCH:
            raise DemoProvisioningRefused(
                f"linked demo target must use the immutable {DEMO_DEFAULT_BRANCH!r} branch"
            )

        demo.is_demo = True
        demo.demo_source_project_id = source.project_id
        demo.source_file_path = source.project.source_file_path
        provisioned.append(
            ProvisionedDemo(
                project_id=demo.id,
                source_repository="/".join(source_target),
                destination_repository="/".join(destination_target),
                created=created,
            )
        )

    await db.commit()
    return tuple(provisioned)


async def finalize_demo_publication(
    db: AsyncSession,
    provisioned: Sequence[ProvisionedDemo],
) -> None:
    """Publish the complete demo set in one transaction after preparation.

    Callers must finish cloning and commit-matched indexing for every returned
    ``ProvisionedDemo`` before invoking this function. Revalidating the
    immutable database contract under row locks prevents a stale provisioning
    result from publishing a target that changed while repositories were being
    prepared.
    """
    expected_pairs = {
        ("/".join(source), "/".join(destination))
        for source, destination in DEMO_REPOSITORY_PAIRS.items()
    }
    observed_pairs = {(item.source_repository, item.destination_repository) for item in provisioned}
    project_ids = {item.project_id for item in provisioned}
    if observed_pairs != expected_pairs or len(project_ids) != len(expected_pairs):
        raise DemoProvisioningRefused(
            "publication requires every approved demo repository exactly once"
        )

    result = await db.execute(select(Project).where(Project.id.in_(project_ids)).with_for_update())
    projects = {project.id: project for project in result.scalars().all()}
    if set(projects) != project_ids:
        raise DemoProvisioningRefused("publication target project disappeared during preparation")

    source_ids = {
        project.demo_source_project_id
        for project in projects.values()
        if project.demo_source_project_id is not None
    }
    integration_result = await db.execute(
        select(GitHubIntegration)
        .where(GitHubIntegration.project_id.in_(project_ids | source_ids))
        .with_for_update()
    )
    integrations = {
        integration.project_id: integration for integration in integration_result.scalars().all()
    }

    for item in provisioned:
        project = projects[item.project_id]
        integration = integrations.get(project.id)
        source_target = tuple(item.source_repository.split("/", 1))
        destination_target = tuple(item.destination_repository.split("/", 1))
        if (
            len(source_target) != 2
            or len(destination_target) != 2
            or DEMO_REPOSITORY_PAIRS.get(source_target) != destination_target
        ):
            raise DemoProvisioningRefused("publication target left the immutable demo contract")
        if not project.is_demo or project.demo_source_project_id is None:
            raise DemoProvisioningRefused("publication target is missing its demo identity")
        source = integrations.get(project.demo_source_project_id)
        if (
            source is None
            or normalize_repository(source.repo_owner, source.repo_name) != source_target
        ):
            raise DemoProvisioningRefused("publication source changed during preparation")
        if integration is None:
            raise DemoProvisioningRefused("publication target has no GitHub integration")
        if (
            normalize_repository(integration.repo_owner, integration.repo_name)
            != destination_target
        ):
            raise DemoProvisioningRefused("publication target changed during preparation")
        if integration.default_branch != DEMO_DEFAULT_BRANCH:
            raise DemoProvisioningRefused(
                f"publication target must use the immutable {DEMO_DEFAULT_BRANCH!r} branch"
            )
        project.is_public = True

    await db.commit()
