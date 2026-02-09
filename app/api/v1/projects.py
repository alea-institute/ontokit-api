"""Project management endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import OptionalUser, RequiredUser, RequiredUserWithToken
from app.core.database import get_db
from app.schemas.project import (
    MemberCreate,
    MemberListResponse,
    MemberResponse,
    MemberUpdate,
    ProjectCreate,
    ProjectImportResponse,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
)
from app.services.project_service import ProjectService, get_project_service
from app.services.storage import StorageService, get_storage_service

router = APIRouter()

# Maximum file size for import (50 MB)
MAX_IMPORT_FILE_SIZE = 50 * 1024 * 1024


def get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> ProjectService:
    """Dependency to get project service with database session."""
    return get_project_service(db)


def get_storage() -> StorageService:
    """Dependency to get storage service."""
    return get_storage_service()


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    service: Annotated[ProjectService, Depends(get_service)],
    user: OptionalUser,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    filter: str | None = Query(default=None, description="Filter: 'public', 'mine', or null for all accessible"),
) -> ProjectListResponse:
    """
    List projects accessible to the current user.

    For anonymous users, only public projects are returned.
    For authenticated users:
    - filter=public: Only public projects
    - filter=mine: Projects where user is a member
    - filter=null: All accessible (public + user's projects)
    """
    return await service.list_accessible(user, skip=skip, limit=limit, filter_type=filter)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    project: ProjectCreate,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> ProjectResponse:
    """Create a new project. Requires authentication."""
    return await service.create(project, user)


@router.post("/import", response_model=ProjectImportResponse, status_code=status.HTTP_201_CREATED)
async def import_project(
    file: UploadFile,
    is_public: Annotated[bool, Form()],
    service: Annotated[ProjectService, Depends(get_service)],
    storage: Annotated[StorageService, Depends(get_storage)],
    user: RequiredUser,
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
) -> ProjectImportResponse:
    """
    Create a project by importing an ontology file.

    Supported formats: OWL (.owl), RDF/XML (.rdf), Turtle (.ttl), N3 (.n3), JSON-LD (.jsonld)

    The project name and description will be extracted from the ontology metadata
    (dc:title, dcterms:title, rdfs:label for name; dc:description, dcterms:description,
    rdfs:comment for description). You can override these by providing the name and
    description form fields.

    Requires authentication.
    """
    # Check file size
    content = await file.read()
    if len(content) > MAX_IMPORT_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_IMPORT_FILE_SIZE // (1024 * 1024)} MB",
        )

    # Get filename
    filename = file.filename or "ontology.owl"

    return await service.create_from_import(
        file_content=content,
        filename=filename,
        is_public=is_public,
        owner=user,
        storage=storage,
        name_override=name,
        description_override=description,
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: UUID,
    service: Annotated[ProjectService, Depends(get_service)],
    user: OptionalUser,
) -> ProjectResponse:
    """
    Get a project by ID.

    Returns 403 if the project is private and user doesn't have access.
    """
    return await service.get(project_id, user)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    project: ProjectUpdate,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> ProjectResponse:
    """
    Update project settings.

    Only the owner or admin can update project settings.
    """
    return await service.update(project_id, project, user)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """
    Delete a project.

    Only the owner can delete a project.
    """
    await service.delete(project_id, user)


# Member management endpoints


@router.get("/{project_id}/members", response_model=MemberListResponse)
async def list_members(
    project_id: UUID,
    service: Annotated[ProjectService, Depends(get_service)],
    user_with_token: RequiredUserWithToken,
) -> MemberListResponse:
    """
    List members of a project.

    Only members can see the member list.
    """
    user, access_token = user_with_token
    return await service.list_members(project_id, user, access_token)


@router.post(
    "/{project_id}/members",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    project_id: UUID,
    member: MemberCreate,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> MemberResponse:
    """
    Add a member to a project.

    Only the owner or admin can add members.
    """
    return await service.add_member(project_id, member, user)


@router.patch("/{project_id}/members/{user_id}", response_model=MemberResponse)
async def update_member(
    project_id: UUID,
    user_id: str,
    member: MemberUpdate,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> MemberResponse:
    """
    Update a member's role.

    Only the owner or admin can update member roles.
    Admins cannot promote others to admin.
    """
    return await service.update_member(project_id, user_id, member, user)


@router.delete(
    "/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    project_id: UUID,
    user_id: str,
    service: Annotated[ProjectService, Depends(get_service)],
    user: RequiredUser,
) -> None:
    """
    Remove a member from a project.

    Owner and admin can remove members.
    Users can remove themselves from a project.
    Cannot remove the project owner.
    """
    await service.remove_member(project_id, user_id, user)
