"""Idempotent database provisioning for the two public demo projects."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.demo_targets import DEMO_REPOSITORY_PAIRS
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration


class DemoProvisioningRefused(RuntimeError):
    """Raised when existing rows do not match the immutable demo contract."""


@dataclass(frozen=True)
class ProvisionedDemo:
    project_id: UUID
    source_repository: str
    destination_repository: str
    created: bool


async def _find_integration(
    db: AsyncSession, owner: str, repo: str
) -> GitHubIntegration | None:
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
                is_public=True,
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
                default_branch=source.default_branch or "main",
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

        demo.is_public = True
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
