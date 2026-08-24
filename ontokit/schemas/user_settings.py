"""User settings schemas for GitHub token management and repo listing."""

from pydantic import BaseModel, Field


class GitHubTokenStatus(BaseModel):
    """Schema for checking whether the user has a stored token."""

    has_token: bool
    github_username: str | None = None


class GitHubRepoInfo(BaseModel):
    """Schema for a GitHub repository returned by the repo picker."""

    full_name: str
    owner: str
    name: str
    description: str | None = None
    private: bool = False
    default_branch: str = "main"
    html_url: str


class GitHubRepoListResponse(BaseModel):
    """Paginated list of GitHub repositories."""

    items: list[GitHubRepoInfo]
    total: int


class UserSearchResult(BaseModel):
    """A single user returned by the user search endpoint."""

    id: str
    username: str
    display_name: str | None = None
    email: str | None = None


class UserSearchResponse(BaseModel):
    """Paginated list of user search results."""

    items: list[UserSearchResult]
    total: int


class CommitIdentityResponse(BaseModel):
    """How this contributor's commits are authored (R14, R15)."""

    display_name: str | None = None
    # The synthetic alias used by default. Never a real address.
    noreply_alias: str
    commit_email: str | None = None
    commit_email_verified: bool = False
    use_verified_email: bool = False
    # The address that will actually appear in the next commit.
    effective_email: str


class CommitIdentityUpdate(BaseModel):
    """Opt in to authoring with a verified address instead of the alias."""

    commit_email: str | None = Field(
        default=None,
        max_length=320,
        description="Verified author address to store; explicit null clears the preference.",
    )
    use_verified_email: bool | None = Field(default=None)
