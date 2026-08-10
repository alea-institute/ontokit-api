"""Project-admin endpoints for the contribution trust ladder (R6, R7, R11).

Grant, refuse, or revoke a member's trusted status, and configure the
per-project promotion threshold and auto-accept quiet period. Owner/admin only,
with a metadata-only audit line on every privilege change.
"""

import base64
import binascii
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.auth import CurrentUser, RequiredUser
from ontokit.core.database import get_db
from ontokit.models.project import Project, ProjectMember
from ontokit.models.suggestion_outcome import SuggestionOutcome
from ontokit.schemas.trust import (
    MemberTrustResponse,
    MemberTrustUpdate,
    ProjectTrustSettings,
    ProjectTrustSettingsUpdate,
    SuggestionOutcomeItem,
    SuggestionOutcomeListResponse,
)
from ontokit.services.trust_service import TrustService

logger = logging.getLogger(__name__)

router = APIRouter()


async def _load_project(db: AsyncSession, project_id: UUID) -> Project:
    result = await db.execute(
        select(Project).options(selectinload(Project.members)).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _require_owner_or_admin(project: Project, user: RequiredUser) -> None:
    """Only owners and admins may move someone on the ladder."""
    if user.is_superadmin or project.owner_id == user.id:
        return
    member = TrustService.get_member(project, user.id)
    if member is None or member.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner or admin access required to manage trust",
        )


def _encode_outcome_cursor(created_at: datetime, outcome_id: UUID) -> str:
    payload = f"{created_at.isoformat()}|{outcome_id}".encode()
    return base64.urlsafe_b64encode(payload).decode()


def _decode_outcome_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        payload = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True).decode()
        created_at_raw, outcome_id_raw = payload.rsplit("|", 1)
        created_at = datetime.fromisoformat(created_at_raw)
        outcome_id = UUID(outcome_id_raw)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp must include a timezone")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid outcome cursor",
        ) from exc
    return created_at, outcome_id


@router.get("/{project_id}/trust/members", response_model=list[MemberTrustResponse])
async def list_member_trust(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> list[MemberTrustResponse]:
    """Every member's tier, grant state, and accepted-suggestion count."""
    project = await _load_project(db, project_id)
    _require_owner_or_admin(project, user)

    service = TrustService(db)
    rows: list[MemberTrustResponse] = []
    for member in project.members:
        rows.append(
            MemberTrustResponse(
                user_id=member.user_id,
                role=member.role,
                tier=service.resolve_tier(project, _member_as_user(member)),
                is_trusted=member.is_trusted,
                trust_override=member.trust_override,  # type: ignore[arg-type]
                trust_granted_at=member.trust_granted_at,
                trust_granted_by=member.trust_granted_by,
                accepted_count=await service.count_accepted(project_id, member.user_id),
            )
        )
    return rows


@router.patch("/{project_id}/trust/members/{target_user_id}", response_model=MemberTrustResponse)
async def update_member_trust(
    project_id: UUID,
    target_user_id: str,
    data: MemberTrustUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> MemberTrustResponse:
    """Grant, refuse, revoke, or clear a member's trusted status (R6).

    Clearing back to ``none`` hands the member back to the ladder and re-runs
    auto-promotion immediately, so reversing a refusal does not make the
    contributor wait for their next acceptance.
    """
    project = await _load_project(db, project_id)
    _require_owner_or_admin(project, user)

    service = TrustService(db)
    try:
        member = await service.set_trust_override(
            project, target_user_id, data.trust_override, user
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    await db.commit()
    await db.refresh(member)

    return MemberTrustResponse(
        user_id=member.user_id,
        role=member.role,
        tier=service.resolve_tier(project, _member_as_user(member)),
        is_trusted=member.is_trusted,
        trust_override=member.trust_override,  # type: ignore[arg-type]
        trust_granted_at=member.trust_granted_at,
        trust_granted_by=member.trust_granted_by,
        accepted_count=await service.count_accepted(project_id, member.user_id),
    )


@router.get("/{project_id}/trust/settings", response_model=ProjectTrustSettings)
async def get_trust_settings(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> ProjectTrustSettings:
    """Read the project's promotion threshold and auto-accept configuration."""
    project = await _load_project(db, project_id)
    _require_owner_or_admin(project, user)
    return ProjectTrustSettings(
        trust_promotion_threshold=project.trust_promotion_threshold,
        auto_accept_enabled=project.auto_accept_enabled,
        auto_accept_quiet_days=project.auto_accept_quiet_days,
    )


@router.get("/{project_id}/trust/outcomes", response_model=SuggestionOutcomeListResponse)
async def list_suggestion_outcomes(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
) -> SuggestionOutcomeListResponse:
    """List stored suggestion-outcome audit facts, newest first."""
    project = await _load_project(db, project_id)
    _require_owner_or_admin(project, user)

    count_result = await db.execute(
        select(func.count(SuggestionOutcome.id)).where(SuggestionOutcome.project_id == project_id)
    )
    total = count_result.scalar_one()

    query = select(SuggestionOutcome).where(SuggestionOutcome.project_id == project_id)
    if cursor is not None:
        cursor_created_at, cursor_id = _decode_outcome_cursor(cursor)
        query = query.where(
            or_(
                SuggestionOutcome.created_at < cursor_created_at,
                and_(
                    SuggestionOutcome.created_at == cursor_created_at,
                    SuggestionOutcome.id < cursor_id,
                ),
            )
        )
    query = query.order_by(SuggestionOutcome.created_at.desc(), SuggestionOutcome.id.desc()).limit(
        limit + 1
    )

    result = await db.execute(query)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [SuggestionOutcomeItem.model_validate(row) for row in page_rows]
    next_cursor = (
        _encode_outcome_cursor(page_rows[-1].created_at, page_rows[-1].id) if has_more else None
    )
    return SuggestionOutcomeListResponse(items=items, total=total, next_cursor=next_cursor)


@router.patch("/{project_id}/trust/settings", response_model=ProjectTrustSettings)
async def update_trust_settings(
    project_id: UUID,
    data: ProjectTrustSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> ProjectTrustSettings:
    """Configure the promotion threshold and auto-accept window (R6, R11).

    Turning auto-accept ON is the single most consequential switch in this
    feature — it is off by default (KTD8) and its flip is audited.
    """
    project = await _load_project(db, project_id)
    _require_owner_or_admin(project, user)

    if data.trust_promotion_threshold is not None:
        project.trust_promotion_threshold = data.trust_promotion_threshold
    if data.auto_accept_quiet_days is not None:
        project.auto_accept_quiet_days = data.auto_accept_quiet_days
    if data.auto_accept_enabled is not None and (
        data.auto_accept_enabled != project.auto_accept_enabled
    ):
        project.auto_accept_enabled = data.auto_accept_enabled
        logger.info(
            "auto-accept %s: project=%s actor=%s quiet_days=%s",
            "ENABLED" if data.auto_accept_enabled else "disabled",
            project_id,
            user.id,
            project.auto_accept_quiet_days,
        )

    await db.commit()
    await db.refresh(project)

    return ProjectTrustSettings(
        trust_promotion_threshold=project.trust_promotion_threshold,
        auto_accept_enabled=project.auto_accept_enabled,
        auto_accept_quiet_days=project.auto_accept_quiet_days,
    )


def _member_as_user(member: ProjectMember) -> CurrentUser:
    """Adapt a membership row to the shape ``resolve_tier`` expects."""
    return CurrentUser(id=member.user_id)
