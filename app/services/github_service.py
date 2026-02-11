"""GitHub integration service using GitHub App authentication."""

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt

from app.core.config import settings


@dataclass
class GitHubPR:
    """GitHub pull request information."""

    number: int
    title: str
    body: str | None
    state: str
    html_url: str
    head_ref: str
    base_ref: str
    user_login: str
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None
    merged: bool = False


@dataclass
class GitHubReview:
    """GitHub pull request review information."""

    id: int
    user_login: str
    state: str
    body: str | None
    submitted_at: datetime


@dataclass
class GitHubComment:
    """GitHub comment information."""

    id: int
    user_login: str
    body: str
    created_at: datetime
    updated_at: datetime


class GitHubService:
    """
    Service for interacting with GitHub API using GitHub App authentication.

    Uses installation tokens to authenticate API requests for specific repositories.
    """

    GITHUB_API_BASE = "https://api.github.com"
    TOKEN_EXPIRY_BUFFER = 300  # 5 minutes buffer before token expiry

    def __init__(self) -> None:
        self.app_id = settings.github_app_id
        self.private_key = settings.github_app_private_key
        self._installation_tokens: dict[int, tuple[str, float]] = {}

    def _generate_jwt(self) -> str:
        """Generate a JWT for GitHub App authentication."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Issued 60 seconds ago to account for clock drift
            "exp": now + (10 * 60),  # Expires in 10 minutes
            "iss": self.app_id,
        }

        # Handle private key that may be stored with literal \n
        private_key = self.private_key.replace("\\n", "\n")

        return jwt.encode(payload, private_key, algorithm="RS256")

    async def _get_installation_token(self, installation_id: int) -> str:
        """
        Get or refresh an installation access token.

        Args:
            installation_id: GitHub App installation ID

        Returns:
            Access token for the installation
        """
        # Check if we have a cached token that's still valid
        if installation_id in self._installation_tokens:
            token, expires_at = self._installation_tokens[installation_id]
            if time.time() < expires_at - self.TOKEN_EXPIRY_BUFFER:
                return token

        # Generate new installation token
        app_jwt = self._generate_jwt()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            data = response.json()

        token = data["token"]
        # Parse expiry time from ISO format
        expires_at_str = data["expires_at"]
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))

        # Cache the token
        self._installation_tokens[installation_id] = (
            token,
            expires_at.timestamp(),
        )

        return token

    async def _request(
        self,
        method: str,
        endpoint: str,
        installation_id: int,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make an authenticated request to the GitHub API.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            installation_id: GitHub App installation ID
            data: Request body data

        Returns:
            JSON response data
        """
        token = await self._get_installation_token(installation_id)

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=f"{self.GITHUB_API_BASE}{endpoint}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=data,
            )
            response.raise_for_status()

            if response.status_code == 204:
                return {}

            return response.json()

    # Pull Request Operations

    async def create_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
    ) -> GitHubPR:
        """
        Create a pull request on GitHub.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            title: PR title
            head: Source branch name
            base: Target branch name
            body: PR description

        Returns:
            GitHubPR with created PR details
        """
        data = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            installation_id,
            {"title": title, "head": head, "base": base, "body": body or ""},
        )

        return self._parse_pr(data)

    async def get_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> GitHubPR:
        """
        Get a pull request from GitHub.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            GitHubPR with PR details
        """
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            installation_id,
        )

        return self._parse_pr(data)

    async def update_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> GitHubPR:
        """
        Update a pull request on GitHub.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number
            title: New title (optional)
            body: New description (optional)
            state: New state: "open" or "closed" (optional)

        Returns:
            GitHubPR with updated PR details
        """
        update_data: dict[str, Any] = {}
        if title is not None:
            update_data["title"] = title
        if body is not None:
            update_data["body"] = body
        if state is not None:
            update_data["state"] = state

        data = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            installation_id,
            update_data,
        )

        return self._parse_pr(data)

    async def merge_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        commit_title: str | None = None,
        commit_message: str | None = None,
        merge_method: str = "merge",
    ) -> dict[str, Any]:
        """
        Merge a pull request on GitHub.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number
            commit_title: Title for the merge commit
            commit_message: Message for the merge commit
            merge_method: "merge", "squash", or "rebase"

        Returns:
            Merge result data
        """
        merge_data: dict[str, Any] = {"merge_method": merge_method}
        if commit_title:
            merge_data["commit_title"] = commit_title
        if commit_message:
            merge_data["commit_message"] = commit_message

        return await self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            installation_id,
            merge_data,
        )

    async def close_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> GitHubPR:
        """
        Close a pull request without merging.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            GitHubPR with updated PR details
        """
        return await self.update_pull_request(
            installation_id, owner, repo, pr_number, state="closed"
        )

    async def reopen_pull_request(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> GitHubPR:
        """
        Reopen a closed pull request.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            GitHubPR with updated PR details
        """
        return await self.update_pull_request(
            installation_id, owner, repo, pr_number, state="open"
        )

    async def list_pull_requests(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        state: str = "open",
        base: str | None = None,
    ) -> list[GitHubPR]:
        """
        List pull requests for a repository.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            state: "open", "closed", or "all"
            base: Filter by base branch

        Returns:
            List of GitHubPR objects
        """
        endpoint = f"/repos/{owner}/{repo}/pulls?state={state}"
        if base:
            endpoint += f"&base={base}"

        data = await self._request("GET", endpoint, installation_id)

        return [self._parse_pr(pr) for pr in data]

    # Review Operations

    async def create_review(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        event: str,
        body: str | None = None,
    ) -> GitHubReview:
        """
        Create a review on a pull request.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number
            event: "APPROVE", "REQUEST_CHANGES", or "COMMENT"
            body: Review body text

        Returns:
            GitHubReview with review details
        """
        review_data: dict[str, Any] = {"event": event}
        if body:
            review_data["body"] = body

        data = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            installation_id,
            review_data,
        )

        return self._parse_review(data)

    async def list_reviews(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[GitHubReview]:
        """
        List reviews for a pull request.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            List of GitHubReview objects
        """
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            installation_id,
        )

        return [self._parse_review(review) for review in data]

    # Comment Operations

    async def create_comment(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> GitHubComment:
        """
        Create a comment on a pull request.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number
            body: Comment body

        Returns:
            GitHubComment with comment details
        """
        # Use the issues comments API for PR comments (not review comments)
        data = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            installation_id,
            {"body": body},
        )

        return self._parse_comment(data)

    async def list_comments(
        self,
        installation_id: int,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[GitHubComment]:
        """
        List comments on a pull request.

        Args:
            installation_id: GitHub App installation ID
            owner: Repository owner
            repo: Repository name
            pr_number: PR number

        Returns:
            List of GitHubComment objects
        """
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            installation_id,
        )

        return [self._parse_comment(comment) for comment in data]

    # Webhook Verification

    @staticmethod
    def verify_webhook_signature(
        payload: bytes, signature: str, secret: str
    ) -> bool:
        """
        Verify a GitHub webhook signature.

        Args:
            payload: Raw request body
            signature: X-Hub-Signature-256 header value
            secret: Webhook secret

        Returns:
            True if signature is valid
        """
        if not signature.startswith("sha256="):
            return False

        expected_signature = signature[7:]
        computed_signature = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected_signature, computed_signature)

    # Helper Methods

    def _parse_pr(self, data: dict[str, Any]) -> GitHubPR:
        """Parse GitHub PR API response to GitHubPR dataclass."""
        merged_at = None
        if data.get("merged_at"):
            merged_at = datetime.fromisoformat(
                data["merged_at"].replace("Z", "+00:00")
            )

        return GitHubPR(
            number=data["number"],
            title=data["title"],
            body=data.get("body"),
            state=data["state"],
            html_url=data["html_url"],
            head_ref=data["head"]["ref"],
            base_ref=data["base"]["ref"],
            user_login=data["user"]["login"],
            created_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            updated_at=datetime.fromisoformat(
                data["updated_at"].replace("Z", "+00:00")
            ),
            merged_at=merged_at,
            merged=data.get("merged", False),
        )

    def _parse_review(self, data: dict[str, Any]) -> GitHubReview:
        """Parse GitHub review API response to GitHubReview dataclass."""
        return GitHubReview(
            id=data["id"],
            user_login=data["user"]["login"],
            state=data["state"],
            body=data.get("body"),
            submitted_at=datetime.fromisoformat(
                data["submitted_at"].replace("Z", "+00:00")
            ),
        )

    def _parse_comment(self, data: dict[str, Any]) -> GitHubComment:
        """Parse GitHub comment API response to GitHubComment dataclass."""
        return GitHubComment(
            id=data["id"],
            user_login=data["user"]["login"],
            body=data["body"],
            created_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            updated_at=datetime.fromisoformat(
                data["updated_at"].replace("Z", "+00:00")
            ),
        )


def get_github_service() -> GitHubService:
    """Factory function for dependency injection."""
    return GitHubService()
