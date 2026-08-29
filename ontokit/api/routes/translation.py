"""Project translation configuration and public language palette routes."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from rdflib.namespace import SKOS
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.api.utils.redis import get_arq_pool
from ontokit.core.auth import CurrentUser, OptionalUser, RequiredUser
from ontokit.core.database import get_db
from ontokit.git import GitRepositoryService, get_git_service
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.project import Project, ProjectMember, get_git_ontology_path
from ontokit.models.translation import (
    NativeReviewerLanguage,
    ProjectTranslationConfig,
    TranslationJob,
    TranslationRecord,
)
from ontokit.schemas.translation import (
    LanguagePaletteEntry,
    ReviewerEntry,
    ReviewerLanguagesResponse,
    ReviewerLanguagesUpdate,
    TranslateFieldRequest,
    TranslationBackfillPreview,
    TranslationBackfillRequest,
    TranslationBackfillStatus,
    TranslationBulkConfirmRequest,
    TranslationBulkConfirmResponse,
    TranslationBulkResult,
    TranslationConfigResponse,
    TranslationConfigUpdate,
    TranslationJobAccepted,
    TranslationRecordSummary,
    TranslationReviewRequest,
    TranslationSpeedMode,
    VerificationMechanism,
)
from ontokit.services.commit_identity import CommitIdentityService
from ontokit.services.language_palette import LANGUAGE_PALETTE
from ontokit.services.llm import release_rate_limit_units, reserve_rate_limit_units
from ontokit.services.llm.crypto import encrypt_secret
from ontokit.services.project_access_policy import (
    load_visible_project,
    require_user_managed_project,
    require_visible_project,
    visible_project_clause,
)
from ontokit.services.translation_backfill import preview_backfill_cost, select_backfill_literals
from ontokit.services.translation_coverage import TranslationCoverageService
from ontokit.services.translation_jobs import (
    TranslationEnqueueError,
    TranslationTask,
    enqueue_translation_tasks,
    translation_entity_job_id,
)
from ontokit.services.translation_review import TranslationReviewConflict, TranslationReviewService

logger = logging.getLogger(__name__)
router = APIRouter()
public_router = APIRouter()


def _get_git() -> GitRepositoryService:
    return get_git_service()


async def _get_member_role(db: AsyncSession, project_id: UUID, user_id: str) -> str | None:
    member = await _get_member(db, project_id, user_id)
    return member.role if member else None


async def _get_member(db: AsyncSession, project_id: UUID, user_id: str) -> ProjectMember | None:
    result = await db.execute(
        select(ProjectMember)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            visible_project_clause(),
        )
    )
    return result.scalar_one_or_none()


async def _reviewer_languages(db: AsyncSession, member_id: UUID) -> set[str]:
    result = await db.execute(
        select(NativeReviewerLanguage.language).where(NativeReviewerLanguage.member_id == member_id)
    )
    return set(result.scalars().all())


async def _review_context(
    db: AsyncSession, project_id: UUID, user: CurrentUser
) -> tuple[Project, ProjectMember, set[str]]:
    project_result = await db.execute(
        select(Project)
        .options(selectinload(Project.github_integration))
        .where(Project.id == project_id)
    )
    project = project_result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    require_visible_project(project)
    member = await _get_member(db, project_id, str(user.id))
    if member is None:
        raise HTTPException(status_code=403, detail="Not a project member")
    return project, member, await _reviewer_languages(db, member.id)


def _record_summary(record: TranslationRecord) -> TranslationRecordSummary:
    return TranslationRecordSummary(
        id=record.id,
        project_id=record.project_id,
        entity_iri=record.entity_iri,
        predicate=record.predicate,
        language=record.language,
        proposed_value=record.proposed_value,
        state=record.state,
        confirming_member_id=record.confirming_member_id,
    )


async def _require_member(
    db: AsyncSession, project_id: UUID, user_id: str, is_superadmin: bool
) -> str:
    role = await _get_member_role(db, project_id, user_id)
    if role is None:
        if is_superadmin:
            await load_visible_project(db, project_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a project member",
            )
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
    require_visible_project(project)
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


def _get_redis() -> Any:
    try:
        from ontokit.main import redis_pool

        return redis_pool
    except (ImportError, AttributeError):
        return None


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


@router.get("/{project_id}/translation/backfill/preview", response_model=TranslationBackfillPreview)
async def preview_translation_backfill(
    project_id: UUID,
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
    language: Annotated[str | None, Query()] = None,
    era_before: Annotated[datetime | None, Query()] = None,
    never_confirmed: Annotated[bool | None, Query()] = None,
) -> TranslationBackfillPreview:
    """Return scope and conservative cost without provider calls or audit writes."""
    await _require_member(db, project_id, user.id, user.is_superadmin)
    config = await _get_config(db, project_id)
    llm_config = await db.scalar(
        select(ProjectLLMConfig).where(ProjectLLMConfig.project_id == project_id)
    )
    if config is None or llm_config is None or not config.primary_model:
        raise HTTPException(status_code=409, detail="Translation configuration is incomplete")
    literals = await select_backfill_literals(
        db,
        git,
        project_id,
        branch,
        language=language,
        era_before=era_before,
        never_confirmed=never_confirmed,
    )
    result = await preview_backfill_cost(
        literals,
        mechanism=config.verification_mechanism,
        primary_model=config.primary_model,
        verifier_model=config.verifier_model or config.primary_model,
        primary_provider=config.primary_provider or llm_config.provider,
        speed_mode=config.speed_mode,
    )
    return TranslationBackfillPreview(
        literal_count=result.literal_count,
        expected_cost_usd=result.expected_cost_usd,
        upper_bound_cost_usd=result.upper_bound_cost_usd,
        batch_discount_applied=result.batch_discount_applied,
    )


@router.post(
    "/{project_id}/translation/backfill",
    response_model=TranslationJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def launch_translation_backfill(
    project_id: UUID,
    data: TranslationBackfillRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> TranslationJobAccepted:
    """Claim a durable project job before enqueueing asynchronous backfill work."""
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)
    active = await db.execute(
        select(TranslationJob).where(
            TranslationJob.project_id == project_id,
            TranslationJob.status.in_(("pending", "running")),
        )
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Translation backfill already in progress")
    job = TranslationJob(
        id=uuid.uuid4(),
        project_id=project_id,
        branch=data.branch,
        language=data.language,
        era_before=data.era_before,
        never_confirmed=data.never_confirmed,
        status="pending",
    )
    try:
        db.add(job)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Translation backfill already in progress"
        ) from None
    try:
        pool = await get_arq_pool()
        if pool is None:
            raise RuntimeError("background job queue unavailable")
        queued = await pool.enqueue_job(
            "run_translation_backfill_task",
            str(project_id),
            data.branch,
            str(job.id),
            user.id,
        )
        if queued is None:
            raise RuntimeError("translation queue refused the job")
    except Exception:
        await db.execute(delete(TranslationJob).where(TranslationJob.id == job.id))
        await db.commit()
        raise
    return TranslationJobAccepted(job_id=str(job.id))


@router.get(
    "/{project_id}/translation/backfill/status",
    response_model=TranslationBackfillStatus | None,
)
async def get_translation_backfill_status(
    project_id: UUID,
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> TranslationBackfillStatus | None:
    await _require_member(db, project_id, user.id, user.is_superadmin)
    result = await db.execute(
        select(TranslationJob)
        .where(TranslationJob.project_id == project_id, TranslationJob.branch == branch)
        .order_by(TranslationJob.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        return None
    return TranslationBackfillStatus(
        job_id=str(job.id),
        status=cast(Literal["pending", "running", "completed", "failed"], job.status),
        total=job.total_literals,
        completed=job.completed_literals,
        error=job.error_message,
    )


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
    branch: Annotated[str, Query(min_length=1)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: OptionalUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
    language: Annotated[str | None, Query(min_length=1)] = None,
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
    require_user_managed_project(await load_visible_project(db, project_id))
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
        if value is None:
            if field in {
                "primary_provider",
                "primary_model",
                "verifier_provider",
                "verifier_model",
            }:
                setattr(config, field, None)
            continue
        setattr(config, field, value.value if hasattr(value, "value") else value)
    if data.verifier_api_key:
        config.verifier_api_key_encrypted = encrypt_secret(data.verifier_api_key)

    await db.commit()
    await db.refresh(config)
    return _to_response(config)


@router.post(
    "/{project_id}/translation/entities/translate-field",
    response_model=TranslationJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def translate_entity_field(
    project_id: UUID,
    data: TranslateFieldRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> TranslationJobAccepted:
    """Queue an explicitly requested definition/example translation for one entity."""
    role = await _require_member(db, project_id, user.id, user.is_superadmin)
    if role not in {"owner", "admin", "editor"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"LLM features are not available for your role ({role})",
        )
    redis = _get_redis()
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Translation rate limiter is unavailable",
        )
    config = await _get_config(db, project_id)
    if config is None:
        raise HTTPException(status_code=409, detail="Translation is not configured")
    pool = await get_arq_pool()
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background job queue unavailable",
        )
    predicate = SKOS.definition if data.predicate == "skos:definition" else SKOS.example
    task = TranslationTask(data.entity_iri, str(predicate), None, None, None, "fast")
    reservation_id = translation_entity_job_id(project_id, data.branch, task)
    call_units = len(config.language_tags) * (
        4 if config.verification_mechanism == "consensus" else 2
    )
    reservation = await reserve_rate_limit_units(
        redis,
        str(project_id),
        user.id,
        role,
        call_units,
        reservation_id=reservation_id,
    )
    if not reservation.accepted:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily LLM call limit cannot cover {call_units} provider calls",
        )
    try:
        enqueue_result = await enqueue_translation_tasks(
            pool, redis, project_id, data.branch, user.id, [task]
        )
    except TranslationEnqueueError as exc:
        if reservation.acquired:
            await release_rate_limit_units(
                redis,
                str(project_id),
                user.id,
                role,
                call_units,
                reservation_id=reservation_id,
            )
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.queue_unavailable
                else status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=str(exc),
        ) from exc
    if reservation.acquired and not enqueue_result.newly_queued_job_ids:
        await release_rate_limit_units(
            redis,
            str(project_id),
            user.id,
            role,
            call_units,
            reservation_id=reservation_id,
        )
    return TranslationJobAccepted(job_id=enqueue_result.job_ids[0])


@router.get("/{project_id}/translation/reviewers", response_model=list[ReviewerEntry])
async def list_translation_reviewers(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> list[ReviewerEntry]:
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)
    result = await db.execute(
        select(ProjectMember, NativeReviewerLanguage.language)
        .outerjoin(NativeReviewerLanguage, NativeReviewerLanguage.member_id == ProjectMember.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.id, NativeReviewerLanguage.language)
    )
    entries: dict[UUID, ReviewerEntry] = {}
    for member, language in result.all():
        entry = entries.setdefault(
            member.id, ReviewerEntry(member_id=member.id, user_id=member.user_id, languages=[])
        )
        if language is not None:
            entry.languages.append(language)
    return [entry for entry in entries.values() if entry.languages]


@router.put("/{project_id}/translation/reviewers/{member_id}", response_model=ReviewerEntry)
async def update_translation_reviewer(
    project_id: UUID,
    member_id: UUID,
    data: ReviewerLanguagesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> ReviewerEntry:
    await _require_owner_or_admin(db, project_id, user.id, user.is_superadmin)
    require_user_managed_project(await load_visible_project(db, project_id))
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.id == member_id, ProjectMember.project_id == project_id
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Project member not found")
    await db.execute(
        delete(NativeReviewerLanguage).where(NativeReviewerLanguage.member_id == member_id)
    )
    for language in data.languages:
        db.add(NativeReviewerLanguage(member_id=member_id, language=language))
    await db.commit()
    return ReviewerEntry(member_id=member.id, user_id=member.user_id, languages=data.languages)


@router.get(
    "/{project_id}/translation/my-reviewer-languages",
    response_model=ReviewerLanguagesResponse,
)
async def get_my_reviewer_languages(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
) -> ReviewerLanguagesResponse:
    await load_visible_project(db, project_id)
    member = await _get_member(db, project_id, user.id)
    if member is None:
        if user.is_superadmin:
            return ReviewerLanguagesResponse(languages=[])
        raise HTTPException(status_code=403, detail="Not a project member")
    return ReviewerLanguagesResponse(languages=sorted(await _reviewer_languages(db, member.id)))


async def _review_record(
    *,
    project_id: UUID,
    record_id: UUID,
    data: TranslationReviewRequest,
    db: AsyncSession,
    user: CurrentUser,
    git: GitRepositoryService,
    reject: bool,
) -> TranslationRecordSummary:
    project, member, languages = await _review_context(db, project_id, user)
    result = await db.execute(
        select(TranslationRecord).where(
            TranslationRecord.id == record_id, TranslationRecord.project_id == project_id
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Translation record not found")
    try:
        author_name, author_email = await CommitIdentityService(db).resolve(
            str(user.id), getattr(user, "name", None)
        )
        service = TranslationReviewService(db, git)
        if reject:
            await service.reject_loaded(
                project_id=project_id,
                branch=data.branch,
                filename=get_git_ontology_path(project),
                member=member,
                reviewer_languages=languages,
                record=record,
                author_name=author_name,
                author_email=author_email,
            )
        else:
            await service.confirm_loaded(
                project_id=project_id,
                branch=data.branch,
                filename=get_git_ontology_path(project),
                member=member,
                reviewer_languages=languages,
                record=record,
                author_name=author_name,
                author_email=author_email,
            )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, TranslationReviewConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _record_summary(record)


@router.post(
    "/{project_id}/translation/records/{record_id:uuid}/confirm",
    response_model=TranslationRecordSummary,
)
async def confirm_translation_record(
    project_id: UUID,
    record_id: UUID,
    data: TranslationReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> TranslationRecordSummary:
    return await _review_record(
        project_id=project_id,
        record_id=record_id,
        data=data,
        db=db,
        user=user,
        git=git,
        reject=False,
    )


@router.post(
    "/{project_id}/translation/records/{record_id:uuid}/reject",
    response_model=TranslationRecordSummary,
)
async def reject_translation_record(
    project_id: UUID,
    record_id: UUID,
    data: TranslationReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> TranslationRecordSummary:
    return await _review_record(
        project_id=project_id,
        record_id=record_id,
        data=data,
        db=db,
        user=user,
        git=git,
        reject=True,
    )


@router.post(
    "/{project_id}/translation/records/confirm-bulk",
    response_model=TranslationBulkConfirmResponse,
)
async def confirm_translation_records_bulk(
    project_id: UUID,
    data: TranslationBulkConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: RequiredUser,
    git: Annotated[GitRepositoryService, Depends(_get_git)],
) -> TranslationBulkConfirmResponse:
    results: list[TranslationBulkResult] = []
    for record_id in data.record_ids:
        try:
            await _review_record(
                project_id=project_id,
                record_id=record_id,
                data=TranslationReviewRequest(branch=data.branch),
                db=db,
                user=user,
                git=git,
                reject=False,
            )
        except HTTPException as exc:
            await db.rollback()
            results.append(
                TranslationBulkResult(record_id=record_id, ok=False, error=str(exc.detail))
            )
        else:
            results.append(TranslationBulkResult(record_id=record_id, ok=True, error=None))
    return TranslationBulkConfirmResponse(results=results)


@public_router.get("/translation/palette", response_model=list[LanguagePaletteEntry])
async def get_language_palette() -> list[LanguagePaletteEntry]:
    return LANGUAGE_PALETTE
