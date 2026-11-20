"""Analytics API endpoints — entity history, project activity, contributors."""

import asyncio
import logging
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import CurrentUser, OptionalUser
from ontokit.core.database import get_db
from ontokit.git import get_git_service
from ontokit.models.change_event import ChangeOrigin
from ontokit.schemas.analytics import (
    ChangedEntitiesResponse,
    ContributorStats,
    EntityHistoryResponse,
    HotEntity,
    ProjectActivity,
    ProjectChangesResponse,
)
from ontokit.services.change_event_service import ChangeEventService, InvalidChangesCursor
from ontokit.services.project_service import get_project_service

logger = logging.getLogger(__name__)

router = APIRouter()

# PostgreSQL text cannot contain NUL characters.
_QueryText = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]


async def _verify_access(project_id: UUID, db: AsyncSession, user: CurrentUser | None) -> None:
    await get_project_service(db).get(project_id, user)


def _default_branch(project_id: UUID) -> str:
    repo = get_git_service().get_repository(project_id)
    return repo.get_default_branch() if repo.is_initialized else "main"


async def _resolve_branch(project_id: UUID, branch: str | None) -> str:
    if branch is not None:
        return branch
    return await asyncio.to_thread(_default_branch, project_id)


def get_change_events(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChangeEventService:
    """Dependency to get change event service with database session."""
    return ChangeEventService(db)


@router.get("/{project_id}/analytics/changes", response_model=ProjectChangesResponse)
async def get_project_changes(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    days: int = Query(default=7, ge=1, le=365),
    branch: Annotated[_QueryText | None, Query()] = None,
    origin: Annotated[list[ChangeOrigin] | None, Query()] = None,
    author: Annotated[list[_QueryText] | None, Query()] = None,
    entity_type: Annotated[list[_QueryText] | None, Query()] = None,
    event_type: Annotated[list[_QueryText] | None, Query()] = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> ProjectChangesResponse:
    """Get the net changes to entities in the filtered window."""
    await _verify_access(project_id, db, user)
    try:
        return await service.list_changes(
            project_id,
            branch=await _resolve_branch(project_id, branch),
            days=days,
            origin=origin,
            author=author,
            entity_type=entity_type,
            event_type=event_type,
            limit=limit,
            cursor=cursor,
        )
    except InvalidChangesCursor as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{project_id}/analytics/changed-entities", response_model=ChangedEntitiesResponse)
async def get_changed_entities(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    days: int = Query(default=7, ge=1, le=365),
    branch: Annotated[_QueryText | None, Query()] = None,
    origin: Annotated[list[ChangeOrigin] | None, Query()] = None,
) -> ChangedEntitiesResponse:
    """Get changed-entity markers for the project tree."""
    await _verify_access(project_id, db, user)
    return await service.list_changed_entities(
        project_id, branch=await _resolve_branch(project_id, branch), days=days, origin=origin
    )


@router.get(
    "/{project_id}/analytics/activity",
    response_model=ProjectActivity,
)
async def get_project_activity(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    days: int = Query(default=30, ge=1, le=365),
) -> ProjectActivity:
    """Get project activity over time."""
    await _verify_access(project_id, db, user)
    return await service.get_activity(project_id, days)


@router.get(
    "/{project_id}/analytics/entity/{iri:path}/history",
    response_model=EntityHistoryResponse,
)
async def get_entity_history(
    project_id: UUID,
    iri: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    branch: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> EntityHistoryResponse:
    """Get change history for a specific entity."""
    await _verify_access(project_id, db, user)
    decoded_iri = unquote(iri)
    return await service.get_entity_history(project_id, decoded_iri, branch, limit)


@router.get(
    "/{project_id}/analytics/hot-entities",
    response_model=list[HotEntity],
)
async def get_hot_entities(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[HotEntity]:
    """Get most frequently edited entities in the last 30 days."""
    await _verify_access(project_id, db, user)
    return await service.get_hot_entities(project_id, limit)


@router.get(
    "/{project_id}/analytics/contributors",
    response_model=list[ContributorStats],
)
async def get_contributors(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ChangeEventService, Depends(get_change_events)],
    user: OptionalUser,
    days: int = Query(default=30, ge=1, le=365),
) -> list[ContributorStats]:
    """Get contributor statistics."""
    await _verify_access(project_id, db, user)
    return await service.get_contributors(project_id, days)
