"""Shared visibility and mutability policy for managed demo projects."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ontokit.models.project import Project


def visible_project_clause() -> ColumnElement[bool]:
    """Return the fail-closed SQL predicate for user-visible projects."""
    return or_(Project.is_demo.is_(False), Project.is_public.is_(True))


def require_visible_project(project: Project) -> None:
    """Hide non-public demo generations from every user-facing ID lookup."""
    if project.is_demo is True and project.is_public is not True:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )


async def load_visible_project(db: AsyncSession, project_id: UUID) -> Project:
    """Load a project by ID without disclosing managed hidden generations."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    require_visible_project(project)
    return project


def require_user_managed_project(project: Project) -> None:
    """Prevent users from independently mutating refresh-managed demo rows."""
    if project.is_demo is True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Demo projects are managed by the refresh pipeline and cannot be modified",
        )
