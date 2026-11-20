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

# PostgreSQL text cannot contain NUL characters. Lengths match the stored columns.
_QueryText = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$", max_length=255)]
# Repeatable filters become SQL IN lists, so their size is bounded.
_MAX_FILTER_VALUES = 50
_OriginFilter = Annotated[list[ChangeOrigin] | None, Query(max_length=len(ChangeOrigin))]
_TextFilter = Annotated[list[_QueryText] | None, Query(max_length=_MAX_FILTER_VALUES)]


async def _verify_access(project_id: UUID, db: AsyncSession, user: CurrentUser | None) -> None:
    await get_project_service(db).get(project_id, user)


def _default_branch(project_id: UUID) -> str:
    repo = get_git_service().get_repository(project_id)
    return repo.get_default_branch() if repo.is_initialized else "main"


async def _resolve_branch(project_id: UUID, branch: str | None) -> str:
    if branch is not None:
        return branch
    return await asyncio.to_thread(_default_branch, project_id)


async def _begin_snapshot(db: AsyncSession) -> None:
    """Serve every query of one feed response from the same snapshot.

    A feed runs several queries (page, payload, summary). Under the default
    isolation each sees the rows committed so far, so they disagree while
    history is being recorded. The access check has already used this session;
    its read-only transaction ends here because isolation is set at the start
    of a transaction.
    """
    await db.rollback()
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


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
    origin: _OriginFilter = None,
    author: _TextFilter = None,
    entity_type: _TextFilter = None,
    event_type: _TextFilter = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=4096),
) -> ProjectChangesResponse:
    """Get the net changes to entities in the filtered window."""
    await _verify_access(project_id, db, user)
    resolved_branch = await _resolve_branch(project_id, branch)
    await _begin_snapshot(db)
    try:
        return await service.list_changes(
            project_id,
            branch=resolved_branch,
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
    origin: _OriginFilter = None,
) -> ChangedEntitiesResponse:
    """Get changed-entity markers for the project tree."""
    await _verify_access(project_id, db, user)
    resolved_branch = await _resolve_branch(project_id, branch)
    await _begin_snapshot(db)
    return await service.list_changed_entities(
        project_id, branch=resolved_branch, days=days, origin=origin
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
