"""Suggestion session management endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import OptionalUser, RequiredUser
from ontokit.core.database import get_db
from ontokit.schemas.suggestion import (
    BulkReviewRequest,
    BulkReviewResponse,
    SuggestionBeaconRequest,
    SuggestionCapabilitiesResponse,
    SuggestionRejectRequest,
    SuggestionRequestChangesRequest,
    SuggestionResubmitRequest,
    SuggestionSaveRequest,
    SuggestionSaveResponse,
    SuggestionSessionListResponse,
    SuggestionSessionResponse,
    SuggestionSubmitRequest,
    SuggestionSubmitResponse,
)
from ontokit.services.suggestion_service import SuggestionService, get_suggestion_service

router = APIRouter()


def get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> SuggestionService:
    """Dependency to get suggestion service with database session."""
    return get_suggestion_service(db)


@router.post(
    "/{project_id}/suggestions/sessions",
    response_model=SuggestionSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    project_id: UUID,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> SuggestionSessionResponse:
    """Create a new suggestion session with a dedicated branch."""
    return await service.create_session(project_id, user)


@router.put(
    "/{project_id}/suggestions/sessions/{session_id}/save",
    response_model=SuggestionSaveResponse,
)
async def save_to_session(
    project_id: UUID,
    session_id: str,
    data: SuggestionSaveRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> SuggestionSaveResponse:
    """Save content to a suggestion session's branch."""
    return await service.save(project_id, session_id, data, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/submit",
    response_model=SuggestionSubmitResponse,
)
async def submit_session(
    project_id: UUID,
    session_id: str,
    data: SuggestionSubmitRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> SuggestionSubmitResponse:
    """Submit a suggestion session by creating a pull request."""
    return await service.submit(project_id, session_id, data, user)


@router.get(
    "/{project_id}/suggestions/sessions",
    response_model=SuggestionSessionListResponse,
)
async def list_sessions(
    project_id: UUID,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> SuggestionSessionListResponse:
    """List the current user's suggestion sessions for a project."""
    return await service.list_sessions(project_id, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/discard",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def discard_session(
    project_id: UUID,
    session_id: str,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """Discard a suggestion session and delete its branch."""
    await service.discard(project_id, session_id, user)


@router.post(
    "/{project_id}/suggestions/beacon",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def beacon_save(
    project_id: UUID,
    data: SuggestionBeaconRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    token: str = Query(..., description="Beacon authentication token"),
) -> None:
    """Handle a sendBeacon flush with token-based authentication.

    This endpoint does not require an Authorization header.
    Authentication is via the short-lived beacon token query parameter.
    """
    await service.beacon_save(project_id, data, token)


@router.get(
    "/{project_id}/suggestions/pending",
    response_model=SuggestionSessionListResponse,
)
async def list_pending(
    project_id: UUID,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
    queue: str | None = Query(
        None,
        pattern="^(triage|review)$",
        description=(
            "Split the queue by submitter tier (R9): 'triage' for anonymous and "
            "untrusted submissions, 'review' for trusted ones. Omit for all."
        ),
    ),
) -> SuggestionSessionListResponse:
    """List pending suggestion sessions for review (editors/admins only)."""
    return await service.list_pending(project_id, user, queue)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/approve",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def approve_session(
    project_id: UUID,
    session_id: str,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """Approve a suggestion session — merges the PR."""
    await service.approve(project_id, session_id, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reject_session(
    project_id: UUID,
    session_id: str,
    data: SuggestionRejectRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """Reject a suggestion session with a reason."""
    await service.reject(project_id, session_id, data, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/request-changes",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def request_changes(
    project_id: UUID,
    session_id: str,
    data: SuggestionRequestChangesRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """Request changes on a suggestion session with feedback."""
    await service.request_changes(project_id, session_id, data, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/resubmit",
    response_model=SuggestionSubmitResponse,
)
async def resubmit_session(
    project_id: UUID,
    session_id: str,
    data: SuggestionResubmitRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> SuggestionSubmitResponse:
    """Resubmit a suggestion session after addressing requested changes."""
    return await service.resubmit(project_id, session_id, data, user)


@router.get(
    "/{project_id}/suggestions/capabilities",
    response_model=SuggestionCapabilitiesResponse,
)
async def get_capabilities(
    project_id: UUID,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: OptionalUser,
) -> SuggestionCapabilitiesResponse:
    """What the caller may do on this project, and how trust is earned.

    Drives the editor's explained-disabled affordances (AE2). Reads the same
    tier resolution the server-side gates use, so the UI and the enforcement
    can never disagree.
    """
    return await service.get_capabilities(project_id, user)


@router.post(
    "/{project_id}/suggestions/sessions/{session_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_session(
    project_id: UUID,
    session_id: str,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
    note: str | None = Query(None, max_length=1000),
) -> None:
    """Dismiss a triage-queue suggestion without merging it (editors/admins)."""
    await service.dismiss(project_id, session_id, user, note)


@router.post(
    "/{project_id}/suggestions/bulk-review",
    response_model=BulkReviewResponse,
)
async def bulk_review(
    project_id: UUID,
    data: BulkReviewRequest,
    service: Annotated[SuggestionService, Depends(get_service)],
    user: RequiredUser,
) -> BulkReviewResponse:
    """Accept or dismiss many suggestions at once (editors/admins only).

    Partial-success: the response reports per-session failures rather than
    aborting the batch on the first stale row.
    """
    return await service.bulk_review(project_id, data, user)
