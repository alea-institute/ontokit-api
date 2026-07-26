"""User settings endpoints.

The per-user GitHub PAT WRITE endpoints are retired (R3/KD6): all mirror
operations now authenticate as the system mirror identity, and a lay
contributor should never be asked for a GitHub credential.

The read paths remain for one release (KTD15) so an in-flight deployment with
stored tokens does not break: the status endpoint lets an older client render a
coherent state, and repo listing still works while a project is being migrated
onto the system identity. A follow-up drops the table.

**PR Party reviewers are the one carve-out, and they are not an exception to
the above.** A registered PR Party reviewer does hold a personal GitHub write
PAT, because casting a review *as that human* is the entire point (R8) and no
system identity can stand in for it. That credential has nothing to do with
this module: it lives in its own table (``pr_party_credential``), under its own
MultiFernet key domain, reachable only through ``/pr-party/*``, and the reviewer
registry is provisioned from configuration rather than self-service (KTD12).
Contributor PAT retirement stands — a lay contributor is still never asked for
a GitHub credential, and nothing here comes back.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import RequiredUser
from ontokit.core.database import get_db
from ontokit.core.encryption import decrypt_token
from ontokit.models.user_github_token import UserGitHubToken
from ontokit.schemas.user_settings import (
    CommitIdentityResponse,
    CommitIdentityUpdate,
    GitHubRepoInfo,
    GitHubRepoListResponse,
    GitHubTokenStatus,
    UserSearchResponse,
    UserSearchResult,
)
from ontokit.services.commit_identity import CommitIdentityService, noreply_alias
from ontokit.services.github_service import GitHubService, get_github_service
from ontokit.services.user_service import UserService, get_user_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me/github-token", response_model=GitHubTokenStatus)
async def get_github_token_status(
    user: RequiredUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GitHubTokenStatus:
    """Report whether a legacy stored GitHub token exists.

    Read-only and deprecated. There is no longer any way to create one — see
    the module docstring.
    """
    result = await db.execute(select(UserGitHubToken).where(UserGitHubToken.user_id == user.id))
    token_row = result.scalar_one_or_none()
    if not token_row:
        return GitHubTokenStatus(has_token=False)
    return GitHubTokenStatus(
        has_token=True,
        github_username=token_row.github_username,
    )


@router.get("/me/github-repos", response_model=GitHubRepoListResponse)
async def list_github_repos(
    user: RequiredUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    github_service: Annotated[GitHubService, Depends(get_github_service)],
    q: str | None = Query(default=None, description="Search query to filter repos"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
) -> GitHubRepoListResponse:
    """List GitHub repositories accessible to the user's stored PAT.

    Supports optional `?q=` search query for filtering.
    """
    # Get the user's token
    result = await db.execute(select(UserGitHubToken).where(UserGitHubToken.user_id == user.id))
    token_row = result.scalar_one_or_none()
    if not token_row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token found. Connect your GitHub account in Settings first.",
        )

    token = decrypt_token(token_row.encrypted_token)

    try:
        repos = await github_service.list_user_repos(
            token=token, query=q, page=page, per_page=per_page
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch repos from GitHub: {e}",
        ) from e

    items = [
        GitHubRepoInfo(
            full_name=r.get("full_name", ""),
            owner=r.get("owner", {}).get("login", ""),
            name=r.get("name", ""),
            description=r.get("description"),
            private=r.get("private", False),
            default_branch=r.get("default_branch", "main"),
            html_url=r.get("html_url", ""),
        )
        for r in repos
    ]

    return GitHubRepoListResponse(items=items, total=len(items))


@router.get("/search", response_model=UserSearchResponse)
async def search_users(
    user: RequiredUser,  # noqa: ARG001
    user_service: Annotated[UserService, Depends(get_user_service)],
    q: str = Query(..., min_length=2, description="Search query for username, email, or name"),
    limit: int = Query(default=10, ge=1, le=50),
) -> UserSearchResponse:
    """Search Zitadel users by username, email, or display name.

    Requires authentication. Returns matching users for the add-member autocomplete.
    """
    results, total = await user_service.search_users(query=q, limit=limit)

    items = [
        UserSearchResult(
            id=r.get("id") or "",
            username=r.get("username") or "",
            display_name=r.get("display_name"),
            email=r.get("email"),
        )
        for r in results
    ]

    return UserSearchResponse(items=items, total=total)


@router.get("/me/commit-identity", response_model=CommitIdentityResponse)
async def get_commit_identity(
    user: RequiredUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommitIdentityResponse:
    """How this contributor's commits are authored (R14, R15).

    The default is a synthetic noreply alias — a real address never enters
    permanent, publicly mirrored git history unless the contributor explicitly
    opts in with a verified one.
    """
    service = CommitIdentityService(db)
    preference = await service.get_preference(user.id)
    _, effective = await service.resolve(user.id, user.name)

    return CommitIdentityResponse(
        display_name=user.name,
        noreply_alias=noreply_alias(user.id, user.name),
        commit_email=preference.commit_email if preference else None,
        commit_email_verified=bool(preference and preference.commit_email_verified),
        use_verified_email=bool(preference and preference.use_verified_email),
        effective_email=effective,
    )


@router.patch("/me/commit-identity", response_model=CommitIdentityResponse)
async def update_commit_identity(
    body: CommitIdentityUpdate,
    user: RequiredUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommitIdentityResponse:
    """Set the opt-in authoring address, or toggle its use (R15).

    Changing the address resets verification: an unverified address is never
    honored, and re-pointing the preference is exactly when someone would try
    to slip one through.
    """
    service = CommitIdentityService(db)
    # exclude_unset preserves PATCH semantics: omission means unchanged, while
    # an explicit JSON null for commit_email clears the stored address.
    await service.set_preference(user.id, **body.model_dump(exclude_unset=True))
    await db.commit()

    return await get_commit_identity(user, db)
