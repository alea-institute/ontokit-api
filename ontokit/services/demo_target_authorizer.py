"""Fail-closed authorization for project-aware GitHub write targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Load, joinedload

from ontokit.core.config import settings
from ontokit.core.demo_targets import (
    DEMO_REPOSITORY_PAIRS,
    DemoTargetAuthorization,
    is_demo_repository,
    normalize_repository,
)
from ontokit.models.project import Project
from ontokit.models.pull_request import GitHubIntegration


class DemoTargetDenied(RuntimeError):
    """Raised when a live/demo project crosses the repository trust boundary."""


@dataclass(frozen=True)
class AuthorizedTarget:
    token: str | None
    capability: DemoTargetAuthorization | None


def integration_target_load() -> Load:
    """Eager-load the persisted context needed by both authorization passes."""
    return cast(
        Load,
        joinedload(GitHubIntegration.project)
        .joinedload(Project.demo_source_project)
        .joinedload(Project.github_integration),
    )


async def _demo_source_integration(
    db: AsyncSession,
    project: Project,
) -> GitHubIntegration | None:
    if "demo_source_project" in project.__dict__:
        source_project = project.__dict__["demo_source_project"]
        if source_project is None:
            return None
        if "github_integration" in source_project.__dict__:
            return cast(
                GitHubIntegration | None,
                source_project.__dict__["github_integration"],
            )

    result = await db.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.project_id == project.demo_source_project_id
        )
    )
    return result.scalar_one_or_none()


async def authorize_integration_target(
    db: AsyncSession,
    integration: GitHubIntegration,
    *,
    operation: str,
) -> AuthorizedTarget:
    """Authorize one integration's exact repository for an outbound write.

    Demo projects may write only their configured, approved dummy repository
    and only with the dedicated demo credential. Live projects may never write
    either dummy repository, even if an integration row is misconfigured.
    """
    owner, repo = normalize_repository(integration.repo_owner, integration.repo_name)
    target_is_demo = is_demo_repository(owner, repo)
    project = integration.__dict__.get("project")
    if project is None:
        # Unit-test and adapter doubles are not SQLAlchemy entities. Treat them
        # as live only for ordinary targets; even a double cannot cross into a
        # demo repository. Production integrations always take the DB-backed
        # path below when their relationship is not eagerly loaded.
        if not isinstance(integration, GitHubIntegration):
            if target_is_demo:
                raise DemoTargetDenied(
                    f"{operation} refused: live project cannot target demo repository "
                    f"{owner}/{repo}"
                )
            return AuthorizedTarget(token=None, capability=None)
        result = await db.execute(
            select(Project)
            .options(joinedload(Project.demo_source_project).joinedload(Project.github_integration))
            .where(Project.id == integration.project_id)
        )
        project = result.scalar_one_or_none()
    if project is None:
        raise DemoTargetDenied(f"{operation} refused: project does not exist")

    if project.is_demo:
        if not target_is_demo:
            raise DemoTargetDenied(
                f"{operation} refused: demo project cannot target {owner}/{repo}"
            )
        if not settings.github_demo_mirror_token:
            raise DemoTargetDenied(
                f"{operation} refused: GITHUB_DEMO_MIRROR_TOKEN is not configured"
            )
        if project.demo_source_project_id is None:
            raise DemoTargetDenied(f"{operation} refused: demo project has no live source")
        source = await _demo_source_integration(db, project)
        if source is None:
            raise DemoTargetDenied(f"{operation} refused: demo project's live source has no target")
        source_target = normalize_repository(source.repo_owner, source.repo_name)
        if DEMO_REPOSITORY_PAIRS.get(source_target) != (owner, repo):
            raise DemoTargetDenied(
                f"{operation} refused: demo target does not match its live source"
            )
        return AuthorizedTarget(
            token=settings.github_demo_mirror_token,
            capability=DemoTargetAuthorization(project.id, owner, repo),
        )

    if target_is_demo:
        raise DemoTargetDenied(
            f"{operation} refused: live project cannot target demo repository {owner}/{repo}"
        )
    return AuthorizedTarget(token=None, capability=None)
