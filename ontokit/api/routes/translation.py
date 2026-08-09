"""Project translation configuration and public language palette routes."""

from __future__ import annotations

import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import OptionalUser, RequiredUser
from ontokit.core.database import get_db
from ontokit.git import GitRepositoryService, get_git_service
from ontokit.models.project import Project, ProjectMember
from ontokit.models.translation import ProjectTranslationConfig
from ontokit.schemas.translation import (
    LanguagePaletteEntry,
    TranslationConfigResponse,
    TranslationConfigUpdate,
    TranslationSpeedMode,
    VerificationMechanism,
)
from ontokit.services.language_palette import LANGUAGE_PALETTE
from ontokit.services.llm.crypto import encrypt_secret
from ontokit.services.translation_coverage import TranslationCoverageService

router = APIRouter()
public_router = APIRouter()


def _get_git() -> GitRepositoryService:
    return get_git_service()


async def _get_member_role(db: AsyncSession, project_id: UUID, user_id: str) -> str | None:
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    return member.role if member else None


async def _require_member(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool
) -> str:
    role = await _get_member_role(db, project_id, user_id)
    if role is None and not is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a project member")
    return role if role is not None else "admin"


async def _require_owner_or_admin(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool
) -> None:
    role = await _require_member(db, project_id, user_id, is_superadmin)
    if role not in ("owner", "admin") and not is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owner or admin can perform this action",
        )


async def _require_project_view(db: AsyncSession, project_id: UUID, user: object | None) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.is_public:
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project access denied")
    member = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if member.scalar_one_or_none() is None and not getattr(user, "is_superadmin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Project access denied")


async def _get_config(db: AsyncSession, project_id: UUID) -> ProjectTranslationConfig | None:
    result = await db.execute(
        select(ProjectTranslationConfig).where(ProjectTranslationConfig.project_id == project_id)
    )
    return result.scalar_one_or_none()


def _to_response(config: ProjectTranslationConfig | None) -> TranslationConfigResponse:
    if config is None:
        return TranslationConfigResponse()
    return TranslationConfigResponse(
        language_tags=config.language_tags,
        verification_mechanism=VerificationMechanism(config.verification_mechanism),
        consensus_threshold=config.consensus_threshold,
        confidence_threshold=config.confidence_threshold,
        translate_definitions=config.translate_definitions,
        translate_examples=config.translate_examples,
        speed_mode=TranslationSpeedMode(config.speed_mode),
        provisional_gate=config.provisional_gate,
        primary_provider=config.primary_provider,
        primary_model=config.primary_model,
        verifier_provider=config.verifier_provider,
        verifier_model=config.verifier_model,
        verifier_api_key_set=bool(config.verifier_api_key_encrypted),
    )


@router.get("/{project_id}/translation/config", response_model=TranslationConfigResponse)
async def get_translation_config(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> TranslationConfigResponse:
    await _require_member(db, project_id, user.id, user.is_superadmin)
    return _to_response(await _get_config(db, project_id))


@router.get("/{project_id}/translation/coverage")
async def get_translation_coverage(
    project_id: UUID,
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> dict[str, object]:
    await _require_project_view(db, project_id, user)
    return await TranslationCoverageService(db, git).coverage(project_id, branch)


@router.get("/{project_id}/translation/entity-state")
async def get_translation_entity_state(
    project_id: UUID,
    entity_iri: Annotated[str, Query(min_length=1)],
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> dict[str, object]:
    await _require_project_view(db, project_id, user)
    return await TranslationCoverageService(db, git).entity_state(project_id, entity_iri, branch)


@router.get("/{project_id}/translation/provisional")
async def get_provisional_translations(
    project_id: UUID,
    language: Annotated[str, Query(min_length=1)],
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> list[dict[str, object]]:
    await _require_project_view(db, project_id, user)
    return await TranslationCoverageService(db, git).provisional(project_id, language, branch)


@router.put("/{project_id}/translation/config", response_model=TranslationConfigResponse)
async def update_translation_config(
    project_id: UUID,
    data: TranslationConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> TranslationConfigResponse:
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)
    config = await _get_config(db, project_id)
    if config is None:
        config = ProjectTranslationConfig(
            id=uuid.uuid4(),
            project_id=project_id,
            language_tags=[],
            verification_mechanism="consensus",
            consensus_threshold=0.85,
            confidence_threshold=0.80,
            translate_definitions=False,
            translate_examples=False,
            speed_mode="batch",
            provisional_gate=False,
        )
        db.add(config)

    values = data.model_dump(exclude_unset=True, exclude={"verifier_api_key"})
    for field, value in values.items():
        if value is not None or field in {"primary_provider", "primary_model"}:
            setattr(config, field, value.value if hasattr(value, "value") else value)
    if data.verifier_api_key:
        config.verifier_api_key_encrypted = encrypt_secret(data.verifier_api_key)

    await db.commit()
    await db.refresh(config)
    return _to_response(config)


@public_router.get("/translation/palette", response_model=list[LanguagePaletteEntry])
async def get_language_palette() -> list[LanguagePaletteEntry]:
    return LANGUAGE_PALETTE
